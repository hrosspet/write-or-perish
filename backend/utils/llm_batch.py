"""Provider-agnostic Batch API helpers (Anthropic + OpenAI).

Submit / poll / collect for the providers' Batch API (~50% cheaper, async,
≤24h SLA). Extracted from the prompt-RCT harness so the production
profile-batch pipeline and the RCT share one implementation.

These functions are deliberately dependency-light (no Flask / DB): they take
an ``api_keys`` dict and plain request dicts, so they unit-test cleanly with
mocked SDK clients. Persistence and scheduling live with the caller.

Request shape (per item):
    {"custom_id": str, "model_id": str, "api_model": str,
     "messages": list, "max_tokens": int}

Result shape (per custom_id), from batch_check_and_collect:
    {"content": str, "input_tokens": int, "output_tokens": int,
     "batch": True, + the provider's cache counters under the keys the
     live calls use (Anthropic cache_read_input_tokens /
     cache_creation_input_tokens; OpenAI cached_tokens /
     cache_write_subset_tokens), so llm_cost_from_response prices it}
"""
import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone

from backend.utils import soft_limit

log = logging.getLogger(__name__)

# Backstop for a batch we can no longer READ (lost batch id, revoked
# key): once it is older than this, the collector abandons the job. A
# slow batch never needs this — both providers end a batch themselves at
# 24h (OpenAI `expired`, Anthropic `ended` with expired items) and the
# collectors treat that as ended. So: the 24h window plus polling slack.
# Shared by the external-digest and recent-context collectors.
BATCH_JOB_MAX_AGE = timedelta(hours=25)

# The OpenAI batch states after which nothing changes any more.
OPENAI_BATCH_TERMINAL = ("completed", "failed", "expired", "cancelled")

# OpenAI batch endpoints. A request dict may name "endpoint"; the default
# is chat/completions. The Responses API is the one the live calls use
# (LLMProvider._call_openai), so a batch request on it runs the model the
# way a live call does, reasoning effort included (a profile merge, #545).
CHAT_ENDPOINT = "/v1/chat/completions"
RESPONSES_ENDPOINT = "/v1/responses"


def openai_batch_key(api_model, endpoint=None):
    """The batch-id key of one OpenAI batch: one per model and endpoint
    (a batch file holds one endpoint)."""
    suffix = ":responses" if endpoint == RESPONSES_ENDPOINT else ""
    return f"openai:{api_model}{suffix}"


class BatchItemFailed(RuntimeError):
    """The provider's terminal verdict on a one-item batch: the batch or
    its item failed, expired or was cancelled, or the batch ended without
    the item. Polling again cannot change it, so the caller fails the
    node. Any other error raised while polling (the network, a 5xx, a
    bad key) is not a verdict and the caller polls again."""


class BatchItemCancelled(BatchItemFailed):
    """The batch was cancelled before the item was sent to the model, so
    the item never ran and is not billed (Anthropic: the item's result
    type is "canceled"; OpenAI: the batch is "cancelled" and the item is
    not in its output file). A verdict like its parent, told apart by
    the poll that asked for the cancel."""


# ── Content-policy blocks (Peter, 2026-11-04) ───────────────────────────
# OpenAI's content-policy filter answers a request it blocks with HTTP 400,
# type invalid_request_error, and one of these codes (in a batch, the line
# lands in the batch's error file, not its output file). The same request
# is blocked again, and every blocked request counts against the account,
# so a block is final: no caller sends the item again on its own.
# Codes: bio_policy (seen in batch error files), invalid_prompt ("flagged
# as potentially violating our usage policy") and
# image_content_policy_violation, all three in the SDK's ResponseError
# code list (openai.types.responses.ResponseError, openai>=2.44).
OPENAI_POLICY_BLOCK_CODES = (
    "bio_policy", "invalid_prompt", "image_content_policy_violation")


def is_policy_block(code):
    """Whether a provider error code is a content-policy block."""
    return code in OPENAI_POLICY_BLOCK_CODES


class BatchItemPolicyBlocked(BatchItemFailed):
    """The provider's content-policy filter blocked the item (an error
    code in OPENAI_POLICY_BLOCK_CODES, e.g. bio_policy). A verdict like
    its parent, and final: the caller records the block and never sends
    the item again automatically. ``code``: the provider's error code."""

    def __init__(self, message, code=None, error_type=None):
        super().__init__(message)
        self.code = code
        self.error_type = error_type


def openai_item_error(entry):
    """(status_code, code, type) of an OpenAI batch result line (output
    or error file) that is not a usable 200, else None. The error object
    is the response body's ``error`` or, for a request that never got a
    response, the line's own ``error``."""
    response = entry.get("response") or {}
    body = response.get("body") or {}
    status = response.get("status_code")
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict) or not error:
        error = entry.get("error") if isinstance(entry.get("error"), dict) \
            else None
    if status == 200 and not error and not entry.get("error"):
        return None
    error = error or {}
    return status, error.get("code"), error.get("type")


def _log_item_error(batch_id, custom_id, status, code, etype):
    """Log a failed item by status, code and type only: never the
    request, and not the provider's message."""
    log.warning("OpenAI batch %s item %s failed: status=%s code=%s type=%s%s",
                batch_id, custom_id, status, code, etype,
                " (content-policy block)" if is_policy_block(code) else "")


def _raise_item_error(batch_id, custom_id, status, code, etype, message=None,
                      logged=False):
    if not logged:
        _log_item_error(batch_id, custom_id, status, code, etype)
    if is_policy_block(code):
        raise BatchItemPolicyBlocked(
            f"The provider's content-policy filter blocked batch item "
            f"{custom_id} ({code})", code=code, error_type=etype)
    raise BatchItemFailed(
        f"Batch item {custom_id} failed: status={status} code={code} "
        f"type={etype}" + (f": {message}" if message else ""))


def _file_lines(client, file_id):
    """The JSON lines of a batch output or error file."""
    raw = client.files.content(file_id).content.decode()
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            yield json.loads(line)
        except ValueError:
            log.warning("Unreadable line in batch file %s", file_id)


def openai_batch_errors(client, batch_id, batch):
    """{custom_id: (status_code, code, type)} from an ended batch's error
    file, where OpenAI writes the requests that failed with a non-200
    status; {} when the batch has none. Each is logged (code and type)."""
    error_file_id = getattr(batch, "error_file_id", None)
    if not error_file_id or not isinstance(error_file_id, str):
        return {}
    errors = {}
    for entry in _file_lines(client, error_file_id):
        cid = entry.get("custom_id")
        err = openai_item_error(entry)
        if cid is None or err is None:
            continue
        errors[cid] = err
        _log_item_error(batch_id, cid, *err)
    return errors


class CollectedResults(dict):
    """The results batch_check_and_collect returns, by custom_id, plus
    ``queue_full``: the provider keys whose batch failed on the provider's
    full queue (``batch_queue_full``), and ``blocked``: {custom_id: code}
    of the items the provider's content-policy filter blocked
    (OPENAI_POLICY_BLOCK_CODES). A blocked item has no result, and its
    caller must not send it again. A plain dict from a stand-in has
    neither; read ``blocked`` with ``blocked_items(results)``.

    And ``transient_failures``: the custom_ids of the requests that
    failed for a reason outside the request (the provider down or
    overloaded, a rate limit, or an account reason such as a spend limit,
    billing or the key; #369). They are not billed, and the same request
    can succeed later, so a caller that counts failed items against a
    retry limit leaves these out, as for an account-refused submit."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.queue_full = set()
        self.blocked = {}
        self.transient_failures = set()


def blocked_items(results):
    """{custom_id: code} of the policy-blocked items of a collect."""
    return getattr(results, "blocked", None) or {}


def _note_account_failure(exc, provider, model=None, api_key=None):
    """Report a submit refused for an account reason — a spend limit,
    billing, the key (#369) — to the admin, as a live call's would be.
    True when it was one. Background batches have no user to tell; the
    alert is what makes someone fix the account."""
    try:
        from backend.llm_providers import note_account_failure
        return note_account_failure(
            exc, provider, model, api_key=api_key) is not None
    except Exception:  # pragma: no cover — never mask the submit error
        log.exception("Classifying the batch submit error failed")
        return False


class SubmittedBatches(dict):
    """What batch_submit returns: the batch ids by provider key
    ("anthropic", "openai:<api_model>"), as a dict, plus
    ``account_refused``: the provider keys whose submit the provider
    refused for an account reason (a spend limit, billing, the key; #369),
    already reported to the admin. The requests themselves were fine and
    go through once the account is fixed, so a caller that counts failed
    submits against its items leaves these out. ``queue_full``: the
    provider keys refused because the provider's batch queue or batch rate
    limit is full (``queue_full_error``): nothing is wrong with the
    requests or the account, and they go out on a later pass."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.account_refused = set()
        self.queue_full = set()


# ── A full provider queue (Peter, 2026-11-03) ───────────────────────────
# A run that fills the provider's batch queue on purpose must not count
# the refusals against the items: they wait and go out on a later pass.
# Anthropic: the Message Batches API's limits (requests per minute and
# batch requests in the processing queue) answer a create with HTTP 429
# rate_limit_error (platform.claude.com/docs/en/api/rate-limits,
# "Message Batches API"); its spend cap is a 429 too, but with
# error_code enforced_spend_limit_reached, an account failure.
# OpenAI: the per-model enqueued-token limit
# (developers.openai.com/api/docs/guides/rate-limits, "batch queue
# limits") lets the batch be created and then fails it in validation,
# its errors carrying code token_limit_exceeded ("Enqueued token limit
# reached for <model> ..."). OpenAI's docs name no code; this one is what
# the API returns (community.openai.com reports), and the message is
# matched as well.
OPENAI_QUEUE_FULL_CODES = ("token_limit_exceeded",)
OPENAI_QUEUE_FULL_TEXT = "enqueued token limit"


def queue_full_error(exc):
    """Whether a batch create failed because the provider's batch queue
    or batch rate limit is full: a 429 that is not an account failure
    (once the SDK's own retries ran out), or an OpenAI error naming the
    enqueued-token limit."""
    import anthropic
    import openai
    status_errors = tuple(
        t for t in (getattr(anthropic, "APIStatusError", None),
                    getattr(openai, "APIStatusError", None))
        if isinstance(t, type))
    if not status_errors or not isinstance(exc, status_errors):
        return False
    try:
        from backend.llm_providers import account_failure_kind
        if account_failure_kind(exc) is not None:
            return False
    except Exception:  # pragma: no cover — classify as before
        log.exception("Classifying the batch submit error failed")
        return False
    if exc.status_code == 429:
        return True
    return (getattr(exc, "code", None) in OPENAI_QUEUE_FULL_CODES
            or OPENAI_QUEUE_FULL_TEXT in str(exc).lower())


def batch_queue_full(key, batch):
    """Whether an ended batch failed only because the provider's queue was
    full: an OpenAI batch "failed" in validation on its enqueued-token
    limit. Nothing ran and nothing was billed."""
    if not key.startswith("openai:") or getattr(batch, "status", None) != "failed":
        return False
    errors = getattr(batch, "errors", None)
    data = (getattr(errors, "data", None) if errors is not None else None) or []
    for err in data:
        code = getattr(err, "code", None)
        message = (getattr(err, "message", None) or "").lower()
        if code in OPENAI_QUEUE_FULL_CODES or OPENAI_QUEUE_FULL_TEXT in message:
            return True
    return False


# Anthropic error types for a provider that is down or overloaded
# (Claude API docs, API → Errors): 429 rate_limit_error, 500 api_error,
# 504 timeout_error, 529 overloaded_error.
_ANTHROPIC_TRANSIENT_TYPES = {"rate_limit_error", "api_error",
                              "timeout_error", "overloaded_error"}


def _failure_is_transient(provider, status, error):
    """Whether a batch request's failure says nothing about the request
    itself: a 5xx or 429 status, a provider-down error type, or an
    account failure (#369). An unknown model counts against the request:
    resending it fails the same way."""
    from backend.llm_providers import account_failure_kind_of_error
    error = error if isinstance(error, dict) else {}
    if isinstance(status, int) and (status == 429 or status >= 500):
        return True
    if provider == "anthropic" and \
            error.get("type") in _ANTHROPIC_TRANSIENT_TYPES:
        return True
    kind = account_failure_kind_of_error(provider, status, error)
    return kind is not None and kind != "model_not_found"


def _anthropic_item_error(result):
    """An errored Anthropic batch result's error object as a dict
    ({type, message, details}); the SDK nests it in an ErrorResponse."""
    err = getattr(getattr(result, "error", None), "error", None)
    return {"type": getattr(err, "type", None),
            "message": getattr(err, "message", None),
            "details": getattr(err, "details", None)}


def apply_batch_key_override(api_keys, config):
    """Overlay the batch-specific OpenAI key (``OPENAI_API_KEY_BATCH``) if set.

    Batch jobs may run on a separate key/quota from interactive calls.
    Returns a new dict; the input is not mutated.
    """
    keys = dict(api_keys)
    batch_oai_key = config.get("OPENAI_API_KEY_BATCH")
    if batch_oai_key:
        keys["openai"] = batch_oai_key
    return keys


def _convert_messages_for_anthropic(messages):
    """Convert OpenAI-style messages to Anthropic batch params format."""
    system_messages = [m for m in messages if m.get("role") == "system"]
    system_text = "\n\n".join([
        m["content"][0]["text"] if isinstance(m.get("content"), list)
        else m["content"]
        for m in system_messages if m.get("content")
    ])
    system_param = ([{"type": "text", "text": system_text}]
                    if system_text else [])

    anthropic_messages = []
    for msg in messages:
        if msg["role"] in ["user", "assistant"]:
            content = msg["content"]
            if isinstance(content, list) and len(content) > 0:
                if isinstance(content[0], dict) and "text" in content[0]:
                    content = content[0]["text"]
            anthropic_messages.append({"role": msg["role"],
                                       "content": content})
    return system_param, anthropic_messages


def batch_submit(requests_by_provider, api_keys, phase=None):
    """Submit batch requests grouped by provider.

    requests_by_provider: dict mapping provider -> list of
        {"custom_id": str, "model_id": str, "api_model": str,
         "messages": list, "max_tokens": int}

    Returns a SubmittedBatches: the dict of batch IDs keyed by provider
    (+ model for OpenAI), whose ``account_refused`` names the provider
    keys refused for an account reason.

    The task's soft time limit is not a failed submission: it is raised
    (utils/soft_limit; the SDK does not send the request again), with the
    batches created before it as its ``submitted`` (a SubmittedBatches),
    so a caller can record those before it stops.
    """
    import anthropic as anthropic_sdk
    import openai as openai_sdk
    from anthropic import Anthropic
    from openai import OpenAI

    batch_ids = SubmittedBatches()

    # --- Anthropic: single batch with all requests ---
    anthropic_reqs = requests_by_provider.get("anthropic", [])
    if anthropic_reqs:
        try:
            with soft_limit.passes():
                client = Anthropic(api_key=api_keys["anthropic"],
                                   **soft_limit.client_kwargs(anthropic_sdk))
                batch_requests = []
                for req in anthropic_reqs:
                    system_param, ant_messages = (
                        _convert_messages_for_anthropic(req["messages"]))
                    params = {
                        "model": req["api_model"],
                        "max_tokens": req.get("max_tokens", 10000),
                        "messages": ant_messages,
                    }
                    if system_param:
                        params["system"] = system_param
                    batch_requests.append({
                        "custom_id": req["custom_id"],
                        "params": params,
                    })
                batch = client.messages.batches.create(
                    requests=batch_requests)
            batch_ids["anthropic"] = batch.id
            log.info(f"Anthropic batch submitted: {batch.id} "
                     f"({len(batch_requests)} requests)")
        except Exception as e:
            _stop_on_soft_limit(e, batch_ids)
            if _note_account_failure(e, "Anthropic",
                                     api_key=api_keys.get("anthropic")):
                batch_ids.account_refused.add("anthropic")
                log.warning(f"Anthropic batch submission refused for an "
                            f"account reason (reported): {e}")
            elif queue_full_error(e):
                # A warning, not an error: no Sentry event for each pass
                # while the queue stays full.
                batch_ids.queue_full.add("anthropic")
                log.warning(f"Anthropic batch submission refused: the "
                            f"batch queue or rate limit is full "
                            f"({len(anthropic_reqs)} requests wait for a "
                            f"later pass): {e}")
            else:
                log.error(f"Anthropic batch submission failed: {e}")

    # --- OpenAI: one batch per model (all requests must share a model) ---
    openai_reqs = requests_by_provider.get("openai", [])
    if openai_reqs:
        from backend.utils.safety_identifier import openai_safety_identifier
        client = OpenAI(api_key=api_keys["openai"],
                        **soft_limit.client_kwargs(openai_sdk))
        without_user = sum(1 for r in openai_reqs if r.get("user_id") is None)
        if without_user:
            log.warning(f"{without_user} OpenAI batch request(s) without a "
                        f"user_id ({phase}): sent with the system safety "
                        f"identifier")
        # One batch per model and endpoint.
        by_model = {}
        for req in openai_reqs:
            by_model.setdefault(
                (req["api_model"], req.get("endpoint") or CHAT_ENDPOINT),
                []).append(req)

        for (oai_model, endpoint), reqs in by_model.items():
            tmp_path = None
            try:
                with soft_limit.passes():
                    # Write JSONL to a temp file
                    # UTF-8 as is, not \\u escapes: a Czech letter is 2
                    # bytes instead of 6 and an emoji 4 instead of 12,
                    # against the 200 MB input file limit (a large build
                    # sends its whole corpus in one submission, #545).
                    with tempfile.NamedTemporaryFile(
                        mode="w", suffix=".jsonl", delete=False,
                        encoding="utf-8",
                    ) as tmp:
                        # Named first: a failure while writing still
                        # removes the file below.
                        tmp_path = tmp.name
                        for req in reqs:
                            effort = req.get("reasoning_effort")
                            if endpoint == RESPONSES_ENDPOINT:
                                # The live call's body (_call_openai).
                                body = {
                                    "model": oai_model,
                                    "input": _responses_input(
                                        req["messages"]),
                                    "max_output_tokens": req.get(
                                        "max_tokens", 10000),
                                    "store": False,
                                }
                                if effort:
                                    body["reasoning"] = {"effort": effort}
                            else:
                                body = {
                                    "model": oai_model,
                                    "messages": req["messages"],
                                    "max_completion_tokens": req.get(
                                        "max_tokens", 10000),
                                    "temperature": 1,
                                }
                                if effort:
                                    body["reasoning_effort"] = effort
                            # Both endpoints: which Loore user the request
                            # is for (Peter, 2026-11-04).
                            body["safety_identifier"] = (
                                openai_safety_identifier(req.get("user_id")))
                            line = {
                                "custom_id": req["custom_id"],
                                "method": "POST",
                                "url": endpoint,
                                "body": body,
                            }
                            tmp.write(json.dumps(line, ensure_ascii=False)
                                      + "\n")

                    with open(tmp_path, "rb") as f:
                        uploaded = client.files.create(file=f,
                                                       purpose="batch")
                    os.unlink(tmp_path)
                    tmp_path = None

                    batch = client.batches.create(
                        input_file_id=uploaded.id,
                        endpoint=endpoint,
                        completion_window="24h",
                    )
                key = openai_batch_key(oai_model, endpoint)
                batch_ids[key] = batch.id
                log.info(f"OpenAI batch submitted for {oai_model}: "
                         f"{batch.id} ({len(reqs)} requests)")
            except Exception as e:
                if tmp_path and os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                _stop_on_soft_limit(e, batch_ids)
                if _note_account_failure(e, "OpenAI", oai_model,
                                         api_key=api_keys.get("openai")):
                    batch_ids.account_refused.add(
                        openai_batch_key(oai_model, endpoint))
                    log.warning(f"OpenAI batch submission for {oai_model} "
                                f"refused for an account reason "
                                f"(reported): {e}")
                elif queue_full_error(e):
                    batch_ids.queue_full.add(
                        openai_batch_key(oai_model, endpoint))
                    log.warning(f"OpenAI batch submission for {oai_model} "
                                f"refused: the batch queue or rate limit "
                                f"is full ({len(reqs)} requests wait for a "
                                f"later pass): {e}")
                else:
                    log.error(f"OpenAI batch submission failed for "
                              f"{oai_model}: {e}")

    return batch_ids


def _stop_on_soft_limit(exc, batch_ids):
    """Raise the task's soft time limit when ``exc`` is or was raised from
    it, with the batches created before it as its ``submitted``."""
    found = soft_limit.found_in(exc)
    if found is not None:
        found.submitted = batch_ids
        raise found


# ── Batch statuses by listing (Peter, 2026-11-03) ───────────────────────
# "I want to have as many accounts in the queue as the provider allows. I
# just don't want to spam them with querying for results thousands of
# times per minute." A poller with many pending batches reads their
# statuses from the providers' list endpoints, many per call, and
# retrieves a batch on its own only when the listing does not show it.
#   Anthropic GET /v1/messages/batches: limit 1-1000 (default 20),
#   after_id = the page's last_id for the next page, newest first
#   (platform.claude.com/docs/en/api/messages/batches/list).
#   OpenAI GET /v1/batches: limit 1-100 (default 20), after = the last
#   id of the page; every batch of the organisation, other jobs' too; the
#   order is not documented
#   (developers.openai.com/api/reference/resources/batches/methods/list).
# Page size: each provider's documented maximum.
LIST_PAGE_SIZE = {"anthropic": 1000, "openai": 100}
# At most this many pages per provider in one poll (a bound on the calls;
# a batch the listing did not reach is retrieved on its own). 50 OpenAI
# pages are 5,000 batches, more than a 25 h window of jobs holds.
LIST_MAX_PAGES = 50
# The listing stops once it reaches batches created this long before the
# oldest pending job was saved (the job row is written after the create
# returns, and the two clocks differ). Only while the batches come newest
# first, as observed in the listing itself, so an order OpenAI does not
# document cannot hide a batch: without it the listing runs to the end
# or to LIST_MAX_PAGES.
LIST_STOP_SLACK = timedelta(hours=1)


def _family(key):
    if key == "anthropic":
        return "anthropic"
    if key.startswith("openai:"):
        return "openai"
    return None


def _created_utc(batch):
    """A batch's created_at as a naive UTC datetime (Anthropic gives a
    datetime, OpenAI unix seconds), or None."""
    value = getattr(batch, "created_at", None)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).replace(
            tzinfo=None)
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
    return None


def _list_pages(family, api_keys):
    """Yield the provider's batches page by page (newest first where the
    provider lists them so)."""
    size = LIST_PAGE_SIZE[family]
    cursor = None
    if family == "anthropic":
        from anthropic import Anthropic
        client = Anthropic(api_key=api_keys["anthropic"])
    else:
        from openai import OpenAI
        client = OpenAI(api_key=api_keys["openai"])
    while True:
        if family == "anthropic":
            page = client.messages.batches.list(
                limit=size, **({"after_id": cursor} if cursor else {}))
        else:
            page = client.batches.list(
                limit=size, **({"after": cursor} if cursor else {}))
        data = list(getattr(page, "data", None) or [])
        yield data
        if not data or not getattr(page, "has_more", False):
            return
        cursor = getattr(page, "last_id", None) or data[-1].id


def list_batches(wanted, api_keys, oldest=None):
    """The batch objects of ``wanted`` ((provider key, batch id) pairs)
    that the providers' list endpoints show, as {batch_id: batch}: one
    listing per provider (every OpenAI model and endpoint in one), paged
    until each wanted batch is found, the pages run out, LIST_MAX_PAGES
    is reached, or the listing has passed ``oldest`` (the earliest
    submission time of the wanted batches, naive UTC) by LIST_STOP_SLACK.
    A provider whose listing fails is left out (logged): its batches are
    retrieved one by one, as before. Batches not wanted (another job's,
    other features') are ignored."""
    ids = {"anthropic": set(), "openai": set()}
    for key, batch_id in wanted:
        family = _family(key or "")
        if family and batch_id:
            ids[family].add(batch_id)
    found = {}
    for family, wanted_ids in ids.items():
        if not wanted_ids:
            continue
        remaining = set(wanted_ids)
        calls, previous, newest_first = 0, None, True
        try:
            for data in _list_pages(family, api_keys):
                calls += 1
                for batch in data:
                    if batch.id in remaining:
                        found[batch.id] = batch
                        remaining.discard(batch.id)
                    created = _created_utc(batch)
                    if created is not None:
                        if previous is not None and created > previous:
                            newest_first = False
                        previous = created
                if not remaining or calls >= LIST_MAX_PAGES:
                    break
                if (oldest is not None and newest_first
                        and previous is not None
                        and previous < oldest - LIST_STOP_SLACK):
                    break
        except Exception as e:
            log.warning(f"Listing {family} batches failed "
                        f"({type(e).__name__}); each pending batch is "
                        f"retrieved on its own")
            continue
        log.info(f"Listed {family} batches: {calls} call(s) for "
                 f"{len(wanted_ids)} pending; {len(remaining)} not in the "
                 f"listing")
    return found


def retrieve_batch(key, batch_id, api_keys):
    """One batch's status object: the per-batch call, for a batch the
    listing did not show."""
    if key == "anthropic":
        from anthropic import Anthropic
        client = Anthropic(api_key=api_keys["anthropic"])
        return client.messages.batches.retrieve(batch_id)
    from openai import OpenAI
    return OpenAI(api_key=api_keys["openai"]).batches.retrieve(batch_id)


def batch_ended(key, batch):
    """Whether the provider has ended the batch (Anthropic "ended";
    OpenAI completed, failed, expired or cancelled)."""
    if key == "anthropic":
        return getattr(batch, "processing_status", None) == "ended"
    return getattr(batch, "status", None) in OPENAI_BATCH_TERMINAL


def batch_check_and_collect(batch_ids, api_keys, known=None):
    """Check batch statuses and collect completed results.

    ``known``: {batch_id: batch object} already read (``list_batches``,
    ``retrieve_batch``); such a batch is not retrieved again.

    Returns (results_by_custom_id, still_pending, batch_durations) where:
        results_by_custom_id: dict mapping custom_id -> result dict with
            "content", "input_tokens", "output_tokens", "batch": True and
            every cache counter the provider reported, in the same keys
            the live calls use (Anthropic cache_read_input_tokens /
            cache_creation_input_tokens; OpenAI cached_tokens /
            cache_write_subset_tokens), so llm_cost_from_response prices
            a batch result exactly like a live one (#286).
        still_pending: dict of batch_ids still processing
        batch_durations: dict mapping provider key -> duration in seconds

    A batch that has ended (Anthropic "ended"; OpenAI completed, failed,
    expired or cancelled) is not pending, also when none of its requests
    succeeded: the ones that did are in the results, also when the batch
    expired or was cancelled partway (the provider bills them), and the
    callers fail every item that has no result. The one exception is an
    OpenAI batch that counts succeeded requests but has no output file:
    it stays pending until the file can be read.
    results_by_custom_id is a CollectedResults: its ``queue_full`` names
    the provider keys whose batch failed only because the provider's
    queue was full (``batch_queue_full``); their items have no result
    and should wait, not count as failed. Its ``blocked`` names the
    OpenAI items the provider's content-policy filter blocked, read from
    the batch's error file (and any non-200 line of its output file).
    Its ``transient_failures`` names the requests that failed for a
    reason outside the request (#369).
    """
    from anthropic import Anthropic
    from openai import OpenAI
    from backend.llm_providers import TRUNCATED_STOP_REASONS

    results = CollectedResults()
    still_pending = {}
    batch_durations = {}

    for key, batch_id in batch_ids.items():
        if key == "anthropic":
            client = Anthropic(api_key=api_keys["anthropic"])
            batch = ((known or {}).get(batch_id)
                     or client.messages.batches.retrieve(batch_id))
            log.info(f"Anthropic batch {batch_id}: "
                     f"status={batch.processing_status}, "
                     f"counts={batch.request_counts}")
            if batch.processing_status != "ended":
                still_pending[key] = batch_id
                continue
            # Compute batch duration
            if batch.created_at and batch.ended_at:
                duration = (batch.ended_at - batch.created_at
                            ).total_seconds()
                batch_durations[key] = round(duration, 1)
                log.info(f"Anthropic batch duration: {duration:.0f}s "
                         f"({duration/60:.1f}min)")
            # Collect results
            collected, not_collected = 0, {}
            for entry in client.messages.batches.results(batch_id):
                cid = entry.custom_id
                if entry.result.type == "succeeded":
                    msg = entry.result.message
                    content = ""
                    for block in msg.content:
                        if hasattr(block, "text"):
                            content += block.text
                    usage = msg.usage
                    results[cid] = {
                        "content": content,
                        "input_tokens": usage.input_tokens,
                        "output_tokens": usage.output_tokens,
                        "cache_read_input_tokens": getattr(
                            usage, "cache_read_input_tokens", 0) or 0,
                        "cache_creation_input_tokens": getattr(
                            usage, "cache_creation_input_tokens", 0) or 0,
                        # Cut off at the output limit — the collectors
                        # refuse an empty cut-off result (#368).
                        "truncated": getattr(msg, "stop_reason", None)
                        in TRUNCATED_STOP_REASONS,
                        "refused": getattr(msg, "stop_reason", None)
                        == "refusal",
                        "batch": True,
                    }
                    collected += 1
                else:
                    error = (_anthropic_item_error(entry.result)
                             if entry.result.type == "errored" else {})
                    log.warning(f"Anthropic batch item {cid}: "
                                f"type={entry.result.type}"
                                + (f" error={error['type']}"
                                   if error.get("type") else ""))
                    if error and _failure_is_transient(
                            "anthropic", None, error):
                        results.transient_failures.add(cid)
                    not_collected[entry.result.type] = (
                        not_collected.get(entry.result.type, 0) + 1)
            # As loud as an OpenAI batch that did not complete (#419):
            # nothing collected, or requests that never ran.
            if not collected or not_collected.keys() & {"expired",
                                                        "canceled"}:
                log.error(f"Anthropic batch {batch_id} ended: {collected} "
                          f"result(s) collected, not collected: "
                          f"{not_collected}")

        elif key.startswith("openai:"):
            client = OpenAI(api_key=api_keys["openai"])
            batch = ((known or {}).get(batch_id)
                     or client.batches.retrieve(batch_id))
            log.info(f"OpenAI batch {batch_id}: status={batch.status}, "
                     f"counts={batch.request_counts}")
            if batch.status not in OPENAI_BATCH_TERMINAL:
                still_pending[key] = batch_id
                continue
            if batch_queue_full(key, batch):
                # Failed in validation on the enqueued-token limit: nothing
                # ran, nothing was billed, and the requests go out again on
                # a later pass. A warning, not an error: no Sentry event
                # for each such batch while the queue stays full.
                results.queue_full.add(key)
                log.warning(f"OpenAI batch {batch_id} failed: the "
                            f"enqueued-token limit for its model is full")
                continue
            # Every terminal state is read like Anthropic's "ended" (#419).
            # The output file holds the requests that succeeded, also in
            # a batch that expired or was cancelled partway: OpenAI bills
            # those, so they are collected and the callers log their
            # cost. The rest are in the error file. No output file means
            # no request succeeded (every one failed, or the batch failed
            # validation): the batch has ended with no results, and the
            # callers fail its items now rather than at the age backstop.
            output_file_id = getattr(batch, "output_file_id", None)
            succeeded = getattr(getattr(batch, "request_counts", None),
                                "completed", None)
            if not output_file_id and isinstance(succeeded, int) \
                    and succeeded > 0:
                # Results the provider counts (and bills) but whose file
                # is not there: not readable yet, so not ended for us.
                log.warning(f"OpenAI batch {batch_id}: {batch.status} with "
                            f"{succeeded} succeeded but no output file; "
                            f"polling again")
                still_pending[key] = batch_id
                continue
            # The requests that failed with a non-200 status are in the
            # error file, whatever the batch's end state: a content-policy
            # block is final (blocked), a provider or account reason is
            # transient (#369), and neither is billed.
            for cid, (status, code, etype) in openai_batch_errors(
                    client, batch_id, batch).items():
                if is_policy_block(code):
                    results.blocked[cid] = code
                elif _failure_is_transient(
                        "openai", status, {"code": code, "type": etype}):
                    results.transient_failures.add(cid)
            # Compute batch duration (completed_at, expired_at, ...)
            ended_at = getattr(batch, f"{batch.status}_at", None)
            if batch.created_at and ended_at:
                duration = ended_at - batch.created_at
                batch_durations[key] = round(duration, 1)
                log.info(f"OpenAI batch {key} duration: {duration:.0f}s "
                         f"({duration/60:.1f}min)")
            entries = (_file_lines(client, output_file_id)
                       if output_file_id else [])
            collected = 0
            for entry in entries:
                cid = entry["custom_id"]
                resp = entry.get("response") or entry.get("result") or {}
                body = resp.get("body") or {}
                if (resp.get("status_code") == 200
                        and body.get("object") == "response"):
                    results[cid] = _responses_result(body)
                    collected += 1
                elif resp.get("status_code") == 200 and body.get("choices"):
                    usage = body.get("usage")
                    if not usage:
                        # Priced at $0 otherwise, silently — say so.
                        log.warning(f"OpenAI batch item {cid}: 200 without "
                                    f"usage; cost row will be $0")
                    usage = usage or {}
                    # chat/completions spells the counters prompt_tokens /
                    # prompt_tokens_details (the Responses API says
                    # input_tokens / input_tokens_details). Both subsets
                    # of the prompt, same semantics as the live call.
                    details = usage.get("prompt_tokens_details") or {}
                    choice = body["choices"][0]
                    results[cid] = {
                        "content": choice["message"]["content"],
                        "refused": bool(choice["message"].get("refusal")),
                        "input_tokens": usage.get("prompt_tokens", 0) or 0,
                        "output_tokens": usage.get(
                            "completion_tokens", 0) or 0,
                        "cached_tokens": details.get(
                            "cached_tokens", 0) or 0,
                        "cache_write_subset_tokens": details.get(
                            "cache_write_tokens", 0) or 0,
                        # chat/completions: "length" = the output limit.
                        "truncated": choice.get("finish_reason") == "length",
                        "batch": True,
                    }
                    collected += 1
                else:
                    err = openai_item_error(entry) or (
                        resp.get("status_code"), None, None)
                    _log_item_error(batch_id, cid, *err)
                    if is_policy_block(err[1]):
                        results.blocked[cid] = err[1]
                    elif _failure_is_transient(
                            "openai", err[0],
                            {"code": err[1], "type": err[2]}):
                        results.transient_failures.add(cid)
            if batch.status != "completed" or not collected:
                errors = getattr(getattr(batch, "errors", None), "data",
                                 None)
                log.error(f"OpenAI batch {batch_id} ended with "
                          f"status={batch.status}: {collected} result(s) "
                          f"collected, counts={batch.request_counts}"
                          + (f", errors={errors}" if errors else ""))

    return results, still_pending, batch_durations


def _responses_result(body):
    """A Responses API batch result in the collector's shape: the text of
    the output messages, a refusal block as ``refused``, an incomplete
    response (the output cap) as ``truncated``, and the usage counters
    under the live call's keys."""
    content, refused = "", False
    for item in body.get("output") or []:
        if item.get("type") != "message":
            continue
        for block in item.get("content") or []:
            if block.get("type") == "output_text":
                content += block.get("text") or ""
            elif block.get("type") == "refusal":
                refused = True
    usage = body.get("usage") or {}
    details = usage.get("input_tokens_details") or {}
    return {
        "content": content,
        "refused": refused,
        "input_tokens": usage.get("input_tokens", 0) or 0,
        "output_tokens": usage.get("output_tokens", 0) or 0,
        "cached_tokens": details.get("cached_tokens", 0) or 0,
        "cache_write_subset_tokens": details.get("cache_write_tokens", 0) or 0,
        "truncated": body.get("status") == "incomplete",
        "batch": True,
    }


# ── single-request Anthropic batch round-trip ──────────────────────────────
# Used by generate_llm_response for prompts that carry {ca_tweets}: one
# conversation turn submitted as a one-item batch (half price, no latency
# requirement), polled by the same Celery task via retry/countdown.

def anthropic_batch_submit_one(api_key, custom_id, api_model, messages,
                               max_tokens, output_schema=None):
    """Submit ONE conversation as a one-item Anthropic batch. ``messages``
    are OpenAI-style (system messages collapse into the system param).
    ``output_schema`` (a JSON schema) constrains the reply to that shape
    via structured outputs. Returns the batch id. The task's soft time
    limit is raised, not sent again (utils/soft_limit)."""
    import anthropic as anthropic_sdk
    from anthropic import Anthropic
    system_param, ant_messages = _convert_messages_for_anthropic(messages)
    params = {"model": api_model, "max_tokens": max_tokens,
              "messages": ant_messages}
    if system_param:
        params["system"] = system_param
    if output_schema:
        params["output_config"] = {
            "format": {"type": "json_schema", "schema": output_schema}}
    with soft_limit.passes():
        client = Anthropic(api_key=api_key,
                           **soft_limit.client_kwargs(anthropic_sdk))
        batch = client.messages.batches.create(
            requests=[{"custom_id": custom_id, "params": params}])
    log.info("Anthropic one-item batch submitted: %s (%s)", batch.id,
             custom_id)
    return batch.id


def anthropic_batch_cancel_one(api_key, batch_id):
    """Cancel a batch (the admin's cancel-and-rerun). Best effort: a
    batch that already ended cannot be cancelled and the API says so;
    the caller only needs the item not to be collected any more."""
    from anthropic import Anthropic
    client = Anthropic(api_key=api_key)
    batch = client.messages.batches.cancel(batch_id)
    log.info("Anthropic batch %s cancel requested: status=%s", batch_id,
             batch.processing_status)
    return batch.processing_status


def anthropic_batch_collect_one(api_key, batch_id, custom_id):
    """Poll a one-item batch. Returns (processing_status, resp) where resp
    is None until the batch has ended, and otherwise the same dict shape
    LLMProvider._call_anthropic returns (plus ``batch: True``) so the
    caller's finalize path is unchanged. A batch still "canceling" after
    a cancel request is pending like "in_progress"; once it has ended,
    an item processed before the cancel took effect is returned (it was
    billed), one canceled before it ran raises BatchItemCancelled (not
    billed), and an errored / expired item raises BatchItemFailed."""
    from anthropic import Anthropic
    from backend.llm_providers import TRUNCATED_STOP_REASONS
    client = Anthropic(api_key=api_key)
    batch = client.messages.batches.retrieve(batch_id)
    log.info("Anthropic batch %s: status=%s counts=%s", batch_id,
             batch.processing_status, batch.request_counts)
    if batch.processing_status != "ended":
        return batch.processing_status, None
    for entry in client.messages.batches.results(batch_id):
        if entry.custom_id != custom_id:
            continue
        if entry.result.type == "canceled":
            raise BatchItemCancelled(
                f"Batch item {custom_id} canceled before it ran")
        if entry.result.type != "succeeded":
            err = getattr(entry.result, "error", None)
            raise BatchItemFailed(
                f"Batch item {custom_id} {entry.result.type}: {err}")
        msg = entry.result.message
        content = "".join(
            block.text for block in msg.content if hasattr(block, "text"))
        usage = msg.usage
        return "ended", {
            "content": content,
            "total_tokens": usage.input_tokens + usage.output_tokens,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read_input_tokens": getattr(
                usage, "cache_read_input_tokens", 0) or 0,
            "cache_creation_input_tokens": getattr(
                usage, "cache_creation_input_tokens", 0) or 0,
            "tool_calls": [],
            "truncated": msg.stop_reason in TRUNCATED_STOP_REASONS,
            "refused": msg.stop_reason == "refusal",
            "batch": True,
            "batch_id": batch_id,
        }
    raise BatchItemFailed(f"Batch {batch_id} ended without item {custom_id}")


def _responses_input(messages):
    """OpenAI-style messages → Responses API input items (the same
    conversion LLMProvider._call_openai does for the live call)."""
    items = []
    for msg in messages:
        content = msg["content"]
        if isinstance(content, list):
            block_type = ("output_text" if msg["role"] == "assistant"
                          else "input_text")
            content = [{"type": block_type,
                        "text": (b["text"] if isinstance(b, dict) and "text" in b
                                 else str(b))} for b in content]
        items.append({"role": msg["role"], "content": content})
    return items


def openai_batch_submit_one(api_key, custom_id, api_model, messages,
                            max_tokens, output_schema=None, user_id=None):
    """Submit ONE conversation as a one-item OpenAI batch against the
    Responses endpoint (the live path uses Responses too, so the input
    shape and model support match). ``output_schema`` becomes a strict
    JSON-schema text format. ``user_id``: the Loore user the request is
    for (its safety identifier). Returns the batch id. The task's soft
    time limit is raised, not sent again (utils/soft_limit)."""
    import openai as openai_sdk
    from openai import OpenAI
    from backend.utils.safety_identifier import openai_safety_identifier
    body = {
        "model": api_model,
        "input": _responses_input(messages),
        "max_output_tokens": max_tokens,
        "store": False,
        "safety_identifier": openai_safety_identifier(user_id),
    }
    if output_schema:
        body["text"] = {"format": {
            "type": "json_schema", "name": "feed_reply",
            "schema": output_schema, "strict": True}}
    line = {"custom_id": custom_id, "method": "POST",
            "url": "/v1/responses", "body": body}
    client = OpenAI(api_key=api_key, **soft_limit.client_kwargs(openai_sdk))
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl",
                                     delete=False) as tmp:
        tmp.write(json.dumps(line) + "\n")
        tmp_path = tmp.name
    with soft_limit.passes():
        try:
            with open(tmp_path, "rb") as f:
                uploaded = client.files.create(file=f, purpose="batch")
        finally:
            os.unlink(tmp_path)
        batch = client.batches.create(
            input_file_id=uploaded.id, endpoint="/v1/responses",
            completion_window="24h")
    log.info("OpenAI one-item batch submitted: %s (%s)", batch.id, custom_id)
    return batch.id


def openai_batch_cancel_one(api_key, batch_id):
    """Cancel a batch (the admin's cancel-and-rerun); see the Anthropic
    twin."""
    from openai import OpenAI
    client = OpenAI(api_key=api_key)
    batch = client.batches.cancel(batch_id)
    log.info("OpenAI batch %s cancel requested: status=%s", batch_id,
             batch.status)
    return batch.status


def openai_batch_collect_one(api_key, batch_id, custom_id):
    """Poll a one-item OpenAI batch. Returns (status, resp): resp is None
    while the batch is validating / in progress / finalizing / cancelling,
    else the dict shape the live OpenAI call returns (plus ``batch:
    True``). A batch that ended early (cancelled, expired) still carries
    the responses of the requests that had completed by then, and those
    are billed, so the item is collected whenever it is in the output.
    Raises BatchItemCancelled when the batch was cancelled before the
    item ran (not billed), BatchItemPolicyBlocked when the provider's
    content-policy filter blocked it (its code read from the batch's
    error file), and BatchItemFailed for any other terminal state
    without a usable response for the item."""
    from openai import OpenAI
    client = OpenAI(api_key=api_key)
    batch = client.batches.retrieve(batch_id)
    log.info("OpenAI batch %s: status=%s counts=%s", batch_id,
             batch.status, batch.request_counts)
    if batch.status not in OPENAI_BATCH_TERMINAL:
        return batch.status, None
    output_file_id = getattr(batch, "output_file_id", None)
    if output_file_id:
        for entry in _file_lines(client, output_file_id):
            if entry.get("custom_id") != custom_id:
                continue
            response = entry.get("response") or {}
            body = response.get("body") or {}
            err = openai_item_error(entry)
            if err is not None:
                error = entry.get("error") or body.get("error") or {}
                _raise_item_error(
                    batch_id, custom_id, *err,
                    message=(error.get("message")
                             if isinstance(error, dict) else None))
            content = ""
            refused = False
            for item in body.get("output") or []:
                if item.get("type") != "message":
                    continue
                for block in item.get("content") or []:
                    if block.get("type") == "output_text":
                        content += block.get("text") or ""
                    elif block.get("type") == "refusal":
                        refused = True
            usage = body.get("usage") or {}
            in_toks = usage.get("input_tokens", 0)
            out_toks = usage.get("output_tokens", 0)
            details = usage.get("input_tokens_details") or {}
            cached = details.get("cached_tokens", 0) or 0
            written = details.get("cache_write_tokens", 0) or 0
            return batch.status, {
                "content": content,
                "total_tokens": in_toks + out_toks,
                "input_tokens": in_toks,
                "output_tokens": out_toks,
                "cached_tokens": cached,
                "cache_write_subset_tokens": written,
                "tool_calls": [],
                "truncated": body.get("status") == "incomplete",
                "refused": refused,
                "batch": True,
                "batch_id": batch_id,
            }
    # Not in the output file: a request that failed with a non-200 status
    # is in the error file.
    error = openai_batch_errors(client, batch_id, batch).get(custom_id)
    if error is not None:
        _raise_item_error(batch_id, custom_id, *error, logged=True)
    if batch.status == "cancelled":
        raise BatchItemCancelled(
            f"OpenAI batch {batch_id} cancelled before item {custom_id} ran")
    if batch.status == "completed":
        raise BatchItemFailed(
            f"Batch {batch_id} ended without item {custom_id}")
    raise BatchItemFailed(f"OpenAI batch {batch_id} {batch.status}")

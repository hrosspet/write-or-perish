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
from datetime import timedelta

log = logging.getLogger(__name__)

# Backstop for a batch we can no longer READ (lost batch id, revoked
# key): once it is older than this, the collector abandons the job. A
# slow batch never needs this — both providers end a batch themselves at
# 24h (OpenAI `expired`, Anthropic `ended` with expired items) and the
# collectors treat that as ended. So: the 24h window plus polling slack.
# Shared by the external-digest and recent-context collectors.
BATCH_JOB_MAX_AGE = timedelta(hours=25)

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
    ``blocked``: {custom_id: code} of the items the provider's
    content-policy filter blocked (OPENAI_POLICY_BLOCK_CODES). Such an
    item has no result, and its caller must not send it again. A plain
    dict from a stand-in has no ``blocked``; read it with
    ``blocked_items(results)``."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.blocked = {}


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
    submits against its items leaves these out."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.account_refused = set()


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
    """
    from anthropic import Anthropic
    from openai import OpenAI

    batch_ids = SubmittedBatches()

    # --- Anthropic: single batch with all requests ---
    anthropic_reqs = requests_by_provider.get("anthropic", [])
    if anthropic_reqs:
        try:
            client = Anthropic(api_key=api_keys["anthropic"])
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
            if _note_account_failure(e, "Anthropic",
                                     api_key=api_keys.get("anthropic")):
                batch_ids.account_refused.add("anthropic")
                log.warning(f"Anthropic batch submission refused for an "
                            f"account reason (reported): {e}")
            else:
                log.error(f"Anthropic batch submission failed: {e}")

    # --- OpenAI: one batch per model (all requests must share a model) ---
    openai_reqs = requests_by_provider.get("openai", [])
    if openai_reqs:
        from backend.utils.safety_identifier import openai_safety_identifier
        client = OpenAI(api_key=api_keys["openai"])
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
                # Write JSONL to a temp file
                # UTF-8 as is, not \\u escapes: a Czech letter is 2 bytes
                # instead of 6 and an emoji 4 instead of 12, against the
                # 200 MB input file limit (a large build sends its whole
                # corpus in one submission, #545).
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".jsonl", delete=False,
                    encoding="utf-8",
                ) as tmp:
                    for req in reqs:
                        effort = req.get("reasoning_effort")
                        if endpoint == RESPONSES_ENDPOINT:
                            # The live call's body (_call_openai).
                            body = {
                                "model": oai_model,
                                "input": _responses_input(req["messages"]),
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
                        # Both endpoints: which Loore user the request is
                        # for (Peter, 2026-11-04).
                        body["safety_identifier"] = openai_safety_identifier(
                            req.get("user_id"))
                        line = {
                            "custom_id": req["custom_id"],
                            "method": "POST",
                            "url": endpoint,
                            "body": body,
                        }
                        tmp.write(json.dumps(line, ensure_ascii=False) + "\n")
                    tmp_path = tmp.name

                with open(tmp_path, "rb") as f:
                    uploaded = client.files.create(file=f, purpose="batch")
                os.unlink(tmp_path)

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
                if _note_account_failure(e, "OpenAI", oai_model,
                                         api_key=api_keys.get("openai")):
                    batch_ids.account_refused.add(
                        openai_batch_key(oai_model, endpoint))
                    log.warning(f"OpenAI batch submission for {oai_model} "
                                f"refused for an account reason "
                                f"(reported): {e}")
                else:
                    log.error(f"OpenAI batch submission failed for "
                              f"{oai_model}: {e}")
                if tmp_path and os.path.exists(tmp_path):
                    os.unlink(tmp_path)

    return batch_ids


def batch_check_and_collect(batch_ids, api_keys):
    """Check batch statuses and collect completed results.

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
    results_by_custom_id is a CollectedResults: its ``blocked`` names the
    OpenAI items the provider's content-policy filter blocked, read from
    the batch's error file (and any non-200 line of its output file).
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
            batch = client.messages.batches.retrieve(batch_id)
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
                else:
                    log.warning(f"Anthropic batch item {cid}: "
                                f"type={entry.result.type}")

        elif key.startswith("openai:"):
            client = OpenAI(api_key=api_keys["openai"])
            batch = client.batches.retrieve(batch_id)
            log.info(f"OpenAI batch {batch_id}: status={batch.status}, "
                     f"counts={batch.request_counts}")
            if batch.status not in ("completed", "failed", "expired",
                                    "cancelled"):
                still_pending[key] = batch_id
                continue
            # The requests that failed with a non-200 status are in the
            # error file, whatever the batch's end state.
            for cid, (_status, code, _etype) in openai_batch_errors(
                    client, batch_id, batch).items():
                if is_policy_block(code):
                    results.blocked[cid] = code
            if batch.status != "completed":
                # An expired or cancelled batch still carries the results
                # of the requests that finished by then, and those were
                # billed (#419): they are collected below; the rest are
                # missing from the results, as failed items are.
                log.error(f"OpenAI batch {batch_id} ended with "
                          f"status={batch.status}")
            # Compute batch duration
            if batch.created_at and getattr(batch, "completed_at", None):
                duration = batch.completed_at - batch.created_at
                batch_durations[key] = round(duration, 1)
                log.info(f"OpenAI batch {key} duration: {duration:.0f}s "
                         f"({duration/60:.1f}min)")
            output_file_id = getattr(batch, "output_file_id", None)
            if not output_file_id:
                # Nothing finished (every request failed, or the batch
                # ended before any did): no results, at once.
                continue
            # Download results
            content_bytes = client.files.content(output_file_id).content
            for line in content_bytes.decode().strip().split("\n"):
                if not line.strip():
                    continue
                entry = json.loads(line)
                cid = entry["custom_id"]
                resp = entry.get("response", entry.get("result", {}))
                body = resp.get("body", {})
                if (resp.get("status_code") == 200
                        and body.get("object") == "response"):
                    results[cid] = _responses_result(body)
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
                else:
                    err = openai_item_error(entry) or (
                        resp.get("status_code"), None, None)
                    _log_item_error(batch_id, cid, *err)
                    if is_policy_block(err[1]):
                        results.blocked[cid] = err[1]

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
    via structured outputs. Returns the batch id."""
    from anthropic import Anthropic
    system_param, ant_messages = _convert_messages_for_anthropic(messages)
    params = {"model": api_model, "max_tokens": max_tokens,
              "messages": ant_messages}
    if system_param:
        params["system"] = system_param
    if output_schema:
        params["output_config"] = {
            "format": {"type": "json_schema", "schema": output_schema}}
    client = Anthropic(api_key=api_key)
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
    for (its safety identifier). Returns the batch id."""
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
    client = OpenAI(api_key=api_key)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl",
                                     delete=False) as tmp:
        tmp.write(json.dumps(line) + "\n")
        tmp_path = tmp.name
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


OPENAI_BATCH_TERMINAL = ("completed", "failed", "expired", "cancelled")


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

"""External-references digest (quote-as-response, #155/#208).

Rebuilds the ``external_digest`` UserArtifact — a compact topic map of the
user's imported external content (tweets, bookmarks). It appears in the
agentic artifacts index like any other artifact, so the model can pull it
with read_artifact to learn what MIGHT exist in the saved corpus before
deciding to search it — and it costs nothing in contexts that never look
(most Loore contexts don't need external content).

Rebuilt ONCE A NIGHT, in the user's own night, and only when their
corpus changed since the last rebuild. The digest is a single LLM call
over a capped, compact rendering of the WHOLE corpus, so its cost scales
with the corpus, not with what just arrived: riding individual saves
cost $26 in one day of clipping (30 rebuilds x ~77k input tokens of a
1223-item corpus on a flagship model).

It runs on GPT-6.2 Sol for an account on an OpenAI model or one that
answered "both providers", and on Sonnet 5.5 for an account that keeps
to Anthropic (Peter, 2026-11-24; DIGEST_MODEL_OPENAI /
DIGEST_MODEL_ANTHROPIC), with no fallback to another provider or model.
The list is capped in OpenAI tokens under GPT-6.2 Sol's price step, for
both models (DIGEST_MAX_PROMPT_TOKENS), and the first time an account's
list is cut there, one warning goes to Sentry.

Nothing waits on a digest, so the nightly sweep submits the stale
users' rebuilds as provider batches (~50% cheaper, <=24h SLA), one
batch per account, each sent as soon as it is ready, and a beat
collector saves the results — see sweep_external_digests /
collect_external_digest_batches. The synchronous rebuild_external_digest
task stays as the direct path (a future "rebuild now" button).
"""
import math
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from itertools import islice

from celery.utils.log import get_task_logger
from sqlalchemy import func, select

from backend.celery_app import celery, flask_app
from backend.config import Config
from backend.extensions import db
from backend.models import (
    AnthropicBatchDeletion, APICostLog, BYLINE_SOURCES,
    ExternalDigestBatchJob, ExternalItem, User, UserArtifact,
)
from backend.llm_providers import (
    LLMProvider, ProviderAccountError, is_refused, policy_block_code)
from backend.utils.api_keys import get_api_keys_for_usage
from backend.utils.cost import llm_cost_log_fields
from backend.utils.encryption import (
    DEK_PREFETCH_CONCURRENCY, key_service_unavailable, prefetch_deks)
from backend.utils.error_privacy import describe_exception
from backend.utils import refusal_backoff, run_locks, soft_limit
from backend.utils.refusal_backoff import REFUSED_REF
from backend.utils.privacy import account_allows_ai
from backend.utils.profile_providers import agreed_to_both, profile_provider
from backend.utils.approval import background_work_allowed, not_active
from backend.utils.llm_batch import (
    BATCH_JOB_MAX_AGE, BATCH_TAG_KEY, anthropic_custom_ids,
    apply_batch_key_override, batch_check_and_collect, batch_ended,
    batch_submit, blocked_items, delete_openai_uploads, drop_openai_batch,
    list_batches, listed_since, openai_batch_key, openai_uploads_since,
    request_total, tagged_upload_name,
)
from backend.utils import anthropic_batch_deletion
from backend.utils.timefmt import user_local_time

from backend.utils.activity import in_use_user_ids
from backend.utils.hidden_rows import account_on_hold, corpus_changed

logger = get_task_logger(__name__)

DIGEST_KIND = "external_digest"
DIGEST_TITLE = "Saved References Digest"
DIGEST_DESCRIPTION = (
    "Topic map of the tweets, bookmarks and web pages {name} saved "
    "elsewhere — read this before searching their saved references, to "
    "see what might be there."
)

# The nightly rebuild fires at this hour on the USER's own clock, one
# hour after the X bookmark sync (NIGHTLY_SYNC_LOCAL_HOUR = 3), so a
# night's new bookmarks are stored before the digest reads the corpus.
NIGHTLY_DIGEST_LOCAL_HOUR = 4
# Heuristic (2026-11-25): the last hour of the user's night in which an
# account that was due at NIGHTLY_DIGEST_LOCAL_HOUR:00 and has had no
# digest request sent since is still taken: two more hourly runs, at 5
# and 6 o'clock. An account a run did not reach (the task's soft time
# limit, a key-service outage, a crash, a run that did not happen) is
# then taken the same night instead of a day later. An account whose
# list changed only after 4:00 waits for its next 4 o'clock, as before,
# so a normal account's digest is made when it was.
NIGHTLY_DIGEST_LAST_LOCAL_HOUR = 6
# Output cap for a digest request. Real digests run ~2k tokens; this is
# headroom, matching the direct path's provider default.
DIGEST_MAX_TOKENS = 10000
# BATCH_JOB_MAX_AGE (utils/llm_batch, 25h = the providers' 24h window plus
# polling slack): a batch we can no longer read is abandoned after it, so
# its users, still stale, are resubmitted by the next sweep.

# Each item is one line of the prompt, its text (with the title, if any)
# cropped to this many characters.
ITEM_SNIPPET_CHARS = 280
# The cap on the whole request is DIGEST_MAX_PROMPT_TOKENS (config.py;
# one setting, overridable by the environment variable of the same name),
# counted in o200k tokens on this machine (tokens.o200k_counter): each
# listed line with one more token for its line break (an overcount of
# about 1 %, on the safe side), plus the instructions, the longest form
# of the corpus header and the message framing. It replaces the cap of
# 300,000 characters and 1,500 items (2026-11-24); there is no item cap.
# Per-message framing of a chat request, as LLMProvider.count_tokens
# counts it for OpenAI.
MESSAGE_FRAMING_TOKENS = 4
# Heuristic: o200k tokens per character when the encoding cannot be
# loaded in this process (tokens._o200k_encoding; it then never retries).
# Dense Japanese and Chinese lists measured 0.90 to 1.14 o200k tokens a
# character (review of #802, 2026-11-24): at 0.8, a list filled to
# 258,345 estimated tokens was 282,169 real ones, above Sol's 272,000
# step. 1.2 is above the densest measured, so the estimate errs short.
ESTIMATED_TOKENS_PER_CHAR = 1.2
# Rows fetched from the database per round trip while the list is
# rendered. The list is read newest-saved first and only until the cap
# is reached, never loaded whole: a list of tens of thousands of saved
# tweets would not fit the worker's memory. Memory only: it changes
# nothing in the prompt.
RENDER_ROWS_PER_FETCH = 500
# Heuristic (2026-11-25): rows whose data keys are unwrapped together
# (encryption.prefetch_deks, DEK_PREFETCH_CONCURRENCY = 8 calls in
# flight) before their texts are read, instead of one key-service call
# after another: a list at the cap renders in about an eighth of the
# time. The first group is one round of the pool, so a key-service
# outage ends the run after one round of retries (about 30 s), not
# eight. Then the next group is the rows still expected to fit (by the
# tokens a listed line took on average) plus the one that will not, at
# least one round of the pool and at most this many. Past the cap no
# text is read, and the keys of at most KEY_GROUP_ROWS - 1 rows are
# unwrapped without their text being read (seven or fewer while the
# lines are of even length); those keys stay only in this process's key
# cache, as on every other prefetch path.
KEY_GROUP_ROWS = 64
# The Sentry issue every account's first cut at the cap is grouped into.
CUT_EVENT_MESSAGE = "Saved References Digest: a list is cut at the token cap"
CUT_EVENT_FINGERPRINT = ["external-digest-list-cut-at-cap"]

DIGEST_PROMPT = """\
Below is a collection of tweets and bookmarks a user saved on external \
platforms — content they marked as worth returning to. Write a compact \
digest of this corpus as a topic map:

- Cluster the items into topics (roughly 5-15, whatever the corpus \
actually supports). For each topic give a short heading, a one-sentence \
description of what's there, an approximate item count, and 2-4 \
representative items (author handle + a few words each).
- Note the overall shape at the top: total items, dominant themes, rough \
time range. Use the exact total from the corpus header — you cannot \
reliably count the list yourself, so don't estimate how many items you \
were shown.
- Your clusters need not cover every item — a fat head of clear topics \
beats exhaustive coverage. But SAY what you did: roughly how many items \
the clusters account for, and one line characterizing the uncovered \
remainder (e.g. "long tail of one-off links and jokes").
- This digest is read by an AI assistant deciding whether the user's \
saved references might contain something relevant to a live \
conversation — write for that reader: dense, factual, scannable. No \
preamble, no advice, markdown headings only.

The corpus:

{corpus}
"""


def _newest_item_at(user_id):
    # Saved references only: a Read pick nobody saved is not in the
    # corpus, and a day's picks must not make the digest stale (#352).
    return db.session.query(func.max(ExternalItem.fetched_at)).filter(
        ExternalItem.user_id == user_id, ExternalItem.saved()).scalar()


def _digest_built_at(user_id):
    return db.session.query(func.max(UserArtifact.created_at)).filter(
        UserArtifact.user_id == user_id,
        UserArtifact.kind == DIGEST_KIND,
    ).scalar()


def digest_is_stale(user_id):
    """True when the corpus changed since the last digest was built.

    ``fetched_at`` moves both when an item is inserted and when a re-clip
    replaces a reference's stored text, so both count as a change. A
    DELETED item moves no timestamp, so the digest keeps describing it
    until the next save — unchanged from the behaviour before the nightly
    gate, where deletion triggered no rebuild either.

    Nothing else makes a digest stale: a change of the digest's model,
    prompt or cap (a deploy) rebuilds no digest by itself. An account's
    digest is built with the new ones at its next rebuild, after its
    next save.
    """
    newest = _newest_item_at(user_id)
    if newest is None:
        return False
    built = _digest_built_at(user_id)
    return built is None or built < newest


def _setting(key):
    """A digest setting of the app's config, else its default in
    config.py (an app built without it, as in some tests)."""
    value = flask_app.config.get(key)
    return getattr(Config, key) if value in (None, "") else value


def _digest_provider(user):
    """The provider whose digest model the user's digest runs on (Peter,
    2026-11-24: "pls make Sol 6.2 default for digests for users who
    either use OAI models or who allowed to use both providers. For
    anthropic only users use Sonnet 5.5"):

    - "openai": the account answered "both providers"
      (profile_providers.agreed_to_both, read from the database now),
      whatever its own model;
    - else the provider of the account's own model (profile_provider:
      the preferred model, else the default model), so an account on an
      OpenAI model stays on OpenAI and one on an Anthropic model that
      answered "one provider", or never answered, stays on Anthropic."""
    if agreed_to_both(user):
        return "openai"
    return profile_provider(user)


def _digest_model_id(user):
    """The model the user's digest runs on: DIGEST_MODEL_OPENAI or
    DIGEST_MODEL_ANTHROPIC, by ``_digest_provider``. None when the
    provider can't be told or its setting names no model of that
    provider (the boot check rules that out): the digest is then not
    built and the account stays due, as for a model that is not
    configured. No fallback to the other provider or another model."""
    provider = _digest_provider(user)
    if provider not in ("anthropic", "openai"):
        return None
    model_id = _setting(f"DIGEST_MODEL_{provider.upper()}")
    supported = flask_app.config.get("SUPPORTED_MODELS") or {}
    if (supported.get(model_id) or {}).get("provider") != provider:
        return None
    return model_id


def _newest_first():
    """The list's order (Peter, 2026-11-24: "Why don't we use the
    timestamp when it's saved?"): by the day the person saved the
    reference, newest first; within a day, by its publishing date, newest
    first, undated last; then by the moment it was saved, then id. The
    oldest-saved are the ones the cap leaves out.

    "Saved" is ``saved_at``, else ``fetched_at`` for rows from before
    ``saved_at`` existed (the convention of
    external_sync.count_known_bookmarks); a re-clip does not move
    ``saved_at``. Heuristic: the day (UTC), because every row of a bulk
    import gets its own insert time, microseconds to minutes apart, so
    the moment alone would order an import by the order of its file and
    the publishing date would never decide."""
    saved = func.coalesce(ExternalItem.saved_at, ExternalItem.fetched_at)
    return (func.date(saved).desc().nullslast(),
            ExternalItem.posted_at.desc().nullslast(),
            saved.desc().nullslast(),
            ExternalItem.id.desc())


# The corpus header. It states the count explicitly: the model can't
# count a long list and will otherwise invent a "sample of ~N" caveat.
HEADER_ALL = ("The corpus holds {total} saved items; ALL of them are "
              "listed below.")
HEADER_CUT = ("The corpus holds {total} saved items; the {listed} most "
              "recently saved are listed below ({left_out} saved earlier "
              "are not shown).")


@dataclass
class Rendered:
    """A digest prompt over the user's list."""
    prompt: str
    total: int      # the user's saved references
    listed: int     # the lines in the prompt
    tokens: int     # the request's tokens as the cap counts them
    cut: bool       # the cap left the rest of the list out
    counted: str    # "o200k" (tiktoken) or "estimate"


def _token_counter():
    """(count, how): o200k_base on this machine (tokens.o200k_counter),
    else ESTIMATED_TOKENS_PER_CHAR."""
    from backend.utils.tokens import o200k_counter
    count = o200k_counter()
    if count is not None:
        return count, "o200k"
    return (lambda text: math.ceil(len(text or "")
                                   * ESTIMATED_TOKENS_PER_CHAR)), "estimate"


def _fixed_prompt_tokens(count, total):
    """The request's tokens besides the listed lines: the instructions,
    the longest form of the corpus header for *total* items and the line
    break after it, and the message framing."""
    longest = HEADER_CUT.format(total=total, listed=total, left_out=total)
    return (count(DIGEST_PROMPT.replace("{corpus}", "")) + count(longest)
            + 1 + MESSAGE_FRAMING_TOKENS)


def _line(item):
    """The item's line in the prompt, or None for an item without text."""
    text = (item.get_content() or "").strip().replace("\n", " ")
    if not text:
        return None
    snippet = text[:ITEM_SNIPPET_CHARS]
    stamp = item.posted_at.strftime("%Y-%m-%d") if item.posted_at else "?"
    author = item.author_handle or item.source
    if item.source not in BYLINE_SOURCES:
        author = f"@{author}"
    if item.title:
        snippet = f"{item.title} — {snippet}"[:ITEM_SNIPPET_CHARS]
    return f"- {author} ({stamp}): {snippet}"


def _key_group_size(room, listed, listed_tokens):
    """How many rows' keys to unwrap together next (KEY_GROUP_ROWS):
    *room* is the tokens left under the cap, *listed* the lines listed so
    far and *listed_tokens* their tokens."""
    if not listed:
        return DEK_PREFETCH_CONCURRENCY
    expected = math.ceil(room * listed / listed_tokens) + 1
    return max(DEK_PREFETCH_CONCURRENCY, min(KEY_GROUP_ROWS, expected))


def _render_digest_prompt(user_id):
    """The digest prompt over the user's current list (a Rendered), or
    None when they have no saved references.

    Lines are added newest-saved first (``_newest_first``) while the
    request stays within DIGEST_MAX_PROMPT_TOKENS, each counted with one
    token for its line break; the rest is left out and counted in the
    header. Rows are fetched RENDER_ROWS_PER_FETCH at a time; their keys
    are unwrapped in groups (KEY_GROUP_ROWS) and their texts decrypted
    one by one, so a rebuild decrypts the lines it lists and the one
    that did not fit, nothing further — the one place that reads the
    corpus."""
    total = ExternalItem.query.filter(
        ExternalItem.user_id == user_id, ExternalItem.saved()).count()
    if not total:
        return None

    count, counted = _token_counter()
    cap = int(_setting("DIGEST_MAX_PROMPT_TOKENS"))
    fixed = used = _fixed_prompt_tokens(count, total)
    lines = []
    cut = False
    rows = db.session.execute(
        select(ExternalItem).where(
            ExternalItem.user_id == user_id, ExternalItem.saved())
        .order_by(*_newest_first())
        .execution_options(yield_per=RENDER_ROWS_PER_FETCH))
    try:
        stream = iter(rows.scalars())
        while not cut:
            group = list(islice(stream, _key_group_size(
                cap - used, len(lines), used - fixed)))
            if not group:
                break
            prefetch_deks(item.content for item in group)
            for item in group:
                line = _line(item)
                if line is None:
                    continue
                size = count(line) + 1   # the line and its line break
                if used + size > cap:
                    cut = True
                    break
                lines.append(line)
                used += size
    finally:
        # Closed here, before any commit: on Postgres the rows stream
        # through a server-side cursor that a commit would invalidate.
        rows.close()

    if cut or len(lines) < total:
        header = HEADER_CUT.format(total=total, listed=len(lines),
                                   left_out=total - len(lines))
    else:
        header = HEADER_ALL.format(total=total)
    corpus = header + "\n" + "\n".join(lines)
    return Rendered(DIGEST_PROMPT.replace("{corpus}", corpus), total,
                    len(lines), used, cut, counted)


def _sentry():
    """The Sentry SDK (one place, so tests replace it here only)."""
    import sentry_sdk
    return sentry_sdk


def _sentry_active():
    try:
        return _sentry().get_client().is_active()
    except Exception:  # noqa: BLE001 — no Sentry, no report
        return False


def _report_cut(user_id, rendered, cap):
    """One Sentry warning (Peter, 2026-11-24: "I'd like to know when users
    start crossing that threshold"): a fixed message and fingerprint, so
    every account's report lands in one issue, and numbers only: the
    account's id, the request's tokens, the cap, the lines kept and the
    references left out. Never a text or a name."""
    logger.warning("Digest for user %s: list cut at the token cap (%s "
                   "tokens, cap %s, %s listed, %s left out)", user_id,
                   rendered.tokens, cap, rendered.listed,
                   rendered.total - rendered.listed)
    try:
        sentry_sdk = _sentry()
        with sentry_sdk.new_scope() as scope:
            scope.fingerprint = list(CUT_EVENT_FINGERPRINT)
            scope.set_extra("user_id", user_id)
            scope.set_extra("prompt_tokens", rendered.tokens)
            scope.set_extra("counted", rendered.counted)
            scope.set_extra("cap_tokens", cap)
            scope.set_extra("items_kept", rendered.listed)
            scope.set_extra("items_cut", rendered.total - rendered.listed)
            sentry_sdk.capture_message(CUT_EVENT_MESSAGE, level="warning")
    except Exception:  # pragma: no cover — reporting never stops a digest
        logger.exception("Sentry report of a digest cut failed")


def _note_cut(user, rendered):
    """Report the first time the cap cuts this account's list, once
    (User.digest_cut_reported_at, kept across restarts), and again only
    after a list that fitted whole in between, which clears the mark.
    Nothing is reported or marked while Sentry is not set up (local
    instances, tests). Commits a change of the mark."""
    if rendered.cut:
        if user.digest_cut_reported_at is not None or not _sentry_active():
            return
        _report_cut(user.id, rendered,
                    int(_setting("DIGEST_MAX_PROMPT_TOKENS")))
        user.digest_cut_reported_at = datetime.utcnow()
    elif user.digest_cut_reported_at is not None:
        user.digest_cut_reported_at = None
    else:
        return
    db.session.commit()


def _log_empty_digest_cost(user_id, model_id, response, batch):
    """Cost row for a billed call that returned no digest text (#368): no
    version is saved, the digest stays stale and the next sweep retries,
    but the call was billed. A result cut off before any text is marked
    like the other background jobs' refusals."""
    db.session.add(APICostLog(
        user_id=user_id,
        model_id=model_id,
        request_type="external_digest",
        request_ref=(REFUSED_REF if (response.get("truncated")
                                      or is_refused(response)) else None),
        **llm_cost_log_fields(model_id, response, batch=batch),
    ))
    logger.warning(
        "External digest for user %s not saved (model %s, refused=%s, "
        "truncated=%s, output_tokens=%s); nothing saved", user_id,
        model_id, is_refused(response), bool(response.get("truncated")),
        response.get("output_tokens"))
    if is_refused(response) or response.get("truncated"):
        # Second refusal in a row: the sweep stops submitting this user
        # until a digest is saved (#470); report it.
        db.session.flush()
        n, until, stopped = refusal_backoff.digest_backoff_state(user_id)
        if stopped:
            refusal_backoff.report_stop(
                user_id, "external digest", n, model_id, "external_digest",
                cause=(refusal_backoff.REFUSED_CAUSE if is_refused(response)
                       else refusal_backoff.CUT_OFF_CAUSE), until=until)


def _save_digest(user, model_id, digest_text, response, corpus_at, batch):
    """Log the cost and write the new digest version. ``response`` is the
    provider result dict (live call or batch collector) so the cost row
    is priced cache-aware for either provider (#286), by ``model_id``,
    the model the request was sent to (on the nightly path the one its
    batch job recorded, so a request sent before a change of the digest
    model is priced at its own model's prices).
    ``corpus_at`` is the moment the prompt was rendered: the artifact is
    stamped with the corpus state it reflects, not with the moment the
    response arrived (seconds later on the direct path, hours later from
    a batch), so an item saved in between still reads as newer than the
    digest and the staleness check picks it up next time."""
    fields = llm_cost_log_fields(model_id, response, batch=batch)
    db.session.add(APICostLog(
        user_id=user.id,
        model_id=model_id,
        request_type="external_digest",
        **fields,
    ))
    if corpus_changed(user.id, corpus_at):
        # "Delete all my content" hid the references this digest read
        # after it was rendered (#590): the cost is logged, nothing saved.
        logger.info("External digest for user %s not saved: content "
                    "deleted after it was read", user.id)
        return None
    previous = UserArtifact.latest_for(user.id, DIGEST_KIND)
    artifact = UserArtifact(
        user_id=user.id,
        kind=DIGEST_KIND,
        created_at=corpus_at,
        title=(previous.title if previous else DIGEST_TITLE),
        description=(
            previous.description if previous and previous.description
            else DIGEST_DESCRIPTION.replace(
                "{name}", user.username or "the user")),
        generated_by=model_id,
        tokens_used=fields["input_tokens"] + fields["output_tokens"],
        # The account's AI-usage setting at the time this version is
        # written, like every artifact writer (#326). A prompt that opens
        # the digest counts it by this value (Peter, 2026-10-01).
        ai_usage=user.default_ai_usage,
    )
    artifact.set_content(digest_text)
    db.session.add(artifact)
    return artifact


# ── Nightly path: one provider batch per account, sent as it is ready ───
#
# Peter, 2026-11-25: "make it prepare and send the batch request one at a
# time". Each account's request is submitted as its own batch, and its
# job row committed, before the next account is rendered, so a run that
# stops (the task's soft time limit, a key-service outage, a crash) loses
# only the account it was preparing; that account stays due and the next
# hourly run of the same night takes it (NIGHTLY_DIGEST_LAST_LOCAL_HOUR).

# One sweep at a time: sweeps queued while the server was frozen run
# side by side afterwards (2026-11-07), and two runs that took the same
# account would each send its digest. Heuristic: the key lives the
# task's hard time limit (one hour), longer than any run can live, so a
# run that dies holds it at most until the next hourly run.
SWEEP_LOCK_KEY = "loore:external_digest:sweep"
SWEEP_LOCK_TTL = 60 * 60
# A job row is committed in this status before its batch is created, so
# the account counts as taken that night whatever happens to the create
# (review of #806). A create the provider answered makes it "pending";
# one that created nothing removes it.
SUBMITTING = "submitting"
# Heuristic (2026-11-25): a SUBMITTING row this old was left by a run
# that can no longer be alive (the task's hard time limit is one hour),
# so its create has either reached the provider or not: from then on the
# collector looks for the batch (_resolve_submitting).
SUBMITTING_SETTLED_AFTER = timedelta(hours=1)
# The random part of a request's custom_id (_custom_id): 4 bytes, 8 hex
# characters, against another night's batch of the same account and
# another Loore instance's under the same provider account.
CUSTOM_ID_TOKEN_BYTES = 4


def _batch_keys():
    return apply_batch_key_override(
        get_api_keys_for_usage(flask_app.config, "chat"),
        flask_app.config)


def _users_in_pending_batches():
    pending = ExternalDigestBatchJob.query.filter_by(status="pending").all()
    return {item["user_id"] for job in pending for item in job.items}


def _staleness():
    """(newest saved reference, last digest) by user: the newest
    ``fetched_at`` of each user's saved references, and the build time
    of each user's last digest."""
    newest_by_user = dict(
        db.session.query(
            ExternalItem.user_id, func.max(ExternalItem.fetched_at)
        ).filter(ExternalItem.saved())
        .group_by(ExternalItem.user_id).all()
    )
    built_by_user = dict(
        db.session.query(
            UserArtifact.user_id, func.max(UserArtifact.created_at)
        ).filter(UserArtifact.kind == DIGEST_KIND)
        .group_by(UserArtifact.user_id).all()
    )
    return newest_by_user, built_by_user


def _stale_user_ids(staleness=None):
    newest_by_user, built_by_user = staleness or _staleness()
    return [
        user_id for user_id, newest in newest_by_user.items()
        if newest is not None
        and (built_by_user.get(user_id) is None
             or built_by_user[user_id] < newest)
    ]


def _night_start(user, now):
    """NIGHTLY_DIGEST_LOCAL_HOUR:00 of the user's current night (naive
    UTC) while their clock reads that hour up to
    NIGHTLY_DIGEST_LAST_LOCAL_HOUR, else None."""
    local = user_local_time(user, now)
    if not (NIGHTLY_DIGEST_LOCAL_HOUR <= local.hour
            <= NIGHTLY_DIGEST_LAST_LOCAL_HOUR):
        return None
    start = local.replace(hour=NIGHTLY_DIGEST_LOCAL_HOUR, minute=0,
                          second=0, microsecond=0)
    return start.astimezone(timezone.utc).replace(tzinfo=None)


def _sent_since(cutoff):
    """The latest time a digest request of each user was submitted since
    *cutoff*, whatever became of it (pending, collected, abandoned)."""
    latest = {}
    jobs = ExternalDigestBatchJob.query.filter(
        ExternalDigestBatchJob.submitted_at >= cutoff).all()
    for job in jobs:
        for item in job.items:
            uid = item["user_id"]
            if latest.get(uid) is None or latest[uid] < job.submitted_at:
                latest[uid] = job.submitted_at
    return latest


def _changed_by(user_id, built, at):
    """Whether the user had saved a reference newer than their digest
    (built at *built*, or never) by *at*: the account was due then."""
    query = db.session.query(ExternalItem.id).filter(
        ExternalItem.user_id == user_id, ExternalItem.saved(),
        ExternalItem.fetched_at <= at)
    if built is not None:
        query = query.filter(ExternalItem.fetched_at > built)
    return query.first() is not None


def _due_accounts(now):
    """The accounts the sweep at *now* (naive UTC) takes, in the order
    it takes them.

    Due: a saved reference newer than the last digest and no batch of
    the account pending, as before, while the account's clock reads
    NIGHTLY_DIGEST_LOCAL_HOUR (4) — or a later hour up to
    NIGHTLY_DIGEST_LAST_LOCAL_HOUR (6) when its list had already changed
    at 4:00. Once a night: an account a digest request was submitted for
    since 4:00 is not taken again that night, whatever became of the
    request (#470's backoff and a failed batch wait for the next night,
    as before).

    Order, those that waited longest first: the account whose 4:00
    passed earliest (one the previous runs did not reach), then the one
    whose last digest is oldest (never built first), then the id. An
    account left over on one night is first among its hour the next
    night, so a long list cannot keep the same accounts waiting."""
    staleness = _staleness()
    newest_by_user, built_by_user = staleness
    stale_ids = (set(_stale_user_ids(staleness))
                 - _users_in_pending_batches())
    if not stale_ids:
        return []
    in_use = in_use_user_ids(stale_ids, flask_app.config, now=now)
    sent = _sent_since(now - timedelta(
        hours=NIGHTLY_DIGEST_LAST_LOCAL_HOUR - NIGHTLY_DIGEST_LOCAL_HOUR + 1))
    due = []
    for user in User.query.filter(User.id.in_(stale_ids)).all():
        started = _night_start(user, now)
        if started is None:
            continue
        if sent.get(user.id) is not None and sent[user.id] >= started:
            continue   # taken tonight already
        built = built_by_user.get(user.id)
        late = now >= started + timedelta(hours=1)
        if not (
            user.id in in_use   # #647: paused while nobody uses it
            and account_allows_ai(user)   # #346
            # Payments on: no background model call for an account
            # without the paid set (#438), one the admin blocked (#697)
            # or one held as spam (#713); always True while off.
            and background_work_allowed(user)
            and user.deleted_at is None   # hidden: account deleted (#269)
            # Account being deleted (#269). A waiting "Delete all my
            # content" does not hold the digest of what is saved since
            # (#590): the hidden references are left out of it.
            and not account_on_hold(user.id)
            # After a refused or cut-off digest: wait an hour, then one
            # more try, then stop until a digest is saved (#368/#470).
            and not refusal_backoff.digest_in_backoff(user.id, now=now)
        ):
            continue
        # After 4 o'clock: only an account that was due at 4:00. One
        # whose list changed only since then waits for its next night.
        if late and newest_by_user[user.id] > started \
                and not _changed_by(user.id, built, started):
            continue
        due.append((started, built or datetime.min, user.id, user))
    due.sort(key=lambda entry: entry[:3])
    return [entry[3] for entry in due]


def _custom_id(user_id):
    """The request's custom_id: the account and a token of its own, so a
    batch whose create was not answered is found again by it, and by
    nothing else (``_resolve_submitting``): not by a batch of another
    night, nor by one another Loore instance sent under the same
    provider account."""
    token = secrets.token_hex(CUSTOM_ID_TOKEN_BYTES)
    return f"external-digest-{user_id}-{token}"


def _prepare(user, supported, refused=()):
    """The user's digest request as (provider, request, metadata), or
    None when nothing is sent for them in this run. *refused*: providers
    that refused a submission earlier in this run; an account of one is
    not read. Raises the task's soft time limit and a key-service
    outage."""
    # Before the corpus is read: nothing is decrypted for an account
    # whose digest model is not set up. It stays due; no other
    # provider's model and no other model is tried.
    model_id = _digest_model_id(user)
    model_cfg = supported.get(model_id)
    if model_cfg is None:
        logger.warning("Digest for user %s skipped: no digest model "
                       "of its provider is configured", user.id)
        return None
    if model_cfg["provider"] in refused:
        return None   # stays due for the next hourly run
    # Stamped BEFORE the corpus is read (see _save_digest).
    corpus_at = datetime.utcnow()
    try:
        rendered = _render_digest_prompt(user.id)
    except Exception as e:  # noqa: BLE001 — one account's render
        # One account whose references cannot be read is left out of
        # this run, not every account due in it. Nothing is written for
        # it, so it is due again. A key-service outage ends the run as
        # before (every account would wait out its own retries); the
        # accounts before it are already submitted.
        soft_limit.reraise(e)
        if key_service_unavailable(e):
            raise
        db.session.rollback()
        logger.warning("Digest for user %s skipped: its references "
                       "could not be read (%s)", user.id,
                       describe_exception(e))
        return None
    if rendered is None:
        return None
    _note_cut(user, rendered)
    custom_id = _custom_id(user.id)
    request = {
        "custom_id": custom_id,
        "model_id": model_id,
        "api_model": model_cfg["api_model"],
        "messages": [{"role": "user", "content": rendered.prompt}],
        "max_tokens": DIGEST_MAX_TOKENS,
        # The user the request is for: its OpenAI safety identifier.
        "user_id": user.id,
        # One request per user, never followed by one with the same
        # prefix: no prompt cache write (#803).
        "one_shot": True,
    }
    meta = {
        "custom_id": custom_id,
        "user_id": user.id,
        "model_id": model_id,
        "corpus_at": corpus_at.isoformat(),
        "total_items": rendered.total,
    }
    return model_cfg["provider"], request, meta


def _adopt(job, provider_key, batch_id):
    """The job's batch is known: pending, registered for deletion at
    Anthropic (#693) in the same commit."""
    job.provider_key = provider_key
    job.batch_id = batch_id
    job.status = "pending"
    anthropic_batch_deletion.register(
        provider_key, batch_id, ExternalDigestBatchJob.__tablename__)
    db.session.commit()


def _submit_one(provider, request, meta, keys):
    """Submit one account's request as its own batch. Returns (submitted,
    refused): 1 or 0, and whether the provider refused the submission
    for its account or a full queue or rate limit
    (SubmittedBatches.account_refused / queue_full).

    The job row is committed as SUBMITTING before the create, so the
    account is taken once that night whatever happens next. A batch
    created makes it pending; a submission that created nothing (refused
    or failed, logged by batch_submit) removes it, and the account stays
    due. When the create raises (the task's soft time limit, a killed
    worker), whether it reached the provider is not known: the row stays
    SUBMITTING and the collector finds its batch, or gives it up
    (``_resolve_submitting``). A soft limit is raised after a batch it
    reports as created is recorded."""
    key = (provider if provider == "anthropic"
           else openai_batch_key(request["api_model"]))
    job = ExternalDigestBatchJob(provider_key=key, batch_id="",
                                 status=SUBMITTING, items=[meta])
    db.session.add(job)
    db.session.commit()
    try:
        batch_ids = batch_submit({provider: [request]}, keys, "digest",
                                 tag=request["custom_id"])
    except Exception as e:
        found = soft_limit.found_in(e)
        created = (getattr(found, "submitted", None) or {}) if found else {}
        for provider_key, batch_id in created.items():
            _adopt(job, provider_key, batch_id)
        if found is not None:
            raise found
        raise
    for provider_key, batch_id in batch_ids.items():
        _adopt(job, provider_key, batch_id)
        return 1, False
    db.session.delete(job)
    db.session.commit()
    refusals = (set(getattr(batch_ids, "account_refused", None) or ())
                | set(getattr(batch_ids, "queue_full", None) or ()))
    return 0, key in refusals


def _submit_digest_batch(users):
    """Prepare and submit one account at a time, in the given order: its
    request is rendered, sent as its own batch and its job row committed
    before the next account starts. Returns the number of accounts
    submitted.

    Once a provider refuses a submission for its account (spend limit,
    billing, key) or a full queue or rate limit, its remaining accounts
    are not read in this run: they stay due for the next hourly run. One
    account whose list cannot be read, or whose submission fails for
    another reason, stops nothing.

    When the task's soft time limit, a key-service outage or another
    error ends the run, every account before it is already submitted;
    the one being prepared stays due (or, if its create was under way,
    is resolved by the collector), as do the accounts after it, and the
    next hourly run of the same night takes them. The error is raised:
    the run stops, and shows."""
    supported = flask_app.config.get("SUPPORTED_MODELS", {})
    keys = _batch_keys()
    submitted = 0
    refused = set()
    for done, user in enumerate(users):
        try:
            prepared = _prepare(user, supported, refused)
            if prepared is None:
                continue
            sent, refusal = _submit_one(*prepared, keys)
            submitted += sent
            if refusal:
                # Every provider-wide limit or account failure: a
                # spend limit, billing and the key are the provider
                # account's; OpenAI's batch creates per hour are the
                # organisation's, and its queue limit is per model, the
                # one model the digest sends every OpenAI account to.
                refused.add(prepared[0])
                logger.warning(
                    "Digest sweep: %s refused a submission; its other "
                    "accounts wait for the next hourly run", prepared[0])
        except Exception as e:
            try:
                db.session.rollback()
            except Exception:  # noqa: BLE001 — a connection left mid-query
                db.session.remove()
            logger.warning(
                "Digest sweep stopped by %s after %d of %d accounts (%d "
                "submitted); the rest stay due for the next hourly run",
                type(e).__name__, done, len(users), submitted)
            raise
    return submitted


@celery.task(name='backend.tasks.external_digest.sweep_external_digests')
def sweep_external_digests():
    """Hourly beat gate: submit a digest rebuild for each user whose
    references changed since their last digest — at most once a night,
    in their own night, and never while a batch of theirs is pending
    (``_due_accounts``).

    This is the ONLY scheduled dispatcher. Saving a reference (clipper,
    import, bookmark sync) deliberately does not rebuild — a burst of
    clips would otherwise pay for one whole-corpus LLM call each.

    Accounts nobody uses (#647): no digest while the owner has not used
    Loore for UNUSED_ACCOUNT_PAUSE_DAYS (utils/activity.in_use_user_ids).
    The digest stays stale meanwhile, so it is rebuilt once, from the whole
    corpus, in the owner's first night after they are back.
    """
    with flask_app.app_context():
        with run_locks.single_run(SWEEP_LOCK_KEY, SWEEP_LOCK_TTL) as ok:
            if not ok:
                logger.info("Digest sweep skipped: another sweep is "
                            "running")
                return {"status": "skipped", "reason": "running"}
            return _sweep(datetime.utcnow())


def _sweep(now):
    """The sweep at *now* (naive UTC), inside an app context."""
    due = _due_accounts(now)
    if not due:
        return {"status": "ok", "submitted": 0}
    submitted = _submit_digest_batch(due)
    if submitted:
        logger.info(
            "Nightly external-digest batches submitted for %d users",
            submitted)
    return {"status": "ok", "submitted": submitted}


def _tag(job):
    """The custom_id a SUBMITTING job's request carried: its batch's
    tag (``batch_submit(tag=...)``)."""
    return (job.items or [{}])[0].get("custom_id")


def _give_up(job, now, why):
    job.status = "abandoned"
    job.collected_at = now
    db.session.commit()
    logger.warning("External-digest job %s left mid-create given up (%s); "
                   "its account is due again on its next night", job.id, why)


def _resolve_openai(jobs, keys, now):
    """OpenAI shows a batch in its listing as soon as it exists: the one
    whose metadata carries the job's tag is adopted. Without one, an
    upload named after the tag (the stop fell between the upload and
    the create) is deleted, and the job given up."""
    oldest = min(job.submitted_at for job in jobs)
    by_tag = {_tag(job): job for job in jobs}
    batches = {}
    for batch in listed_since("openai", keys, oldest):
        tag = (getattr(batch, "metadata", None) or {}).get(BATCH_TAG_KEY)
        if tag in by_tag:
            batches[tag] = batch.id
    uploads = {}
    names = {tagged_upload_name(tag): tag for tag in by_tag
             if tag not in batches}
    if names:
        for upload in openai_uploads_since(keys, oldest):
            tag = names.get(getattr(upload, "filename", None))
            if tag is not None:
                uploads.setdefault(tag, []).append(upload.id)
    for tag, job in by_tag.items():
        if tag in batches:
            _adopt(job, job.provider_key, batches[tag])
            continue
        files = uploads.get(tag) or []
        if files and delete_openai_uploads(keys.get("openai"),
                                           files) < len(files):
            continue   # the next pass tries the delete again
        _give_up(job, now, "no batch at the provider")


def _resolve_anthropic(jobs, keys, now):
    """An Anthropic batch has no metadata: a one-request batch this
    database has not registered (#693 registers every batch Loore
    records) is read once it has ended, and the one whose request
    carries the job's tag as its custom_id is adopted. While such a
    batch is still running the job waits; with none, or after
    BATCH_JOB_MAX_AGE (the provider ends every batch within 24 h), it is
    given up."""
    oldest = min(job.submitted_at for job in jobs)
    by_tag = {_tag(job): job for job in jobs}
    listed = [batch for batch in listed_since("anthropic", keys, oldest)
              if request_total(batch) == 1]
    known = {batch_id for (batch_id,) in db.session.query(
        AnthropicBatchDeletion.batch_id).filter(
        AnthropicBatchDeletion.batch_id.in_([b.id for b in listed]))} \
        if listed else set()
    found, running = {}, False
    for batch in listed:
        if batch.id in known:
            continue
        if getattr(batch, "processing_status", None) != "ended":
            running = True
            continue
        for tag in anthropic_custom_ids(keys, batch.id) & set(by_tag):
            found[tag] = batch.id
        if len(found) == len(by_tag):
            break
    for tag, job in by_tag.items():
        if tag in found:
            _adopt(job, "anthropic", found[tag])
        elif not running or now - job.submitted_at > BATCH_JOB_MAX_AGE:
            _give_up(job, now, "no batch at the provider")


def _settled_submitting(now):
    """Jobs left SUBMITTING by a run that can no longer be alive."""
    return ExternalDigestBatchJob.query.filter(
        ExternalDigestBatchJob.status == SUBMITTING,
        ExternalDigestBatchJob.submitted_at
        <= now - SUBMITTING_SETTLED_AFTER).order_by(
        ExternalDigestBatchJob.id).all()


def _resolve_submitting(jobs, keys, now):
    """Find the batches of jobs a run left SUBMITTING (its create was
    under way when the task's soft time limit struck or the worker was
    killed), per provider, by the tag the create sent: adopted, the
    collector saves the digest and deletes the batch as for any other.
    A listing that fails is tried again on the next pass."""
    by_family = {}
    for job in jobs:
        family = "anthropic" if job.provider_key == "anthropic" else "openai"
        by_family.setdefault(family, []).append(job)
    for family, family_jobs in by_family.items():
        resolve = (_resolve_anthropic if family == "anthropic"
                   else _resolve_openai)
        try:
            resolve(family_jobs, keys, now)
        except Exception as e:  # noqa: BLE001 — tried again next pass
            soft_limit.reraise(e)
            db.session.rollback()
            logger.warning("External-digest jobs left mid-create: the %s "
                           "lookup failed (%s); tried again on the next "
                           "pass", family, type(e).__name__)
            # Problems show: a provider unreadable for longer than any
            # batch can live is given up on, and reported.
            for job in family_jobs:
                if now - job.submitted_at > BATCH_JOB_MAX_AGE:
                    logger.error("External-digest job %s left mid-create: "
                                 "%s could not be read for %s", job.id,
                                 family, BATCH_JOB_MAX_AGE)
                    _give_up(job, now, "its provider could not be read")


def _job_outcome(job, listed, keys):
    """(results, still_pending) of one pending job. A batch the listing
    shows as still running costs no call of its own; an ended one is
    read from the listing's object (its results still fetched); one the
    listing does not show is retrieved on its own, as before."""
    batch = listed.get(job.batch_id)
    if batch is not None and not batch_ended(job.provider_key, batch):
        return {}, {job.provider_key: job.batch_id}
    known = {"known": {job.batch_id: batch}} if batch is not None else {}
    try:
        results, still_pending, _ = batch_check_and_collect(
            {job.provider_key: job.batch_id}, keys, **known)
        return results, still_pending
    except Exception:
        logger.exception("Collect failed for external-digest batch %s",
                         job.batch_id)
        # Unreadable, not merely unfinished — the case the age
        # backstop exists for, so it must apply here too.
        return {}, {job.provider_key: job.batch_id}


def _collect_digest_batches():
    """Core of the collector (plain function so tests can call it
    without the Celery machinery)."""
    now = datetime.utcnow()
    jobs = ExternalDigestBatchJob.query.filter_by(status="pending").all()
    settled = _settled_submitting(now)
    if not jobs and not settled:
        return {"collected": 0, "abandoned": 0}

    keys = _batch_keys()
    if settled:
        # Adopted ones are collected with the others below.
        _resolve_submitting(settled, keys, now)
        jobs = ExternalDigestBatchJob.query.filter_by(
            status="pending").all()
        if not jobs:
            return {"collected": 0, "abandoned": 0}
    # Every pending job's status from the providers' list endpoints, many
    # per call (Peter, 2026-11-03: "I just don't want to spam them with
    # querying for results thousands of times per minute"): with one
    # batch per account (2026-11-25) a night has as many digest batches
    # as accounts.
    listed = list_batches(
        [(job.provider_key, job.batch_id) for job in jobs], keys,
        oldest=min(job.submitted_at for job in jobs))
    collected = abandoned = 0
    for job in jobs:
        results, still_pending = _job_outcome(job, listed, keys)
        if still_pending:
            if now - job.submitted_at > BATCH_JOB_MAX_AGE:
                logger.error(
                    "External-digest batch %s not ended after %s; "
                    "abandoning (%d users stay stale for the next sweep)",
                    job.batch_id, BATCH_JOB_MAX_AGE, len(job.items))
                job.status = "abandoned"
                job.collected_at = now
                db.session.commit()
                # Nothing reads the batch again: cancelled if it still
                # runs, and its files go (#667).
                if (job.provider_key or "").startswith("openai:"):
                    drop_openai_batch(keys.get("openai"), job.batch_id)
                abandoned += 1
            continue

        # Batch ended: every item either has a result or failed. A
        # failed item leaves its user stale, so the next sweep retries,
        # unless the provider's content-policy filter blocked it: that
        # is final (Peter, 2026-11-04), and the block's cost row stops
        # the sweep for this user (refusal_backoff.digest_in_backoff).
        blocked = blocked_items(results)
        for item in job.items:
            if item["custom_id"] in blocked:
                refusal_backoff.record_policy_block(
                    item["user_id"], "external_digest", item["model_id"],
                    blocked[item["custom_id"]], "external digest batch")
                continue
            result = results.get(item["custom_id"])
            digest_text = ((result or {}).get("content") or "").strip()
            if result and is_refused(result):
                # The model refused (#470): its text is no digest.
                digest_text = ""
            if result and not digest_text:
                # Billed, but empty or refused: record the cost, save
                # nothing.
                _log_empty_digest_cost(
                    item["user_id"], item["model_id"], result, batch=True)
            user = User.query.get(item["user_id"]) if digest_text else None
            if user is None:
                logger.warning(
                    "External-digest item %s of batch %s yielded no "
                    "digest", item["custom_id"], job.batch_id)
                continue
            _save_digest(
                user, item["model_id"], digest_text, result,
                datetime.fromisoformat(item["corpus_at"]), batch=True)
            logger.info(
                "External digest rebuilt for user %s from batch (%d "
                "items, model %s)", user.id, item["total_items"],
                item["model_id"])
        job.status = "collected"
        job.collected_at = now
        db.session.commit()
        # Saved: the batch's files at OpenAI (#667) and the batch at
        # Anthropic (#693) can go.
        anthropic_batch_deletion.delete_collected_batches(
            results, keys, ExternalDigestBatchJob.__tablename__)
        collected += 1
    return {"collected": collected, "abandoned": abandoned}


@celery.task(
    name='backend.tasks.external_digest.collect_external_digest_batches')
def collect_external_digest_batches():
    """Beat task: retrieve finished digest batches and save the digests.
    No-op when nothing is pending."""
    with flask_app.app_context():
        return _collect_digest_batches()


# ── Direct path: one synchronous call, kept for a manual rebuild ────────

@celery.task(name='backend.tasks.external_digest.rebuild_external_digest',
             bind=True, max_retries=2, default_retry_delay=60)
def rebuild_external_digest(self, user_id, force=False):
    """Rebuild one user's digest with a direct (non-batch) call. The
    scheduled path is the batch sweep above; this stays for a manual
    "rebuild now" (pass force=True to rebuild a digest the staleness
    check calls current)."""
    with flask_app.app_context():
        user = User.query.get(user_id)
        if user is None:
            return {"status": "no_user"}
        if not account_allows_ai(user):   # #346
            return {"status": "ai_opt_out"}
        # Payments on: no paid set (#438), or blocked by the admin (#697)
        # or held as spam (#713).
        if not background_work_allowed(user):
            return {"status": "blocked" if not_active(user)
                    else "no_subscription"}
        if account_on_hold(user_id):   # account being deleted (#269)
            return {"status": "on_hold"}
        if not force and not digest_is_stale(user_id):
            return {"status": "not_stale"}
        model_id = _digest_model_id(user)
        if model_id is None:
            # Not built, as on the nightly path: never another
            # provider's model or another model.
            logger.warning("Digest for user %s not built: no digest model "
                           "of its provider is configured", user_id)
            return {"status": "no_model"}

        corpus_at = datetime.utcnow()
        rendered = _render_digest_prompt(user_id)
        if rendered is None:
            return {"status": "no_items"}
        _note_cut(user, rendered)
        prompt_text, total = rendered.prompt, rendered.total

        api_keys = get_api_keys_for_usage(flask_app.config, 'chat')
        messages = [{
            "role": "user",
            "content": [{"type": "text", "text": prompt_text}],
        }]
        try:
            # One-shot: no prompt cache write (#803).
            response = LLMProvider.get_completion(
                model_id, messages, api_keys, user_id=user_id,
                one_shot=True)
        except Exception as exc:
            logger.warning(
                "External digest generation failed for user %s: %s",
                user_id, exc)
            # An account failure (#369) lasts until someone fixes the
            # account: no retry; it is already reported.
            if isinstance(exc, ProviderAccountError):
                raise
            # A content-policy block is final (Peter, 2026-11-04): not
            # retried, and its cost row stops the nightly sweep for this
            # user (already reported by the provider call).
            code = policy_block_code(exc)
            if code:
                refusal_backoff.record_policy_block(
                    user_id, "external_digest", model_id, code,
                    "external digest rebuild", report=False)
                raise
            raise self.retry(exc=exc)

        digest_text = (response.get("content") or "").strip()
        if is_refused(response):
            digest_text = ""   # the model refused (#470): no digest
        if not digest_text:
            _log_empty_digest_cost(user_id, model_id, response, batch=False)
            db.session.commit()
            return {"status": "empty_response"}

        artifact = _save_digest(
            user, model_id, digest_text, response, corpus_at, batch=False)
        db.session.commit()
        if artifact is None:
            return {"status": "on_hold"}
        logger.info(
            "External digest rebuilt for user %s (%d items, model %s)",
            user_id, total, model_id)
        return {"status": "ok", "artifact_id": artifact.id,
                "items": total}

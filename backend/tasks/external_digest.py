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

Nothing waits on a digest, so the nightly sweep submits every stale
user's rebuild as ONE provider batch (~50% cheaper, <=24h SLA) and a
beat collector saves the results — see sweep_external_digests /
collect_external_digest_batches. The synchronous rebuild_external_digest
task stays as the direct path (a future "rebuild now" button).
"""
import math
from dataclasses import dataclass
from datetime import datetime

from celery.utils.log import get_task_logger
from sqlalchemy import func, select

from backend.celery_app import celery, flask_app
from backend.config import Config
from backend.extensions import db
from backend.models import (
    APICostLog, BYLINE_SOURCES, ExternalDigestBatchJob, ExternalItem, User,
    UserArtifact,
)
from backend.llm_providers import (
    LLMProvider, ProviderAccountError, is_refused, policy_block_code)
from backend.utils.api_keys import get_api_keys_for_usage
from backend.utils.cost import llm_cost_log_fields
from backend.utils.encryption import key_service_unavailable
from backend.utils.error_privacy import describe_exception
from backend.utils import refusal_backoff, soft_limit
from backend.utils.refusal_backoff import REFUSED_REF
from backend.utils.privacy import account_allows_ai
from backend.utils.profile_providers import agreed_to_both, profile_provider
from backend.utils.approval import background_work_allowed, not_active
from backend.utils.llm_batch import (
    BATCH_JOB_MAX_AGE, apply_batch_key_override, batch_check_and_collect,
    batch_submit, blocked_items, drop_openai_batch,
)
from backend.utils import anthropic_batch_deletion
from backend.utils.timefmt import user_local_hour

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
# loaded in this process (tokens._o200k_encoding; it then never retries):
# the densest text measured, Japanese, is 3.10 tokens per 4 characters
# (0.775 a character), rounded up, so the estimate errs short.
ESTIMATED_TOKENS_PER_CHAR = 0.8
# Rows fetched from the database per round trip while the list is
# rendered. The list is read newest-saved first and only until the cap
# is reached, never loaded whole: a list of tens of thousands of saved
# tweets would not fit the worker's memory. Memory only: it changes
# nothing in the prompt.
RENDER_ROWS_PER_FETCH = 500
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


def _render_digest_prompt(user_id):
    """The digest prompt over the user's current list (a Rendered), or
    None when they have no saved references.

    Lines are added newest-saved first (``_newest_first``) while the
    request stays within DIGEST_MAX_PROMPT_TOKENS, each counted with one
    token for its line break; the rest is left out and counted in the
    header. Rows are fetched RENDER_ROWS_PER_FETCH at a time and
    decrypted one by one, so a rebuild decrypts the lines it lists and
    the one that did not fit, nothing further — the one place that reads
    the corpus."""
    total = ExternalItem.query.filter(
        ExternalItem.user_id == user_id, ExternalItem.saved()).count()
    if not total:
        return None

    count, counted = _token_counter()
    cap = int(_setting("DIGEST_MAX_PROMPT_TOKENS"))
    used = _fixed_prompt_tokens(count, total)
    lines = []
    cut = False
    rows = db.session.execute(
        select(ExternalItem).where(
            ExternalItem.user_id == user_id, ExternalItem.saved())
        .order_by(*_newest_first())
        .execution_options(yield_per=RENDER_ROWS_PER_FETCH))
    try:
        for item in rows.scalars():
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


# ── Nightly path: one provider batch for every stale user ───────────────

def _batch_keys():
    return apply_batch_key_override(
        get_api_keys_for_usage(flask_app.config, "chat"),
        flask_app.config)


def _users_in_pending_batches():
    pending = ExternalDigestBatchJob.query.filter_by(status="pending").all()
    return {item["user_id"] for job in pending for item in job.items}


def _stale_user_ids():
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
    return [
        user_id for user_id, newest in newest_by_user.items()
        if newest is not None
        and (built_by_user.get(user_id) is None
             or built_by_user[user_id] < newest)
    ]


def _submit_digest_batch(users):
    """Render one request per user, submit per provider, persist the
    jobs. Returns the number of users submitted."""
    supported = flask_app.config.get("SUPPORTED_MODELS", {})
    requests_by_provider = {}
    meta_by_custom_id = {}
    for user in users:
        # Before the corpus is read: nothing is decrypted for an account
        # whose digest model is not set up. It stays due; no other
        # provider's model and no other model is tried.
        model_id = _digest_model_id(user)
        model_cfg = supported.get(model_id)
        if model_cfg is None:
            logger.warning("Digest for user %s skipped: no digest model "
                           "of its provider is configured", user.id)
            continue
        # Stamped BEFORE the corpus is read (see _save_digest).
        corpus_at = datetime.utcnow()
        try:
            rendered = _render_digest_prompt(user.id)
        except Exception as e:  # noqa: BLE001 — one account's render
            # One account whose references cannot be read is left out of
            # this run, not every account due in it. Nothing is written
            # for it, so it is due again in its next night. A key-service
            # outage ends the run as before (every account would wait out
            # its own retries).
            soft_limit.reraise(e)
            if key_service_unavailable(e):
                raise
            db.session.rollback()
            logger.warning("Digest for user %s skipped: its references "
                           "could not be read (%s)", user.id,
                           describe_exception(e))
            continue
        if rendered is None:
            continue
        _note_cut(user, rendered)
        prompt_text, total = rendered.prompt, rendered.total
        custom_id = f"external-digest-{user.id}"
        requests_by_provider.setdefault(model_cfg["provider"], []).append({
            "custom_id": custom_id,
            "model_id": model_id,
            "api_model": model_cfg["api_model"],
            "messages": [{"role": "user", "content": prompt_text}],
            "max_tokens": DIGEST_MAX_TOKENS,
            # The user the request is for: its OpenAI safety identifier.
            "user_id": user.id,
        })
        meta_by_custom_id[custom_id] = {
            "custom_id": custom_id,
            "user_id": user.id,
            "model_id": model_id,
            "corpus_at": corpus_at.isoformat(),
            "total_items": total,
        }
    if not requests_by_provider:
        return 0

    batch_ids = batch_submit(requests_by_provider, _batch_keys(), "digest")
    submitted = 0
    for provider_key, batch_id in batch_ids.items():
        # batch_submit keys OpenAI jobs "openai:<api_model>"; route each
        # request's metadata to the job that carries it.
        provider, _, api_model = provider_key.partition(":")
        items = [
            meta_by_custom_id[req["custom_id"]]
            for req in requests_by_provider.get(provider, [])
            if not api_model or req["api_model"] == api_model
        ]
        db.session.add(ExternalDigestBatchJob(
            provider_key=provider_key, batch_id=batch_id, items=items))
        # Deleted at Anthropic once the job is no longer pending (#693).
        anthropic_batch_deletion.register(
            provider_key, batch_id, ExternalDigestBatchJob.__tablename__)
        submitted += len(items)
    db.session.commit()
    # batch_submit logs (not raises) a provider's failure; those users
    # stay stale and the next sweep tries again.
    return submitted


@celery.task(name='backend.tasks.external_digest.sweep_external_digests')
def sweep_external_digests():
    """Hourly beat gate: submit a digest rebuild for each user whose
    references changed since their last digest — at most once a night,
    in their own night, and never while a batch of theirs is pending.

    This is the ONLY scheduled dispatcher. Saving a reference (clipper,
    import, bookmark sync) deliberately does not rebuild — a burst of
    clips would otherwise pay for one whole-corpus LLM call each.

    Accounts nobody uses (#647): no digest while the owner has not used
    Loore for UNUSED_ACCOUNT_PAUSE_DAYS (utils/activity.in_use_user_ids).
    The digest stays stale meanwhile, so it is rebuilt once, from the whole
    corpus, in the owner's first night after they are back.
    """
    with flask_app.app_context():
        stale_ids = set(_stale_user_ids()) - _users_in_pending_batches()
        if not stale_ids:
            return {"status": "ok", "submitted": 0}
        in_use = in_use_user_ids(stale_ids, flask_app.config)
        due = [
            user for user in User.query.filter(User.id.in_(stale_ids)).all()
            if user_local_hour(user) == NIGHTLY_DIGEST_LOCAL_HOUR
            and user.id in in_use   # #647: paused while nobody uses it
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
            and not refusal_backoff.digest_in_backoff(user.id)
        ]
        if not due:
            return {"status": "ok", "submitted": 0}
        submitted = _submit_digest_batch(due)
        if submitted:
            logger.info(
                "Nightly external-digest batch submitted for %d users",
                submitted)
        return {"status": "ok", "submitted": submitted}


def _collect_digest_batches():
    """Core of the collector (plain function so tests can call it
    without the Celery machinery)."""
    jobs = ExternalDigestBatchJob.query.filter_by(status="pending").all()
    if not jobs:
        return {"collected": 0, "abandoned": 0}

    keys = _batch_keys()
    now = datetime.utcnow()
    collected = abandoned = 0
    for job in jobs:
        try:
            results, still_pending, _ = batch_check_and_collect(
                {job.provider_key: job.batch_id}, keys)
        except Exception:
            logger.exception("Collect failed for external-digest batch %s",
                             job.batch_id)
            # Unreadable, not merely unfinished — the case the age
            # backstop exists for, so it must apply here too.
            still_pending = {job.provider_key: job.batch_id}
            results = {}
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
            response = LLMProvider.get_completion(
                model_id, messages, api_keys, user_id=user_id)
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

"""
Celery tasks for asynchronous data export operations.
"""
from datetime import datetime
from celery import Task
from celery.utils.log import get_task_logger
import os

from backend.celery_app import celery, flask_app
from backend.models import User, UserProfile, APICostLog
from backend.extensions import db
from backend.llm_providers import (
    LLMProvider, PromptTooLongError, DEFAULT_MAX_OUTPUT_TOKENS,
    model_input_cap, is_empty_truncated, EmptyTruncatedOutputError)

from backend.utils.tokens import (
    approximate_token_count, reduce_export_tokens, format_date_metadata,
)
from backend.utils.api_keys import get_api_keys_for_usage
from backend.utils.cost import llm_cost_log_fields
from backend.utils.privacy import AI_ALLOWED, account_allows_ai
from backend.utils import refusal_backoff
from backend.utils.refusal_backoff import REFUSED_REF

logger = get_task_logger(__name__)

# Equal-chunk planner (design note 2026-09-03, docs/design/chunk-planner.md):
# every chunk is planned over the remainder in STORED content units
# (Node.token_count, chars/4 of the node's own text). The constants — the
# 90k target, the 80k organic-growth gate, the cap margin — live in one
# module and are re-exported here for the chunk loops and the batch builder.
from backend.utils.chunk_plan import (  # noqa: E402,F401
    CHUNK_TARGET_UNITS, UPDATE_THRESHOLD_UNITS, CAP_MARGIN,
    PROVISIONAL_THRESHOLDS, next_build_threshold,
    plan_chunks, next_window_budget, max_units_for_cap)

# Tokenizer families: which BPE a model bills with (config
# ``tokenizer_family``). Chunk balance never depends on the family; it
# only selects the prior for the real-token cap check and tags the user's
# measured ratio, so a figure measured on one tokenizer is never applied
# to another.
TOKENIZER_FAMILIES = ("claude_old", "claude_new", "o200k")
_FAMILY_BY_PROVIDER = {"openai": "o200k", "anthropic": "claude_new"}

# Prior billed-input tokens per stored unit, by content class and family,
# used for the cap check until the user's first chunk on that family has
# been measured. These are the UPPER ends of the ranges measured
# 2026-09-03 (Loore threads: hrosspet's prod chain; tweets: the exgenesis
# and majamediaco compact exports), so an unmeasured user errs toward one
# extra split, never toward the pricing tier. The claude_new tweet figure
# is estimated from the o200k counts (no local tokenizer).
TOKENS_PER_UNIT_PRIOR = {
    "threads": {"o200k": 1.15, "claude_old": 1.55, "claude_new": 2.2},
    "tweets": {"o200k": 2.4, "claude_old": 2.6, "claude_new": 3.1},
}
# Sanity bounds on a measured ratio, so one degenerate measurement (an
# empty or truncated prompt) cannot collapse the cap check for every
# later chunk. Real corpora measured 0.85–2.4 tokens per unit.
TOKENS_PER_UNIT_MIN = 0.25
TOKENS_PER_UNIT_MAX = 8.0


def tokenizer_family(model_id):
    """The model's tokenizer family (config ``tokenizer_family``), by
    provider when the config does not say — Anthropic defaulting to the
    denser new family, the conservative guess for the cap check."""
    from flask import current_app
    cfg = current_app.config.get("SUPPORTED_MODELS", {}).get(model_id) or {}
    family = cfg.get("tokenizer_family")
    if family in TOKENIZER_FAMILIES:
        return family
    return _FAMILY_BY_PROVIDER.get(cfg.get("provider"), "claude_new")


def content_class(user):
    """"tweets" when imported tweets carry the majority of the user's
    AI-readable units, else "threads" — picks the prior row. One SQL sum."""
    from sqlalchemy import case, func, or_
    from backend.models import Node
    from backend.utils.privacy import AI_ALLOWED
    tweets, total = db.session.query(
        func.coalesce(func.sum(case(
            (Node.origin == "twitter", Node.token_count), else_=0)), 0),
        func.coalesce(func.sum(Node.token_count), 0),
    ).filter(
        or_(Node.user_id == user.id, Node.human_owner_id == user.id),
        Node.ai_usage.in_(AI_ALLOWED),
        Node.deleted_at.is_(None),
    ).one()
    return "tweets" if total and tweets * 2 > total else "threads"


def tokens_per_unit(user, model_id):
    """Billed input tokens per stored unit for this user on this model's
    tokenizer family: the measured figure when one exists for the family,
    else the content-class prior. Read only by the cap check."""
    family = tokenizer_family(model_id)
    measured = getattr(user, "profile_token_ratio", None)
    if measured and getattr(user, "profile_token_ratio_family", None) == family:
        return float(measured)
    return TOKENS_PER_UNIT_PRIOR[content_class(user)][family]


def record_token_ratio(user, model_id, chunk_units, actual_input_tokens):
    """Store billed input tokens per stored unit for the chunk just sent,
    tagged with the model's tokenizer family. The prompt's fixed parts
    (profile, template) are folded in rather than subtracted: the ratio
    sizes whole prompts, and folding them in over-estimates a larger next
    chunk and under-estimates a smaller one by at most those parts, which
    the cap margin covers. Returns the ratio, or None without a usable
    measurement."""
    units = float(chunk_units or 0)
    if units <= 0 or not actual_input_tokens:
        return None
    ratio = min(max(actual_input_tokens / units, TOKENS_PER_UNIT_MIN),
                TOKENS_PER_UNIT_MAX)
    user.profile_token_ratio = round(ratio, 3)
    user.profile_token_ratio_family = tokenizer_family(model_id)
    return user.profile_token_ratio


# Prepended to a user-written profile (generated_by == "user") wherever it's
# fed to the LLM — the base of an incremental update, a version in an
# integration, or the dated block a from-scratch build folds in at the
# profile's own date (#183) — so the model treats it as the user's own words
# rather than a prior generated profile. A job that regenerates the profile
# keeps the user's text and refreshes its own observations (voice review,
# 2026-10-02).
USER_WRITTEN_PROFILE_NOTE = (
    "[NOTE: The profile below was written by the user themselves on {date}, "
    "not AI-generated. Keep what they wrote: their own statements about "
    "themselves stay in the profile, in their words where possible. Update "
    "the rest of the profile from the data as usual; their self-description "
    "does not replace what the data shows, and data written before {date} "
    "should not be read through it. Where later data shows a change, "
    "describe the change rather than dropping their words.]"
)


# For a user-written version that is an edit of a generated one (the
# profile page saves such an edit as a new version, #183): the model is
# shown which lines are the user's, so it keeps those and refreshes the
# rest instead of treating the whole profile as the user's words.
USER_EDITED_PROFILE_NOTE = (
    "[NOTE: The profile below was generated, then edited by the user "
    "themselves on {date}. Their edits are listed after it: lines starting "
    "with '-' are text they removed, lines starting with '+' are text they "
    "wrote. Keep their edits: what they removed stays out unless later data "
    "clearly brings it back, and what they wrote stays in, in their words "
    "where possible. Update the rest of the profile from the data as usual. "
    "Data written before {date} should not be read through their edits.]"
)


def _generated_source(profile):
    """The generated version a user-written one was edited from: the
    nearest parent that is not user-written. None for a profile written
    from scratch, or when that version is not AI-readable — its text is
    then never shown to a model (#346)."""
    seen = {profile.id}
    current = profile
    while current.parent_profile_id and current.parent_profile_id not in seen:
        current = UserProfile.query.get(current.parent_profile_id)
        if current is None:
            return None
        seen.add(current.id)
        if current.generated_by != "user":
            return current if current.ai_usage in AI_ALLOWED else None
    return None


def _user_edits(before, after):
    """The lines the user removed ('- ') and wrote ('+ '), one group per
    change, blank lines left out."""
    import difflib
    old, new = before.splitlines(), after.splitlines()
    groups = []
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        lines = ([f"- {line}" for line in old[i1:i2] if line.strip()]
                 + [f"+ {line}" for line in new[j1:j2] if line.strip()])
        if lines:
            groups.append("\n".join(lines))
    return "\n\n".join(groups)


def user_written_profile_text(profile):
    """A user-written profile as every profile prompt shows it: the dated
    note, then the user's text. An edit of a generated version also lists
    the user's edits, so the model knows which part is theirs."""
    date = (profile.created_at.strftime("%Y-%m-%d")
            if profile.created_at else "an unknown date")
    content = profile.get_content()
    source = _generated_source(profile)
    if source is not None:
        edits = _user_edits(source.get_content(), content)
        if edits:
            note = USER_EDITED_PROFILE_NOTE.format(date=date)
            return (f"{note}\n\n{content}\n\n"
                    f"[The user's edits on {date}:]\n{edits}")
    note = USER_WRITTEN_PROFILE_NOTE.format(date=date)
    return f"{note}\n\n{content}"


def profile_text_for_prompt(profile):
    """A profile version's text for a profile prompt: a user-written one
    with its note, a generated one as is. The sync loop and the batch
    builder both read the update base through this (#183)."""
    if profile.generated_by == "user":
        return user_written_profile_text(profile)
    return profile.get_content()


def latest_user_written_profile(user_id):
    """The newest profile the user wrote by hand (generated_by == "user")
    that AI may read, or None. A version marked 'none' (written while the
    account was set to 'none') is never sent to a model (#346); the newest
    readable one before it is used instead."""
    return UserProfile.query.filter(
        UserProfile.user_id == user_id,
        UserProfile.generated_by == "user",
        UserProfile.ai_usage.in_(AI_ALLOWED),
        UserProfile.created_at.isnot(None),
    ).order_by(UserProfile.created_at.desc()).first()


def _descends_from(profile, ancestor_id):
    """Whether ``ancestor_id`` is ``profile`` or one of its parents."""
    seen = set()
    current = profile
    while current is not None and current.id not in seen:
        if current.id == ancestor_id:
            return True
        seen.add(current.id)
        if current.parent_profile_id is None:
            return False
        current = UserProfile.query.get(current.parent_profile_id)
    return False


def place_user_written_profile(user_id, base, chunk):
    """Decide whether the user's hand-written profile goes into this
    chunk's prompt (#183). The sync loop and the batch builder call it on
    every rendered window, before the prompt is built and counted.

    A full rebuild reads the writing oldest first. The user's own profile
    enters it at its own date: in the first window whose data reaches the
    date the profile was written, or in the final window when all the data
    is older than the profile. Earlier windows are read without it, so old
    writing is not read through a later self-description.

    The decision depends only on the base and the window, so a run resumed
    from a saved chunk, and every batch step, make the same choice. It is
    skipped when the base already covers the profile's date (the chain has
    passed it) or descends from the profile (the profile is the chain's
    root and was the base of its first update).

    Sets on ``chunk``:
      ``user_written_block``: the dated profile text to append to the
        window's data, or absent;
      ``version_cutoff``: the cutoff to save on the version built from this
        window. It is the window's last node, except when the profile goes
        into the final window and is newer than every node: then it is the
        profile's date, so the chain records that it has passed that date
        and later steps do not fold the profile in again.
    Returns ``chunk``."""
    window_end = chunk.get("latest_node_created_at")
    chunk["version_cutoff"] = window_end
    chunk.pop("user_written_block", None)
    if window_end is None:
        return chunk
    own = latest_user_written_profile(user_id)
    if own is None:
        return chunk
    written_at = own.created_at
    if base is not None and (base.source_data_cutoff is None
                             or base.source_data_cutoff >= written_at):
        return chunk
    version_cutoff = window_end
    if window_end < written_at:
        # Not this window, unless nothing is left after it.
        if count_remaining_units(user_id, window_end) > 0:
            return chunk
        version_cutoff = written_at
    if base is not None and _descends_from(base, own.id):
        return chunk
    chunk["user_written_block"] = user_written_profile_text(own)
    chunk["version_cutoff"] = version_cutoff
    return chunk


def _is_task_stale(user):
    """Check if the user's profile generation task is stale (lost or timed out).

    Returns True if the guard should be cleared, False if the task is
    legitimately running.
    """
    if not user.profile_generation_task_id:
        return False

    from backend.celery_app import celery as _celery
    task = _celery.AsyncResult(user.profile_generation_task_id)

    if task.state in ('SUCCESS', 'FAILURE', 'REVOKED'):
        return True  # Finished — guard is stale

    dispatched_at = user.profile_generation_task_dispatched_at
    if not dispatched_at:
        return True  # No timestamp = legacy guard, treat as stale

    from datetime import datetime, timedelta
    age = datetime.utcnow() - dispatched_at

    if task.state == 'PENDING' and age > timedelta(minutes=15):
        return True  # PENDING for 15+ min = likely lost

    if task.state in ('STARTED', 'PROGRESS') and age > timedelta(hours=1):
        return True  # Running for 1+ hour = timed out

    return False


# Import from export_data module
def build_user_export_content(user, max_tokens=None, *, filter_ai_usage,
                              **kwargs):
    """Import the actual implementation from export_data routes.
    `filter_ai_usage` is required: see the real function's docstring."""
    from backend.routes.export_data import build_user_export_content as _build
    return _build(user, max_tokens, filter_ai_usage=filter_ai_usage, **kwargs)


def _estimate_source_tokens(user):
    """Stored token_count summed over the profile pipeline's anchor scope
    (own + addressed nodes, AI-readable, alive). Pure SQL — no node
    loading, no decryption — so it is safe for any corpus size."""
    from sqlalchemy import func, or_
    from backend.models import Node
    total = db.session.query(func.coalesce(func.sum(Node.token_count), 0)).filter(
        or_(Node.user_id == user.id, Node.human_owner_id == user.id),
        Node.ai_usage.in_(['chat', 'train']),
        Node.deleted_at.is_(None),
    ).scalar()
    return int(total or 0)


def profile_is_provisional(profile):
    """A generated profile that covers less than one full chunk of data
    (``source_tokens_used`` below ``CHUNK_TARGET_UNITS``) is provisional —
    an early signup's or a small import's build — not a base worth
    patching: it is replaced from scratch each time the account's total
    data crosses the next step of the provisional ladder
    (``provisional_build_due``), the earlier versions staying as history,
    unchained. A profile the user wrote from scratch (no source coverage)
    is never provisional: it is the user's own words and stays the base.
    An edit of a generated version carries that version's coverage and is
    provisional like it, so editing an early profile does not pause the
    ladder; the next rebuild keeps the edit
    (``place_user_written_profile``, #183)."""
    if profile is None:
        return False
    if profile.generated_by == "user" and not profile.source_tokens_used:
        return False
    return (profile.source_tokens_used or 0) < CHUNK_TARGET_UNITS


def provisional_build_due(user, latest_profile):
    """Whether an account with no profile, or a provisional one, has
    crossed the next step of the provisional ladder (5k, 10k, 15k, 25k,
    50k, then T) with its total AI-readable units — the from-scratch build
    that replaces the current version is due. Both seeding gates apply it
    in place of the 80k incremental gate while the profile is provisional.
    Returns (due, total_units, threshold)."""
    covered = (latest_profile.source_tokens_used or 0) if latest_profile else 0
    threshold = next_build_threshold(covered)
    total = _estimate_source_tokens(user)
    return (threshold is not None and total >= threshold), total, threshold


def count_remaining_units(user_id, created_after=None):
    """Units still ahead of the chunk loop after ``created_after``, summed in
    the export window's own scope (backend.routes.export_data). Thin wrapper
    so the task modules and their tests reach it through this module."""
    from backend.routes.export_data import count_remaining_units as _count
    return _count(user_id, created_after)


def should_continue_chain(user, latest_profile):
    """The continue rule (docs/design/chunk-planner.md): AI-readable data in
    the profile's scope that lies beyond the latest version's cutoff but
    already EXISTED when that version's window was rendered is an
    unfinished chain — a pre-fill or an import still being folded in, the
    rest of a multi-chunk update, a chunk lost to a worker restart — and
    the next chunk runs regardless of the interval and volume gates, which
    measure organic growth. Data written after the render is growth and
    waits for those gates.

    One rule, three call sites: the seeding gates (batch seeder, sync
    heartbeat), the step after every saved chunk in both pipelines (so
    writing during a chunk's generation ends the run at the planned
    chunks instead of adding a small one per cycle), and the admin
    "stuck" flag. It replaces the pinned-account special case and the
    minimum-chunk deferral.

    The boundary is ``source_rendered_at``. A version saved before that
    column existed cannot tell its leftover (the old sizing deferred a
    sub-minimum tail after most updates) from growth, so it does not
    continue on its own: the account's next gate-triggered update covers
    everything unread anyway. The exception is a pinned (pre-filled)
    account, which never grows organically and whose leftover would
    otherwise wait forever — it continues from the version's save time,
    as the old pinned special case did. Transitional: every version
    saved from now on carries a render time.
    """
    cutoff = getattr(latest_profile, "source_data_cutoff", None)
    if cutoff is None:
        return False
    boundary = getattr(latest_profile, "source_rendered_at", None)
    if boundary is None:
        if not getattr(user, "profile_force_batch", False):
            return False
        boundary = latest_profile.created_at
    if boundary is None:
        return False
    from sqlalchemy import or_
    from backend.models import Node
    from backend.utils.privacy import AI_ALLOWED
    return db.session.query(Node.id).filter(
        or_(Node.user_id == user.id, Node.human_owner_id == user.id),
        Node.ai_usage.in_(AI_ALLOWED),
        Node.deleted_at.is_(None),
        Node.created_at > cutoff,
        Node.created_at < boundary,
    ).first() is not None


def integration_due(user, latest_profile):
    """Whether the run that saved ``latest_profile`` (the update base,
    ``profile_update_base``) ended before its integration: the base is a
    generated chunk ("iterative" or "update"), it is the newest
    non-integration version of any ai_usage, no integration was saved on
    top of it, its chain is finished (``should_continue_chain`` is false)
    and it has at least two AI-readable versions to merge. Every run that
    finishes its chunks integrates them (both pipelines), so this is a run
    whose integration was refused (#368) or lost to a restart. The next
    scheduled run then integrates alone (``update_user_profile`` with
    ``integrate_only``, or the batch builder with ``allow_chunk=False``).

    It cannot loop: a saved integration makes it false, and a refused one
    counts toward the refusal backoff (one retry, then stopped)."""
    if latest_profile is None or user.profile_needs_full_regen:
        return False
    if latest_profile.generated_by == "user" or \
            latest_profile.generation_type not in ("iterative", "update"):
        return False
    newest = UserProfile.query.with_entities(UserProfile.id).filter(
        UserProfile.user_id == user.id,
        UserProfile.generation_type != 'integration',
    ).order_by(UserProfile.created_at.desc()).first()
    if newest is None or newest[0] != latest_profile.id:
        return False
    if UserProfile.query.with_entities(UserProfile.id).filter_by(
            user_id=user.id, generation_type="integration",
            parent_profile_id=latest_profile.id).first() is not None:
        return False
    if should_continue_chain(user, latest_profile):
        return False
    chain = [p for p in _collect_iterative_chain(latest_profile.id)
             if p.ai_usage in AI_ALLOWED]
    return len(chain) >= 2


class ProfileGenerationTask(Task):
    """Custom task class with error handling."""

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        """Called when task fails."""
        user_id = args[0] if args else None
        if user_id:
            logger.error(f"Profile generation failed for user {user_id}: {exc}")


def _load_prompt(name, user_id=None):
    """Load a prompt template by name, checking user overrides first."""
    if user_id:
        from backend.utils.prompts import get_user_prompt
        # Derive prompt_key from filename (e.g. "profile_generation.txt" -> "profile_generation")
        prompt_key = name.rsplit('.', 1)[0] if '.' in name else name
        content = get_user_prompt(user_id, prompt_key)
        if content:
            return content
    path = os.path.join(flask_app.root_path, "prompts", name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _call_llm_with_retries(self, model_id, prompt_text, user_id,
                            api_keys, progress_base=50,
                            status_label='Generating profile',
                            max_tokens=None):
    """Call LLM with retry logic for prompt-too-long errors.

    Before the first call, the prompt is counted (LLMProvider.count_tokens
    — free/exact on Anthropic, tiktoken estimate on OpenAI) and truncated
    to fit, so an oversized prompt is fixed up front instead of through
    reject round-trips. The truncation is the same proportional string cut
    the reject branch performs; that branch stays as the backstop for
    count drift.

    Returns (response_dict, profile_text, input_tokens, output_tokens).
    """
    from flask import current_app
    model_cfg = current_app.config.get(
        "SUPPORTED_MODELS", {}).get(model_id) or {}
    limit = model_input_cap(model_cfg, max_tokens)
    safety = current_app.config.get("RETRY_SAFETY_FACTOR", 0.99)
    for _ in range(3):
        if not model_cfg:
            break
        real = LLMProvider.count_tokens(
            model_id, [{"role": "user", "content": [
                {"type": "text", "text": prompt_text}]}], api_keys)
        if not isinstance(real, int) or real <= limit:
            break
        logger.info(
            f"Prompt for user {user_id} counted {real} > {limit} limit — "
            f"truncating before the first call")
        prompt_text = prompt_text[:int(len(prompt_text)
                                       * limit / real * safety)]

    max_export_tokens = None
    MAX_RETRIES = 3
    for attempt in range(MAX_RETRIES + 1):
        messages = [{"role": "user", "content": [
            {"type": "text", "text": prompt_text}
        ]}]

        self.update_state(state='PROGRESS', meta={
            'progress': progress_base + 10,
            'status': status_label
        })

        try:
            response = LLMProvider.get_completion(model_id, messages,
                                                  api_keys,
                                                  max_tokens=max_tokens)
            return response
        except PromptTooLongError as e:
            if attempt == MAX_RETRIES:
                raise
            max_export_tokens = reduce_export_tokens(
                max_export_tokens, e.actual_tokens, e.max_tokens,
                export_content=prompt_text
            )
            logger.warning(
                f"Prompt too long ({e.actual_tokens} > {e.max_tokens}), "
                f"retry {attempt + 2}/{MAX_RETRIES + 1}"
            )
            # Truncate the prompt text proportionally
            ratio = max_export_tokens / approximate_token_count(prompt_text)
            prompt_text = prompt_text[:int(len(prompt_text) * ratio)]


def _save_profile(user, model_id, profile_text, response,
                   source_tokens_used, source_data_cutoff,
                   generation_type, parent_profile_id=None, batch=False,
                   source_origin_stats=None, source_rendered_at=None):
    """Save a new UserProfile and log API cost. Returns the profile.

    batch=True records the Batch API discount in the cost log (issue #173).
    source_rendered_at: when the window this version covers was rendered
    (the continue rule's boundary between unfinished chain and growth)."""
    from backend.utils.privacy import PrivacyLevel

    total_tokens = response["total_tokens"]

    _add_profile_cost_log(user, model_id, response, batch=batch)
    # A saved version ends a refusal streak (#368) and whatever error the
    # admin column showed for it.
    if user.profile_seed_error:
        user.profile_seed_error = None

    new_profile = UserProfile(
        user_id=user.id, generated_by=model_id,
        tokens_used=total_tokens,
        privacy_level=PrivacyLevel.PRIVATE,
        # ai_usage follows the user's global default; profile generation is
        # gated to 'chat'/'train' users in profile_eligible_query (#191).
        ai_usage=user.default_ai_usage,
        source_tokens_used=source_tokens_used,
        source_data_cutoff=source_data_cutoff,
        source_origin_stats=source_origin_stats,
        source_rendered_at=source_rendered_at,
        generation_type=generation_type,
        parent_profile_id=parent_profile_id,
    )
    new_profile.set_content(
        format_date_metadata(
            covers_end=source_data_cutoff,
            tokens=source_tokens_used,
        ) + profile_text
    )
    db.session.add(new_profile)
    db.session.commit()
    return new_profile


def _add_profile_cost_log(user, model_id, response, batch=False,
                          refused=False):
    db.session.add(APICostLog(
        user_id=user.id, model_id=model_id,
        request_type="profile_batch" if batch else "profile",
        # A refused call is a consecutive failure for the backoff (#368).
        request_ref=REFUSED_REF if refused else None,
        **llm_cost_log_fields(model_id, response, batch=batch),
    ))


def refuse_truncated_profile(user, model_id, response, job, batch=False):
    """Guard before a profile version is saved from model output (#368).

    A version cut off at the output limit — empty, or stopped mid-document
    — would become the chain tip, the "previous profile" the next chunk
    or update builds on, and the profile would lose everything the cut-off
    left out. Instead: write the cost row (the call was billed, marked
    refused for the backoff), save no version, and raise
    EmptyTruncatedOutputError so the job fails. The last good version
    stays the tip, and the next run resumes from it once the backoff
    (utils/refusal_backoff.py) allows: one more try after an hour, then
    the job stops for this user."""
    if not response.get("truncated"):
        return
    _add_profile_cost_log(user, model_id, response, batch=batch,
                          refused=True)
    db.session.commit()
    empty = is_empty_truncated(response)
    n, until, stopped = refusal_backoff.profile_backoff_state(user.id)
    if stopped:
        # Second refusal in a row: no more automatic runs for this user.
        # Shown in the admin Profile column (seed_error); the next saved
        # version clears it.
        user.profile_seed_error = (
            f"Stopped: output cut off {n} times in a row "
            f"({job}); runs again after an import, a saved version "
            f"or Build profile")[:255]
        db.session.commit()
        refusal_backoff.report_stop(
            user.id, f"profile {job}", n, model_id,
            "profile_batch" if batch else "profile")
    else:
        logger.warning(
            "Truncated profile output for user %s (%s, model %s, empty=%s, "
            "output_tokens=%s): no version saved, the previous one stays; "
            "refusal %d, next try after %s", user.id, job, model_id, empty,
            response.get("output_tokens"), n, until)
    raise EmptyTruncatedOutputError(
        f"profile {job} for user {user.id}", model_id,
        response.get("output_tokens"), empty=empty)


def retip_profile_chain(user_id, version):
    """Make ``version`` the chain tip again by writing a "revert" copy of
    it, stamped with the present moment as its render time. Everything
    unread as of now — the range that later versions had covered, freshly
    imported data, organic writing since — then counts as the unfinished
    chain: the seeder / heartbeat continues from here and the planner
    covers it to the end in equal chunks. The superseded versions stay as
    history. Used by imports and by the repair script."""
    from backend.utils.privacy import PrivacyLevel
    copy = UserProfile(
        user_id=user_id,
        generated_by=version.generated_by,
        tokens_used=0,
        privacy_level=PrivacyLevel.PRIVATE,
        ai_usage=version.ai_usage,
        source_tokens_used=version.source_tokens_used,
        source_data_cutoff=version.source_data_cutoff,
        source_origin_stats=version.source_origin_stats,
        source_rendered_at=datetime.utcnow(),
        generation_type="revert",
        parent_profile_id=version.id,
    )
    copy.set_content(version.get_content())
    db.session.add(copy)
    db.session.flush()
    logger.info(
        "User %d: profile chain re-tipped at version %d (cutoff=%s)",
        user_id, version.id, version.source_data_cutoff)
    return copy


def revert_profile_for_import(user_id, earliest_imported_created_at):
    """Invalidate only the profile versions an import touches.

    A version is invalid when its window would have held the imported
    data: its cutoff is at or later than the earliest imported timestamp
    (a window takes every node on its cutoff's own second and the next
    one starts strictly after it, so a node imported onto that second
    belongs to this version, not a later one). The latest version of the
    CURRENT chain whose cutoff is strictly before that timestamp is still
    valid and becomes the chain tip again (``retip_profile_chain``); the
    newer versions are superseded and the chain is regenerated from there
    to the end. When no version is valid — the import predates the
    chain's root, or no version carries a cutoff — the whole chain is
    invalidated and a from-scratch build is flagged.

    Returns ``("full", None)`` (flag set), ``("revert", copy)`` or
    ``("none", tip)`` when nothing is invalidated: no parseable timestamp,
    or the import is newer than the tip's cutoff.
    """
    profiles = UserProfile.query.filter(
        UserProfile.user_id == user_id,
        UserProfile.generation_type != 'integration'
    ).order_by(UserProfile.created_at.desc()).all()

    def _flag_full():
        user = User.query.get(user_id)
        if user:
            user.profile_needs_full_regen = True
        return "full", None

    if not profiles:
        return _flag_full()
    tip = profiles[0]
    if earliest_imported_created_at is None:
        return "none", tip

    # Walk the CURRENT chain (tip -> root), not every version the account
    # ever had: provisional-ladder builds and older from-scratch rebuilds
    # stay in the table unchained, and re-tipping at one of those would
    # continue the chain from a base the pipeline already discarded.
    chain = list(reversed(_collect_iterative_chain(tip.id)))   # tip first
    valid = next(
        (p for p in chain
         if p.source_data_cutoff
         and p.source_data_cutoff < earliest_imported_created_at),
        None)
    if valid is None:
        return _flag_full()
    if valid.id == tip.id:
        return "none", valid
    logger.info(
        "User %d: import dated %s invalidates %d profile version(s) after "
        "version %d", user_id, earliest_imported_created_at,
        sum(1 for p in chain if p.created_at > valid.created_at), valid.id)
    return "revert", retip_profile_chain(user_id, valid)


@celery.task(base=ProfileGenerationTask, bind=True)
def update_user_profile(self, user_id: int, model_id: str,
                        previous_profile_id: int = None,
                        integrate_only: bool = False):
    """
    Unified profile generation / update task.

    If previous_profile_id is provided, performs an incremental update
    using only new data written after the previous profile's cutoff.
    Otherwise, performs initial generation (possibly iterative if the
    source data exceeds the context window budget).

    integrate_only: only the integration over the chain ending at
    previous_profile_id — the retry of a run whose integration was
    refused (``integration_due``, #368).
    """
    logger.info(
        f"Starting profile update for user {user_id}, model {model_id}, "
        f"prev_profile={previous_profile_id}"
    )

    with flask_app.app_context():
        user = User.query.get(user_id)
        if not user:
            raise ValueError(f"User {user_id} not found")

        from backend.utils.spend import user_is_capped
        if user_is_capped(user):
            logger.warning(
                "User %s is spend-capped; skipping profile update", user_id)
            return
        if not account_allows_ai(user):
            logger.info(
                "User %s has opted out of AI usage; skipping profile update",
                user_id)
            return
        previous_profile_id = ai_readable_base_id(user_id, previous_profile_id)

        # Set concurrency guard
        user.profile_generation_task_id = self.request.id
        user.profile_generation_task_dispatched_at = datetime.utcnow()
        db.session.commit()

        success = False
        try:
            if model_id not in flask_app.config["SUPPORTED_MODELS"]:
                raise ValueError(f"Unsupported model: {model_id}")

            model_cfg = flask_app.config["SUPPORTED_MODELS"][model_id]
            context_window = model_cfg.get("context_window", 200000)
            max_output_tokens = min(
                model_cfg.get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS),
                DEFAULT_MAX_OUTPUT_TOKENS)
            api_keys = get_api_keys_for_usage(flask_app.config, 'chat')

            if integrate_only and previous_profile_id:
                result = _do_integration(
                    self, user, model_id, previous_profile_id, api_keys
                ) or {'user_id': user.id, 'profile_id': None,
                      'status': 'completed',
                      'message': 'Nothing to integrate'}
            elif previous_profile_id:
                result = _do_incremental_update(
                    self, user, model_id, previous_profile_id,
                    context_window, max_output_tokens, api_keys
                )
            else:
                result = _do_initial_generation(
                    self, user, model_id, max_output_tokens, api_keys
                )

            success = True
            if result and result.get('profile_id'):
                from backend.utils.notifications import notify_profile_ready
                notify_profile_ready(user_id)
            return result

        except Exception as e:
            logger.error(
                f"Profile update error for user {user_id}: {e}",
                exc_info=True
            )
            raise
        finally:
            # Clear concurrency guard; only clear full-regen flag on success
            user = User.query.get(user_id)
            if user:
                user.profile_generation_task_id = None
                user.profile_generation_task_dispatched_at = None
                if success:
                    user.profile_needs_full_regen = False
                db.session.commit()


def _do_incremental_update(self, user, model_id, previous_profile_id,
                           context_window, max_output_tokens, api_keys):
    """Incremental update: always uses chunked processing."""
    self.update_state(state='PROGRESS', meta={
        'progress': 10, 'status': 'Loading previous profile'
    })

    prev_profile = UserProfile.query.get(previous_profile_id)
    if not prev_profile or prev_profile.user_id != user.id:
        raise ValueError(f"Previous profile {previous_profile_id} not found")

    cutoff = prev_profile.source_data_cutoff

    # Build update template
    update_template = build_update_template(user.id)

    # Check if there's any new data at all. A null cutoff (user-written or
    # legacy profile) has no baseline, so treat ALL eligible data as new and
    # let the chunk loop fold it into the existing profile — rather than
    # crashing on `Node.created_at > None` or discarding the profile via a
    # full regen.
    from backend.models import Node
    q = Node.query.filter(
        Node.user_id == user.id,
        Node.ai_usage.in_(['chat', 'train'])
    )
    if cutoff is not None:
        q = q.filter(Node.created_at > cutoff)
    has_new_data = q.first() is not None

    if not has_new_data:
        logger.info(f"No new data for user {user.id} since cutoff {cutoff}")
        return {
            'user_id': user.id,
            'profile_id': prev_profile.id,
            'status': 'completed',
            'total_tokens': 0,
            'message': 'No new data to update',
        }

    return _do_iterative_incremental_update(
        self, user, model_id, prev_profile, update_template,
        cutoff, api_keys, max_output_tokens=max_output_tokens
    )


def _do_iterative_incremental_update(self, user, model_id, prev_profile,
                                     update_template, cutoff, api_keys,
                                     max_output_tokens=None):
    """Incremental update with chunked processing."""
    logger.info(
        f"Starting iterative incremental update for user {user.id}, "
        f"cutoff={cutoff}"
    )

    # When the base is the user's own hand-written profile, tell the LLM so
    # (it's chunk 1's {existing_profile}; later chunks build on generated
    # output and need no note). The batch builder reads its base the same
    # way.
    base_content = profile_text_for_prompt(prev_profile)

    current_profile_id, chunk_num, cumulative_source_tokens = \
        _chunked_profile_loop(
            self, user, model_id, update_template, api_keys,
            max_output_tokens=max_output_tokens,
            initial_profile_content=base_content,
            initial_profile_id=prev_profile.id,
            initial_source_tokens=prev_profile.source_tokens_used or 0,
            initial_cutoff=cutoff,
            initial_origin_stats=prev_profile.source_origin_stats,
            generation_type="update",
            status_prefix="Updating profile",
        )

    logger.info(
        f"Iterative incremental update for user {user.id}: "
        f"{chunk_num} chunks, profile {current_profile_id}"
    )

    result = {
        'user_id': user.id,
        'profile_id': current_profile_id,
        'status': 'completed',
        'total_tokens': cumulative_source_tokens,
        'chunks_processed': chunk_num,
    }

    # Run integration if we processed chunks and profile changed
    if chunk_num > 0 and prev_profile.id != current_profile_id:
        logger.info(
            f"User {user.id}: running integration after "
            f"{chunk_num} chunks"
        )
        integration_result = _do_integration(
            self, user, model_id, current_profile_id, api_keys
        )
        if integration_result:
            result = integration_result
    elif chunk_num == 0:
        logger.info(
            f"User {user.id}: no chunks processed — nothing renders "
            f"after the cutoff"
        )

    return result


def _do_initial_generation(self, user, model_id, max_output_tokens,
                           api_keys):
    """From-scratch generation: the whole corpus is the remainder and is
    planned into equal chunks like any other — one chunk (saved as
    "initial") when it rounds to one, a chain plus integration otherwise."""
    self.update_state(state='PROGRESS', meta={
        'progress': 10, 'status': 'Gathering writing samples'
    })
    # A SQL sum decides whether there is anything to do — never a render
    # of the whole corpus, which loads and decrypts every node (the 512 MB
    # staging worker was OOM-killed that way on a 61k-node import).
    if _estimate_source_tokens(user) == 0:
        raise ValueError("No writing found to analyze")
    gen_template = _load_prompt("profile_generation.txt", user_id=user.id)
    return _iterative_generation(
        self, user, model_id, gen_template, max_output_tokens, api_keys)


def _collect_iterative_chain(last_profile_id):
    """Walk parent_profile_id backwards to collect the full iterative chain.

    Includes profiles with generation_type in ("iterative", "update",
    "initial") — "initial" for backwards compat with users whose last
    iterative profile was already re-typed by the old code.

    Returns list in chronological order (oldest first).
    """
    chain = []
    seen = set()
    current_id = last_profile_id

    while current_id and current_id not in seen:
        seen.add(current_id)
        profile = UserProfile.query.get(current_id)
        if not profile:
            break
        if profile.generation_type not in (
            "iterative", "update", "initial", "revert"
        ):
            break
        chain.append(profile)
        current_id = profile.parent_profile_id

    chain.reverse()  # chronological: oldest first
    return chain


def _calculate_months_span(first_profile, last_profile):
    """Months between source_data_cutoff of first and last profiles."""
    first_ts = getattr(first_profile, 'source_data_cutoff', None)
    last_ts = getattr(last_profile, 'source_data_cutoff', None)
    if not first_ts or not last_ts:
        return 1
    delta = last_ts - first_ts
    return max(1, round(delta.days / 30.44))


# --- Shared prompt/message builders -------------------------------------
# Extracted so the synchronous chunk loop AND the batch request builder
# (backend/tasks/profile_batch.py) produce byte-identical prompts — single
# source of truth (issue #173, Part A).

def build_update_template(user_id):
    """Assemble the incremental-update template: profile_update.txt with the
    generation prompt (minus its OUTPUT section) embedded."""
    update_template = _load_prompt("profile_update.txt", user_id=user_id)
    gen_template = _load_prompt("profile_generation.txt", user_id=user_id)
    gen_template_no_output = gen_template.split("## OUTPUT")[0]
    return update_template.replace(
        "{profile_generation_prompt}", gen_template_no_output
    )


ORIGIN_LABELS = {
    "twitter": "public tweets (imported from Twitter/X)",
    "chatgpt": "ChatGPT conversations (imported)",
    "claude": "Claude conversations (imported)",
    "markdown": "markdown documents (imported)",
    "loore": "written in Loore",
}


def merge_origin_stats(prev_stats, chunk_stats):
    """Sum two {origin: {nodes, tokens}} dicts (either may be None)."""
    merged = {k: dict(v) for k, v in (prev_stats or {}).items()}
    for origin, s in (chunk_stats or {}).items():
        entry = merged.setdefault(origin, {"nodes": 0, "tokens": 0})
        entry["nodes"] += s.get("nodes", 0)
        entry["tokens"] += s.get("tokens", 0)
    return merged


def _format_origin_shares(stats):
    total = sum(s["tokens"] for s in stats.values()) or 1
    parts = []
    for origin, s in sorted(stats.items(), key=lambda kv: -kv[1]["tokens"]):
        label = ORIGIN_LABELS.get(origin, f"imported from {origin}")
        pct = round(s["tokens"] / total * 100)
        parts.append(f"{pct}% {label}, {s['nodes']:,} entries")
    return "; ".join(parts)


def _loore_only(stats):
    return not stats or set(stats) == {"loore"}


def source_mix_preamble(chunk, prev_stats=None):
    """Source-mix header for a profile-generation chunk.

    prev_stats: cumulative origin stats of the profile being updated
    (UserProfile.source_origin_stats), None for initial generation.

    Empty when the WHOLE history (base + chunk) is Loore-native — the
    default costs no tokens. Otherwise, initial generation gets
    "[Source mix: 94% public tweets (...), 3,100 entries; 6% written in
    Loore, 12 entries]"; an update gets both the base's mix and the new
    data's, so the model sees e.g. a 100%-tweets base receiving its
    first Loore-native writing instead of treating a public-tweets
    corpus as a private journal.
    """
    stats = (chunk or {}).get("origin_stats") or {}
    if _loore_only(stats) and _loore_only(prev_stats):
        return ""
    if not prev_stats:
        return "[Source mix: " + _format_origin_shares(stats) + "]\n\n"
    new_part = _format_origin_shares(stats) if stats else "none"
    return ("[Source mix — existing profile built from: "
            + _format_origin_shares(prev_stats)
            + ". New data below: " + new_part + "]\n\n")


def chunk_content_for_prompt(chunk, prev_stats=None):
    """The chunk's export text as fed to the profile prompts: source-mix
    preamble (when any content, base or new, is imported) + content, then
    the user's own profile when this window is where a rebuild folds it in
    (``place_user_written_profile``, #183)."""
    text = source_mix_preamble(chunk, prev_stats) + chunk["content"]
    block = chunk.get("user_written_block")
    if block:
        text = f"{text}\n\n{block}"
    return text


def build_chunk_prompt(update_template, current_profile_content,
                       cumulative_units, chunk, prev_origin_stats=None):
    """Build the per-chunk incremental-update prompt (the non-first-chunk
    branch of _chunked_profile_loop). The proportionality figures are in
    stored content units on BOTH sides — what the profile has covered so
    far and this window's own rows — so the stated share is not distorted
    by the tokenizer (past used to be billed tokens and new the rendered
    chars/4, which roughly halved every chunk's stated share)."""
    chunk_units = chunk["unit_count"]
    ratio_pct = round(
        chunk_units / max(cumulative_units + chunk_units, 1) * 100, 1)
    prompt = update_template.replace(
        "{existing_profile}", current_profile_content
    )
    prompt = prompt.replace(
        "{new_data}", chunk_content_for_prompt(chunk, prev_origin_stats))
    prompt = prompt.replace("{source_tokens_past}", str(cumulative_units))
    prompt = prompt.replace("{source_tokens_new}", str(chunk_units))
    prompt = prompt.replace("{ratio_percent}", str(ratio_pct))
    return prompt


def build_integration_messages(user_id, last_iterative_profile_id):
    """Build the integration message list — one user message per profile
    version in the chain, then the integration prompt. Returns
    (messages, chain), or (None, None) if there are < 2 versions to merge.
    Versions whose ai_usage is not AI-readable are left out (#346)."""
    chain = [p for p in _collect_iterative_chain(last_iterative_profile_id)
             if p.ai_usage in AI_ALLOWED]
    if len(chain) < 2:
        return None, None

    n_months = _calculate_months_span(chain[0], chain[-1])
    integration_template = _load_prompt(
        "profile_integration.txt", user_id=user_id
    )
    gen_template = _load_prompt("profile_generation.txt", user_id=user_id)
    integration_prompt = integration_template.replace(
        "{N_MONTHS}", str(n_months)
    )
    integration_prompt = integration_prompt.replace(
        "{profile_generation_prompt}", gen_template
    )

    entries = []
    for i, profile in enumerate(chain):
        # The chain root can be the user's hand-written profile — flag it so
        # the integration treats it as the user's own words.
        content = profile_text_for_prompt(profile)
        cutoff = profile.source_data_cutoff
        date_str = cutoff.strftime("%Y-%m-%d") if cutoff else "unknown"
        if i == 0:
            date_from = "start"
        else:
            prev_cutoff = chain[i - 1].source_data_cutoff
            date_from = prev_cutoff.strftime(
                "%Y-%m-%d"
            ) if prev_cutoff else "unknown"
        entries.append((f"- {date_from} to {date_str}", content))

    # #183: a chain that passed the date of the user's own profile without
    # descending from it (a from-scratch rebuild, which folded the profile
    # into one chunk) also shows the profile itself, at its date — as a
    # chain rooted at the profile does — so the merged profile keeps the
    # user's words.
    own = latest_user_written_profile(user_id)
    if own is not None:
        at = next((n for n, p in enumerate(chain)
                   if p.source_data_cutoff is not None
                   and p.source_data_cutoff >= own.created_at), None)
        if at is not None and not _descends_from(chain[-1], own.id):
            entries.insert(at, (
                f"- written by the user on "
                f"{own.created_at.strftime('%Y-%m-%d')}",
                user_written_profile_text(own)))

    messages = []
    for i, (span, content) in enumerate(entries, 1):
        messages.append({
            "role": "user",
            "content": [{
                "type": "text",
                "text": (
                    f"Profile No. {i}\n"
                    f"{span}\n\n"
                    f"{content}"
                )
            }]
        })
    messages.append({
        "role": "user",
        "content": [{"type": "text", "text": integration_prompt}]
    })
    return messages, chain


def _do_integration(self, user, model_id, last_iterative_profile_id,
                    api_keys):
    """Integrate all iterative profile versions into a single unified profile.

    Collects the full iterative chain, sends each version as a separate
    user message, and asks the LLM to produce a unified profile.

    Returns a result dict like other generation functions, or None if
    integration is not needed (< 2 versions in chain).
    """
    messages, chain = build_integration_messages(
        user.id, last_iterative_profile_id
    )
    if messages is None:
        return None

    self.update_state(state='PROGRESS', meta={
        'progress': 90, 'status': 'Integrating profile versions'
    })

    response = LLMProvider.get_completion(model_id, messages, api_keys)
    refuse_truncated_profile(user, model_id, response, "integration")

    last_profile = chain[-1]
    new_profile = _save_profile(
        user, model_id, response["content"], response,
        source_tokens_used=last_profile.source_tokens_used,
        source_data_cutoff=last_profile.source_data_cutoff,
        generation_type="integration",
        parent_profile_id=last_profile.id,
        source_origin_stats=last_profile.source_origin_stats,
    )

    logger.info(
        f"Profile integration for user {user.id}: "
        f"{len(chain)} versions -> profile {new_profile.id}"
    )

    return {
        'user_id': user.id,
        'profile_id': new_profile.id,
        'status': 'completed',
        'total_tokens': response["total_tokens"],
        'profile_length': len(response["content"]),
        'versions_integrated': len(chain),
    }


def _messages_for(prompt_text):
    return [{"role": "user", "content": [{"type": "text", "text": prompt_text}]}]


def build_fitted_chunk(user, model_id, api_keys, input_cap, cutoff, budget,
                       remaining_units, prompt_fn):
    """Render the next window at ``budget`` units and its prompt, count the
    prompt (free and exact on Anthropic, tiktoken on OpenAI) and, while it
    exceeds ``input_cap``, shrink the window by the measured ratio and
    rebuild — the real-count check of the design note, shared by the sync
    loop and the batch builder. Returns (chunk, prompt), or None when
    nothing renders after the cutoff. A window still over the cap after
    the sizing rounds is returned anyway: both callers keep a call-time
    backstop (the pre-call truncation in _call_llm_with_retries; the
    failed-item machinery in the batch poller)."""
    from flask import current_app
    from backend.llm_providers import fit_by_count

    def _build(units):
        chunk = build_user_export_content(
            user, max_tokens=int(units), filter_ai_usage=True,
            created_after=cutoff, chronological_order=True,
            return_metadata=True, include_strategy="engaged_threads")
        if not chunk or not chunk.get("content"):
            return None
        prompt = prompt_fn(chunk)
        return _messages_for(prompt), chunk, prompt

    built, _budget, _real = fit_by_count(
        model_id, api_keys, input_cap, budget, _build,
        corpus_tokens=remaining_units, max_rounds=3, min_budget=5000,
        safety=current_app.config.get("RETRY_SAFETY_FACTOR", 0.99),
        strict=False)
    if built is None:
        return None
    _messages, chunk, prompt = built
    return chunk, prompt


def _chunked_profile_loop(self, user, model_id, update_template,
                          api_keys, max_output_tokens=None,
                          initial_profile_content=None,
                          initial_profile_id=None,
                          initial_source_tokens=0,
                          initial_cutoff=None,
                          initial_origin_stats=None,
                          first_chunk_prompt_fn=None,
                          generation_type="iterative",
                          status_prefix="Generating profile"):
    """Shared chunked profile processing loop.

    Before every chunk the remainder after the current cutoff is summed in
    the window's own scope and planned into equal chunks
    (``next_window_budget``): the chunk COUNT is rounded, so a fixed corpus
    is always covered with no leftover tail, and the model's real-token
    cap only ever raises the count. The final window over-asks and takes
    everything. Nothing is deferred to "the next update cycle" — a
    pre-filled corpus never gets one.

    Args:
        first_chunk_prompt_fn: Optional callable(chunk) -> prompt string
            for the first chunk (from-scratch generation). If None,
            update_template is used for all chunks.
        generation_type: Profile generation_type for saved profiles. A
            from-scratch build that plans into a single chunk is saved as
            "initial".
        status_prefix: Label prefix for progress updates.

    Returns:
        (current_profile_id, chunk_num, cumulative_units)
    """
    from flask import current_app
    model_cfg = current_app.config.get(
        "SUPPORTED_MODELS", {}).get(model_id) or {}
    input_cap = model_input_cap(model_cfg, max_output_tokens)

    current_profile_content = initial_profile_content
    current_profile_id = initial_profile_id
    cumulative_units = initial_source_tokens
    cumulative_origin_stats = initial_origin_stats
    current_cutoff = initial_cutoff
    chunk_num = 0
    saved = None   # the version saved by the previous iteration

    while True:
        # After a saved chunk, go on only over data that existed when its
        # window was rendered; anything written since is organic growth
        # and waits for the gates (the caller's gate decided the first
        # step). Otherwise a user writing during their own update would
        # add a small chunk per iteration until they stopped.
        if saved is not None and not should_continue_chain(user, saved):
            logger.info(
                f"User {user.id}: only data newer than the last window "
                f"remains — ending the run at {chunk_num} chunk(s)")
            break
        remaining = count_remaining_units(user.id, current_cutoff)
        if remaining <= 0:
            break
        progress = min(10 + (chunk_num + 1) * 15, 85)
        self.update_state(state='PROGRESS', meta={
            'progress': progress,
            'status': f'Processing chunk {chunk_num + 1}'
        })

        rho = tokens_per_unit(user, model_id)
        k, size, budget = next_window_budget(
            remaining, max_units=max_units_for_cap(input_cap, rho))
        logger.info(
            f"User {user.id}: chunk {chunk_num + 1} plan — {remaining} "
            f"units remain after {current_cutoff}: {k} chunk(s) of "
            f"{size:.0f}, window budget {budget} (cap {input_cap} tokens "
            f"at {rho:.2f} tokens/unit)")

        is_first_with_gen = bool(
            first_chunk_prompt_fn and current_profile_content is None)
        base = (UserProfile.query.get(current_profile_id)
                if current_profile_id else None)

        def _prompt_for(chunk):
            # #183: the user's own profile joins the window that reaches
            # its date (shared with the batch builder).
            place_user_written_profile(user.id, base, chunk)
            if is_first_with_gen:
                return first_chunk_prompt_fn(chunk)
            return build_chunk_prompt(
                update_template, current_profile_content,
                cumulative_units, chunk, cumulative_origin_stats)

        rendered_at = datetime.utcnow()
        fitted = build_fitted_chunk(
            user, model_id, api_keys, input_cap, current_cutoff, budget,
            remaining, _prompt_for)
        if fitted is None:
            break
        chunk, prompt = fitted
        chunk_num += 1
        chunk_units = chunk["unit_count"]
        # The window's last node, or the user's profile date when the
        # profile went into a final window older than it (#183).
        latest_ts = chunk.get("version_cutoff") or chunk["latest_node_created_at"]

        response = _call_llm_with_retries(
            self, model_id, prompt, user.id, api_keys,
            progress_base=progress,
            status_label=f'{status_prefix}: Chunk {chunk_num}',
            max_tokens=max_output_tokens,
        )
        # A cut-off chunk (empty or partial) ends the run here, before it
        # can become the base of the next chunk; the chunks saved so far
        # stay (#368).
        refuse_truncated_profile(
            user, model_id, response, f"chunk {chunk_num}")

        observed = record_token_ratio(
            user, model_id, chunk_units, response.get("input_tokens"))
        cumulative_units += chunk_units
        cumulative_origin_stats = merge_origin_stats(
            cumulative_origin_stats, chunk.get("origin_stats"))

        profile = _save_profile(
            user, model_id, response["content"], response,
            source_tokens_used=cumulative_units,
            source_data_cutoff=latest_ts,
            generation_type=("initial" if is_first_with_gen and k == 1
                             else generation_type),
            parent_profile_id=current_profile_id,
            source_origin_stats=cumulative_origin_stats,
            source_rendered_at=rendered_at,
        )

        current_profile_content = response["content"]
        current_profile_id = profile.id
        current_cutoff = latest_ts
        saved = profile

        # After the first committed chunk, a from-scratch full regen is no
        # longer needed: chunk 1 is the oldest data, so the chronological
        # rebuild is anchored, and a later timeout can resume incrementally
        # from this chunk instead of restarting from zero next heartbeat.
        # (No-op for incremental updates, where the flag is already False.)
        if chunk_num == 1 and user.profile_needs_full_regen:
            user.profile_needs_full_regen = False
            db.session.commit()
            logger.info(
                f"User {user.id}: cleared profile_needs_full_regen after "
                f"first chunk (profile {profile.id}) — any later timeout "
                f"resumes incrementally"
            )

        logger.info(
            f"User {user.id}: chunk {chunk_num} done — profile {profile.id}, "
            f"{chunk_units} units, {response.get('input_tokens')} billed "
            f"input tokens ({observed} tokens/unit), "
            f"cumulative={cumulative_units}"
        )

    return current_profile_id, chunk_num, cumulative_units


def _iterative_generation(self, user, model_id, gen_template,
                          max_output_tokens, api_keys):
    """From-scratch profile build in planned chronological chunks: chunk 1
    takes the generation prompt, later chunks the update prompt, and a
    multi-chunk chain is integrated at the end."""
    logger.info(f"Starting profile build for user {user.id}")

    update_template = build_update_template(user.id)

    current_profile_id, chunk_num, cumulative_units = \
        _chunked_profile_loop(
            self, user, model_id, update_template, api_keys,
            max_output_tokens=max_output_tokens,
            first_chunk_prompt_fn=lambda chunk: gen_template.replace(
                "{user_export}", chunk_content_for_prompt(chunk)
            ),
            generation_type="iterative",
            status_prefix="Generating profile",
        )

    if current_profile_id is None:
        raise ValueError("No writing found to analyze")

    # Run integration over the iterative chain
    if chunk_num > 1:
        integration_result = _do_integration(
            self, user, model_id, current_profile_id, api_keys
        )
        if integration_result:
            return integration_result

    self.update_state(state='PROGRESS', meta={
        'progress': 95, 'status': 'Finalizing'
    })

    logger.info(
        f"Profile build for user {user.id}: "
        f"{chunk_num} chunks, profile {current_profile_id}"
    )

    return {
        'user_id': user.id,
        'profile_id': current_profile_id,
        'status': 'completed',
        'total_tokens': cumulative_units,
        'chunks_processed': chunk_num,
    }


def ai_readable_base_id(user_id, previous_profile_id):
    """The update base as the task receives it, re-checked when the task
    runs, since its ai_usage may have changed after dispatch (#346). A
    base that is no longer AI-readable is replaced by the newest version
    that is (profile_update_base). None (a from-scratch build) stays
    None."""
    if not previous_profile_id:
        return None
    base = UserProfile.query.get(previous_profile_id)
    if base is None or base.ai_usage in AI_ALLOWED:
        return previous_profile_id
    fallback = profile_update_base(user_id)
    logger.info(
        "Profile %s is not AI-readable; updating from profile %s instead",
        base.id, fallback.id if fallback else None)
    return fallback.id if fallback else None


def profile_update_base(user_id):
    """The newest AI-readable non-integration profile, which the next
    update builds on, or None when there is none (the next build starts
    from the writing).

    A version that is not AI-readable is skipped and never sent to a
    model: a profile written or edited by hand while the account was set
    to 'none', or a batch step collected after the account was switched.
    The chain continues from the version before it, with the normal
    update gates from that version's cutoff, instead of rebuilding the
    whole account. Integration drops such versions the same way (#346)."""
    return UserProfile.query.filter(
        UserProfile.user_id == user_id,
        UserProfile.generation_type != 'integration',
        UserProfile.ai_usage.in_(AI_ALLOWED),
    ).order_by(UserProfile.created_at.desc()).first()


def maybe_trigger_profile_update(user_id, model_id=None,
                                  force_full_regen=False,
                                  integrate_only=False):
    """
    Check concurrency guard and dispatch update_user_profile if safe.
    Returns the task_id or None if skipped. integrate_only: run only the
    integration over the chain ending at the update base
    (``integration_due``).
    """
    user = User.query.get(user_id)
    if not user:
        return None
    if not account_allows_ai(user):
        logger.info(
            f"Skipping profile update for user {user_id}: opted out of AI usage")
        return None

    # Check concurrency guard
    if user.profile_generation_task_id:
        if not _is_task_stale(user):
            logger.info(
                f"Skipping profile update for user {user_id}: "
                f"task {user.profile_generation_task_id} in progress"
            )
            return None
        # Clear stale guard
        logger.info(
            f"Clearing stale profile task guard for user {user_id}: "
            f"task {user.profile_generation_task_id}"
        )
        user.profile_generation_task_id = None
        user.profile_generation_task_dispatched_at = None
        db.session.commit()

    if model_id is None:
        from backend.utils.llm_nodes import default_model_for
        model_id = default_model_for(user)

    latest_profile = profile_update_base(user_id)

    prev_id = None if force_full_regen else (
        latest_profile.id if latest_profile else None
    )

    if integrate_only:
        if prev_id is None:
            return None
        task = update_user_profile.delay(
            user_id, model_id, prev_id, integrate_only=True)
    else:
        task = update_user_profile.delay(user_id, model_id, prev_id)
    user.profile_generation_task_id = task.id
    user.profile_generation_task_dispatched_at = datetime.utcnow()
    db.session.commit()

    logger.info(
        f"Dispatched profile update task {task.id} for user {user_id}"
        f" (force_full_regen={force_full_regen},"
        f" integrate_only={integrate_only})"
    )
    return task.id


def maybe_trigger_incremental_profile_update(user, ignore_backoff=False):
    """
    Check if enough new writing has accumulated to trigger an
    incremental profile update. Called periodically by Celery beat.

    ignore_backoff: skip the refusal backoff (wait and stop). Only the
    import hand-off passes it: new data is a new input, so a build the
    import triggers runs at once (voice review, 2026-10-01). The hourly
    check never does.
    """
    from datetime import datetime, timedelta
    from backend.models import Node

    if not account_allows_ai(user):
        return None

    # Only for paid plans
    if (user.plan or "free") not in User.VOICE_MODE_PLANS:
        return None

    # Batch-selected users are driven by the batch pipeline
    # (backend/tasks/profile_batch.py), not the synchronous path — unless they
    # exhausted their batch retries, in which case sync is the last resort.
    # Local import avoids a circular import (profile_batch imports exports).
    from backend.tasks.profile_batch import (
        use_batch_for_user, MAX_BATCH_ATTEMPTS)
    if use_batch_for_user(user, flask_app.config) and (
            user.profile_force_batch
            or (user.profile_batch_attempts or 0) < MAX_BATCH_ATTEMPTS):
        # Pinned (pre-filled) accounts never take the sync last resort —
        # a persistent batch failure leaves them visibly "generating" in
        # the admin list rather than silently running at full price.
        return None

    # User must have been inactive for at least 30 minutes
    last_node = Node.query.filter_by(user_id=user.id) \
        .order_by(Node.created_at.desc()).first()
    MIN_INACTIVITY = timedelta(minutes=30)
    if last_node and (datetime.utcnow() - last_node.created_at) < MIN_INACTIVITY:
        return None

    # After a refused (cut-off) output the gates below stay open — nothing
    # was saved — so without this the same billed call repeats hourly (#368).
    if not ignore_backoff and refusal_backoff.profile_in_backoff(user.id):
        return None

    latest_profile = profile_update_base(user.id)

    MIN_INTERVAL = timedelta(hours=1)

    if latest_profile:
        # An unfinished chain — data beyond the cutoff that is OLDER than
        # the version (an import still being folded in, a chunk lost to a
        # restart) — continues regardless of the interval / volume gates.
        if should_continue_chain(user, latest_profile):
            logger.info(
                f"User {user.id}: continuing an unfinished profile chain")
            return maybe_trigger_profile_update(
                user.id, force_full_regen=user.profile_needs_full_regen)
        # A finished chain whose integration was refused (or lost): run the
        # integration alone (#368).
        if integration_due(user, latest_profile):
            logger.info(
                f"User {user.id}: chain {latest_profile.id} has no "
                f"integration; integrating it")
            return maybe_trigger_profile_update(
                user.id, integrate_only=True)
        # Check minimum interval
        if (datetime.utcnow() - latest_profile.created_at) < MIN_INTERVAL:
            return None

    # No profile, or a provisional one: the ladder decides, and every
    # build is from scratch (the earlier provisional versions stay as
    # history, unchained).
    if latest_profile is None or profile_is_provisional(latest_profile):
        due, total, threshold = provisional_build_due(user, latest_profile)
        if not due:
            logger.debug(
                f"User {user.id}: {total} units, next provisional build at "
                f"{threshold}")
            return None
        logger.info(
            f"User {user.id}: {total} units >= {threshold} — "
            f"{'first' if latest_profile is None else 'next provisional'} "
            f"build from scratch")
        return maybe_trigger_profile_update(user.id, force_full_regen=True)

    if latest_profile:
        cutoff = latest_profile.source_data_cutoff
        from sqlalchemy import func, or_
        q = db.session.query(
            func.coalesce(func.sum(Node.token_count), 0)
        ).filter(
            or_(Node.user_id == user.id,
                Node.human_owner_id == user.id),
            Node.ai_usage.in_(['chat', 'train']),
        )
        if cutoff:
            new_tokens = q.filter(Node.updated_at >= cutoff).scalar()
        else:
            # No cutoff (e.g. a user-written profile): nothing has been folded
            # into it yet, so all the user's data counts as new. Measure it and
            # apply the same threshold rather than force-triggering on volume we
            # never checked — otherwise a hand-written profile with almost no
            # data gets needlessly overwritten by an LLM generation.
            new_tokens = q.scalar()

    if new_tokens >= UPDATE_THRESHOLD_UNITS:
        logger.info(
            f"User {user.id}: triggering profile update — "
            f"{new_tokens} units >= {UPDATE_THRESHOLD_UNITS} threshold"
        )
        return maybe_trigger_profile_update(
            user.id, force_full_regen=user.profile_needs_full_regen
        )

    logger.debug(
        f"User {user.id}: skipping profile update — "
        f"{new_tokens} units < {UPDATE_THRESHOLD_UNITS} threshold"
    )
    return None


@celery.task
def check_pending_profile_updates():
    """Periodic task: check all eligible users for pending profile updates."""
    with flask_app.app_context():
        if flask_app.config.get("PROFILE_UPDATES_PAUSED"):
            logger.info(
                "PROFILE_UPDATES_PAUSED — skipping sync profile-update check")
            return
        # Voice-Mode users minus the llm-<model> placeholder accounts.
        # Shared helper keeps NULL-twitter_id (email signup) users in —
        # a bare NOT LIKE drops them (NULL NOT LIKE = NULL). See
        # User.profile_eligible_query.
        users = User.profile_eligible_query().all()
        for user in users:
            try:
                maybe_trigger_incremental_profile_update(user)
            except Exception as e:
                logger.warning(
                    f"Profile update check failed for user {user.id}: {e}"
                )


@celery.task(bind=True)
def export_user_threads(self, user_id: int):
    """
    Asynchronously export user's threads to formatted text.

    Args:
        user_id: Database ID of the user

    Returns:
        dict: Export result with formatted text
    """
    logger.info(f"Starting thread export task for user {user_id}")

    with flask_app.app_context():
        user = User.query.get(user_id)
        if not user:
            raise ValueError(f"User {user_id} not found")

        try:
            self.update_state(state='PROGRESS', meta={'progress': 20, 'status': 'Building export'})

            # The user's own download: rows marked 'none' are included.
            export_content = build_user_export_content(
                user, filter_ai_usage=False)

            if not export_content:
                raise ValueError("No threads found to export")

            logger.info(f"Thread export successful for user {user_id}: {len(export_content)} characters")

            return {
                'user_id': user_id,
                'status': 'completed',
                'export_length': len(export_content),
                'content': export_content
            }

        except Exception as e:
            logger.error(f"Thread export error for user {user_id}: {e}", exc_info=True)
            raise

"""Independent profile versions for large builds (#545).

A profile build whose remainder plans into two or more chunks on the
user's chat model (a huge import, a long backlog) no longer runs as a
chain, where chunk N's prompt carries chunk N-1's profile and the steps go
out one Batch round trip at a time. Instead:

- The remainder is cut into windows by the same planner, sized for the
  VERSION model of the user's provider (Anthropic: Haiku 5.5, whose 100k
  price tier sets the size; OpenAI: GPT-6 Luna).
- Each window gets the generation prompt plus Peter's events paragraph
  (exports.window_generation_template) and is read on its own, and all of
  a user's windows go out in one Batch submission (split only above
  PROFILE_BATCH_MAX_BYTES, every part submitted in the same run).
- Each output is checked (exports.profile_output_problem) before it is
  saved as a "window" row of the user's window set.
- The MERGE model of the user's provider integrates the versions with the
  integration prompt and independent labels ("Covers the writing from A to
  B"): in one call while they fit the merge model's cap ("flat", the
  default), else in fixed groups of PROFILE_MERGE_GROUP_SIZE first (always
  above that size with "grouped"). The final merge is an "integration"
  row of the set, and only it is ever the user's profile: until it is
  saved, every reader keeps the previous one (utils/profile_versions
  .current_profile_query).
- Later updates of such a profile add windows to the same set and redo
  only the newest group and the final.

A from-scratch build that fits ONE chunk on the user's chat model takes
this path too where its provider's PROFILE_ONE_CHUNK_WINDOW_* is on
(Peter, 2026-11-03; both on by default): its writing is read as a window
version with the events paragraph, and a final rewrites that version as
the profile with the single-version integration prompt
(exports.single_integration_messages). Two Batch rounds, one after the
other. Later updates are the set's, as for a large build.

Which models read the writing (Peter, 2026-11-05), chosen per pass
(``pass_models``):

- A from-scratch build that fits one window on the user's chat model,
  writing that may reach only the user's provider: the chat model reads
  the window and writes the final (``one_window_pass``,
  ``single_models_for``).
- More than one window, writing that may reach only the user's provider:
  the version and merge models of the provider of the user's chat model
  (Haiku 5.5 and Opus 5.5, or GPT-6 Luna and GPT-6.1 Sol at high effort).
- Every entry the build can read is public (a pre-fill, the lab) or the
  user agreed to both providers (User.profile_both_providers): GPT-6 Luna
  writes the versions and Opus 5.5 merges them, whatever the number of
  windows (``profile_models_for`` with ``cross``).
- A regular update of a merged set, one window to read: the user's chat
  model writes a chained update of the set's newest profile (the update
  prompt, as main's chained path), which stands in the window's place, and
  the same model runs the group and final merges after it
  (``chained_update_base``, ``chained_models_for``).

Without public-only writing or the user's consent, a user's writing
reaches only models of their own chat model's provider: checked at boot,
per pass, and again for every request before it is sent (_Submission).
The consent is read from the database at each check, and a pass that may
cross providers checks it again before each slice goes out, so a user who
withdraws their yes (#599) gets their own provider from that moment.

State lives in the database, as on the chained batch path: the set's rows
(window, window_skip, group, final), the items of pending ProfileBatchJob
rows (what is in flight), and the outcomes the collector writes back onto
the items of collected jobs (the attempt history). Nothing else is
persisted, so the advance task can be killed at any point and re-run.
One run per user at a time: a Redis lock with a heartbeat
(utils/run_locks) that a killed run leaves to expire.

See docs/design/profile-batch-processing.md, "Independent versions for
large builds".
"""
import json
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import uuid4

from celery.utils.log import get_task_logger
from flask import current_app
from sqlalchemy import or_
from sqlalchemy.orm import defer

from backend.celery_app import celery, flask_app
from backend.extensions import db
from backend.models import (
    User, UserProfile, ProfileBatchJob, ProfileBlockedRange, Node)
from backend.llm_providers import (
    DEFAULT_MAX_OUTPUT_TOKENS, LLMProvider, EmptyTruncatedOutputError,
    model_input_cap)
from backend.utils.chunk_plan import (
    CHUNK_TARGET_UNITS, UPDATE_THRESHOLD_UNITS, max_units_for_cap,
    next_window_budget, plan_chunks)
from backend.utils.api_keys import get_api_keys_for_usage
from backend.utils.error_privacy import describe_exception
from backend.utils.heavy_slot import HeavyTask
from backend.utils.hidden_rows import WritingOnHold, writing_on_hold
from backend.utils.llm_batch import apply_batch_key_override, blocked_items
from backend.utils.privacy import AI_ALLOWED, account_allows_ai
from backend.utils.profile_versions import (
    WINDOW_TYPES, current_profile_query)
from backend.utils.profile_providers import (  # noqa: F401 (re-exported)
    PUBLIC_PROVENANCES, agreed_to_both, note_private_read,
    writing_is_public_only)
from backend.utils import refusal_backoff, run_locks, soft_limit
from backend.utils.tokens import approximate_token_count, format_date_metadata
# Module references (see profile_batch's note): exports and profile_batch
# may be half-initialised when this module is imported.
from backend.tasks import exports as _exports
from backend.tasks import profile_batch as _batch

logger = get_task_logger(__name__)

ITEM_KINDS = ("window", "group", "final")
# A regular one-window update of a merged set, written as a chained update
# by the user's chat model (Peter, 2026-11-05). Stands in the window's
# place in the set's merges; one of profile_versions.WINDOW_TYPES.
CHAINED_TYPE = "window_update"

CONTENT_FAILURES = ("refused", "invalid", "truncated")
PROVIDER_FAILURES = ("missing", "error")
# The provider's content-policy filter blocked the item (an error code in
# llm_batch.OPENAI_POLICY_BLOCK_CODES, e.g. bio_policy; Peter,
# 2026-11-04). Neither a content nor a provider failure, and never sent
# again automatically. A blocked WINDOW is skipped: a window_skip row
# takes its place and the build goes on over the other windows (Peter,
# 2026-11-04: "skip the flagged windows, but finish their profile"), and
# its range is remembered for good (ProfileBlockedRange), so every later
# window plan cuts around it (cut_around_blocked). A
# blocked GROUP or FINAL cannot be skipped: it stops the account's build
# (refusal_backoff.policy_block_for) until the admin's confirmed Build
# profile lifts the stop.
POLICY_BLOCKED = "blocked"
# The cost row's request type of a skipped window's block: a $0 record of
# the block that is not one of PROFILE_REQUEST_TYPES, so it stops nothing.
WINDOW_BLOCK_REQUEST_TYPE = "profile_window"

# ── Heuristics (named here, flagged in the PR) ────────────────────────────
# How far back the attempt history reaches. Long enough to span a batch's
# 24 h window three times over plus a stop; short enough that the scan of
# job rows stays small.
ATTEMPT_LOOKBACK = timedelta(days=7)
# Output tokens one window version costs, by version model, for the cost
# estimate a build's windows must fit (Peter, 2026-11-05: the expected
# cost, not a worst case): the measured means. Haiku 5.5 thinks by
# default: 11.0k on average over 53 versions (7.1-15.8k, 2026-10-22);
# Luna 2.4-2.7k on average in the three events runs (201 versions,
# 1.6-4.2k, 2026-10-21/22). The default is for a version model nobody
# measured.
EXPECTED_OUTPUT = {"claude-haiku-5.5": 11_000, "gpt-6-luna": 2_500,
                   # A chained update by Opus 5.5: 7.1k on average over 26
                   # update steps (5.0-8.7k, 2026-11-05).
                   "claude-opus-5.5": 7_000}
DEFAULT_EXPECTED_OUTPUT = 6_000
# Merge input tokens per version, measured: 53 Haiku versions came to
# 247,885 tokens in one Opus merge (4.7k each); a Luna version is about
# 2.0k o200k tokens.
MERGE_TOKENS_PER_VERSION = {"claude-haiku-5.5": 4_700, "gpt-6-luna": 2_000}
DEFAULT_MERGE_TOKENS_PER_VERSION = 4_700
# Fixed parts of a request, for the estimate: the window prompt's template,
# the merge prompt, and a merge's output, the measured typical size: Opus
# 5.5 merges wrote 3.3-5.1k output tokens, about 3.9k on average (14
# merges, 2026-10-21 to 11-03); GPT-6.1 Sol at high effort 2.8k, its
# reasoning included (2026-10-22).
WINDOW_PROMPT_OVERHEAD_TOKENS = 3_000
# A chained update's fixed part: the update template (3.5k tokens,
# measured 2026-11-05) and the profile it updates (a final of 2-3k tokens,
# a chained version up to about 4.5k).
CHAINED_PROMPT_OVERHEAD_TOKENS = 8_000
MERGE_PROMPT_TOKENS = 4_000
MERGE_OUTPUT_TOKENS = 4_000
# The token count of a merge is checked exactly only when the character
# estimate is below this many times the cap; above it the merge is over
# the cap without asking the counting endpoint.
OVER_CAP_ESTIMATE_FACTOR = 1.5
SET_ID_HEX = 12
# Fitting a window to the version model's cap by its counted tokens: at
# most this many renders, each shrinking the window by the counted
# overshoot with this margin. The tokens per unit of one window moved
# between 1.33 and 2.92 in the 14.1M-unit dry run (deep conversations
# render long node headers), so the planner's ratio alone does not fit.
FIT_ROUNDS = 8
FIT_SAFETY = 0.97
# The defaults of PROFILE_BATCH_MAX_BYTES and PROFILE_WINDOW_RENDER_SECONDS
# (the same in config.py), for an app config without them. A profile build
# for a multi-million-unit import ran the server out of memory with
# 100 MB of prompts held before a submit and a 2400 s render per task
# (2026-11-07). 15 MB is a slice of 15-40 windows (a Haiku window is about
# 400 kB of prompt, a Luna window about 1 MB), so a build of 160 windows
# goes out in 5-11 batches instead of one or two. 600 s of
# rendering per task keeps one task far under the soft time limit, and a
# task that stops for memory or a deploy loses at most that much work;
# the next task picks up where it stopped.
DEFAULT_BATCH_MAX_BYTES = 15_000_000
DEFAULT_RENDER_SECONDS = 600

HOLD_NOTE = "Waiting for spend headroom"
STOP_NOTE = "Stopped: a profile chunk failed at the provider"
MERGE_STOP_NOTE = "Stopped: a profile merge is over the merge model's cap"
NOTHING_READ_NOTE = ("Stopped: every window of the profile build was given "
                     "up; no profile from it")
POLICY_STOP_NOTE = "Stopped: the provider's policy filter blocked"
POLICY_SKIP_NOTE = "Skipped window"
# The outcome of an advance that found another run holding the user's lock
# (run_locks) and exited without rendering.
RUNNING_ELSEWHERE = "running elsewhere"


def enabled():
    """The kill switch (PROFILE_INDEPENDENT_VERSIONS). An app config
    without the key (the unit tests' bare apps) has it off."""
    return bool(current_app.config.get("PROFILE_INDEPENDENT_VERSIONS", False))


def one_chunk_windows(user):
    """Whether a from-scratch build of this user that fits one chunk on
    their chat model takes the window path (a window version with the
    events paragraph, then the single-version integration) rather than
    main's one chained call: PROFILE_ONE_CHUNK_WINDOW_<PROVIDER> of the
    provider of the user's chat model. It decides the path only; the
    models come from ``pass_models``. An app config without the key has
    it off, as has a provider without profile models."""
    supported = current_app.config.get("SUPPORTED_MODELS") or {}
    provider = (supported.get(_batch._model_for(user)) or {}).get("provider")
    if provider not in ("anthropic", "openai"):
        return False
    return bool(current_app.config.get(
        f"PROFILE_ONE_CHUNK_WINDOW_{provider.upper()}", False))


def _setting(key, default):
    value = current_app.config.get(key)
    return default if value in (None, "") else value


# ── models ────────────────────────────────────────────────────────────────

class ProfileModelError(RuntimeError):
    """No version or merge model of the user's own provider is set, or a
    request would reach a provider the user's writing may not reach."""


@dataclass
class Models:
    provider: str            # the provider of the user's chat model
    version: str
    merge: str
    merge_effort: str = None
    cross: bool = False      # the cross-provider pair (public or consent)
    chained: bool = False    # a regular update by the user's chat model


def _provider_of(model_id):
    return _model_cfg(model_id).get("provider")


def _users_provider(user):
    provider = _provider_of(_batch._model_for(user))
    if provider not in ("anthropic", "openai"):
        raise ProfileModelError(f"no profile models for provider {provider!r}")
    return provider


def cross_provider_allowed(user):
    """Whether this user's build may use both providers' models: they
    agreed (User.profile_both_providers, #599, read from the database at
    the time of the check, so a no counts from the next check), or their
    writing is public only (``writing_is_public_only``)."""
    return agreed_to_both(user) or writing_is_public_only(user)


def allowed_providers(user, models):
    """The providers this user's writing may reach now: both, or the
    provider of their preferred model (``models.provider``)."""
    return ({"anthropic", "openai"} if cross_provider_allowed(user)
            else {models.provider})


def chained_models_for(user):
    """A regular update of a merged set: the user's chat model writes the
    chained version and runs the merges after it (Peter, 2026-11-05)."""
    chat_model = _batch._model_for(user)
    return Models(_users_provider(user), chat_model, chat_model,
                  chained=True)


def single_models_for(user):
    """A from-scratch build that fits one window on the user's chat model,
    over writing that may reach only the user's provider: the chat model
    reads the window, with the events paragraph, and rewrites that version
    as the profile with the single-version prompt. Peter, 2026-11-05: "for
    1 window updates on prod I'd like to use the chained version we just
    tested, with user preferred model producing both the version and the
    merge"; 2026-11-03: "the events paragraph is definitely needed" and
    "then we need one more step to convert it into the final profile". No
    reasoning-effort parameter on an OpenAI chat model, as for the merges
    after a regular update (the live replies send none)."""
    chat_model = _batch._model_for(user)
    return Models(_users_provider(user), chat_model, chat_model)


def profile_models_for(user, cross=False):
    """The version and merge models for a pass with more than one window,
    and with ``cross`` for any window pass that is not a regular update.
    By default the settings of the provider of the user's chat model
    (``default_model_for``): each must be that provider's model, anything
    else raises, so a user's writing never reaches another provider (also
    checked at boot). With ``cross`` (``cross_provider_allowed``) the
    cross-provider pair, GPT-6 Luna and Opus 5.5 by default. A user who
    switches provider gets the new provider's models from the next step
    on; the windows the other provider's model wrote are still merged, as
    chained versions are after a switch."""
    supported = current_app.config.get("SUPPORTED_MODELS") or {}
    provider = _users_provider(user)
    if cross:
        version = current_app.config.get(
            "PROFILE_VERSION_MODEL_CROSS_PROVIDER")
        merge = current_app.config.get("PROFILE_MERGE_MODEL_CROSS_PROVIDER")
        for model_id in (version, merge):
            if (supported.get(model_id) or {}).get("provider") not in (
                    "anthropic", "openai"):
                raise ProfileModelError(
                    f"profile model {model_id!r} is not set up")
        effort = None
        if _provider_of(merge) == "openai":
            effort = current_app.config.get(
                "PROFILE_MERGE_REASONING_EFFORT_OPENAI") or None
        return Models(provider, version, merge, effort, cross=True)
    suffix = provider.upper()
    version = current_app.config.get(f"PROFILE_VERSION_MODEL_{suffix}")
    merge = current_app.config.get(f"PROFILE_MERGE_MODEL_{suffix}")
    for model_id in (version, merge):
        if (supported.get(model_id) or {}).get("provider") != provider:
            raise ProfileModelError(
                f"profile model {model_id!r} is not a {provider} model")
    effort = None
    if provider == "openai":
        effort = current_app.config.get(
            "PROFILE_MERGE_REASONING_EFFORT_OPENAI") or None
    return Models(provider, version, merge, effort)


def _model_cfg(model_id):
    return (current_app.config.get("SUPPORTED_MODELS") or {}).get(model_id) or {}


def _cap(model_id):
    """The model's input cap, never across its price tier (#259): Haiku
    5.5 100k, GPT-6 Luna and GPT-6.1 Sol 272k, Opus 5.5 968k."""
    return model_input_cap(_model_cfg(model_id), DEFAULT_MAX_OUTPUT_TOKENS)


# ── small helpers ─────────────────────────────────────────────────────────

def _iso(ts):
    return ts.isoformat() if ts else None


def _dt(value):
    return datetime.fromisoformat(value) if value else None


def _after(ts):
    """The exclusive upper bound that includes ``ts`` itself."""
    return ts + timedelta(microseconds=1) if ts is not None else None


def new_set_id():
    return uuid4().hex[:SET_ID_HEX]


def inflight_items(user_id):
    """The user's window, group and final items in pending jobs."""
    out = []
    for job in ProfileBatchJob.query.filter_by(status="pending").all():
        for item in job.items or []:
            if item.get("user_id") == user_id and item.get("kind") in ITEM_KINDS:
                out.append(item)
    return out


def _set_rows(user_id, set_id, types):
    return (UserProfile.query.options(defer(UserProfile.content))
            .filter(UserProfile.user_id == user_id,
                    UserProfile.window_set == set_id,
                    UserProfile.generation_type.in_(types),
                    UserProfile.ai_usage.in_(AI_ALLOWED))
            .order_by(UserProfile.created_at.asc(), UserProfile.id.asc())
            .all())


def set_windows(user_id, set_id):
    """The set's AI-readable windows and skipped windows, by cutoff. Two
    rows with the same start (a window built again) count once: the newer
    one."""
    by_start = {}
    for row in _set_rows(user_id, set_id, WINDOW_TYPES):
        by_start[row.covers_start] = row
    return sorted(by_start.values(),
                  key=lambda r: (r.source_data_cutoff or datetime.min))


def _visible_profile(user_id):
    return current_profile_query(user_id).filter(
        UserProfile.ai_usage.in_(AI_ALLOWED)).first()


# ── the attempt history ───────────────────────────────────────────────────

def _attempt_key(kind, start, cutoff=None):
    if kind == "window":
        return ("window", start)
    return (kind, start, cutoff)


def _item_key(item):
    return _attempt_key(item.get("kind"), item.get("start"),
                        item.get("cutoff") if item.get("kind") != "window"
                        else None)


def attempt_history(user_id, now=None):
    """{key: [(outcome, when), ...]} over the user's window, group and
    final items in jobs submitted within ATTEMPT_LOOKBACK. A window's key
    is its start, not its set, so a set given up and built again still
    counts the failures of the same window."""
    now = now or datetime.utcnow()
    history = {}
    jobs = ProfileBatchJob.query.filter(
        ProfileBatchJob.submitted_at >= now - ATTEMPT_LOOKBACK,
        ProfileBatchJob.status != "pending").all()
    for job in jobs:
        when = job.collected_at or job.submitted_at
        for item in job.items or []:
            if (item.get("user_id") != user_id
                    or item.get("kind") not in ITEM_KINDS
                    or not item.get("outcome")):
                continue
            history.setdefault(_item_key(item), []).append(
                (item["outcome"], when))
    return history


def _count(history, key, outcomes):
    return sum(1 for outcome, _ in history.get(key, ()) if outcome in outcomes)


def provider_stop_until(history, key, now=None):
    """When a window (or merge) stopped by provider failures may be tried
    again, or None when it is not stopped: after
    PROFILE_VERSION_PROVIDER_ATTEMPTS failed submissions it waits
    UNBILLED_STOP_EXPIRY (24 h) after the newest one, then gets one more
    try. The same rule as recent context's unbilled failures (#380):
    nobody was billed, and an outage must not stop the build for good."""
    limit = int(_setting("PROFILE_VERSION_PROVIDER_ATTEMPTS", 3))
    times = [when for outcome, when in history.get(key, ())
             if outcome in PROVIDER_FAILURES]
    if len(times) < limit:
        return None
    until = max(times) + refusal_backoff.UNBILLED_STOP_EXPIRY
    return until if (now or datetime.utcnow()) < until else None


def _content_attempts():
    return int(_setting("PROFILE_VERSION_CONTENT_ATTEMPTS", 2))


def _report(title, level="error", **tags):
    """A Sentry event with tags only (ids, dates, model, counts), never
    text from a prompt or an output; the pattern of
    refusal_backoff.report_stop."""
    logger.warning("%s %s", title, " ".join(
        f"{k}={v}" for k, v in sorted(tags.items())))
    try:
        import sentry_sdk
        with sentry_sdk.new_scope() as scope:
            for key, value in tags.items():
                scope.set_tag(key, str(value))
            sentry_sdk.capture_message(title, level=level)
    except Exception:  # pragma: no cover — reporting never masks the work
        logger.exception("Sentry report failed")


# ── which set, and what it still needs ────────────────────────────────────

def resolve_set(user):
    """(set_id, base, fresh): the window set this user's build works on.
    In order: the set of items already in flight; a new set from the start
    of the writing when a full rebuild is flagged (the flag is cleared
    when the set's first window is saved, so a set flag means no set of
    this rebuild has rows yet); the set whose tip is the update base; else
    a new set on the update base (a chained profile, a version the user
    wrote, a re-tip), which the merge reads as "Profile No. 1"."""
    items = inflight_items(user.id)
    if items:
        base_id = items[0].get("base_id")
        base = db.session.get(UserProfile, base_id) if base_id else None
        return items[0]["set"], base, False
    if user.profile_needs_full_regen:
        return new_set_id(), None, True
    tip = _exports.profile_update_base(user.id)
    if tip is not None and tip.generation_type in WINDOW_TYPES:
        base = (db.session.get(UserProfile, tip.parent_profile_id)
                if tip.parent_profile_id else None)
        return tip.window_set, base, False
    return new_set_id(), tip, True


@dataclass
class Entry:
    """A window of the set, saved (``row``) or in flight (``item``)."""
    start: datetime
    cutoff: datetime
    cumulative: int
    stats: dict
    rendered_at: datetime
    row: object = None
    item: dict = None


@dataclass
class SetState:
    set_id: str
    base: object
    windows: list
    entries: list
    inflight: list
    due: list = field(default_factory=list)       # (start, end, units)
    stopped: list = field(default_factory=list)   # (start, end, until)
    prefix: int = 0          # saved windows before the first gap or flight

    @property
    def windows_in_flight(self):
        return [i for i in self.inflight if i.get("kind") == "window"]

    @property
    def complete(self):
        return not (self.due or self.stopped or self.windows_in_flight)


def _unread_before(user, cutoff, boundary):
    """The continue rule (exports.should_continue_chain) for a window that
    may still be in flight: AI-readable writing after ``cutoff`` that
    already existed at ``boundary``, when the window was rendered."""
    if cutoff is None or boundary is None:
        return False
    return db.session.query(Node.id).filter(
        or_(Node.user_id == user.id, Node.human_owner_id == user.id),
        Node.ai_usage.in_(AI_ALLOWED),
        Node.deleted_at.is_(None),
        Node.created_at > cutoff,
        Node.created_at < boundary,
    ).first() is not None


def set_state(user, set_id, base, history=None, now=None):
    """What the set holds and what it still needs: its saved windows, the
    windows in flight, and the ranges no window covers yet.

    A range is missing before the first window (when it does not start at
    the base's cutoff), between two windows that do not meet, and after
    the last one when there is writing to read there: writing that existed
    when the last window was rendered (the continue rule, a build cut by
    the render budget or a restart), or the 80k gate's worth of new
    writing (an update), or anything at all on a set with no windows yet.
    The 80k gate does not apply to a provisional set (it covers less than
    one full chunk, ``exports.profile_is_provisional``: a small one-window
    build): the provisional ladder rebuilds it from scratch instead, as it
    does a chained provisional profile. A range with nothing to read in it
    is not missing. A range whose window failed at the provider too often
    waits (``stopped``)."""
    history = attempt_history(user.id, now) if history is None else history
    inflight = [i for i in inflight_items(user.id) if i.get("set") == set_id]
    windows = set_windows(user.id, set_id)
    entries = [Entry(r.covers_start, r.source_data_cutoff,
                     r.source_tokens_used or 0, r.source_origin_stats,
                     r.source_rendered_at, row=r) for r in windows]
    entries += [Entry(_dt(i.get("start")), _dt(i.get("cutoff")),
                      i.get("cumulative") or 0, i.get("origin_stats"),
                      _dt(i.get("rendered_at")), item=i)
                for i in inflight if i.get("kind") == "window"]
    entries.sort(key=lambda e: e.cutoff or datetime.min)
    state = SetState(set_id, base, windows, entries, inflight)

    cursor = base.source_data_cutoff if base is not None else None
    ranges = []
    broken = False
    for e in entries:
        if e.start is not None and (cursor is None or e.start > cursor):
            ranges.append((cursor, e.start))
            broken = True
        if e.row is None:
            broken = True
        if not broken:
            state.prefix += 1
        if cursor is None or (e.cutoff and e.cutoff > cursor):
            cursor = e.cutoff
    if not entries:
        ranges.append((cursor, None))
    else:
        last = entries[-1]
        provisional = (last.cumulative or 0) < CHUNK_TARGET_UNITS
        if (_unread_before(user, last.cutoff, last.rendered_at)
                or (not provisional
                    and _exports.count_remaining_units(user.id, cursor)
                    >= UPDATE_THRESHOLD_UNITS)):
            ranges.append((cursor, None))
    for start, end in ranges:
        units = (_exports.count_remaining_units(
            user.id, start, created_before=_after(end))
            if end is not None else
            _exports.count_remaining_units(user.id, start))
        if units <= 0:
            continue
        until = provider_stop_until(
            history, _attempt_key("window", _iso(start)), now)
        if until is not None:
            state.stopped.append((start, end, until))
        else:
            state.due.append((start, end, units))
    return state


def chained_update_base(user, state, fresh):
    """The profile a regular update of this set builds on, or None when
    the pass is not one (Peter, 2026-11-05: "for 1 window updates on prod
    I'd like to use the chained version"). A regular update: the set
    exists, nothing of it is in flight or stopped, its only missing range
    is the writing after its last window, and that range plans into one
    chunk on the user's chat model, as the chained path plans it. The base
    is the set's newest chained version when the last window is one, else
    the set's final merged over the last window (the only profile that
    covers all the writing). A set still being built (no final over its
    last window) reads its windows independently."""
    if (fresh or len(state.due) != 1 or state.stopped
            or state.windows_in_flight or not state.windows):
        return None
    start, end, units = state.due[0]
    tip = state.windows[-1]
    if end is not None or start != tip.source_data_cutoff:
        return None
    if tip.generation_type == CHAINED_TYPE:
        base = tip
    else:
        from backend.utils.profile_versions import latest_set_final
        base = latest_set_final(user.id, state.set_id)
        if base is None or base.parent_profile_id != tip.id:
            return None
    chat_model = _batch._model_for(user)
    k, _size = plan_chunks(units, max_units=max_units_for_cap(
        _cap(chat_model), _exports.tokens_per_unit(user, chat_model)))
    return base if k == 1 else None


def _fits_one_chat_window(user, units):
    """Whether ``units`` of writing plan into one chunk on the user's chat
    model, as the chained path plans it (and wants_window_build)."""
    chat_model = _batch._model_for(user)
    k, _size = plan_chunks(units, max_units=max_units_for_cap(
        _cap(chat_model), _exports.tokens_per_unit(user, chat_model)))
    return k == 1


def one_window_pass(user, state):
    """Whether this pass belongs to a from-scratch build (no base) that
    fits one window on the user's chat model: all its writing is still to
    read and plans into one chunk on the chat model (the window pass), or
    its one window is saved or in flight, starts at the first writing,
    covers no more than one such chunk, and nothing more is to read (the
    final). The count comes from the chat model, as wants_window_build
    decides the path, so the window is sized for the chat model too. A
    build of more, or one that a blocked range cut into several windows,
    is not one."""
    if state.base is not None:
        return False
    if not state.entries:
        if len(state.due) != 1:
            return False
        start, end, units = state.due[0]
        return (start is None and end is None
                and _fits_one_chat_window(user, units))
    if len(state.entries) != 1 or state.due:
        return False
    entry = state.entries[0]
    return (entry.start is None
            and _fits_one_chat_window(user, entry.cumulative or 0))


def pass_models(user, state, chained_base, cross):
    """The models of this pass (Peter, 2026-11-05): the user's chat model
    for a regular update (``chained_base``) and for the merges after one
    (the set's last window is a chained version); the chat model for both
    steps of a from-scratch build that fits one window on it, unless the
    writing may reach both providers (``one_window_pass``); else the
    version and merge models of ``profile_models_for``: the user's
    provider's pair, or Luna and Opus for public-only writing or a user
    who agreed to both providers, whatever the number of windows."""
    if chained_base is not None or (
            not state.due and state.windows
            and state.windows[-1].generation_type == CHAINED_TYPE):
        return chained_models_for(user)
    if not cross and one_window_pass(user, state):
        return single_models_for(user)
    return profile_models_for(user, cross=cross)


# ── merge planning ────────────────────────────────────────────────────────

@dataclass
class MergePlan:
    groups: list = field(default_factory=list)       # member lists to send
    final_inputs: list = None                        # None: not due
    user_version: object = None
    waiting: bool = False                            # something in flight
    nothing_read: bool = False                       # every input skipped


def _group_size():
    return max(2, int(_setting("PROFILE_MERGE_GROUP_SIZE", 30)))


def _structure():
    return _setting("PROFILE_MERGE_STRUCTURE", "flat")


def _set_started_at(windows):
    times = [w.source_rendered_at or w.created_at for w in windows
             if (w.source_rendered_at or w.created_at)]
    return min(times) if times else None


def user_version_for(user, set_id, windows):
    """The version of the user's that the set's final merges, at its date:
    the newest AI-readable version the user wrote (or reverted to) after
    the set started, else the version a rebuild still has to merge
    (User.profile_pending_merge_id, #183), else the one the set's last
    final merged. None when there is none."""
    started = _set_started_at(windows)
    if started is not None:
        newest = UserProfile.query.filter(
            UserProfile.user_id == user.id,
            UserProfile.ai_usage.in_(AI_ALLOWED),
            UserProfile.created_at > started,
            or_(UserProfile.generated_by == "user",
                UserProfile.generation_type == "revert"),
        ).order_by(UserProfile.created_at.desc()).first()
        if newest is not None:
            return newest
    pending = _exports.pending_merge_version(user)
    if pending is not None:
        return pending
    from backend.utils.profile_versions import latest_set_final
    final = latest_set_final(user.id, set_id)
    if final is not None and final.merged_profile_id:
        merged = db.session.get(UserProfile, final.merged_profile_id)
        if (merged is not None and merged.user_id == user.id
                and merged.ai_usage in AI_ALLOWED):
            return merged
    return None


def _group_matches(group, members):
    return (group.covers_start == members[0].covers_start
            and group.source_data_cutoff == members[-1].source_data_cutoff
            and all(group.created_at > m.created_at for m in members))


def set_superseded(user, set_id, final=None):
    """Whether the user saved a version after the set's newest final
    (``final``, looked up when not given). The set is then no longer
    extended: the next update builds on the user's version ("the user's
    edit wins"), so nothing more of the set is sent, and a window of it
    collected afterwards is not saved (its cost row is)."""
    if final is None:
        from backend.utils.profile_versions import latest_set_final
        final = latest_set_final(user.id, set_id)
    return final is not None and _user_version_after(
        user, final.created_at) is not None


def _with_user_version(inputs, version, base):
    """``inputs`` (by cutoff) with the user's version placed at its date
    (exports.merge_date): after every input whose cutoff is not later."""
    if version is None or (base is not None and version.id == base.id):
        return list(inputs)
    key = _exports.merge_date(version)
    at = sum(1 for p in inputs
             if p.source_data_cutoff is not None and key is not None
             and p.source_data_cutoff <= key)
    return list(inputs[:at]) + [version] + list(inputs[at:])


def merge_plan(user, state, grouped=None):
    """The merges due now. Groups are fixed slices of the windows by time
    (1-30, 31-60, ...): a full slice that ends inside the leading run of
    saved windows (before the first gap or window in flight) is final, so
    it can go out while a later window is being retried; the slices from
    the gap on wait until every window is in. A saved group
    is reused while it starts and ends where its slice does and is newer
    than every member; a slice of one window is not merged. The final is
    due when every window is in, every group is current, nothing of the
    set is in flight, and the set has no final yet over these same inputs
    and the same version of the user's. A final whose inputs are all
    skipped windows (a one-window build whose window was given up) is not
    sent: there is nothing to merge, and a merge of the skip note alone
    would be saved as the user's profile (``nothing_read``)."""
    plan = MergePlan()
    from backend.utils.profile_versions import latest_set_final
    final = latest_set_final(user.id, state.set_id)
    if set_superseded(user, state.set_id, final):
        return plan
    grouped = (_structure() == "grouped") if grouped is None else grouped
    size = _group_size()
    windows = state.windows
    inflight_merges = [i for i in state.inflight
                       if i.get("kind") in ("group", "final")]
    if inflight_merges:
        plan.waiting = True
    saved_groups = _set_rows(user.id, state.set_id, ("group",))
    inflight_groups = {(i.get("start"), i.get("cutoff"))
                       for i in inflight_merges if i.get("kind") == "group"}
    complete = state.complete
    slices = []
    if grouped and (len(windows) > size or not complete):
        # While a window is missing or in flight, only full slices that end
        # inside the leading run of saved windows go out: a slice reaching
        # past the gap would take the windows after it in the gap's place,
        # and once the gap is filled it would match no slice (billed, never
        # used).
        starts = (range(0, len(windows), size) if complete
                  else range(0, state.prefix - size + 1, size))
        slices = [windows[i:i + size] for i in starts]
    mid = []
    all_current = True
    for members in slices:
        if len(members) == 1:
            mid.append(members[0])
            continue
        current = [g for g in saved_groups if _group_matches(g, members)]
        if current:
            mid.append(current[-1])
            continue
        all_current = False
        key = (_iso(members[0].covers_start),
               _iso(members[-1].source_data_cutoff))
        if key in inflight_groups:
            continue
        plan.groups.append(members)
    if not complete or plan.waiting or not all_current:
        return plan
    if not slices:
        mid = list(windows)
    if not mid:
        return plan
    base = state.base if (state.base is not None
                          and state.base.ai_usage in AI_ALLOWED) else None
    version = user_version_for(user, state.set_id, windows)
    inputs = ([base] if base is not None else []) + mid
    inputs = _with_user_version(inputs, version, base)
    if all(p.generation_type == "window_skip" for p in inputs):
        plan.nothing_read = True
        return plan
    tip = windows[-1]
    if (final is not None and final.parent_profile_id == tip.id
            and final.merged_profile_id == (version.id if version else None)
            and all(final.created_at > p.created_at for p in inputs)):
        return plan
    plan.final_inputs = inputs
    plan.user_version = version
    return plan


def single_version(inputs):
    """Whether a merge's inputs are one window version and nothing else
    (no base, no version of the user's): a one-window set's final."""
    return len(inputs) == 1 and inputs[0].generation_type == "window"


def _merge_messages(user, inputs):
    if single_version(inputs):
        return _exports.single_integration_messages(user.id, inputs[0])
    return _exports._integration_messages(user.id, inputs, independent=True)


def merge_over_cap(model_id, messages, keys):
    """Whether a merge request exceeds the merge model's input cap
    (never across its price tier, #259). Returns (over, tokens)."""
    cap = _cap(model_id)
    text = "\n\n".join(m["content"][0]["text"] for m in messages)
    estimate = approximate_token_count(text)
    if estimate > cap * OVER_CAP_ESTIMATE_FACTOR:
        return True, estimate
    run_locks.beat()
    counted = LLMProvider.count_tokens(model_id, messages, keys)
    tokens = counted if isinstance(counted, int) else estimate
    return tokens > cap, tokens


def set_work_due(user, tip):
    """Whether the set whose tip is ``tip`` has work to do now, apart from
    the 80k gate's update: a missing range, a group or the final. The
    seeder's gate (profile_batch._should_seed) asks this before the
    interval check. Reads metadata only."""
    if not enabled() or tip is None or tip.generation_type not in WINDOW_TYPES:
        return False
    if inflight_items(user.id) or _policy_blocked(user):
        return False
    base = (db.session.get(UserProfile, tip.parent_profile_id)
            if tip.parent_profile_id else None)
    state = set_state(user, tip.window_set, base)
    if state.due:
        return True
    if merge_stopped(user):
        return False
    plan = merge_plan(user, state)
    return bool(plan.groups or plan.final_inputs)


def wants_window_build(user):
    """Whether this user's next build is a window set: always for a set in
    flight or a base that is a set's tip (every later update of such a
    profile adds windows and re-merges), else when the remainder after the
    update base (all the writing, for a full rebuild) plans into two or
    more chunks on the user's chat model, as the chained pipeline plans
    it. A from-scratch build (no update base) that fits one chunk is a
    window set too where its provider's switch is on
    (``one_chunk_windows``); else it takes the chained path, as does a
    chained update that fits one chunk, unchanged. A remainder that meets
    a blocked range always takes the window path, the only one that cuts
    around it."""
    if not enabled() or not account_allows_ai(user):
        return False
    if inflight_items(user.id):
        return True
    prev = (None if user.profile_needs_full_regen
            else _exports.profile_update_base(user.id))
    if prev is not None and prev.generation_type in WINDOW_TYPES:
        return True
    if meets_blocked(user.id, prev.source_data_cutoff
                     if prev is not None else None):
        # Only the window path cuts around a blocked range; a chained
        # chunk would hold it whole.
        return True
    chat_model = _batch._model_for(user)
    cap = _cap(chat_model)
    rho = _exports.tokens_per_unit(user, chat_model)
    remaining = _exports.count_remaining_units(
        user.id, prev.source_data_cutoff if prev is not None else None)
    k, _size = plan_chunks(remaining, max_units=max_units_for_cap(cap, rho))
    if k >= 2:
        return True
    return k == 1 and prev is None and one_chunk_windows(user)


# ── the spend estimate ────────────────────────────────────────────────────

def estimate_cost_usd(user, state, models, plan_windows, plan=None):
    """Dollars the rest of the build costs at Batch prices: the windows
    still to read (``plan_windows``: (count, units)) and the merges after
    them, or, with no window left to read, the merges due now (``plan``).
    The expected cost, from the measured typical sizes above, not a worst
    case: Peter, 2026-11-05, "Better err on the job going through leading
    to $2-$3 overshoot, than postponing it for a month". Not a bill."""
    from backend.utils.cost import calculate_llm_cost_microdollars
    n_new, units_new = plan_windows
    rho = _exports.tokens_per_unit(user, models.version)
    cost = 0
    if n_new:
        # Priced per (average) window: a price tier applies per request.
        overhead = (CHAINED_PROMPT_OVERHEAD_TOKENS if models.chained
                    else WINDOW_PROMPT_OVERHEAD_TOKENS)
        cost += n_new * calculate_llm_cost_microdollars(
            models.version, int(units_new * rho / n_new + overhead),
            EXPECTED_OUTPUT.get(models.version, DEFAULT_EXPECTED_OUTPUT),
            batch=True)
    per_version = MERGE_TOKENS_PER_VERSION.get(
        models.version, DEFAULT_MERGE_TOKENS_PER_VERSION)
    size = _group_size()
    calls = []   # versions per merge call
    if n_new or state.windows_in_flight:
        n_after = len(state.windows) + len(state.windows_in_flight) + n_new
        # As the merge goes out: flat while one call fits the merge
        # model's cap, else in groups (_merge_requests).
        flat_tokens = (n_after + 2) * per_version + MERGE_PROMPT_TOKENS
        if n_after > size and (_structure() == "grouped"
                               or flat_tokens > _cap(models.merge)):
            groups = math.ceil(n_after / size)
            current = len(_set_rows(user.id, state.set_id, ("group",)))
            to_build = max(1, groups - max(0, min(current, groups - 1)))
            # The newest groups are built; only the last can be short.
            calls += [min(size, n_after - size * i)
                      for i in range(groups - to_build, groups)]
            calls.append(groups + 2)
        else:
            calls.append(n_after + 2)
    elif plan is not None:
        calls += [len(members) for members in plan.groups]
        if plan.final_inputs is not None:
            calls.append(len(plan.final_inputs))
    for versions in calls:
        cost += calculate_llm_cost_microdollars(
            models.merge, versions * per_version + MERGE_PROMPT_TOKENS,
            MERGE_OUTPUT_TOKENS, batch=True)
    return cost / 1e6


def _hold(user, needed=None, spent=None, limit=None):
    """Hold the set's next submission: the user is over their monthly
    limit (the spend cap), or, with ``needed``, the windows due and the
    merges after them are estimated at more than what is left of the limit.
    Nothing is submitted; the hourly seeder resumes the set once there is
    room (profile_cap_held: the month turns or the admin raises the limit).
    What is in flight is collected and saved meanwhile. A wait on the
    estimate says so in the admin Profile column, with the amount."""
    _batch._hold_for_cap(user)
    if needed is None:
        logger.warning("User %s: spend-capped; no profile window step",
                       user.id)
        return "held"
    note = (f"{HOLD_NOTE}: the build needs about ${needed:.2f}; "
            f"${spent:.2f} of ${limit:.2f} used this month")
    first = not (user.profile_seed_error or "").startswith(HOLD_NOTE)
    user.profile_seed_error = note[:255]
    db.session.commit()
    (logger.error if first else logger.info)(
        "User %s: profile build waits: needs about $%.2f, $%.2f of $%.2f "
        "used this month", user.id, needed, spent, limit)
    return "held"


def _short_of_headroom(user, estimate):
    """(spent, limit) in dollars when ``estimate`` does not fit into what
    is left of the user's monthly spend limit this month, else None. A user
    with no limit (0) is never short."""
    from backend.utils.spend import (
        get_user_month_spend_microdollars, get_user_spend_limit_usd)
    limit = get_user_spend_limit_usd(user, current_app.config) or 0
    if limit <= 0:
        return None
    spent = get_user_month_spend_microdollars(user.id) / 1e6
    if estimate > limit - spent:
        return spent, limit
    return None


# ── requests ──────────────────────────────────────────────────────────────

def _request(models, model_id, custom_id, messages, merge=False):
    """A batch request. An OpenAI merge goes to the Responses endpoint,
    as the live call the merge experiments ran through, with the
    configured reasoning effort; a window stays on chat/completions, as
    the Luna windows were tested."""
    provider, api_model = _batch._provider_and_model(model_id)
    req = {"custom_id": custom_id, "model_id": model_id,
           "api_model": api_model, "messages": messages,
           "max_tokens": DEFAULT_MAX_OUTPUT_TOKENS}
    if merge and provider == "openai":
        from backend.utils.llm_batch import RESPONSES_ENDPOINT
        req["endpoint"] = RESPONSES_ENDPOINT
        if models.merge_effort:
            req["reasoning_effort"] = models.merge_effort
    return req


def _request_bytes(req):
    return len(json.dumps(req, ensure_ascii=False).encode("utf-8"))


def _cumulative_before(state, start):
    """(cumulative units, origin stats) of what the set covers up to
    ``start``: the window ending there, else the base."""
    for e in reversed(state.entries):
        if e.cutoff is not None and start is not None and e.cutoff <= start:
            return e.cumulative, e.stats
    if state.base is not None:
        return (state.base.source_tokens_used or 0,
                state.base.source_origin_stats)
    return 0, None


def _save_skip(user, meta):
    """A "window_skip" row for a window given up after its outputs failed:
    its dates, coverage and the WINDOW_SKIPPED_TEXT the merge sees. No
    model call, no cost row (the failed calls wrote theirs)."""
    from backend.utils.privacy import PrivacyLevel
    start, cutoff = _dt(meta.get("start")), _dt(meta.get("cutoff"))
    row = UserProfile(
        user_id=user.id, generated_by=meta["model_id"], tokens_used=0,
        privacy_level=PrivacyLevel.PRIVATE, ai_usage=user.default_ai_usage,
        source_tokens_used=meta.get("cumulative"), source_data_cutoff=cutoff,
        source_origin_stats=meta.get("origin_stats"),
        source_rendered_at=_dt(meta.get("rendered_at")),
        generation_type="window_skip", parent_profile_id=meta.get("base_id"),
        window_set=meta["set"], covers_start=start)
    row.set_content(format_date_metadata(
        covers_start=start or _dt(meta.get("earliest")), covers_end=cutoff,
        tokens=meta.get("units")) + _exports.WINDOW_SKIPPED_TEXT)
    db.session.add(row)
    if meta.get("base_id") is None and user.profile_needs_full_regen:
        user.profile_needs_full_regen = False
    db.session.commit()
    return row


class _Submission:
    """Requests collected for one user's submission, sent in slices of
    PROFILE_BATCH_MAX_BYTES, every slice in the same run."""

    def __init__(self, keys, providers, recheck=None, own=None):
        self.keys = keys
        # The providers this user's writing may reach in this pass.
        self.providers = set(providers)
        # Returns the providers allowed now; called before each slice is
        # sent and before anything (a request or a token count) goes to a
        # provider other than ``own``, the provider of the user's preferred
        # model, since rendering a pass can take many minutes (#599).
        self.recheck = recheck
        self.own = own
        self.built = []
        self.size = 0
        self.sent = 0
        self.skipped = 0     # windows given up, saved as window_skip rows
        self.failed = False
        # The user withdrew a yes to both providers (or their writing
        # stopped being public only) while the pass rendered: nothing more
        # goes out in this pass, and the next one plans again.
        self.withdrawn = False
        self.max_bytes = int(_setting("PROFILE_BATCH_MAX_BYTES",
                                      DEFAULT_BATCH_MAX_BYTES))

    @property
    def stopped(self):
        return self.failed or self.withdrawn

    def may_reach(self, provider, user_id=None):
        """Whether anything of this pass, a request or a token count, may
        go to ``provider`` now. A provider outside the pass's providers
        raises (the hard line). A provider other than the user's own is
        checked again first: after a no, the pass is withdrawn and nothing
        more goes out, not even a token count (#599)."""
        if self.stopped:
            return False
        if provider not in self.providers:
            raise ProfileModelError(
                f"user {user_id}: a profile request to {provider} is not "
                f"allowed for this user's writing")
        if self.recheck is None or provider == self.own:
            return True
        allowed = set(self.recheck())
        if provider in allowed:
            return True
        self._withdraw(allowed, user_id)
        return False

    def _withdraw(self, allowed, user_id):
        # A no given while this pass ran: what was built is dropped unsent
        # (nothing is in flight for it), and the next pass plans it again
        # for the user's own provider.
        logger.info("User %s: profile requests to %s withdrawn before "
                    "sending; the next pass plans again", user_id,
                    sorted(self.providers - allowed))
        self.providers &= allowed
        self.withdrawn = True
        self.built, self.size = [], 0

    def add(self, provider, request, meta):
        if self.stopped:
            return          # nothing more goes out in this pass
        if provider not in self.providers:
            # The hard line (Peter, 2026-10-02 and 2026-11-05): never sent.
            raise ProfileModelError(
                f"user {meta.get('user_id')}: a profile request to "
                f"{provider} is not allowed for this user's writing")
        self.built.append({"provider": provider, "request": request,
                           "meta": meta})
        self.size += _request_bytes(request)
        if self.size >= self.max_bytes:
            self.flush()

    def flush(self):
        if not self.built or self.stopped:
            self.built, self.size = [], 0
            return
        if self.recheck is not None and any(
                b["provider"] != self.own for b in self.built):
            allowed = set(self.recheck())
            if any(b["provider"] not in allowed for b in self.built):
                self._withdraw(allowed, self.built[0]["meta"].get("user_id"))
                return
        # The run is alive and still holds its user's lock: a run that
        # lost it while frozen (RunLockLost) sends nothing more.
        run_locks.beat()
        n = _batch._submit_requests(self.built, self.keys)
        if n < len(self.built):
            self.failed = True
        self.sent += n
        self.built, self.size = [], 0


def fit_window(user, model_id, keys, cap, cur, budget, end, prompt_fn,
               rho):
    """Render the window after ``cur`` at ``budget`` units and count its
    prompt; while it is over ``cap`` tokens, render it again smaller by
    the counted overshoot. A window is never cut and never sent over the
    cap: the smaller render ends at the last entry it holds, and the next
    window starts there. Returns (chunk, prompt, tokens, fits), or None
    when nothing renders after ``cur``. ``fits`` is False only for a
    window that cannot shrink below the cap (one entry larger than it).
    When counting is unavailable, the tokens are estimated from the
    planner's ratio."""
    last = None
    for _ in range(FIT_ROUNDS):
        # The run is alive (run_locks): before each render, which can take
        # long on a large corpus.
        run_locks.beat()
        chunk = _exports.render_profile_window(user, budget, cur, until=end)
        if not chunk or not chunk.get("content"):
            return last
        prompt = prompt_fn(chunk)
        counted = LLMProvider.count_tokens(model_id, [{
            "role": "user", "content": [{"type": "text", "text": prompt}]}],
            keys)
        units = chunk.get("unit_count") or 0
        tokens = (counted if isinstance(counted, int)
                  else int(units * rho) + WINDOW_PROMPT_OVERHEAD_TOKENS)
        if tokens <= cap:
            return chunk, prompt, tokens, True
        last = (chunk, prompt, tokens, False)
        smaller = int(units * cap / tokens * FIT_SAFETY)
        if units <= 1 or smaller >= units or (chunk.get("node_count") or 2) <= 1:
            break
        logger.info("User %s: window after %s counted %s > %s tokens; "
                    "rendering it at %s units", user.id, cur, tokens, cap,
                    smaller)
        budget = max(smaller, 1)
    return last


def _render_windows(user, state, models, sub, history, deadline,
                    chained_base=None):
    """Plan and render every due range into window requests, oldest
    first, exactly as the chain loop sizes its chunks, but at the version
    model's cap. A due range is first cut around the user's blocked
    ranges (``cut_around_blocked``): a window ends where a blocked range
    starts, the next begins where it ends, and the blocked range itself
    gets a window_skip row without a request. Returns "partial" when the
    render budget ran out (the rendered windows are submitted and a new
    task renders the rest), else None.

    With ``chained_base`` (a regular update, chained_update_base) the one
    window is a chained update of that profile: the update prompt over the
    profile and the window, as main's chained path builds it, by the
    user's chat model (``models``). One window per pass: a remainder that
    did not fit is chained on this version in the next pass."""
    template = _exports.window_generation_template(user.id)
    if chained_base is not None:
        update_template = _exports.build_update_template(user.id)
        base_content = _exports.update_base_content(chained_base)
    cap = _cap(models.version)
    rho = _exports.tokens_per_unit(user, models.version)
    max_units = max_units_for_cap(cap, rho)
    keys = get_api_keys_for_usage(current_app.config, "chat")
    provider, _api = _batch._provider_and_model(models.version)
    planned = sum(plan_chunks(units, max_units=max_units)[0]
                  for _s, _e, units in state.due)
    total = len(state.windows) + len(state.windows_in_flight) + planned
    blocked = blocked_ranges(user.id)
    n = 0
    for rng in list(state.due):
        start, end, _units = rng
        cumulative, stats = _cumulative_before(state, start)
        rendered = False
        for is_blocked, piece_start, piece_end in cut_around_blocked(
                start, end, blocked):
            if is_blocked:
                units = _units_in(user, piece_start, piece_end)
                if units <= 0:
                    continue
                cumulative += units
                _save_skip(user, {
                    "user_id": user.id, "kind": "window",
                    "set": state.set_id,
                    "base_id": (state.base.id if state.base is not None
                                else None),
                    "start": _iso(piece_start), "cutoff": _iso(piece_end),
                    "earliest": _iso(
                        piece_start or _exports._first_writing_date(
                            user.id, piece_end)),
                    "units": units, "cumulative": cumulative,
                    "origin_stats": stats,
                    "rendered_at": _iso(datetime.utcnow()),
                    "model_id": models.version})
                sub.skipped += 1
                rendered = True
                logger.info("User %s: profile set %s: blocked range %s..%s "
                            "skipped without a request", user.id,
                            state.set_id, _iso(piece_start) or "start",
                            _iso(piece_end))
                continue
            cur, end_ = piece_start, piece_end
            while True:
                remaining = _units_in(user, cur, end_)
                if remaining <= 0:
                    break
                _k, _size, budget = next_window_budget(
                    remaining, max_units=max_units)
                rendered_at = datetime.utcnow()

                def _prompt(chunk):
                    if chained_base is not None:
                        return _exports.build_chunk_prompt(
                            update_template, base_content,
                            chained_base.source_tokens_used or 0, chunk,
                            chained_base.source_origin_stats)
                    return template.replace(
                        "{user_export}",
                        _exports.chunk_content_for_prompt(chunk))

                if not sub.may_reach(provider, user.id):
                    # A no given while the pass rendered (#599): not even
                    # the window's token count goes to the other provider.
                    return None
                fitted = fit_window(user, models.version, keys, cap, cur,
                                    budget, end_, _prompt, rho)
                if fitted is None:
                    break
                rendered = True
                chunk, prompt, tokens, fits = fitted
                cutoff = chunk["latest_node_created_at"]
                units = chunk["unit_count"]
                cumulative += units
                stats = _exports.merge_origin_stats(
                    stats, chunk.get("origin_stats"))
                key = _attempt_key("window", _iso(cur))
                attempts = len(history.get(key, ()))
                n += 1
                meta = {
                    "user_id": user.id, "kind": "window",
                    "set": state.set_id,
                    "base_id": (state.base.id if state.base is not None
                                else None),
                    "n": n,
                    "total": None if chained_base is not None else total,
                    "attempt": attempts + 1,
                    "start": _iso(cur), "cutoff": _iso(cutoff),
                    "earliest": _iso(chunk.get("earliest_node_created_at")),
                    "units": units, "cumulative": cumulative,
                    "origin_stats": stats, "rendered_at": _iso(rendered_at),
                    "model_id": models.version,
                    "generation_type": ("window" if chained_base is None
                                        else CHAINED_TYPE),
                }
                if chained_base is not None:
                    meta["chained_base_id"] = chained_base.id
                if not fits:
                    # One entry larger than the version model's cap: never
                    # cut, never sent across the cap's price tier. Given
                    # up, loudly.
                    _save_skip(user, meta)
                    sub.skipped += 1
                    _report("Profile window over the version model's cap",
                            user_id=user.id, set=state.set_id,
                            start=_iso(cur), end=_iso(cutoff),
                            model=models.version, tokens=tokens, cap=cap,
                            reason="over cap")
                elif (_count(history, key, CONTENT_FAILURES)
                        >= _content_attempts()):
                    # The same window failed twice before, in this set or
                    # an earlier one: give it up without another call.
                    _save_skip(user, meta)
                    sub.skipped += 1
                    _report("Profile window skipped after repeated failures",
                            user_id=user.id, set=state.set_id,
                            start=_iso(cur), end=_iso(cutoff),
                            model=models.version, reason="earlier failures")
                elif provider_stop_until(history, key) is not None:
                    pass   # waits; the range stays missing
                else:
                    tag = "w" if chained_base is None else "u"
                    cid = (f"profile_{user.id}_{state.set_id}_{tag}{n}_"
                           f"{attempts + 1}")
                    meta["custom_id"] = cid
                    sub.add(provider, _request(
                        models, models.version, cid,
                        [{"role": "user", "content": [
                            {"type": "text", "text": prompt}]}]), meta)
                    if sub.stopped:
                        # A slice was refused at submission, or withdrawn
                        # before it: nothing rendered after it would go
                        # out in this run.
                        return None
                if chained_base is not None:
                    # One chained window per pass: a remainder that did
                    # not fit is chained on this version in the next pass.
                    return None
                cur = cutoff
                if end_ is not None and cutoff is not None and cutoff >= end_:
                    break
                if time.monotonic() > deadline:
                    logger.info("User %s: render budget used after %d "
                                "windows; the rest goes to a new task",
                                user.id, n)
                    return "partial"
        if not rendered:
            # Units are counted in the range but nothing in it renders
            # (for example threads holding only a system prompt, or
            # tombstones): it is covered, so the set can complete and its
            # final go out.
            logger.info("User %s: profile set %s: the range %s..%s counts "
                        "units but renders nothing; counted as covered",
                        user.id, state.set_id, _iso(start) or "start",
                        _iso(end) or "end")
            state.due.remove(rng)
    return None


def _units_in(user, start, end):
    """Stored units of the writing after ``start`` up to ``end``
    inclusive (None: to the newest)."""
    if end is not None:
        return _exports.count_remaining_units(
            user.id, start, created_before=_after(end))
    return _exports.count_remaining_units(user.id, start)


def _merge_config_tag():
    return f"[{_structure()}/{_group_size()}]"


def merge_stopped(user):
    """Whether a merge of this user's set stopped over the merge model's
    cap under the current merge settings. Nothing retries it (hourly or
    otherwise) until the settings change or a new version is saved (any
    saved version clears profile_seed_error)."""
    note = user.profile_seed_error or ""
    return note.startswith(MERGE_STOP_NOTE) and _merge_config_tag() in note


def _stop_merge(user, kind, set_id, tokens, model_id):
    user.profile_seed_error = (
        f"{MERGE_STOP_NOTE} {_merge_config_tag()}: the {kind} merge is "
        f"{tokens:,} tokens for {model_id}; change PROFILE_MERGE_STRUCTURE "
        f"or PROFILE_MERGE_GROUP_SIZE")[:255]
    db.session.commit()
    _report("Profile merge over the merge model's cap", user_id=user.id,
            set=set_id, kind=kind, tokens=tokens, model=model_id,
            structure=_structure(), group_size=_group_size())


def _merge_requests(user, state, models, sub, keys, history):
    """Queue the due groups and the final. Returns True when a merge
    stopped (over the cap). Each merge's token count and request is
    preceded by ``sub.may_reach``: after a no given while the pass ran,
    nothing more goes to the other provider, and the merges stop."""
    if refusal_backoff.profile_in_backoff(user.id) or merge_stopped(user):
        return False
    provider, _api = _batch._provider_and_model(models.merge)
    plan = merge_plan(user, state)
    if plan.final_inputs is not None and _structure() == "flat":
        if not sub.may_reach(provider, user.id):
            return False
        messages = _merge_messages(user, plan.final_inputs)
        over, tokens = merge_over_cap(models.merge, messages, keys)
        if over:
            logger.info("User %s: a flat merge of %d versions is %s tokens, "
                        "over %s's cap; merging in groups", user.id,
                        len(plan.final_inputs), tokens, models.merge)
            plan = merge_plan(user, state, grouped=True)
    base_id = state.base.id if state.base is not None else None
    windows = state.windows
    own = {}
    prev_cum = (state.base.source_tokens_used or 0) if state.base else 0
    for w in windows:
        own[w.id] = max(0, (w.source_tokens_used or 0) - prev_cum)
        prev_cum = w.source_tokens_used or 0
    for i, members in enumerate(plan.groups, 1):
        key = _attempt_key("group", _iso(members[0].covers_start),
                           _iso(members[-1].source_data_cutoff))
        if provider_stop_until(history, key) is not None:
            continue
        if not sub.may_reach(provider, user.id):
            return False
        messages = _merge_messages(user, members)
        over, tokens = merge_over_cap(models.merge, messages, keys)
        if over:
            _stop_merge(user, "group", state.set_id, tokens, models.merge)
            return True
        last = members[-1]
        start_day = members[0].covers_start or _exports._first_writing_date(
            user.id, last.source_data_cutoff)
        attempt = len(history.get(key, ())) + 1
        cid = f"profile_{user.id}_{state.set_id}_g{i}_{attempt}"
        sub.add(provider, _request(models, models.merge, cid, messages,
                                   merge=True), {
            "custom_id": cid, "user_id": user.id, "kind": "group",
            "set": state.set_id, "base_id": base_id,
            "members": [m.id for m in members], "attempt": attempt,
            "start": _iso(members[0].covers_start),
            "start_day": _iso(start_day),
            "cutoff": _iso(last.source_data_cutoff),
            "units": sum(own.get(m.id, 0) for m in members),
            "cumulative": last.source_tokens_used,
            "origin_stats": last.source_origin_stats,
            "parent_id": last.id, "model_id": models.merge,
        })
    if plan.final_inputs is None:
        return False
    tip = windows[-1]
    key = _attempt_key("final", None, _iso(tip.source_data_cutoff))
    if provider_stop_until(history, key) is not None:
        return False
    if not sub.may_reach(provider, user.id):
        return False
    messages = _merge_messages(user, plan.final_inputs)
    over, tokens = merge_over_cap(models.merge, messages, keys)
    if over:
        _stop_merge(user, "final", state.set_id, tokens, models.merge)
        return True
    attempt = len(history.get(key, ())) + 1
    cid = f"profile_{user.id}_{state.set_id}_f_{attempt}"
    sub.add(provider, _request(models, models.merge, cid, messages,
                               merge=True), {
        "custom_id": cid, "user_id": user.id, "kind": "final",
        "set": state.set_id, "base_id": base_id, "attempt": attempt,
        "start": None, "cutoff": _iso(tip.source_data_cutoff),
        "tip_id": tip.id, "inputs": [p.id for p in plan.final_inputs],
        "user_version_id": (plan.user_version.id
                            if plan.user_version is not None else None),
        "built_at": _iso(datetime.utcnow()),
        "cumulative": tip.source_tokens_used,
        "origin_stats": tip.source_origin_stats, "model_id": models.merge,
    })
    return False


def next_work(user, keys, deadline, wait_for_imports=True):
    """One pass: everything of the user's set that is due and whose inputs
    are ready, in one submission: windows for every missing range, the
    groups whose windows are all in, and the final when everything is.
    Returns "submitted", "partial" (render budget used; the caller hands
    the rest to a new task), "idle", "held", "waiting", "blocked" (a
    content-policy block of a group or final stops the account's build) or
    "failed"."""
    run_locks.beat()
    code = _policy_blocked(user)
    if code:
        # A blocked group or final (a blocked window is skipped instead):
        # nothing goes out for this account, not the blocked merge and not
        # the rest of its set, until the admin's confirmed Build profile
        # lifts the stop (Peter, 2026-11-04).
        _show_policy_stop(user, code)
        return POLICY_BLOCKED
    if wait_for_imports and _batch.import_settling(user):
        # The user is importing (#598): nothing goes out until the imports
        # stop, so the next import is read by the same windows and merges.
        # The import's deferred seed or the hourly seeder resumes the set.
        return "waiting"
    set_id, base, fresh = resolve_set(user)
    if fresh and not wants_window_build(user):
        # A set is started only for a build that plans into two or more
        # chunks; anything smaller is the chained path's (the seeder).
        return "idle"
    if not fresh and set_superseded(user, set_id):
        # A set still in flight after the user saved a version on its
        # final: nothing more goes out for it. Once its items are in (and
        # discarded), the next pass builds on the user's version.
        return "idle"
    history = attempt_history(user.id)
    if fresh and base is None:
        # A rebuild from the start merges the user's version the current
        # profile carries, at its date (#183).
        if _exports.plan_user_version_merge(
                user, _visible_profile(user.id)) is not None:
            db.session.commit()
    state = set_state(user, set_id, base, history)
    # Again after the in-flight items are read: the poller may have
    # collected a blocked item since the check at the top. It commits the
    # block's row before the job stops being pending, so a blocked item
    # that set_state no longer sees in flight is seen here, and its range
    # or final is not rendered again (review of #596).
    code = _policy_blocked(user)
    if code:
        _show_policy_stop(user, code)
        return POLICY_BLOCKED
    plan = (MergePlan() if merge_stopped(user)
            else merge_plan(user, state))
    if not (state.due or state.stopped or plan.groups
            or plan.final_inputs is not None):
        if plan.nothing_read and not (user.profile_seed_error or "").startswith(
                NOTHING_READ_NOTE):
            # Shown in the admin Profile column; the skip itself was
            # reported to Sentry. The next saved version clears it.
            user.profile_seed_error = NOTHING_READ_NOTE
            db.session.commit()
        return "idle"
    # The models of this pass and the providers the writing may reach
    # (Peter, 2026-11-05).
    cross = cross_provider_allowed(user)
    # A pass over writing that is not public-only marks the user for good.
    if not user.profile_private_read:
        note_private_read(user)
        if user.profile_private_read:
            db.session.commit()
    chained_base = chained_update_base(user, state, fresh)
    models = pass_models(user, state, chained_base, cross)
    providers = ({"anthropic", "openai"} if cross
                 else {models.provider})
    # A pass that may cross providers checks again before each slice is
    # sent: a no given while it renders counts at once (#599).
    recheck = ((lambda: allowed_providers(user, models)) if cross
               else None)
    if state.stopped:
        start, end, until = state.stopped[0]
        note = (f"{STOP_NOTE} {_count(history, _attempt_key('window', _iso(start)), PROVIDER_FAILURES)} "
                f"times ({_iso(start) or 'start'}..{_iso(end) or 'end'}); "
                f"next try after {until:%Y-%m-%d %H:%M} UTC")[:255]
        if user.profile_seed_error != note:
            user.profile_seed_error = note
            db.session.commit()
    if _batch._spend_capped(user):
        return _hold(user)
    cap_v = _cap(models.version)
    max_units = max_units_for_cap(
        cap_v, _exports.tokens_per_unit(user, models.version))
    plan_windows = (sum(plan_chunks(u, max_units=max_units)[0]
                        for _s, _e, u in state.due),
                    sum(u for _s, _e, u in state.due))
    estimate = estimate_cost_usd(user, state, models, plan_windows, plan)
    if state.due:
        # Windows go out only when the windows and the merges after them
        # are expected to fit into what is left of the user's monthly limit
        # (Peter, 2026-11-05); else the build waits until the month turns
        # or the limit is raised. Merges alone are held by the spend cap
        # above (Peter, 2026-10-22: "let the cap stop it halfway").
        short = _short_of_headroom(user, estimate)
        if short is not None:
            return _hold(user, estimate, *short)
        if (user.profile_seed_error or "").startswith(HOLD_NOTE):
            user.profile_seed_error = None
            db.session.commit()
    logger.info("User %s: profile set %s: %d windows saved, %d in flight, "
                "%d ranges to read (about %d windows), estimate $%.2f",
                user.id, set_id, len(state.windows),
                len(state.windows_in_flight), len(state.due),
                plan_windows[0], estimate)
    sub = _Submission(keys, providers, recheck, own=models.provider)
    outcome = None
    if state.due:
        outcome = _render_windows(user, state, models, sub, history,
                                  deadline, chained_base)
    if outcome != "partial" and not sub.stopped:
        _merge_requests(user, state, models, sub, keys, history)
    sub.flush()
    if sub.failed:
        return "failed"
    if outcome == "partial" or sub.withdrawn:
        # Withdrawn: a new task plans the rest with the providers allowed
        # now (the user's own, after a no).
        return "partial"
    # A pass that only gave windows up still changed the set: the caller
    # runs another pass, which may find the final due.
    return "submitted" if (sub.sent or sub.skipped) else "idle"


def _policy_blocked(user):
    """The code of the content-policy block that stops this account's
    profile build, or None (refusal_backoff.policy_block_for)."""
    return refusal_backoff.policy_block_for(
        user.id, refusal_backoff.PROFILE_REQUEST_TYPES)


def _span(item):
    return f"{item.get('start') or 'start'}..{item.get('cutoff') or 'end'}"


def _policy_note(item, code):
    """The admin Profile column's note for a blocked group or final."""
    what = {"group": "group merge", "final": "final merge"}.get(
        item.get("kind"), "request")
    return (f"{POLICY_STOP_NOTE} {what} {_span(item)} ({code}); no "
            f"automatic resend, Build profile with the override sends it "
            f"again")[:255]


def _skip_note(item, code):
    """The admin Profile column's note for a skipped window."""
    return (f"{POLICY_SKIP_NOTE} {_span(item)}: provider policy block "
            f"({code})")[:255]


def record_blocked_range(user_id, start, end, code, model_id=None):
    """Remember, for good, that the writing after ``start`` (None: from
    the first writing) up to ``end`` was blocked: every later window plan
    cuts around it (``cut_around_blocked``). Recorded whatever the
    account's AI usage. Once per range. Commits."""
    if end is None:
        return None
    row = ProfileBlockedRange.query.filter_by(
        user_id=user_id, range_start=start, range_end=end,
        lifted_at=None).first()
    if row is None:
        row = ProfileBlockedRange(user_id=user_id, range_start=start,
                                  range_end=end, code=code or "unknown",
                                  model_id=model_id)
        db.session.add(row)
        db.session.commit()
    return row


def blocked_range_of(item):
    """(start, end) of the writing a blocked window or chunk held: from
    just before its earliest entry (the item's ``earliest``) to its
    cutoff, so writing imported later before that entry, which was never
    sent, is read by the next plan. An item without ``earliest`` (sent
    before it was recorded) falls back to its start."""
    earliest = _dt(item.get("earliest"))
    start = (earliest - timedelta(microseconds=1) if earliest is not None
             else _dt(item.get("start")))
    return start, _dt(item.get("cutoff") or item.get("source_data_cutoff"))


def meets_blocked(user_id, start, end=None):
    """Whether the writing after ``start`` (None: from the first writing)
    up to ``end`` (None: the newest) meets a blocked range not lifted."""
    for r_start, r_end in blocked_ranges(user_id):
        a, b = _max_start(start, r_start), _min_end(end, r_end)
        if a is None or a < b:
            return True
    return False


def remainder_meets_blocked(user):
    """Whether the writing the user's next build reads (after the update
    base; all of it for a full rebuild) meets a blocked range: such a
    build must take the window path, which cuts around it."""
    prev = (None if user.profile_needs_full_regen
            else _exports.profile_update_base(user.id))
    return meets_blocked(
        user.id, prev.source_data_cutoff if prev is not None else None)


def blocked_ranges(user_id):
    """[(start, end)] of the user's blocked ranges not lifted, by start."""
    rows = (ProfileBlockedRange.query
            .with_entities(ProfileBlockedRange.range_start,
                           ProfileBlockedRange.range_end)
            .filter(ProfileBlockedRange.user_id == user_id,
                    ProfileBlockedRange.lifted_at.is_(None)).all())
    return sorted(((s, e) for s, e in rows),
                  key=lambda r: (r[0] or datetime.min, r[1]))


def lift_blocked_ranges(user_id):
    """Let the user's blocked ranges be read again: the next window plan
    sends that writing to the provider once more. The one way to resend a
    blocked window, from a shell only: no button does it (Peter,
    2026-11-04, skip the flagged windows). Returns how many were
    lifted. Commits."""
    n = ProfileBlockedRange.query.filter(
        ProfileBlockedRange.user_id == user_id,
        ProfileBlockedRange.lifted_at.is_(None)).update(
        {ProfileBlockedRange.lifted_at: datetime.utcnow()},
        synchronize_session=False)
    db.session.commit()
    return n


def _max_start(a, b):
    """The later of two range starts, None standing for the first
    writing."""
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def _min_end(a, b):
    """The earlier of two range ends, None standing for the newest
    writing."""
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def cut_around_blocked(start, end, ranges):
    """The range (start, end] (start None: from the first writing; end
    None: to the newest) cut around the blocked ``ranges`` [(start, end)],
    as [(blocked, piece_start, piece_end)] in order: a piece to read ends
    where a blocked range starts, the blocked piece is exactly the overlap,
    and the next piece to read begins where it ends."""
    pieces, lo = [], start
    for r_start, r_end in sorted(ranges,
                                 key=lambda r: (r[0] or datetime.min, r[1])):
        a, b = _max_start(lo, r_start), _min_end(end, r_end)
        if a is not None and not a < b:
            continue   # no overlap
        if a is not None and (lo is None or a > lo):
            pieces.append((False, lo, a))
        pieces.append((True, a, b))
        lo = b
        if end is not None and lo >= end:
            return pieces
    pieces.append((False, lo, end))
    return pieces


def skip_blocked_window(user, item, code, report=True):
    """A window the provider's content-policy filter blocked: its range is
    remembered for good (``record_blocked_range``, also for an account
    whose AI usage is off), a window_skip row takes its place, so the
    set's other windows are merged without it, and a $0 record of the
    block stops nothing (WINDOW_BLOCK_REQUEST_TYPE). No skip row for an
    account that no longer allows AI, nor for a set the user's own version
    superseded (as for a window's result); none twice for the same range.
    Shared by collect() and scripts/backfill_policy_blocks.py. Returns the
    skip row written, or None."""
    row = None
    start = _dt(item.get("start"))
    record_blocked_range(user.id, *blocked_range_of(item), code,
                         item.get("model_id"))
    if (account_allows_ai(user) and item.get("set")
            and not set_superseded(user, item["set"])
            and not UserProfile.query.filter(
                UserProfile.user_id == user.id,
                UserProfile.window_set == item["set"],
                UserProfile.covers_start == start,
                UserProfile.generation_type.in_(WINDOW_TYPES)).first()):
        # No row of the set starts there yet: a version saved by another
        # attempt (whatever its cutoff) is kept, never replaced.
        row = _save_skip(user, item)
    refusal_backoff.record_policy_block(
        user.id, WINDOW_BLOCK_REQUEST_TYPE, item.get("model_id"), code,
        "profile window", report=report)
    return row


def stop_for_blocked_merge(user, item, code, report=True):
    """A group or final the provider's content-policy filter blocked: it
    cannot be skipped (it is what makes the profile), so the account's
    build stops (a PROFILE_REQUEST_TYPES block row) until the admin's
    confirmed Build profile lifts it. Shared like skip_blocked_window."""
    refusal_backoff.record_policy_block(
        user.id, "profile_batch", item.get("model_id"), code,
        f"profile {item.get('kind')}", report=report)


def _show_policy_stop(user, code):
    """Keep the block's note in the admin Profile column: a window of the
    same batch saved after the block clears profile_seed_error. The note
    names the newest blocked group or final of the attempt history (only
    those stop the build), else only the code."""
    if (user.profile_seed_error or "").startswith(POLICY_STOP_NOTE):
        return
    blocked = [(when, key) for key, entries in attempt_history(user.id).items()
               for outcome, when in entries
               if outcome == POLICY_BLOCKED and key[0] in ("group", "final")]
    if blocked:
        _when, key = max(blocked, key=lambda b: b[0])
        item = {"kind": key[0], "start": key[1], "cutoff": key[2]}
        note = _policy_note(item, code)
    else:
        note = (f"{POLICY_STOP_NOTE} a profile request ({code}); no "
                f"automatic resend, Build profile with the override sends "
                f"it again")[:255]
    user.profile_seed_error = note
    db.session.commit()


# ── the advance task ──────────────────────────────────────────────────────

def stop_killed_advance(user_id, task_id):
    """The advance's runs were killed (the worker's memory limit, the hard
    time limit) again and again, each delivered again by
    reject_on_worker_lost (#613). Recorded as a failed build is (the
    admin's Profile column), with its guard cleared and no re-dispatch;
    the hourly seeder starts the build again. Runs inside an app
    context."""
    from backend.utils.heavy_slot import HeavyJobKilled
    user = db.session.get(User, user_id)
    if user is None or user.profile_generation_task_id != task_id:
        return
    user.profile_seed_error = (
        f"Profile window build failed: "
        f"{describe_exception(HeavyJobKilled())}")[:255]
    user.profile_generation_task_id = None
    user.profile_generation_task_dispatched_at = None
    user.profile_batch_pending = bool(inflight_items(user_id))
    db.session.commit()


def _stop_killed_advance(task, user_id, wait_for_imports=True):
    """HeavyTask's on_repeated_kill for advance_profile_windows."""
    with flask_app.app_context():
        stop_killed_advance(user_id, task.request.id)


def _claim(user, task_id):
    user.profile_generation_task_id = task_id
    user.profile_generation_task_dispatched_at = datetime.utcnow()
    user.profile_batch_pending = True
    # Pinned to Batch: a window build never takes the full-price
    # synchronous fallback (audit R6).
    user.profile_force_batch = True
    db.session.commit()


def dispatch_advance(user, wait_for_imports=True):
    """Start the advance task for ``user`` unless one is running (the
    per-user guard, profile_generation_task_id; a running task checks the
    state again before it exits). Returns the task id or None.
    ``wait_for_imports``: hold the work while the user's imports settle
    (#598); False for a build the admin started."""
    if user is None:
        return None
    if user.profile_generation_task_id and not _exports._is_task_stale(user):
        return None
    if _running_run(user.id) is not None:
        # A run whose heartbeat is fresh holds the user's lock, even with
        # no guard (run_locks): a second run would only exit again.
        return None
    task_id = uuid4().hex
    _claim(user, task_id)
    advance_profile_windows.apply_async(
        (user.id,) if wait_for_imports else (user.id, False),
        task_id=task_id)
    return task_id


def run_advance_inline(user, task_id):
    """The synchronous profile task's hand-off: run the advance inside it,
    under its own task id, so a build that needs windows has one
    implementation also for accounts not on the batch path."""
    guard = user.profile_generation_task_id
    running = _running_run(user.id)
    if (guard and guard != task_id and not _exports._is_task_stale(user)) or (
            running is not None and running != task_id):
        return {"user_id": user.id, "profile_id": None,
                "status": "completed", "message": "A build is running"}
    _claim(user, task_id)
    # The synchronous path starts its builds at once, imports or not.
    outcome = advance(user.id, task_id, wait_for_imports=False)
    return {"user_id": user.id, "profile_id": None, "status": "completed",
            "message": f"Independent versions on the Batch API: {outcome}"}


def _collected_since(user_id, since):
    """Whether a job carrying this user's set items was collected (or
    failed) after ``since``: its results came in while the task ran."""
    jobs = ProfileBatchJob.query.filter(
        ProfileBatchJob.collected_at.isnot(None),
        ProfileBatchJob.collected_at >= since).all()
    return any(item.get("user_id") == user_id
               and item.get("kind") in ITEM_KINDS
               for job in jobs for item in job.items or [])


def _running_run(user_id):
    """The task id of the advance whose heartbeat is fresh for this user
    (run_locks), else None (none, or Redis cannot be reached)."""
    return run_locks.holder(run_locks.profile_run_key(user_id))


def _leave_to_running(user, task_id, running):
    """A copy that found another run holding the user's lock exits
    without rendering. When the guard names this copy (a dispatch while
    the other run had no guard), it goes back to the run that holds the
    lock, so that run's exit clears it and dispatches what is left.

    The work is held for a later pass (profile_batch._hold_work) in every
    case: the run that holds the lock may be dead (most likely when the
    lock holds this copy's own id: its first delivery, killed for memory,
    the message requeued at once). Its lock expires, the hourly seeder
    clears the stale guard, and an account outside the seeder's cohort (a
    pre-fill waiting for its owner) is resumed only through the hold. A
    live run's exit makes the hold harmless: the next pass that finds
    nothing due releases it."""
    if running and running != task_id and \
            user.profile_generation_task_id == task_id:
        user.profile_generation_task_id = running
        db.session.commit()
    _batch._hold_work([user.id])
    if running == task_id:
        # An earlier delivery of this same task holds the lock: it died
        # (or, unexpectedly, still runs past the visibility timeout).
        logger.error("User %s: profile window advance %s delivered again "
                     "while its earlier delivery holds the user's lock "
                     "(killed?); the build resumes after the lock expires",
                     user.id, task_id)
    else:
        logger.warning("User %s: profile window advance %s exits: run %s "
                       "holds the user's lock", user.id, task_id,
                       running or "?")


def advance(user_id, task_id, wait_for_imports=True):
    """The advance: derive what the user's set needs from the database and
    submit it, up to twice (work that came due while it ran is not
    stranded). Idempotent: work in flight is excluded. One run per user
    at a time (run_locks, a heartbeat before each window): a copy that
    finds the user's lock held (its own message delivered again while it
    runs, or a second dispatch) exits without rendering and leaves the
    guard to the run that holds it; a run killed before its exit leaves
    the lock to expire, and the next dispatch renders again (nothing was
    submitted, or what was is in flight). Clears its guard at the end;
    re-dispatches itself when the render budget ran out or a job of the
    user's was collected meanwhile. ``wait_for_imports``: see
    dispatch_advance."""
    user = db.session.get(User, user_id)
    if user is None or user.profile_generation_task_id != task_id:
        return "superseded"
    lock = run_locks.RunLock(run_locks.profile_run_key(user_id), task_id,
                             run_locks.PROFILE_RUN_TTL)
    if not lock.acquire():
        _leave_to_running(user, task_id, lock.holder())
        return RUNNING_ELSEWHERE
    started = datetime.utcnow()
    outcome = "idle"
    redispatch = False
    lost = False
    try:
        with run_locks.holding(lock):
            if not account_allows_ai(user) or writing_on_hold(user_id):
                outcome = "off"
            else:
                cfg = current_app.config
                keys = apply_batch_key_override(
                    get_api_keys_for_usage(cfg, "chat"), cfg)
                deadline = time.monotonic() + int(
                    _setting("PROFILE_WINDOW_RENDER_SECONDS",
                             DEFAULT_RENDER_SECONDS))
                submitted = False
                for _ in range(2):
                    outcome = next_work(user, keys, deadline,
                                        wait_for_imports)
                    if outcome == "partial":
                        redispatch = True
                    if outcome != "submitted":
                        break
                    submitted = True
                if submitted and outcome == "idle":
                    outcome = "submitted"
    except run_locks.RunLockLost:
        # This run did not beat for longer than the lock's TTL (a frozen
        # server) and another run holds the lock now: what this one built
        # and did not send is dropped, and the guard is the other run's.
        db.session.rollback()
        lost = True
        outcome = "superseded"
        logger.warning("User %s: profile window advance %s lost the user's "
                       "lock to another run; stopped", user_id, task_id)
    except Exception as e:
        # Also the soft time limit (SoftTimeLimitExceeded, which the
        # provider calls let through, utils/soft_limit): the run stops
        # here and its guard and lock are released below.
        db.session.rollback()
        outcome = "error"
        user = db.session.get(User, user_id)
        if user is not None:
            user.profile_seed_error = (
                f"Profile window build failed: {describe_exception(e)}")[:255]
            db.session.commit()
            if soft_limit.found_in(e) is not None:
                # The work is held for a later pass: the hourly seeder
                # resumes it, also for an account outside its cohort (a
                # pre-fill waiting for its owner), which it reaches only
                # through the hold.
                _batch._hold_work([user_id])
        logger.error("Profile window build failed for user %s: %s",
                     user_id, describe_exception(e))
    finally:
        # Before the re-dispatch below, whose task takes the lock.
        lock.release()
        user = None if lost else db.session.get(User, user_id)
        if user is not None and user.profile_generation_task_id == task_id:
            user.profile_generation_task_id = None
            user.profile_generation_task_dispatched_at = None
            user.profile_batch_pending = bool(inflight_items(user_id))
            db.session.commit()
            if outcome == "idle":
                # Nothing of the set is due: no queue-full work waits.
                _batch._release_work_hold(user)
            if outcome not in ("off", "error", "held", "waiting",
                               POLICY_BLOCKED) and (
                    redispatch or _collected_since(user_id, started)):
                dispatch_advance(user, wait_for_imports)
    logger.info("User %s: profile window advance: %s", user_id, outcome)
    return outcome


@celery.task(bind=True, base=HeavyTask, acks_late=True,
             reject_on_worker_lost=True, on_repeated_kill=_stop_killed_advance)
def advance_profile_windows(self, user_id, wait_for_imports=True):
    """Render, check and submit what the user's window set needs now.
    Holds only the per-user guard and the user's run lock (run_locks),
    never the batch pipeline lock: a render of a few hundred windows takes
    minutes."""
    with flask_app.app_context():
        return advance(user_id, self.request.id, wait_for_imports)


# ── collection (called by the poller) ─────────────────────────────────────

def _window_problem(response, template):
    if response.get("truncated"):
        return "truncated"
    if response.get("refused"):
        return "refused"
    if _exports.profile_output_problem(
            response.get("content"), template, events=True):
        return "invalid"
    return None


def _chained_problem(response, gen_template):
    """A chained update's output check: the three maps of the generation
    prompt, as for a merge; no events section (the update prompt asks for
    none)."""
    if response.get("truncated"):
        return "truncated"
    if response.get("refused"):
        return "refused"
    if _exports.profile_output_problem(response.get("content"), gen_template):
        return "invalid"
    return None


def _collect_window(user, item, result, job, history, template, notes,
                    gen_template=None):
    key = _item_key(item)
    gen_type = item.get("generation_type") or "window"
    chained = gen_type == CHAINED_TYPE
    if result is None:
        n = _count(history, key, PROVIDER_FAILURES) + 1
        if n >= int(_setting("PROFILE_VERSION_PROVIDER_ATTEMPTS", 3)):
            notes["stopped"].append(item)
        return "missing"
    response = _batch._response_from_result(result)
    if not account_allows_ai(user):
        _exports.discard_step_output(user, item["model_id"], response,
                                     "profile window", batch=True)
        return "dropped"
    start, cutoff = _dt(item.get("start")), _dt(item.get("cutoff"))
    existing = UserProfile.query.filter_by(
        user_id=user.id, window_set=item["set"], covers_start=start,
        source_data_cutoff=cutoff, generation_type=gen_type).filter(
        UserProfile.created_at >= job.submitted_at).first()
    if existing is not None:
        return "saved"
    if set_superseded(user, item["set"]):
        # The user saved a version after the set's final while this window
        # was in the batch. Saving it (or a skip row in its place) would
        # make the set's tip the update base again, on a set that is no
        # longer merged: the edit would be stranded and every later window
        # billed and never used. The next update builds on the user's
        # version instead.
        _exports.discard_step_output(user, item["model_id"], response,
                                     "profile window", batch=True)
        return "superseded"
    problem = (_chained_problem(response, gen_template) if chained
               else _window_problem(response, template))
    if problem:
        # Billed: the cost row is written. Not marked refused for the
        # user-level backoff, which would stop a build of hundreds of
        # windows after two refused ones in a row; a window has its own
        # bound (one identical retry, then skipped).
        _exports._add_profile_cost_log(user, item["model_id"], response,
                                       batch=True)
        db.session.commit()
        if _count(history, key, CONTENT_FAILURES) + 1 >= _content_attempts():
            _save_skip(user, item)
            notes["skipped"].append((item, problem))
        return problem
    # A chained version covers all the writing to its cutoff: its date line
    # says so ("Covers data through B"). Its parent is the set's base, as a
    # window's (resolve_set reads the set's base from its tip); the profile
    # it updated is in its job item ("chained_base_id").
    _exports._save_profile(
        user, item["model_id"], response["content"], response,
        source_tokens_used=item.get("cumulative"), source_data_cutoff=cutoff,
        generation_type=gen_type, parent_profile_id=item.get("base_id"),
        batch=True, source_origin_stats=item.get("origin_stats"),
        source_rendered_at=_dt(item.get("rendered_at")),
        window_set=item["set"], covers_start=start,
        date_line_start=(None if chained
                         else start or _dt(item.get("earliest"))),
        date_line_tokens=None if chained else item.get("units"))
    _exports.record_token_ratio(user, item["model_id"], item.get("units"),
                                response.get("input_tokens"))
    if item.get("base_id") is None and user.profile_needs_full_regen:
        user.profile_needs_full_regen = False
    user.profile_batch_attempts = 0
    db.session.commit()
    return "saved"


def _user_version_after(user, since):
    if since is None:
        return None
    return UserProfile.query.filter(
        UserProfile.user_id == user.id,
        UserProfile.ai_usage.in_(AI_ALLOWED),
        UserProfile.created_at > since,
        or_(UserProfile.generated_by == "user",
            UserProfile.generation_type == "revert"),
    ).first()


def _collect_merge(user, item, result, job, gen_template, history, notes):
    kind = item["kind"]
    if result is None:
        n = _count(history, _item_key(item), PROVIDER_FAILURES) + 1
        if n >= int(_setting("PROFILE_VERSION_PROVIDER_ATTEMPTS", 3)):
            notes["stopped"].append(item)
        return "missing"
    response = _batch._response_from_result(result)
    if not account_allows_ai(user):
        _exports.discard_step_output(user, item["model_id"], response,
                                     f"profile {kind}", batch=True)
        return "dropped"
    try:
        _exports.refuse_truncated_profile(
            user, item["model_id"], response, f"batch {kind}", batch=True,
            problem=_exports.profile_output_problem(
                response.get("content"), gen_template,
                final=(kind == "final")))
    except EmptyTruncatedOutputError:
        return "refused"
    start, cutoff = _dt(item.get("start")), _dt(item.get("cutoff"))
    if kind == "group":
        existing = UserProfile.query.filter_by(
            user_id=user.id, window_set=item["set"], covers_start=start,
            source_data_cutoff=cutoff, generation_type="group").filter(
            UserProfile.created_at >= job.submitted_at).first()
        if existing is None:
            _exports._save_profile(
                user, item["model_id"], response["content"], response,
                source_tokens_used=item.get("cumulative"),
                source_data_cutoff=cutoff, generation_type="group",
                parent_profile_id=item.get("parent_id"), batch=True,
                source_origin_stats=item.get("origin_stats"),
                window_set=item["set"], covers_start=start,
                date_line_start=_dt(item.get("start_day")),
                date_line_tokens=item.get("units"))
        user.profile_batch_attempts = 0
        db.session.commit()
        return "saved"
    # The final. A version the user saved while it was in the batch is
    # newer than what it merged: the next advance merges again with it
    # (#183, the user's edit wins).
    if _user_version_after(user, _dt(item.get("built_at"))) is not None:
        _exports.discard_step_output(user, item["model_id"], response,
                                     "profile final merge", batch=True)
        return "superseded"
    existing = UserProfile.query.filter_by(
        user_id=user.id, window_set=item["set"],
        parent_profile_id=item.get("tip_id"),
        generation_type="integration").filter(
        UserProfile.created_at >= job.submitted_at).first()
    if existing is not None:
        return "saved"
    version_id = item.get("user_version_id")
    final = _exports._save_profile(
        user, item["model_id"], response["content"], response,
        source_tokens_used=item.get("cumulative"), source_data_cutoff=cutoff,
        generation_type="integration", parent_profile_id=item.get("tip_id"),
        batch=True, source_origin_stats=item.get("origin_stats"),
        merged_profile_id=version_id, window_set=item["set"])
    if (version_id and user.profile_pending_merge_id == version_id
            and final.ai_usage in AI_ALLOWED):
        user.profile_pending_merge_id = None
    user.profile_batch_attempts = 0
    db.session.commit()
    logger.info("User %s: saved the final merge of profile set %s (%d "
                "inputs) -> profile %s", user.id, item["set"],
                len(item.get("inputs") or ()), final.id)
    from backend.utils.notifications import notify_profile_ready
    notify_profile_ready(user.id)
    _notify_prefill_set_complete(user, item["set"])
    return "saved"


def _notify_prefill_set_complete(user, set_id):
    """The pre-fill-complete email (profile_batch._notify_prefill_complete)
    for a window set: a pre-filled account whose first profile is this
    set's final. Never raises."""
    if not user.prefilled_handle:
        return
    try:
        rows = UserProfile.query.with_entities(
            UserProfile.window_set, UserProfile.created_at).filter_by(
            user_id=user.id).all()
        mine = [t for s, t in rows if s == set_id]
        if not mine or any(s != set_id and t < min(mine) for s, t in rows):
            return
        tip = UserProfile.query.filter_by(
            user_id=user.id, window_set=set_id,
            generation_type="integration").order_by(
            UserProfile.created_at.desc()).first()
        from backend.utils.email import send_admin_prefill_complete_notification
        send_admin_prefill_complete_notification(
            user.username, user.prefilled_handle, len(mine),
            (tip.source_tokens_used if tip else 0) or 0, bool(user.approved))
    except Exception:
        logger.exception(f"User {user.id}: pre-fill-complete notification failed")


def collect(job, items, results, queue_full=False):
    """Save the collected results of a job's window, group and final items
    and write each item's outcome back onto the job ("saved", "missing",
    "error", "refused", "invalid", "truncated", "superseded", "dropped"),
    which is the attempt history the next advance reads. Returns the ids
    of the users whose sets should advance; the poller dispatches them
    after it commits the job.

    An item the provider's content-policy filter blocked (``results``'
    ``blocked``, llm_batch.blocked_items) gets the outcome "blocked" and is
    never sent again on its own; the block is recorded and reported. A
    window is skipped (``skip_blocked_window``) and the build goes on; a
    group or final stops the account's build (``stop_for_blocked_merge``;
    Peter, 2026-11-04).

    ``queue_full``: the batch failed only because the provider's queue was
    full (Peter, 2026-11-03). Every item gets the outcome "queue_full",
    which is neither a content nor a provider failure, so nothing is
    stopped or skipped; nobody is advanced at once (the queue is still
    full), and the hourly seeder sends the work again."""
    if queue_full:
        job.items = [dict(i, outcome="queue_full")
                     if i.get("kind") in ITEM_KINDS else i
                     for i in job.items or []]
        return set()
    outcomes = {}
    users = set()
    by_user = {}
    blocked = blocked_items(results)
    for item in items:
        by_user.setdefault(item["user_id"], []).append(item)
    for user_id, user_items in by_user.items():
        user = db.session.get(User, user_id)
        if user is None:
            continue
        users.add(user_id)
        history = attempt_history(user_id)
        template = _exports.window_generation_template(user_id)
        gen_template = _exports._load_prompt(
            "profile_generation.txt", user_id=user_id)
        notes = {"skipped": [], "stopped": [], "blocked": []}
        for item in user_items:
            if item["custom_id"] in blocked:
                outcomes[item["custom_id"]] = POLICY_BLOCKED
                notes["blocked"].append((item, blocked[item["custom_id"]]))
                continue
            result = results.get(item["custom_id"])
            try:
                if item["kind"] == "window":
                    outcome = _collect_window(user, item, result, job,
                                              history, template, notes,
                                              gen_template)
                else:
                    outcome = _collect_merge(user, item, result, job,
                                             gen_template, history, notes)
            except WritingOnHold:
                outcome = "dropped"
            except Exception as e:
                db.session.rollback()
                logger.error("Collecting profile %s for user %s failed: %s",
                             item.get("kind"), user_id, describe_exception(e))
                outcome = "error"
            outcomes[item["custom_id"]] = outcome
        _report_notes(user, notes)
        if any(outcomes.get(i["custom_id"]) in WAITING_OUTCOMES
               for i in user_items):
            # Something of the set waits for a later pass (a retry, the
            # refusal backoff, a provider stop): the hourly seeder sends
            # it again also for an account not yet activated.
            _batch._hold_work([user_id])
    job.items = [dict(i, outcome=outcomes[i["custom_id"]])
                 if i.get("custom_id") in outcomes else i
                 for i in job.items or []]
    return users


# Outcomes that leave work of the set for a later pass (a refused merge
# is "refused" too).
WAITING_OUTCOMES = CONTENT_FAILURES + PROVIDER_FAILURES


def provider_stop_waits(user):
    """Whether a window, group or final of the user's set waits out a
    provider stop (a day after three failed submissions,
    ``provider_stop_until``). Reads metadata only."""
    if not enabled():
        return False
    try:
        history = attempt_history(user.id)
        now = datetime.utcnow()
        if any(key[0] in ("group", "final")
               and provider_stop_until(history, key, now) is not None
               for key in history):
            return True
        set_id, base, _fresh = resolve_set(user)
        return bool(set_state(user, set_id, base, history, now).stopped)
    except Exception:  # pragma: no cover — a hold is never worth a crash
        logger.exception("User %s: reading the set's stops failed", user.id)
        return False


def _report_notes(user, notes):
    """After a job's items of one user: Sentry reports and the admin
    Profile column's note. Notes by weight: a skipped window's, then a
    provider stop's, then a blocked merge's (which stops the build)."""
    blocked = notes.get("blocked") or []
    windows = [(i, c) for i, c in blocked if i.get("kind") == "window"]
    merges = [(i, c) for i, c in blocked if i.get("kind") != "window"]
    # Recorded after the user's other items: a window of the same batch
    # saved after the block would clear the note.
    skipped = [(item, code) for item, code in windows
               if skip_blocked_window(user, item, code) is not None]
    if skipped:
        item, code = skipped[0]
        user.profile_seed_error = _skip_note(item, code)
        db.session.commit()
    for item, problem in notes["skipped"]:
        _report("Profile window skipped after repeated failures",
                user_id=user.id, set=item.get("set"),
                start=item.get("start") or "start", end=item.get("cutoff"),
                model=item.get("model_id"), reason=problem)
    stopped = notes["stopped"]
    if stopped:
        first = stopped[0]
        until = datetime.utcnow() + refusal_backoff.UNBILLED_STOP_EXPIRY
        user.profile_seed_error = (
            f"{STOP_NOTE} {int(_setting('PROFILE_VERSION_PROVIDER_ATTEMPTS', 3))} "
            f"times ({first.get('start') or 'start'}..{first.get('cutoff')}"
            f"{'' if len(stopped) == 1 else f', and {len(stopped) - 1} more'}"
            f"); next try after {until:%Y-%m-%d %H:%M} UTC")[:255]
        db.session.commit()
        _report("Profile windows failed at the provider repeatedly",
                user_id=user.id, set=first.get("set"), windows=len(stopped),
                start=first.get("start") or "start", end=first.get("cutoff"),
                model=first.get("model_id"), reason="provider")
    for item, code in merges:
        stop_for_blocked_merge(user, item, code)
    if merges:
        item, code = merges[0]
        user.profile_seed_error = _policy_note(item, code)
        db.session.commit()

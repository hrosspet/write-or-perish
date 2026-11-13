"""The final merge run again on production after a bundle load (#698).

Decision (voice review, Peter, 2026-11-13): the lab wrote most final
profiles with a small model for cost. "Once the transfer is done and the
final profile is from a small model, the final merge should be done on
Opus 5.5", live, as part of the load; the window versions are reused and
nothing reads the tweets again. Accounts the lab merged again with a
frontier model before export need no call.

What runs: the window set's FINAL merge only, from the loaded windows, by
the code a normal build's final uses: ``profile_windows.merge_plan`` (with
``redo_final``) for the inputs, ``_merge_messages`` for the prompt,
``merge_over_cap`` for the cap, the same output checks, and
``exports._save_profile`` (cost row against the account, "integration"
row of the set). The model is the one a normal build's merge step would
use for this account (``profile_windows.pass_models``): the merge model of
the provider of the person's chat model, Opus 5.5 for a new person; the
cross-provider merge model only where that code already allows it. One
live call (not Batch).

When: only when the loaded final was written by another model than that
merge model. Never for an account set to AI usage None (no AI means no
AI: it keeps the loaded profile), never after a "no" to starting from
the tweets, never for an account whose current profile is a version the
person wrote, and at most once per account: a merge done for any load of
the account is never made again (a second Yes, a re-load). The admin's
"Make ready" asks for it only with its option (on by default).

The person waits for it ("Preparing your starting point…") at most
MERGE_WAIT; then the load lets them in with the loaded profile and the
merge goes on in the background, MAX_MERGE_ATTEMPTS times at most, a final
failure reported as an error. A merge that lands after the person saved
a version of their own is not saved (the user's edit wins); its cost row
is.
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy import update

from backend.extensions import db
from backend.models import PrefillBundleLoad, User, UserProfile

logger = logging.getLogger(__name__)

# ── Heuristics (#698, flagged in the PR) ─────────────────────────────────
# How long the person's "Preparing your starting point…" waits for the
# merge before they are let in with the loaded profile: an Opus 5.5 merge
# of a few windows answers in one to two minutes.
MERGE_WAIT = timedelta(minutes=3)
# How often a waiting load looks whether its merge is done.
MERGE_POLL_SECONDS = 10
# Calls at most per load (the first and two retries), then reported.
MAX_MERGE_ATTEMPTS = 3
# Seconds before a retry, by attempt.
RETRY_COUNTDOWNS = (60, 300)
# A merge "running" with no sign of life for this long died with its
# worker: the beat retries it (a live merge call takes minutes, not this).
MERGE_STALE = timedelta(minutes=20)

TERMINAL = ("done", "skipped", "superseded", "failed")


class MergeStop(Exception):
    """The merge must not, or cannot, be made: ``code`` names why."""

    def __init__(self, code, status="skipped"):
        super().__init__(code)
        self.code = code
        self.status = status


def _now():
    return datetime.utcnow()


def _dispatch(load_id, countdown=0):
    """Send a merge run (a module hook: tests run it inline)."""
    from backend.tasks.prefill_bundles import merge_prefill_profile
    merge_prefill_profile.apply_async(args=(load_id,), countdown=countdown)


def merge_waiting_over(load, now=None):
    """Whether the person no longer waits for this load's merge: it ended,
    or MERGE_WAIT has passed since the load began waiting for it."""
    if load.merge_status in TERMINAL or load.merge_status is None:
        return True
    started = load.merge_started_at
    return started is None or (now or _now()) - started >= MERGE_WAIT


def _claim(load_id):
    taken = db.session.execute(
        update(PrefillBundleLoad).where(
            PrefillBundleLoad.id == load_id,
            PrefillBundleLoad.merge_status.in_(("due", "retry")),
        ).values(merge_status="running", merge_heartbeat_at=_now(),
                 merge_attempts=PrefillBundleLoad.merge_attempts + 1)
    ).rowcount
    db.session.commit()
    return bool(taken)


def _set(load_id, **values):
    db.session.execute(update(PrefillBundleLoad).where(
        PrefillBundleLoad.id == load_id).values(**values))
    db.session.commit()


def _merged_before(user_id, load_id):
    """A merge done for another load of this account: never a second."""
    return PrefillBundleLoad.query.filter(
        PrefillBundleLoad.user_id == user_id,
        PrefillBundleLoad.id != load_id,
        PrefillBundleLoad.merge_status == "done").first() is not None


def _current_final(user_id):
    """The account's current profile, when it is a window set's final."""
    from backend.utils.privacy import AI_ALLOWED
    from backend.utils.profile_versions import current_profile_query
    current = current_profile_query(user_id).filter(
        UserProfile.ai_usage.in_(AI_ALLOWED)).first()
    if current is None or current.generated_by in ("user",) \
            or current.generation_type == "revert":
        raise MergeStop("user_version" if current is not None else "no_profile")
    if current.generation_type != "integration" or not current.window_set:
        raise MergeStop("no_window_set")
    return current


def _merge_once(load):
    """The merge itself. Returns "done" or "superseded"; MergeStop when
    no call is made or none can succeed; any other error is retried."""
    from backend.llm_providers import LLMProvider, EmptyTruncatedOutputError
    from backend.tasks import exports as ex
    from backend.tasks import profile_windows as pw
    from backend.utils.api_keys import get_api_keys_for_usage
    from backend.utils.hidden_rows import account_on_hold
    from backend.utils.privacy import account_allows_ai
    from flask import current_app

    user = db.session.get(User, load.user_id)
    if user is None or user.deleted_at is not None or account_on_hold(user.id):
        raise MergeStop("deleted")
    if not account_allows_ai(user):
        raise MergeStop("ai_none")       # no AI means no AI
    if user.prefill_consent == "no":
        raise MergeStop("declined")
    if _merged_before(user.id, load.id):
        raise MergeStop("already_merged")
    current = _current_final(user.id)
    set_id = current.window_set
    windows = pw.set_windows(user.id, set_id)
    if not windows:
        raise MergeStop("no_window_set")
    tip = windows[-1]
    base = (db.session.get(UserProfile, tip.parent_profile_id)
            if tip.parent_profile_id else None)
    state = pw.set_state(user, set_id, base)
    models = pw.pass_models(user, state, None, pw.cross_provider_allowed(user))
    if current.generated_by == models.merge:
        raise MergeStop("current")       # already this account's merge model
    plan = pw.merge_plan(user, state, redo_final=True)
    inputs = pw.final_inputs_to_send(plan)
    if not inputs:
        raise MergeStop("not_mergeable")
    keys = get_api_keys_for_usage(current_app.config, "chat")
    messages = pw._merge_messages(user, inputs)
    over, tokens = pw.merge_over_cap(models.merge, messages, keys)
    if over:
        raise MergeStop("over_cap", status="failed")
    _set(load.id, merge_model=models.merge, merge_heartbeat_at=_now())
    response = LLMProvider.get_completion(models.merge, messages, keys,
                                          user_id=user.id)
    gen_template = ex._load_prompt("profile_generation.txt", user_id=user.id)
    try:
        ex.refuse_truncated_profile(
            user, models.merge, response, "pre-fill final merge",
            problem=ex.profile_output_problem(
                response.get("content"), gen_template, final=True))
    except EmptyTruncatedOutputError:
        raise RuntimeError("the merge's output is not a profile")
    # The user's edit wins: a version they saved since the load began is
    # newer than anything this merge read.
    if pw._user_version_after(user, load.created_at) is not None:
        ex.discard_step_output(user, models.merge, response,
                               "pre-fill final merge")
        return "superseded"
    ex._save_profile(
        user, models.merge, response["content"], response,
        source_tokens_used=tip.source_tokens_used,
        source_data_cutoff=tip.source_data_cutoff,
        generation_type="integration", parent_profile_id=tip.id,
        source_origin_stats=tip.source_origin_stats,
        merged_profile_id=(plan.user_version.id
                           if plan.user_version is not None else None),
        window_set=set_id)
    db.session.commit()
    logger.info("Pre-fill bundle load %s: final merge of set %s (%d inputs) "
                "by %s saved for user %s", load.id, set_id, len(inputs),
                models.merge, user.id)
    return "done"


def run_merge(load_id, dispatch=None):
    """One attempt of a load's merge (the task's body). Returns the merge
    status it left, or "busy" when the merge is not due."""
    from backend.utils import prefill_bundles as pb
    if not pb.bundles_enabled():
        return "disabled"   # the beat resumes it once switched on again
    if not _claim(load_id):
        return "busy"
    load = db.session.get(PrefillBundleLoad, load_id)
    try:
        outcome = _merge_once(load)
        _set(load_id, merge_status=outcome, merge_error=None)
        return outcome
    except MergeStop as stop:
        db.session.rollback()
        _set(load_id, merge_status=stop.status, merge_error=stop.code)
        (logger.error if stop.status == "failed" else logger.info)(
            "Pre-fill bundle load %s: final merge %s (%s)", load_id,
            stop.status, stop.code)
        return stop.status
    except Exception as e:  # noqa: BLE001 - retried, then reported
        db.session.rollback()
        attempts = db.session.query(PrefillBundleLoad.merge_attempts).filter(
            PrefillBundleLoad.id == load_id).scalar() or 0
        code = type(e).__name__[:64]
        if attempts >= MAX_MERGE_ATTEMPTS:
            _set(load_id, merge_status="failed", merge_error=code)
            # Problems show: the person was let in with the loaded profile.
            logger.error("Pre-fill bundle load %s: final merge failed %d "
                         "times (%s); the loaded profile stays", load_id,
                         attempts, code)
            return "failed"
        _set(load_id, merge_status="retry", merge_error=code)
        logger.warning("Pre-fill bundle load %s: final merge attempt %d "
                       "failed (%s); trying again", load_id, attempts, code)
        (dispatch or _dispatch)(load_id, countdown=RETRY_COUNTDOWNS[
            min(attempts, len(RETRY_COUNTDOWNS)) - 1])
        return "retry"


def stalled_merge_ids():
    """Merges to send again (the beat): due or to retry with no attempt
    under way, or running with no sign of life for MERGE_STALE. Their
    heartbeat moves to now, so a waiting one is not sent on every beat."""
    from sqlalchemy import or_
    now = _now()
    stale = now - MERGE_STALE
    rows = db.session.query(
        PrefillBundleLoad.id, PrefillBundleLoad.merge_status,
        PrefillBundleLoad.merge_attempts).filter(
        PrefillBundleLoad.merge_status.in_(("due", "retry", "running")),
        or_(PrefillBundleLoad.merge_heartbeat_at.is_(None),
            PrefillBundleLoad.merge_heartbeat_at < stale)).all()
    ids = []
    for load_id, status, attempts in rows:
        if status == "running" and (attempts or 0) >= MAX_MERGE_ATTEMPTS:
            _set(load_id, merge_status="failed", merge_error="stalled")
            logger.error("Pre-fill bundle load %s: final merge stopped after "
                         "%d attempts whose worker died; the loaded profile "
                         "stays", load_id, attempts)
            continue
        values = {"merge_heartbeat_at": now}
        if status == "running":
            values["merge_status"] = "retry"   # its worker died
        _set(load_id, **values)
        ids.append(load_id)
    return ids

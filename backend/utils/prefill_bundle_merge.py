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
live call (not Batch), with a normal final merge's output bound and, for
an OpenAI merge, its reasoning effort.

A merge that fails is the load's own matter: an output that cannot be
saved writes its cost row outside the profile build's backoff
(UNUSED_OUTPUT_REQUEST_TYPE) and touches nothing on the account, so the
account's ordinary profile updates run as for any other; the person keeps
the loaded profile, and the last failure is logged once at ERROR.

When: only when the loaded final was written by another model than that
merge model. Never for an account set to AI usage None (no AI means no
AI: it keeps the loaded profile), never after a "no" to starting from
the tweets, never for an account an admin deactivated, deleted or held,
never for an account whose current profile is a version the person
wrote, and at most once per account: a merge done for any load of the
account is never made again (a second Yes, a re-load), and while one
load's merge is in flight no other load of the account starts one
(``_claim``). The admin's "Make ready" asks for it only with its option
(on by default).

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
# A merge "running" for this long since its last sign of life is over:
# the worker's hard task limit (celery_app.TASK_TIME_LIMIT, 1 h) has
# ended its call by then. Until then it counts as in flight, so no other
# merge of the account starts and the beat does not send it again. A live
# merge call takes one to two minutes; the margin is for the task limit.
MERGE_STALE = timedelta(minutes=65)

TERMINAL = ("done", "skipped", "superseded", "failed")

# The cost row of a merge whose output cannot be saved (cut off, refused,
# or not a profile): a request type outside refusal_backoff's
# PROFILE_REQUEST_TYPES, as profile_windows.WINDOW_BLOCK_REQUEST_TYPE, so
# a failed merge after a load never counts toward, or stops, the
# account's ordinary profile builds. A saved merge's row is a "profile"
# row like any build's.
UNUSED_OUTPUT_REQUEST_TYPE = "prefill_merge"


class MergeOutputUnusable(RuntimeError):
    """The merge's output is not a profile: retried, then reported."""


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
    """Take this load's merge attempt: "running" (the call may be made),
    "skipped" (another load of the account merged, or is merging) or None
    (not due: another run has it, or it ended). One merge call per
    account under any overlap: the claim is decided under a lock on the
    account's row, so two loads of one account claim one after the
    other, and the second sees the first's "running" (taken while its
    heartbeat is younger than MERGE_STALE) or "done"."""
    from sqlalchemy import and_, or_
    row = db.session.query(PrefillBundleLoad.user_id).filter(
        PrefillBundleLoad.id == load_id,
        PrefillBundleLoad.merge_status.in_(("due", "retry"))).first()
    if row is None:
        db.session.commit()
        return None
    user_id = row[0]
    db.session.query(User.id).filter(User.id == user_id) \
        .with_for_update().first()
    now = _now()
    other = db.session.query(PrefillBundleLoad.merge_status).filter(
        PrefillBundleLoad.user_id == user_id,
        PrefillBundleLoad.id != load_id,
        or_(PrefillBundleLoad.merge_status == "done",
            and_(PrefillBundleLoad.merge_status == "running",
                 PrefillBundleLoad.merge_heartbeat_at > now - MERGE_STALE)),
    ).order_by(PrefillBundleLoad.merge_status.asc()).first()
    due = and_(PrefillBundleLoad.id == load_id,
               PrefillBundleLoad.merge_status.in_(("due", "retry")))
    if other is not None:
        # "done" sorts before "running": a merge made is the reason given.
        code = "already_merged" if other[0] == "done" else "merging_elsewhere"
        taken = db.session.execute(update(PrefillBundleLoad).where(due).values(
            merge_status="skipped", merge_error=code)).rowcount
        db.session.commit()
        if taken:
            logger.info("Pre-fill bundle load %s: final merge skipped (%s)",
                        load_id, code)
        return "skipped" if taken else None
    taken = db.session.execute(update(PrefillBundleLoad).where(due).values(
        merge_status="running", merge_heartbeat_at=now,
        merge_attempts=PrefillBundleLoad.merge_attempts + 1)).rowcount
    db.session.commit()
    return "running" if taken else None


def _set(load_id, **values):
    db.session.execute(update(PrefillBundleLoad).where(
        PrefillBundleLoad.id == load_id).values(**values))
    db.session.commit()


def _account_gates(user):
    """MergeStop when no merge may be made for this account now: it is
    deactivated, deleted or held (prefill_bundle_load.account_stop), set
    to AI usage None (no AI means no AI), or the person said no. Checked
    when the attempt starts and again just before the model call."""
    from backend.utils.prefill_bundle_load import account_stop
    from backend.utils.privacy import account_allows_ai
    stop = account_stop(user)
    if stop is not None:
        raise MergeStop("deleted" if stop == "no_account" else stop)
    if not account_allows_ai(user):
        raise MergeStop("ai_none")
    if user.prefill_consent == "no":
        raise MergeStop("declined")


def _output_problem(response, template):
    """Why the merge's output cannot be saved as the profile, or None:
    the checks a normal final merge's output gets (cut off, refused by
    the model, profile_output_problem), without the profile-build backoff
    that exports.refuse_truncated_profile feeds."""
    from backend.llm_providers import is_refused
    from backend.tasks import exports as ex
    if response.get("truncated"):
        return "output cut off"
    if is_refused(response):
        return "refused by the model"
    return ex.profile_output_problem(response.get("content"), template,
                                     final=True)


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
    from backend.llm_providers import DEFAULT_MAX_OUTPUT_TOKENS, LLMProvider
    from backend.models import APICostLog
    from backend.tasks import exports as ex
    from backend.tasks import profile_windows as pw
    from backend.utils.api_keys import get_api_keys_for_usage
    from backend.utils.cost import llm_cost_log_fields
    from flask import current_app

    user = db.session.get(User, load.user_id)
    _account_gates(user)
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
    gen_template = ex._load_prompt("profile_generation.txt", user_id=user.id)
    # Asked again just before the call: the account may have been
    # deactivated, deleted or set to None while the inputs were read.
    db.session.refresh(user)
    _account_gates(user)
    _set(load.id, merge_model=models.merge, merge_heartbeat_at=_now())
    # A normal final merge's request (profile_windows._request): the same
    # output bound, and an OpenAI merge's configured reasoning effort.
    response = LLMProvider.get_completion(
        models.merge, messages, keys, user_id=user.id,
        max_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
        reasoning_effort=models.merge_effort)
    problem = _output_problem(response, gen_template)
    if problem:
        # Billed, so its cost row is written, outside the profile build's
        # backoff (UNUSED_OUTPUT_REQUEST_TYPE); nothing is saved and
        # nothing on the account changes. The attempt is retried.
        db.session.add(APICostLog(
            user_id=user.id, model_id=models.merge,
            request_type=UNUSED_OUTPUT_REQUEST_TYPE,
            **llm_cost_log_fields(models.merge, response)))
        db.session.commit()
        logger.warning("Pre-fill bundle load %s: the final merge's output "
                       "for user %s (model %s) is not saved: %s", load.id,
                       user.id, models.merge, problem)
        raise MergeOutputUnusable(problem)
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
    claim = _claim(load_id)
    if claim is None:
        return "busy"
    if claim != "running":
        return claim
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
        code = (f"output: {e}" if isinstance(e, MergeOutputUnusable)
                else type(e).__name__)[:64]
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

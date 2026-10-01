"""Backoff for background jobs after a refused model output (#368).

A background job that refuses a cut-off output saves nothing, so the gates
that decide when it runs again (interval since the last version, data since
its cutoff, an unfinished chain) stay open, and the next beat would repeat
the same billed call. Every refusal writes its cost row with
``request_ref = REFUSED_REF``; the refusals newer than the job's last saved
output are its consecutive failures. No extra column: a saved output ends
the streak by itself.

Two strikes (voice review, Peter, 2026-10-01):
  1st refusal in a row  ->  wait RETRY_WAIT (1 h), then one more try;
  2nd refusal in a row  ->  stopped: the scheduler no longer runs the job
                            for this user, and the stop is reported to
                            Sentry as an error (``report_stop``).

A stopped job runs again only after something saves a new output. For a
profile: a build an import triggers (it skips the wait and the stop), the
admin "Build profile" button after the admin confirms the override
(``force``), a revert, or a new hand-written version. For recent context:
a new recent context or a new profile version, since that changes its
input. The hourly schedulers, and the button without ``force``, respect
the stop.
"""
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

REFUSED_REF = "refused:truncated"

RETRY_WAIT = timedelta(hours=1)
STOP_AFTER = 2   # refusals in a row

PROFILE_REQUEST_TYPES = ("profile", "profile_batch")
RECENT_CONTEXT_REQUEST_TYPES = ("recent_context",)


def refusals_since(user_id, request_types, since):
    """Creation times of the user's refused calls of these request types
    newer than ``since`` (the last saved output; None = all), newest
    first."""
    from backend.models import APICostLog
    q = APICostLog.query.with_entities(APICostLog.created_at).filter(
        APICostLog.user_id == user_id,
        APICostLog.request_type.in_(request_types),
        APICostLog.request_ref == REFUSED_REF,
    )
    if since is not None:
        q = q.filter(APICostLog.created_at > since)
    return [row[0] for row in q.order_by(APICostLog.created_at.desc()).all()]


def backoff_state(user_id, request_types, since):
    """(n, until, stopped): the number of consecutive refusals since the
    last saved output; when the next attempt is allowed (None = now, or
    never when stopped); and whether the job is stopped for this user."""
    times = refusals_since(user_id, request_types, since)
    n = len(times)
    if n == 0:
        return 0, None, False
    if n >= STOP_AFTER:
        return n, None, True
    return n, times[0] + RETRY_WAIT, False


def in_backoff(user_id, request_types, since, job, now=None):
    """True while the job must not run for this user: waiting after the
    first refusal, or stopped after the second."""
    n, until, stopped = backoff_state(user_id, request_types, since)
    if stopped:
        logger.info("User %s: %s stopped after %d refused outputs in a row",
                    user_id, job, n)
        return True
    if until is None or (now or datetime.utcnow()) >= until:
        return False
    logger.info("User %s: %s waiting after a refused output until %s",
                user_id, job, until)
    return True


def report_stop(user_id, job, n, model_id, request_type):
    """The job is stopped for this user: log it at ERROR and send a
    tagged Sentry event, so someone looks at it. Nothing retries it."""
    msg = (f"{job} stopped for user {user_id}: output cut off {n} times "
           f"in a row (model {model_id}); no more automatic runs until a "
           f"new version is saved")
    logger.error(msg)
    try:
        import sentry_sdk
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("user_id", str(user_id))
            scope.set_tag("job_type", request_type)
            scope.set_tag("model_id", model_id)
            sentry_sdk.capture_message(
                f"Background job stopped after {n} cut-off outputs: {job}",
                level="error")
    except Exception:  # pragma: no cover — reporting must not mask the refusal
        logger.exception("Sentry report of the stop failed")


def latest_profile_at(user_id):
    """created_at of the user's newest saved profile version (any kind)."""
    from backend.models import UserProfile
    row = (UserProfile.query.with_entities(UserProfile.created_at)
           .filter(UserProfile.user_id == user_id)
           .order_by(UserProfile.created_at.desc()).first())
    return row[0] if row else None


def latest_recent_context_at(user_id):
    """The streak boundary for recent context: its newest saved row, or
    the newest profile version if that is later (a new profile changes
    the recent context's input, so it gets a fresh try)."""
    from backend.models import UserRecentContext
    row = (UserRecentContext.query.with_entities(UserRecentContext.created_at)
           .filter(UserRecentContext.user_id == user_id)
           .order_by(UserRecentContext.created_at.desc()).first())
    times = [t for t in (row[0] if row else None,
                         latest_profile_at(user_id)) if t is not None]
    return max(times) if times else None


def profile_backoff_state(user_id):
    return backoff_state(user_id, PROFILE_REQUEST_TYPES,
                         latest_profile_at(user_id))


def recent_context_backoff_state(user_id):
    return backoff_state(user_id, RECENT_CONTEXT_REQUEST_TYPES,
                         latest_recent_context_at(user_id))


def profile_in_backoff(user_id, now=None):
    return in_backoff(user_id, PROFILE_REQUEST_TYPES,
                      latest_profile_at(user_id), "profile update", now=now)


def recent_context_in_backoff(user_id, now=None):
    return in_backoff(user_id, RECENT_CONTEXT_REQUEST_TYPES,
                      latest_recent_context_at(user_id), "recent context",
                      now=now)

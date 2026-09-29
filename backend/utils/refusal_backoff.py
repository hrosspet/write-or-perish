"""Backoff for background jobs after a refused model output (#368).

A background job that refuses a cut-off output saves nothing, so the gates
that decide when it runs again (interval since the last version, data since
its cutoff, an unfinished chain) stay open, and the next beat would repeat
the same billed call. Every refusal writes its cost row with
``request_ref = REFUSED_REF``; the refusals newer than the job's last saved
output are its consecutive failures. No extra column: a saved output ends
the streak by itself.

After refusal n (1-based) the job waits ``retry_wait(n)``:
  n < MAX_REFUSALS  ->  BACKOFF_BASE * BACKOFF_FACTOR ** (n - 1)   (1 h, 4 h)
  n >= MAX_REFUSALS ->  GIVE_UP_WAIT                               (7 days)
so at most MAX_REFUSALS calls in the first ~5 hours, then one a week until
an output is saved. All four numbers are best guesses (see PR #375).
"""
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

REFUSED_REF = "refused:truncated"

BACKOFF_BASE = timedelta(hours=1)
BACKOFF_FACTOR = 4
MAX_REFUSALS = 3
GIVE_UP_WAIT = timedelta(days=7)

PROFILE_REQUEST_TYPES = ("profile", "profile_batch")
RECENT_CONTEXT_REQUEST_TYPES = ("recent_context",)


def retry_wait(n):
    """How long to wait after the n-th consecutive refusal (n >= 1)."""
    if n >= MAX_REFUSALS:
        return GIVE_UP_WAIT
    return BACKOFF_BASE * (BACKOFF_FACTOR ** (n - 1))


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


def backoff_until(user_id, request_types, since):
    """(n, until): the number of consecutive refusals since the last saved
    output, and when the next attempt is allowed (None = now)."""
    times = refusals_since(user_id, request_types, since)
    if not times:
        return 0, None
    return len(times), times[0] + retry_wait(len(times))


def in_backoff(user_id, request_types, since, job, now=None):
    """True while the job must not run again for this user."""
    n, until = backoff_until(user_id, request_types, since)
    if until is None or (now or datetime.utcnow()) >= until:
        return False
    logger.info("User %s: %s backing off after %d refused output(s) until %s",
                user_id, job, n, until)
    return True


def latest_profile_at(user_id):
    """created_at of the user's newest saved profile version (any kind)."""
    from backend.models import UserProfile
    row = (UserProfile.query.with_entities(UserProfile.created_at)
           .filter(UserProfile.user_id == user_id)
           .order_by(UserProfile.created_at.desc()).first())
    return row[0] if row else None


def latest_recent_context_at(user_id):
    from backend.models import UserRecentContext
    row = (UserRecentContext.query.with_entities(UserRecentContext.created_at)
           .filter(UserRecentContext.user_id == user_id)
           .order_by(UserRecentContext.created_at.desc()).first())
    return row[0] if row else None


def profile_in_backoff(user_id, now=None):
    return in_backoff(user_id, PROFILE_REQUEST_TYPES,
                      latest_profile_at(user_id), "profile update", now=now)


def recent_context_in_backoff(user_id, now=None):
    return in_backoff(user_id, RECENT_CONTEXT_REQUEST_TYPES,
                      latest_recent_context_at(user_id), "recent context",
                      now=now)

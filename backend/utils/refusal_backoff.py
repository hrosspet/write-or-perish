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

Recent context runs through the Batch API (#380), where a request can
also fail without a billed output (the item errored or expired, the
batch ended without it or could not be read). Such a failure has no cost
row; the collector marks the item ``outcome: "failed"`` on its
RecentContextBatchJob, and it counts as one more strike in the same
streak, so a request that keeps failing is not resubmitted every 10
minutes either. Billed and unbilled failures stop differently:
  - STOP_AFTER billed failures in the streak (refused outputs, and
    billed results that could not be saved) stop the job until a new
    version, as above;
  - a stop reached with fewer billed failures (a provider outage, a batch
    unreadable until it is abandoned) lifts UNBILLED_STOP_EXPIRY after
    the newest failure. The next check then tries once more; a failure
    stops it for another UNBILLED_STOP_EXPIRY. An outage that has
    nothing to do with the user's input therefore does not stop their
    recent context until their next profile version, which can be weeks
    away; it is retried at most once a day.

A content-policy block (Peter, 2026-11-04) stops the job at once and for
good: the provider's filter refused the request (an error code in
llm_batch.OPENAI_POLICY_BLOCK_CODES, e.g. bio_policy), the same request
would be refused again, and every refused request counts against the
account. ``record_policy_block`` writes a $0 cost row with
``request_ref = "policy_block:<code>"`` (the block is not billed) and
reports it to Sentry. From then on the job is stopped for the user
(``policy_block_for``): no wait expires it, and neither does a saved
version, an import, a profile edit or another call of the job that goes
through (a window build's other windows), since the blocked writing would
go out again. Only ``lift_policy_block`` lifts it, with a row of its own:
the admin's confirmed Build profile for a profile, the one action that
sends a blocked request again. A request blocked again is a new block.
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy import or_

logger = logging.getLogger(__name__)

REFUSED_REF = "refused:truncated"

RETRY_WAIT = timedelta(hours=1)
STOP_AFTER = 2   # failures in a row
# Heuristic (#380 review, 2026-10-02): how long a stop lasts when fewer
# than STOP_AFTER of its failures were billed. Long enough that an outage
# costs at most one retry (one export rebuild and one request) per user
# per day; short enough that the user's recent context resumes the day
# after the outage ends.
UNBILLED_STOP_EXPIRY = timedelta(hours=24)

POLICY_BLOCK_REF_PREFIX = "policy_block:"
POLICY_BLOCK_LIFTED_REF = "policy_block_lifted"

PROFILE_REQUEST_TYPES = ("profile", "profile_batch")
RECENT_CONTEXT_REQUEST_TYPES = ("recent_context",)
DIGEST_REQUEST_TYPES = ("external_digest",)


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


def backoff_state(user_id, request_types, since, billed_failures=(),
                  unbilled_failures=()):
    """(n, until, stopped) for the streak since the last saved output.

    n: the failures in a row: the refused calls of these request types,
    plus ``billed_failures`` (times of billed runs that failed without a
    refusal cost row, e.g. a batch item with an empty output that was not
    cut off) and ``unbilled_failures`` (times of failures nobody was
    billed for, e.g. a batch item that errored at the provider or a batch
    abandoned unread). Both are already limited to after ``since``.
    stopped: True from the STOP_AFTER-th failure on.
    until: when the next attempt is allowed. None means now (n == 0) or,
    when stopped, never: at least STOP_AFTER of the failures were billed.
    Otherwise RETRY_WAIT after the newest failure, or UNBILLED_STOP_EXPIRY
    after it for a stop with fewer billed failures. A content-policy
    block (``policy_block_for``) is a stop that never lifts by itself."""
    if policy_block_for(user_id, request_types):
        n = len(refusals_since(user_id, request_types, since)) + 1
        return n, None, True
    billed = (refusals_since(user_id, request_types, since)
              + list(billed_failures))
    times = sorted(billed + list(unbilled_failures), reverse=True)
    n = len(times)
    if n == 0:
        return 0, None, False
    if n < STOP_AFTER:
        return n, times[0] + RETRY_WAIT, False
    if len(billed) >= STOP_AFTER:
        return n, None, True
    return n, times[0] + UNBILLED_STOP_EXPIRY, True


def in_backoff(user_id, request_types, since, job, now=None,
               billed_failures=(), unbilled_failures=()):
    """True while the job must not run for this user: waiting after the
    first failure, or stopped after the second (until the stop lifts, for
    a stop with fewer than STOP_AFTER billed failures), and stopped for
    good after a content-policy block (``policy_block_for``)."""
    code = policy_block_for(user_id, request_types)
    if code:
        logger.info("User %s: %s stopped: the provider's content-policy "
                    "filter blocked its last request (%s)", user_id, job,
                    code)
        return True
    n, until, stopped = backoff_state(user_id, request_types, since,
                                      billed_failures, unbilled_failures)
    if n == 0:
        return False
    if stopped and until is None:
        logger.info("User %s: %s stopped after %d refused outputs or "
                    "failed runs in a row", user_id, job, n)
        return True
    if (now or datetime.utcnow()) >= until:
        return False
    if stopped:
        logger.info("User %s: %s stopped until %s after %d refused outputs "
                    "or failed runs in a row", user_id, job, until, n)
    else:
        logger.info("User %s: %s waiting after a refused output or failed "
                    "run until %s", user_id, job, until)
    return True


def policy_block_ref(code):
    """The request_ref of a policy block's cost row."""
    return f"{POLICY_BLOCK_REF_PREFIX}{code}"[:64]


def policy_block_for(user_id, request_types):
    """The code of the content-policy block that stops this job for the
    user: a block row of these request types with no lift row
    (``lift_policy_block``) after it. None when there is none."""
    from backend.models import APICostLog
    q = (APICostLog.query.with_entities(APICostLog.request_ref)
         .filter(APICostLog.user_id == user_id,
                 APICostLog.request_type.in_(request_types),
                 or_(APICostLog.request_ref.like(
                     POLICY_BLOCK_REF_PREFIX + "%"),
                     APICostLog.request_ref == POLICY_BLOCK_LIFTED_REF)))
    fresh = _fresh_start(user_id)
    if fresh is not None:
        # A block of the content "Delete all my content" hid stops nothing
        # the account builds from what it writes since (#590); a restore
        # brings the content and its block back.
        q = q.filter(APICostLog.created_at > fresh)
    row = q.order_by(APICostLog.created_at.desc(),
                     APICostLog.id.desc()).first()
    ref = row[0] if row else None
    if ref and ref.startswith(POLICY_BLOCK_REF_PREFIX):
        return ref[len(POLICY_BLOCK_REF_PREFIX):] or "unknown"
    return None


def lift_policy_block(user_id, request_type, request_types):
    """The admin confirmed sending a blocked job's request again: write a
    $0 row (``request_ref = POLICY_BLOCK_LIFTED_REF``, no call) newer than
    the block, so ``policy_block_for`` no longer stops the job. A request
    blocked again records a new block. Returns the lifted code, or None
    when nothing was blocked (no row written). Commits."""
    from backend.extensions import db
    from backend.models import APICostLog
    code = policy_block_for(user_id, request_types)
    if not code:
        return None
    db.session.add(APICostLog(
        user_id=user_id, model_id="admin-override",
        request_type=request_type, input_tokens=0, output_tokens=0,
        cache_read_tokens=0, cache_write_tokens=0,
        request_ref=POLICY_BLOCK_LIFTED_REF, cost_microdollars=0))
    db.session.commit()
    logger.warning("User %s: the admin lifted the %s content-policy stop "
                   "(%s)", user_id, request_type, code)
    return code


def report_policy_block(user_id, code, model_id, job):
    """Log a content-policy block at ERROR and send a Sentry event with
    tags only (user id, code, model, job): never the request or the
    provider's message."""
    logger.error("Provider content-policy block (%s) for user %s: %s "
                 "(model %s); not sent again automatically", code, user_id,
                 job, model_id)
    try:
        import sentry_sdk
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("user_id", str(user_id))
            scope.set_tag("policy_code", str(code))
            scope.set_tag("model_id", str(model_id))
            scope.set_tag("job_type", str(job))
            sentry_sdk.capture_message(
                f"Provider content-policy block ({code}): {job}",
                level="error")
    except Exception:  # pragma: no cover — reporting must not mask the block
        logger.exception("Sentry report of the policy block failed")


def record_policy_block(user_id, request_type, model_id, code, job,
                        report=True):
    """Record a content-policy block of one of the user's requests: a $0
    cost row (the block is not billed) whose request_ref names the code,
    which stops the job (``policy_block_for``), then the report. Commits."""
    from backend.extensions import db
    from backend.models import APICostLog
    db.session.add(APICostLog(
        user_id=user_id, model_id=model_id or "unknown",
        request_type=request_type, input_tokens=0, output_tokens=0,
        cache_read_tokens=0, cache_write_tokens=0,
        request_ref=policy_block_ref(code), cost_microdollars=0))
    db.session.commit()
    if report:
        report_policy_block(user_id, code, model_id, job)


CUT_OFF_CAUSE = "output cut off"
REFUSED_CAUSE = "refused by the model"


def report_stop(user_id, job, n, model_id, request_type,
                cause=CUT_OFF_CAUSE, until=None):
    """The job is stopped for this user: log it at ERROR and send a
    tagged Sentry event, so someone looks at it. ``cause`` names what
    ended the runs (default: cut-off outputs). ``until``: when the stop
    lifts (a stop with fewer than STOP_AFTER billed failures); None =
    nothing retries it until a new version is saved."""
    msg = (f"{job} stopped for user {user_id}: {cause} {n} times "
           f"in a row (model {model_id}); " + (
               "no more automatic runs until a new version is saved"
               if until is None else
               f"fewer than {STOP_AFTER} of these were billed, so the "
               f"next automatic try is after {until}"))
    logger.error(msg)
    if until is not None:
        hours = int(UNBILLED_STOP_EXPIRY.total_seconds() // 3600)
        title = (f"Background job stopped for {hours} h after {n} runs "
                 f"in a row ({cause}): {job}")
    elif cause == CUT_OFF_CAUSE:
        title = f"Background job stopped after {n} cut-off outputs: {job}"
    else:
        title = (f"Background job stopped after {n} runs in a row "
                 f"({cause}): {job}")
    try:
        import sentry_sdk
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("user_id", str(user_id))
            scope.set_tag("job_type", request_type)
            scope.set_tag("model_id", model_id)
            scope.set_tag("stop_lifts", "never" if until is None
                          else "after_expiry")
            sentry_sdk.capture_message(title, level="error")
    except Exception:  # pragma: no cover — reporting must not mask the refusal
        logger.exception("Sentry report of the stop failed")


def _fresh_start(user_id):
    """When the user's waiting "Delete all my content" was requested, or
    None (hidden_rows.content_deletion_cutoff). The account starts afresh
    then (#590): a stop recorded on the hidden content (a content-policy
    block, a refusal streak) does not hold what it builds since."""
    from backend.utils.hidden_rows import content_deletion_cutoff
    return content_deletion_cutoff(user_id)


def _since_fresh_start(user_id, since):
    """A streak boundary, moved to the fresh start when that is later:
    the versions that ended earlier streaks are hidden, so without it every
    refusal in the account's history would count again (review of #630)."""
    fresh = _fresh_start(user_id)
    if fresh is None:
        return since
    return fresh if since is None or fresh > since else since


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


def recent_context_batch_failures_since(user_id, since):
    """(billed, unbilled): collection times of the user's recent-context
    batch items marked ``outcome: "failed"`` in jobs collected (or
    abandoned) after ``since`` (None = all), split by whether the item was
    billed (its cost row was written, ``billed: true``). A refused item is
    not among them: its refused cost row already counts."""
    from backend.models import RecentContextBatchJob
    q = RecentContextBatchJob.query.with_entities(
        RecentContextBatchJob.collected_at, RecentContextBatchJob.items,
    ).filter(RecentContextBatchJob.collected_at.isnot(None))
    if since is not None:
        q = q.filter(RecentContextBatchJob.collected_at > since)
    billed, unbilled = [], []
    for collected_at, items in q.all():
        for item in items or []:
            if (item.get("user_id") == user_id
                    and item.get("outcome") == "failed"):
                (billed if item.get("billed") else unbilled).append(
                    collected_at)
    return billed, unbilled


def profile_backoff_state(user_id):
    return backoff_state(user_id, PROFILE_REQUEST_TYPES,
                         _since_fresh_start(user_id,
                                            latest_profile_at(user_id)))


def recent_context_backoff_state(user_id):
    since = _since_fresh_start(user_id, latest_recent_context_at(user_id))
    return backoff_state(
        user_id, RECENT_CONTEXT_REQUEST_TYPES, since,
        *recent_context_batch_failures_since(user_id, since))


def latest_digest_at(user_id):
    """created_at of the user's newest saved external digest: the streak
    boundary for digest refusals (#470)."""
    from backend.models import UserArtifact
    row = (UserArtifact.query.with_entities(UserArtifact.created_at)
           .filter(UserArtifact.user_id == user_id,
                   UserArtifact.kind == "external_digest")
           .order_by(UserArtifact.created_at.desc()).first())
    return row[0] if row else None


def digest_backoff_state(user_id):
    return backoff_state(user_id, DIGEST_REQUEST_TYPES,
                         _since_fresh_start(user_id, latest_digest_at(user_id)))


def digest_in_backoff(user_id, now=None):
    return in_backoff(user_id, DIGEST_REQUEST_TYPES,
                      _since_fresh_start(user_id, latest_digest_at(user_id)),
                      "external digest", now=now)


def profile_in_backoff(user_id, now=None):
    return in_backoff(user_id, PROFILE_REQUEST_TYPES,
                      _since_fresh_start(user_id, latest_profile_at(user_id)),
                      "profile update", now=now)


def recent_context_in_backoff(user_id, now=None):
    since = _since_fresh_start(user_id, latest_recent_context_at(user_id))
    billed, unbilled = recent_context_batch_failures_since(user_id, since)
    return in_backoff(
        user_id, RECENT_CONTEXT_REQUEST_TYPES, since, "recent context",
        now=now, billed_failures=billed, unbilled_failures=unbilled)

"""Who started a background task, and what its failure may say.

A status route that takes a Celery task id from the client (an archive
import, a profile build) answers only for an id recorded here as the
caller's; any other id reads as unknown. The record is written when the
task is started, kept as long as Celery keeps the task's result (24 h,
celery_app result_expires), and lives in the Redis the broker uses.
Without Redis nothing is recorded and every id reads as unknown.

A failed task's text goes through failure_text: the message of an error
written for the user, else a fixed fallback. Never the raw exception,
which can carry SQL, file paths or a provider's response.
"""
import logging

logger = logging.getLogger(__name__)

# As long as Celery keeps a task's result (celery_app: result_expires).
OWNER_TTL_SECONDS = 86400
_PREFIX = "task-owner:"

_client = None


class UserFacingTaskError(RuntimeError):
    """A task failure whose message is written for the user who started
    the task; status routes return it as it is."""


# The errors whose message is written for the user. Matched by class name:
# Celery rebuilds a stored exception from its name and module, or as a
# stand-in class of the same name when the module isn't loaded.
USER_FACING_ERRORS = (
    "UserFacingTaskError",
    # llm_providers: a fixed message per class that names no provider.
    "ProviderAccountError",
    "ModelUnavailableError",
)


def _redis(config):
    global _client
    if config.get("TESTING"):
        return None
    if _client is None:
        url = config.get("CELERY_BROKER_URL")
        if not url:
            return None
        import redis
        _client = redis.Redis.from_url(
            url, socket_timeout=0.5, socket_connect_timeout=0.5)
    return _client


def record_task_owner(task_id, user_id, config):
    """Record *user_id* as the user who started *task_id*. Best effort:
    without Redis the task's status reads as unknown to everyone."""
    if not task_id or user_id is None:
        return
    try:
        client = _redis(config)
        if client is not None:
            client.setex(_PREFIX + str(task_id), OWNER_TTL_SECONDS,
                         str(user_id))
    except Exception:  # noqa: BLE001 - the task runs without the record
        logger.warning("Owner of task %s not recorded: Redis unavailable",
                       task_id, exc_info=True)


def task_owned_by(task_id, user_id, config):
    """True only when *task_id* was recorded as started by *user_id*."""
    if not task_id or user_id is None:
        return False
    try:
        client = _redis(config)
        raw = client.get(_PREFIX + str(task_id)) if client is not None \
            else None
    except Exception:  # noqa: BLE001 - unknown, as for any other id
        logger.warning("Owner of task %s not read: Redis unavailable",
                       task_id, exc_info=True)
        return False
    if raw is None:
        return False
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    return raw == str(user_id)


def failure_text(exc, fallback):
    """What a status route says about a task that failed with *exc*: its
    message when it was written for the user, else *fallback*."""
    if exc is not None and type(exc).__name__ in USER_FACING_ERRORS:
        text = str(exc).strip()
        if text:
            return text
    return fallback

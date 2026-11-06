"""Email the owner when the server disk is nearly full.

check_disk_and_alert measures the filesystem holding the audio storage
root (the server's root disk) with shutil.disk_usage. At or above
DISK_ALERT_PERCENT (default 90) it emails SPEND_ALERT_EMAIL, the ops
recipient the spend and provider alerts use.

Repeats are prevented with Redis keys, as in provider_alerts.py (SET NX
EX, so one email goes out across processes; without Redis, in tests or a
local run, each process keeps its own claims):

- ``loore:disk-alert:warn``: claimed for 24 h when the warning email goes
  out, so a disk that stays above the threshold gets one email a day;
- ``loore:disk-alert:critical``: claimed for 24 h when usage is at
  CRITICAL_PERCENT (95) or more, so crossing 95 sends again even inside
  the warning's 24 h. A critical email also claims the warning key, so
  falling from 96 to 92 does not send a second email.

When usage is below the threshold both keys are deleted (the state
resets), so the next crossing emails at once. A failed send releases its
claim and is retried on the next run.
"""
import logging
import shutil
import threading
import time

logger = logging.getLogger(__name__)

DEFAULT_PERCENT = 90.0
CRITICAL_PERCENT = 95.0
REPEAT_SECONDS = 24 * 3600
KEY_PREFIX = "loore:disk-alert:"
WARN, CRITICAL = "warn", "critical"
GB = 1024 ** 3

_local_claims = {}
_local_lock = threading.Lock()


def _redis(config):
    from backend.utils.provider_alerts import _redis as redis_client
    return redis_client(config)


def _claim(level, config):
    """True for the first claim of *level* in REPEAT_SECONDS."""
    name = KEY_PREFIX + level
    try:
        client = _redis(config)
        if client is not None:
            return bool(client.set(name, str(int(time.time())),
                                   nx=True, ex=REPEAT_SECONDS))
    except Exception:
        logger.warning("Disk alert: Redis unavailable; remembering in "
                       "this process only", exc_info=True)
    now = time.monotonic()
    with _local_lock:
        until = _local_claims.get(name)
        if until is not None and until > now:
            return False
        _local_claims[name] = now + REPEAT_SECONDS
        return True


def _mark_sent(level, config):
    """Claim *level* whether or not it was already claimed."""
    name = KEY_PREFIX + level
    try:
        client = _redis(config)
        if client is not None:
            client.set(name, str(int(time.time())), ex=REPEAT_SECONDS)
            return
    except Exception:
        logger.warning("Disk alert: Redis unavailable", exc_info=True)
    with _local_lock:
        _local_claims[name] = time.monotonic() + REPEAT_SECONDS


def _release(levels, config):
    for level in levels:
        name = KEY_PREFIX + level
        try:
            client = _redis(config)
            if client is not None:
                client.delete(name)
        except Exception:
            logger.warning("Disk alert: Redis unavailable", exc_info=True)
        with _local_lock:
            _local_claims.pop(name, None)


def disk_usage_path():
    """The audio storage root, or its nearest existing parent."""
    from backend.utils.audio_storage import AUDIO_STORAGE_ROOT
    path = AUDIO_STORAGE_ROOT
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def _threshold(config):
    try:
        return float(config.get("DISK_ALERT_PERCENT") or DEFAULT_PERCENT)
    except (TypeError, ValueError):
        return DEFAULT_PERCENT


def check_disk_and_alert(config, usage=None, send_email=None):
    """Check disk usage and email when due. Returns a summary dict.
    ``usage`` (a shutil.disk_usage-like (total, used, free)) and
    ``send_email`` are injectable for tests."""
    if not config.get("DISK_ALERT_ENABLED"):
        return {"status": "disabled"}
    if usage is None:
        usage = shutil.disk_usage(disk_usage_path())
    total, used, free = usage[0], usage[1], usage[2]
    # As df reports it: used / (used + free), which leaves out the blocks
    # reserved for root.
    percent = 100.0 * used / (used + free) if (used + free) else 0.0
    summary = {"status": "ok", "percent": round(percent, 1), "sent": False}
    if percent < _threshold(config):
        _release((WARN, CRITICAL), config)
        return summary

    level = CRITICAL if percent >= CRITICAL_PERCENT else WARN
    if not _claim(level, config):
        return summary
    if level == CRITICAL:
        _mark_sent(WARN, config)
    if send_email is None:
        from backend.utils.email import send_disk_alert_email
        send_email = send_disk_alert_email
    try:
        send_email(
            to_email=config.get("SPEND_ALERT_EMAIL") or "signup@loore.org",
            percent=percent, used_gb=used / GB, total_gb=total / GB,
            free_gb=free / GB, critical=level == CRITICAL)
    except Exception:
        logger.exception("Failed to send the disk alert email")
        _release((level,), config)
        return summary
    summary["sent"] = True
    summary["level"] = level
    return summary

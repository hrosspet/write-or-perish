"""Month-to-date API spend aggregation + spend-limit alerting (issue #85).

Core logic lives here (plain functions, app context assumed active) so it
is unit-testable without importing the Celery app. The hourly beat task in
backend/tasks/spend_monitor.py is a thin wrapper around check_and_alert().

Spend source of truth is APICostLog (cost_microdollars), which every
LLM/transcription/TTS task already writes. Provider attribution derives
from SUPPORTED_MODELS config with a "claude*" prefix fallback so rows for
models that have since been removed from config still count correctly.
"""
import logging
from datetime import datetime
from functools import wraps

from sqlalchemy import event, func, or_

from backend.extensions import db
from backend.models import APICostLog, SpendAlert

logger = logging.getLogger(__name__)

# Seconds to defer the per-user cap check after a cost row is inserted, so the
# inserting transaction has committed before we aggregate committed spend.
ENFORCE_CAP_DELAY_SECONDS = 15


class SpendCapExceeded(Exception):
    """Raised when a spend-blocked user would incur a cost-bearing action.
    Registered with a 402 error handler so any HTTP path that hits it
    surfaces the standard monthly_spend_limit_reached payload (→ banner)."""
    pass


def month_start(now=None):
    now = now or datetime.utcnow()
    return datetime(now.year, now.month, 1)


def current_month(now=None):
    """Current calendar month as "YYYY-MM" (UTC)."""
    return (now or datetime.utcnow()).strftime("%Y-%m")


def _anthropic_filter(config):
    ids = {
        model_id
        for model_id, cfg in (config.get("SUPPORTED_MODELS") or {}).items()
        if cfg.get("provider") == "anthropic"
    }
    return or_(APICostLog.model_id.in_(ids),
               APICostLog.model_id.like("claude%"))


def get_month_spend_microdollars(config, provider=None, now=None):
    """Month-to-date spend in microdollars, optionally filtered by provider
    ("anthropic" / "openai")."""
    q = db.session.query(
        func.coalesce(func.sum(APICostLog.cost_microdollars), 0)
    ).filter(APICostLog.created_at >= month_start(now))
    if provider == "anthropic":
        q = q.filter(_anthropic_filter(config))
    elif provider == "openai":
        q = q.filter(~_anthropic_filter(config))
    return int(q.scalar() or 0)


def parse_thresholds(raw):
    """Parse "0.5,0.8,0.95" into a sorted list of floats in (0, 1]."""
    thresholds = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = float(part)
        except ValueError:
            logger.warning("Ignoring invalid spend threshold %r", part)
            continue
        if 0 < value <= 1:
            thresholds.append(value)
    return sorted(set(thresholds))


def check_and_alert(config, now=None, send_email=None):
    """Compare month-to-date Anthropic spend against the configured limit
    and alert once per (month, threshold) crossed.

    Returns a dict summary (also useful as the Celery task result).
    `send_email` is injectable for tests; defaults to the real sender.
    """
    limit_usd = config.get("ANTHROPIC_SPEND_LIMIT_USD") or 0
    if limit_usd <= 0:
        return {"status": "disabled"}

    if send_email is None:
        from backend.utils.email import send_spend_alert_email
        send_email = send_spend_alert_email

    now = now or datetime.utcnow()
    month = now.strftime("%Y-%m")
    spend_usd = get_month_spend_microdollars(
        config, provider="anthropic", now=now) / 1_000_000
    thresholds = parse_thresholds(config.get("SPEND_ALERT_THRESHOLDS"))

    fired = []
    for threshold in thresholds:
        if spend_usd < limit_usd * threshold:
            continue
        already = SpendAlert.query.filter_by(
            provider="anthropic", month=month, threshold=threshold
        ).first()
        if already:
            continue
        try:
            send_email(
                to_email=config.get("SPEND_ALERT_EMAIL"),
                provider="anthropic",
                spend_usd=spend_usd,
                limit_usd=limit_usd,
                threshold=threshold,
            )
        except Exception:
            # Don't record the alert as sent — retry next run.
            logger.exception(
                "Failed to send spend alert (threshold %.2f)", threshold)
            continue
        db.session.add(SpendAlert(
            provider="anthropic", month=month,
            threshold=threshold, spend_usd=spend_usd,
        ))
        db.session.commit()
        fired.append(threshold)

    return {
        "status": "ok",
        "month": month,
        "spend_usd": round(spend_usd, 4),
        "limit_usd": limit_usd,
        "fired": fired,
    }


# --- Per-user monthly hard cap (issue #85 follow-up) -----------------------

def get_user_month_spend_microdollars(user_id, now=None):
    """Month-to-date spend in microdollars for one user, ALL providers."""
    q = db.session.query(
        func.coalesce(func.sum(APICostLog.cost_microdollars), 0)
    ).filter(
        APICostLog.user_id == user_id,
        APICostLog.created_at >= month_start(now),
    )
    return int(q.scalar() or 0)


def user_is_capped(user, now=None):
    """Cheap, query-light check of whether a user is currently spend-blocked.

    Pass a loaded User to avoid any query (the flag is already on the row);
    a bare id triggers a single primary-key lookup. The block auto-expires at
    month rollover because we compare the stored month to the current one.
    """
    from backend.models import User
    if user is None:
        return False
    if not isinstance(user, User):
        user = db.session.get(User, user)
        if user is None:
            return False
    blocked = user.spend_blocked_month
    return bool(blocked) and blocked == current_month(now)


def enforce_user_spend_cap(user_id, config, now=None, send_email=None):
    """Block a user whose month-to-date spend (all providers) has reached
    PER_USER_MONTHLY_LIMIT_USD, and alert the admin once. Idempotent within a
    month. Runs in its own transaction (safe to call out-of-band).

    `send_email` is injectable for tests; defaults to the real sender.
    """
    from backend.models import User
    limit_usd = config.get("PER_USER_MONTHLY_LIMIT_USD") or 0
    if limit_usd <= 0:
        return {"status": "disabled"}

    now = now or datetime.utcnow()
    month = current_month(now)
    user = db.session.get(User, user_id)
    if user is None:
        return {"status": "no_user", "user_id": user_id}
    if user.spend_blocked_month == month:
        return {"status": "already_blocked", "user_id": user_id, "month": month}

    spend_usd = get_user_month_spend_microdollars(user_id, now=now) / 1_000_000
    if spend_usd < limit_usd:
        return {
            "status": "ok", "user_id": user_id,
            "spend_usd": round(spend_usd, 4), "limit_usd": limit_usd,
            "blocked": False,
        }

    # Over the cap. Persist the block first (the critical effect), then alert
    # best-effort — a failed email must not leave the user un-blocked.
    user.spend_blocked_month = month
    db.session.commit()

    if send_email is None:
        from backend.utils.email import send_user_spend_block_email
        send_email = send_user_spend_block_email
    try:
        send_email(
            to_email=config.get("SPEND_ALERT_EMAIL") or "signup@loore.org",
            username=user.username,
            spend_usd=spend_usd,
            limit_usd=limit_usd,
        )
    except Exception:
        logger.exception(
            "Failed to send per-user spend block email for user %s", user_id)

    logger.warning(
        "User %s hard-blocked: $%.2f >= $%.2f cap for %s",
        user_id, spend_usd, limit_usd, month)
    return {
        "status": "blocked", "user_id": user_id, "month": month,
        "spend_usd": round(spend_usd, 4), "limit_usd": limit_usd,
        "blocked": True,
    }


def require_spend_headroom(fn):
    """Route decorator: reject cost-incurring requests from a capped user with
    402 + a clear message. Place below @login_required so current_user is set.
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        from flask import jsonify
        from flask_login import current_user
        if getattr(current_user, "is_authenticated", False) and \
                user_is_capped(current_user):
            return jsonify({
                "error": "monthly_spend_limit_reached",
                "message": (
                    "You've reached your monthly usage limit for the free "
                    "alpha. It resets at the start of next month."
                ),
            }), 402
        return fn(*args, **kwargs)
    return wrapper


@event.listens_for(APICostLog, "after_insert")
def _dispatch_user_cap_check(mapper, connection, target):
    """After any cost row is written, schedule a (deferred, out-of-band)
    per-user cap check. No-op unless the cap is enabled, so tests and the
    default config incur zero broker traffic."""
    try:
        from flask import current_app, has_app_context
        if not has_app_context():
            return
        if (current_app.config.get("PER_USER_MONTHLY_LIMIT_USD") or 0) <= 0:
            return
        if target.user_id is None:
            return
        from backend.tasks.spend_monitor import enforce_user_cap
        enforce_user_cap.apply_async(
            args=[target.user_id], countdown=ENFORCE_CAP_DELAY_SECONDS)
    except Exception:
        logger.exception("Failed to dispatch per-user spend cap check")

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

from sqlalchemy import func, or_

from backend.extensions import db
from backend.models import APICostLog, SpendAlert

logger = logging.getLogger(__name__)


def month_start(now=None):
    now = now or datetime.utcnow()
    return datetime(now.year, now.month, 1)


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

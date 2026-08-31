"""Admin Activity tab: per-user engagement since activation, measured
directly (writes, asks, voice) rather than through spend — spend mixes in
pre-fill, embeddings, summaries and batch profiles, none of which say
anything about whether the person came back.

Also the throttled ``touch_last_seen`` used by the app's before_request.
"""
from datetime import datetime, timedelta

from sqlalchemy import func

from backend.extensions import db
from backend.models import APICostLog, Node, User, UserProfile

TOUCH_INTERVAL = timedelta(minutes=5)
# Paths the frontend polls or hits on every page — they don't say where
# the person is, so they never overwrite last_seen_path (they still
# refresh last_seen_at).
UNINFORMATIVE_PREFIXES = ("/api/dashboard", "/api/notifications", "/api/ready",
                          "/api/health", "/api/terms", "/api/changelog",
                          "/api/updates")  # App.js fetches /updates once per load
# User-initiated cost rows — what the person actually did. Everything
# else in api_cost_log is automation (profile/batch/embedding/summaries,
# pre-fill, bookmarks, polls, cache warming).
USER_REQUEST_TYPES = ("conversation", "transcription", "tts", "embedding_query")
IMPORT_ORIGINS = ("twitter", "chatgpt", "claude", "markdown")


def touch_last_seen(user, path, now=None):
    """Record an authenticated request. Writes when the user moved to a new
    informative area (so a profile visit lands even when the app's
    /api/dashboard bootstrap fired a moment earlier), otherwise at most
    once per TOUCH_INTERVAL. Returns True when it wrote. Never raises."""
    now = now or datetime.utcnow()
    informative = bool(path) and not path.startswith(UNINFORMATIVE_PREFIXES)
    area = path[:128] if informative else None
    moved = informative and area != user.last_seen_path
    if not moved and user.last_seen_at and now - user.last_seen_at < TOUCH_INTERVAL:
        return False
    try:
        user.last_seen_at = now
        if informative:
            user.last_seen_path = area
        db.session.commit()
        return True
    except Exception:
        db.session.rollback()
        return False


def activated_at(user):
    return user.approved_at or user.accepted_terms_at or user.created_at


def _day_counts(query_col, filters, since):
    """{date: count} grouped by UTC day of ``query_col``."""
    day = func.date(query_col)
    rows = (db.session.query(day, func.count())
            .filter(*filters, query_col >= since)
            .group_by(day).all())
    out = {}
    for d, n in rows:
        key = d if isinstance(d, str) else d.isoformat()
        out[key[:10]] = n
    return out


def activity_report(days=14, now=None):
    """One row per approved, non-spam, non-placeholder user, sorted by
    last seen (never-seen last). Everything is counted since activation."""
    now = now or datetime.utcnow()
    today = now.date()
    day_keys = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    users = (User.profile_eligible_query()
             .filter(User.spam.is_(False)).all())
    # Who was seeded from X (cost rows) vs. CA (prefilled_handle only)
    x_seeded = {uid for (uid,) in db.session.query(APICostLog.user_id).filter(
        APICostLog.request_type == "x_prefill").distinct().all()}
    rows = []
    for u in users:
        since = activated_at(u)
        own = (Node.human_owner_id == u.id, Node.deleted_at.is_(None))
        writes_f = own + (Node.node_type == "user", Node.origin.is_(None))
        voice_f = writes_f + (Node.audio_original_url.isnot(None),)
        asks_f = (APICostLog.user_id == u.id, APICostLog.request_type == "conversation")
        writes_by_day = _day_counts(Node.created_at, writes_f, since)
        asks_by_day = _day_counts(APICostLog.created_at, asks_f, since)
        voice_by_day = _day_counts(Node.created_at, voice_f, since)
        active_days = sorted(set(writes_by_day) | set(asks_by_day))
        strip = [{"d": k, "w": writes_by_day.get(k, 0), "a": asks_by_day.get(k, 0),
                  "v": voice_by_day.get(k, 0), "pre": k < since.date().isoformat()}
                 for k in day_keys]
        act_day = since.date()
        d1 = (act_day + timedelta(days=1)).isoformat()
        d2_7 = {(act_day + timedelta(days=i)).isoformat() for i in range(2, 8)}
        voice_sec = db.session.query(func.coalesce(func.sum(APICostLog.audio_duration_seconds), 0)).filter(
            APICostLog.user_id == u.id, APICostLog.request_type == "transcription",
            APICostLog.created_at >= since).scalar() or 0
        user_spend = db.session.query(func.coalesce(func.sum(APICostLog.cost_microdollars), 0)).filter(
            APICostLog.user_id == u.id, APICostLog.request_type.in_(USER_REQUEST_TYPES),
            APICostLog.created_at >= since).scalar() or 0
        imports = Node.query.filter(*own, Node.origin.in_(IMPORT_ORIGINS),
                                    Node.created_at >= since).count()
        profiles_since = UserProfile.query.filter(
            UserProfile.user_id == u.id, UserProfile.created_at >= since).count()
        latest_profile = (UserProfile.query.filter_by(user_id=u.id)
                          .order_by(UserProfile.created_at.desc()).first())
        rows.append({
            "id": u.id, "username": u.username,
            "seeded": "x" if u.id in x_seeded else ("ca" if u.prefilled_handle else None),
            "prefill_consent": u.prefill_consent,
            "activated_at": since.isoformat() if since else None,
            "accepted_terms_at": u.accepted_terms_at.isoformat() if u.accepted_terms_at else None,
            "last_seen_at": u.last_seen_at.isoformat() if u.last_seen_at else None,
            "last_seen_path": u.last_seen_path,
            "strip": strip,
            "active_days": len(active_days),
            "days_since_activation": max((today - act_day).days, 0),
            "writes": sum(writes_by_day.values()),
            "asks": sum(asks_by_day.values()),
            "voice_nodes": sum(voice_by_day.values()),
            "voice_minutes": round(voice_sec / 60, 1),
            "imports": imports,
            "profile_versions_since_activation": profiles_since,
            "latest_profile_at": latest_profile.created_at.isoformat() if latest_profile else None,
            "user_spend_usd": user_spend / 1_000_000,
            "day1_return": d1 in active_days,
            "day7_return": any(d in d2_7 for d in active_days),
        })
    rows.sort(key=lambda r: (r["last_seen_at"] or "", r["accepted_terms_at"] or ""), reverse=True)
    return {"days": day_keys, "users": rows, "generated_at": now.isoformat()}

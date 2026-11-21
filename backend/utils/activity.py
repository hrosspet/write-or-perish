"""Admin Activity tab: per-user activity since activation, measured
directly (writes, asks, voice) rather than through spend — spend mixes in
pre-fill, embeddings, summaries and batch profiles, none of which say
anything about whether the person came back.

Also the throttled ``touch_last_seen`` used by the app's before_request.

The names of the artifacts a user creates are theirs: nobody else sees
them, admins included (Peter, 2026-11-20). The tab names only the kinds
Loore defines (UserArtifact.SYSTEM_KINDS) and counts the views of the
user's own artifacts together (``artifact_views``), and the last-seen
path drops the name (``redact_artifact_path``).
"""
import re
from datetime import datetime, timedelta

from sqlalchemy import func

from backend.extensions import db
from backend.models import (
    APICostLog, ApiToken, ArtifactView, Node, User, UserArtifact,
    UserProfile,
)

# How the tab names the artifacts a user created, all of them together.
USER_CREATED_LABEL = "user-created"

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
IMPORT_ORIGINS = ("twitter", "chatgpt", "claude", "markdown", "docx", "pdf")

# /api/artifacts/<name>[/...]: the routes that carry an artifact's name.
_ARTIFACT_PATH = re.compile(r"^/api/artifacts/([^/]+)(/.*)?$")
# What the artifact routes put after the name; anything else is dropped.
_ARTIFACT_PATH_TAIL = re.compile(r"(?:/viewed|/versions|/revert/\d+)?/?")
# /api/artifacts/versions/<id>[/changes]: a version's id, no name.
_VERSION_PATH_TAIL = re.compile(r"/\d+(?:/changes)?/?")


def redact_artifact_path(path):
    """*path* without the name of an artifact the user created:
    /api/artifacts/<name>/versions becomes
    /api/artifacts/user-created/versions. A name Loore defines stays,
    and so do the routes of a version by id. Other paths come back as
    they are. Fails closed: a name not on UserArtifact.SYSTEM_KINDS is
    replaced, and an unknown tail after the name is dropped."""
    if not path:
        return path
    m = _ARTIFACT_PATH.match(path)
    if not m:
        return path
    name, tail = m.group(1), m.group(2) or ""
    if name == "versions" and _VERSION_PATH_TAIL.fullmatch(tail):
        return path
    if name not in UserArtifact.SYSTEM_KINDS:
        name = USER_CREATED_LABEL
    if not _ARTIFACT_PATH_TAIL.fullmatch(tail):
        tail = ""
    return f"/api/artifacts/{name}{tail}"


def touch_last_seen(user, path, now=None):
    """Record an authenticated request. Writes when the user moved to a new
    informative area (so a profile visit lands even when the app's
    /api/dashboard bootstrap fired a moment earlier), otherwise at most
    once per TOUCH_INTERVAL. Returns True when it wrote. Never raises."""
    now = now or datetime.utcnow()
    informative = bool(path) and not path.startswith(UNINFORMATIVE_PREFIXES)
    area = redact_artifact_path(path)[:128] if informative else None
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


# last_seen_at arrived with migration 0c7843c6ffd3 (created 2026-08-28,
# deployed the next day); use of an account before that left no record.
LAST_SEEN_SINCE = datetime(2026, 8, 29)


def sign_in_status(user):
    """Whether anyone has ever signed into this account, as far as the
    record can tell — the question behind "is this a placeholder?".

    "signed-in": a last-seen or a terms acceptance is on record.
    "never": neither, and the account postdates the last-seen record, so
    any sign-in would have been recorded (X logins stamp it; every app
    request does too).
    "unknown": neither, but the account predates the record — an early X
    signup who never accepted the terms looks exactly like a placeholder
    here. Callers must not treat it as one."""
    if user.last_seen_at is not None or user.accepted_terms_at is not None:
        return "signed-in"
    if user.created_at is None or user.created_at < LAST_SEEN_SINCE:
        return "unknown"
    return "never"


def activated_at(user):
    return user.approved_at or user.accepted_terms_at or user.created_at


# ── Accounts nobody uses (#647) ─────────────────────────────────────────
# Peter, 2026-11-12: background work that bills an account pauses after
# about a month without use (UNUSED_ACCOUNT_PAUSE_DAYS, 30 days) and
# catches up once when the owner is back. Paused: the nightly X bookmark
# sync (tasks/external_sync) and the saved-references digest
# (tasks/external_digest), the two jobs that bill an account with no new
# writing in it.
#
# Use is read from two records that only the owner's own actions write:
#
# - User.last_seen_at: a request of the signed-in app (web or iPhone),
#   written by the before_request hook (touch_last_seen above), and the
#   sign-in and an account restore. The apps run no timer that calls the
#   server while they sit idle: every request comes from opening a page,
#   writing, recording, playing a reply, or a task the owner started
#   (an upload, an import, a reply being written), and each such poll
#   ends with its task. Jobs run outside a request and never write it,
#   and an admin's requests write only the admin's own record.
# - ApiToken.last_used_at: the Chrome clipper's token, sent only when the
#   owner clips a page or checks the token in the extension's options.
#
# Not use: cost rows (the user-initiated ones come with an app request,
# the rest are jobs), a profile build, a sync, a sent notification, an
# admin's approval or pre-fill. A pre-filled account nobody has signed
# into has no last_seen_at, so it counts as unused from the start.

DEFAULT_UNUSED_PAUSE_DAYS = 30.0


def unused_pause_window(config):
    """How long an account may go without use before its billed background
    work pauses, or None when the pause is off (a setting of 0)."""
    days = config.get("UNUSED_ACCOUNT_PAUSE_DAYS", DEFAULT_UNUSED_PAUSE_DAYS)
    try:
        days = float(days)
    except (TypeError, ValueError):
        days = DEFAULT_UNUSED_PAUSE_DAYS
    return timedelta(days=days) if days > 0 else None


def in_use_user_ids(user_ids, config, now=None):
    """The ids among *user_ids* whose owner used Loore within the pause
    window (see above); all of them when the pause is off. Two indexed
    queries, whatever the number of ids."""
    ids = {uid for uid in user_ids if uid is not None}
    window = unused_pause_window(config)
    if window is None or not ids:
        return ids
    since = (now or datetime.utcnow()) - window
    id_list = sorted(ids)
    seen = {uid for (uid,) in db.session.query(User.id).filter(
        User.id.in_(id_list), User.last_seen_at >= since).all()}
    clipped = {uid for (uid,) in db.session.query(ApiToken.user_id).filter(
        ApiToken.user_id.in_(id_list), ApiToken.last_used_at >= since,
    ).distinct().all()}
    return seen | clipped


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


def artifact_views(user_id, since):
    """The user's artifact views since *since*, most recent first.

    One entry per kind Loore defines: {"kind", "count", "last"}. One
    entry for all the artifacts the user created together:
    {"kind": USER_CREATED_LABEL, "user_created": True, "count": views,
    "artifacts": how many different artifacts, "last"}. Their names
    never leave this function: a row from before 2026-11-20 holds one,
    and is matched to its artifact here only so that the artifact is
    counted once with its newer rows."""
    rows = (db.session.query(ArtifactView.kind, ArtifactView.artifact_id,
                             func.count(), func.max(ArtifactView.viewed_at))
            .filter(ArtifactView.user_id == user_id,
                    ArtifactView.viewed_at >= since)
            .group_by(ArtifactView.kind, ArtifactView.artifact_id).all())
    entries = []
    views, last_view, artifacts, named = 0, None, set(), []
    for kind, artifact_id, n, last in rows:
        if artifact_id is None and kind in UserArtifact.SYSTEM_KINDS:
            entries.append({"kind": kind, "count": n,
                            "last": last.isoformat() if last else None})
            continue
        views += n
        if last is not None and (last_view is None or last > last_view):
            last_view = last
        if artifact_id is not None:
            artifacts.add(artifact_id)
        else:
            named.append(kind)
    if named:
        first_ids = dict(
            db.session.query(UserArtifact.kind, func.min(UserArtifact.id))
            .filter(UserArtifact.user_id == user_id,
                    UserArtifact.deleted_at.is_(None),
                    UserArtifact.kind.in_(named))
            .group_by(UserArtifact.kind).all())
        # A name with no live artifact left counts as one artifact.
        artifacts.update(first_ids.get(k, ("gone", k)) for k in named)
    if views:
        entries.append({
            "kind": USER_CREATED_LABEL, "user_created": True,
            "count": views, "artifacts": len(artifacts),
            "last": last_view.isoformat() if last_view else None})
    return sorted(entries, key=lambda v: v["last"] or "", reverse=True)


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
            # Paths stored before 2026-11-20 can hold an artifact's name.
            "last_seen_path": redact_artifact_path(u.last_seen_path),
            "strip": strip,
            "active_days": len(active_days),
            "days_since_activation": max((today - act_day).days, 0),
            "writes": sum(writes_by_day.values()),
            "asks": sum(asks_by_day.values()),
            "voice_nodes": sum(voice_by_day.values()),
            "voice_minutes": round(voice_sec / 60, 1),
            "imports": imports,
            "artifact_views": artifact_views(u.id, since),
            "profile_versions_since_activation": profiles_since,
            "latest_profile_at": latest_profile.created_at.isoformat() if latest_profile else None,
            "user_spend_usd": user_spend / 1_000_000,
            "day1_return": d1 in active_days,
            "day7_return": any(d in d2_7 for d in active_days),
        })
    rows.sort(key=lambda r: (r["last_seen_at"] or "", r["accepted_terms_at"] or ""), reverse=True)
    return {"days": day_keys, "users": rows, "generated_at": now.isoformat()}

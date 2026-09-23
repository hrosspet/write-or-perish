"""Liveness of a streaming recording session (#320).

A Draft with ``streaming_status == 'recording'`` is either being recorded
right now by some tab, or was left behind (page reload, crash, SPA
navigation) and is waiting to be recovered. Only the second kind may be
handed to another view: a second tab that recovers a live session
auto-completes it, the recording tab's SSE then reports ``all_complete``
and the half-spoken turn is sent to the LLM while the recorder keeps
uploading into a dead session.

The recording tab proves it is still there in two ways, both stamping
``Draft.streaming_heartbeat_at``:

- every accepted audio chunk (every 15 s while recording), and
- the transcription SSE stream: on connect, and on each heartbeat (every
  ~15 s while the stream is open). The stream stays open while the
  recording is paused, so a paused recording stays live as long as its
  tab is open.

The tab clears the stamp when it abandons the session (pagehide beacon,
unmount or cancel mid-recording), which makes a reload offer recovery at
once instead of after the window below.

A client that vanishes without closing its socket (laptop lid closed,
network dropped) is different: the server's heartbeat writes keep
succeeding into the socket buffer until TCP gives up on the connection,
roughly 15 minutes with Linux defaults, and the session stays live until
then. Nothing is lost; the session is recoverable after that. A tab
crash or a killed app closes the socket and falls back to the window.
"""
from datetime import datetime, timedelta

from backend.extensions import db
from backend.models import Draft

# A 'recording' session stamped within this many seconds is live.
# Three times the 15 s chunk / SSE heartbeat interval, the same margin
# the client uses before it calls its own SSE stream stale. A heuristic:
# it trades how long a crashed tab's session stays hidden from recovery
# against how many missed stamps a live tab survives.
LIVE_SESSION_WINDOW_SEC = 45


def _cutoff(now=None):
    return (now or datetime.utcnow()) - timedelta(
        seconds=LIVE_SESSION_WINDOW_SEC)


def session_is_live(draft, now=None):
    """True while a tab is recording this session."""
    return (
        draft.streaming_status == "recording"
        and draft.streaming_heartbeat_at is not None
        and draft.streaming_heartbeat_at >= _cutoff(now)
    )


def not_live_clause(now=None):
    """SQL filter keeping every Draft except live recording sessions."""
    return db.or_(
        Draft.session_id.is_(None),
        Draft.streaming_status.is_(None),
        Draft.streaming_status != "recording",
        Draft.streaming_heartbeat_at.is_(None),
        Draft.streaming_heartbeat_at < _cutoff(now),
    )


def stamp_session_alive(session_id, only_if_unreleased=False, now=None):
    """Record a sign of life from the recording tab.

    A Core UPDATE that also sets ``updated_at`` to itself, so the
    column's onupdate does not fire: several routes pick "the most
    recent draft" by ``updated_at``, and a heartbeat is not an edit.

    ``only_if_unreleased`` is for the SSE heartbeat and chunk uploads:
    after the tab released the session (stamp cleared), a stream that has
    not noticed its client left, or an upload still in flight, must not
    make the session live again. Only init and a new SSE connection (the
    tab that resumes the session) stamp unconditionally.
    """
    query = Draft.query.filter(
        Draft.session_id == session_id,
        Draft.streaming_status == "recording",
    )
    if only_if_unreleased:
        query = query.filter(Draft.streaming_heartbeat_at.isnot(None))
    query.update(
        {
            Draft.streaming_heartbeat_at: now or datetime.utcnow(),
            Draft.updated_at: Draft.updated_at,
        },
        synchronize_session=False,
    )


def release_session(session_id, user_id):
    """The recording tab left: clear the stamp so the session is
    recoverable now rather than after LIVE_SESSION_WINDOW_SEC."""
    return Draft.query.filter(
        Draft.session_id == session_id,
        Draft.user_id == user_id,
        Draft.streaming_status == "recording",
    ).update(
        {
            Draft.streaming_heartbeat_at: None,
            Draft.updated_at: Draft.updated_at,
        },
        synchronize_session=False,
    )

"""The device that records a streaming session (#388).

A recording left behind by its device (no sign of life within the
liveness window, or released: utils/streaming_session.py, #320) can be
continued on another device or in another tab ("Continue recording" on
the Voice page). The first device may still be alive: an iPhone whose
paused recording sat in the background, a laptop closed mid-recording.
Before #388 both devices then uploaded chunks with the same indexes, the
server kept whichever copy arrived first, and part of what the user said
was lost with no error on either device.

Each recording client sends its own key, ``device_key``: the web app one
per page load, the iPhone app one per install. Init stores it as the
session's writer. A device that continues the session claims it first
(POST /drafts/streaming/<id>/claim), which makes its key the writer.
Chunk uploads, finalize, release and the transcription stream sent with
any other key get 409 ``session_taken_over``: that device stops
recording and says the recording continued elsewhere. Its audio stays
where the client keeps it (the web recorder's blob, "Save audio" in Text
mode).

A request without a key comes from a client built before #388 and is
handled as before, with no check. So is a session whose init sent none,
until a device claims it.
"""
from datetime import datetime

from flask import jsonify, request

from backend.extensions import db
from backend.models import Draft

TAKEN_OVER = "session_taken_over"

TAKEN_OVER_ERROR = (
    "This recording was continued in another tab or on another device."
)

# Draft.streaming_device_key's column size.
MAX_DEVICE_KEY_LENGTH = 64


def valid_device_key(key):
    """A key init or a claim may store: a non-empty string that fits."""
    return isinstance(key, str) and 0 < len(key) <= MAX_DEVICE_KEY_LENGTH


def request_device_key():
    """The key the current request carries, or None: from a JSON body
    (init, claim, finalize), a form field (chunk uploads) or the query
    string (the transcription stream, release)."""
    data = request.get_json(silent=True)
    key = data.get("device_key") if isinstance(data, dict) else None
    if key is None:
        key = request.values.get("device_key")
    return key if key not in (None, "") else None


def is_taken_over(draft, key):
    """True when *key* names a device other than the session's writer.
    No key (an older client) or no writer (an older session): False."""
    return (key is not None
            and draft.streaming_device_key is not None
            and key != draft.streaming_device_key)


def is_taken_over_locked(draft, key):
    """is_taken_over, read again with the draft row locked until the
    request's transaction ends. A claim takes the same lock before it
    counts the session's chunks, so a chunk is either stored before the
    claim (the continuing device starts after it) or refused after it,
    never stored under an index the continuing device records too."""
    if key is None:
        return False
    writer = db.session.query(Draft.streaming_device_key).filter(
        Draft.id == draft.id).with_for_update().scalar()
    return writer is not None and key != writer


def taken_over_response():
    return jsonify({"error": TAKEN_OVER_ERROR, "code": TAKEN_OVER}), 409


def claim_session(draft, key, now=None):
    """Make *key* the session's writer and stamp the session alive, so no
    third view recovers it while the claiming device starts recording.
    A Core UPDATE that keeps ``updated_at`` (as stamp_session_alive): a
    claim is not an edit of the draft."""
    Draft.query.filter(Draft.id == draft.id).update(
        {
            Draft.streaming_device_key: key,
            Draft.streaming_heartbeat_at: now or datetime.utcnow(),
            Draft.updated_at: Draft.updated_at,
        },
        synchronize_session=False,
    )

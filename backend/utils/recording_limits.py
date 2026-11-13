"""Limits on recording uploads and on transcription per recording.

Audio reaches the server in two ways:

- A streaming recording (dictation and Voice:
  ``/api/drafts/streaming/<session>/audio-chunk``; the older
  ``/api/nodes/<id>/audio-chunk``) arrives in chunks while it is recorded.
  Both apps cut a chunk every 15 s of audio.
- An audio file (``/api/nodes/upload/init|chunk|finalize``, or one request
  to ``POST /api/nodes/``) arrives whole or in 5 MB pieces, after an init
  that names its size and its piece count.

Every number below is far above what the web and iPhone apps send, so a
real recording never meets one, and each is checked by the routes and
tasks themselves, for every account. What the apps send (measured
2026-11-13):

- iPhone: AAC at 64 kbps, 48 kHz mono (FMP4SegmentWriter), one chunk
  per 15 s: about 120 KB a chunk, 8 KB a second.
- Web: MediaRecorder at the browser's default, a 128 kbps target. Chrome
  recording speech: 234-242 KB per 15 s chunk of Opus in WebM; in MP4
  (AAC) it sometimes hands two slices over in one chunk, 486 KB. 16 KB a
  second.

A recording accepted at its start (where the monthly usage limit is
checked, #341) is stored and transcribed to its end (Peter, 2026-11-13:
"User's recorded message should be stored on Loore's servers, possibly
even transcribed (transcription is cheap). Just further AI use should be
blocked"), within these limits:

- One recording is transcribed up to MAX_TRANSCRIBED_SECONDS of audio.
  The audio past it is still stored and kept with the recording; its
  chunks end ``failed`` with NOT_TRANSCRIBED_TEXT, so the finalize waits
  for them no longer, and the recording's text ends with a note
  (untranscribed_note).
- Its audio is refused only past bounds no recording made in the apps
  reaches: a chunk over MAX_STREAMING_CHUNK_BYTES, a chunk index from
  MAX_STREAMING_CHUNKS on, or more than MAX_STREAMING_SESSION_BYTES in
  all. Audio accepted before stays.
- An account starts no new recording while MAX_RECORDINGS_IN_PROGRESS of
  its recordings are in progress, or while it holds
  MAX_UNFINISHED_RECORDINGS unfinished ones; and no new audio upload while
  MAX_UPLOADS_IN_PROGRESS of its uploads wait for their transcript. Each
  start takes the account's lock and counts again inside it
  (lock_account), so two starts at once cannot both pass.
- A recording that has not shown a sign of life for CONTINUES_WITHIN (or
  that its app released) and takes audio again (a claim, a chunk, its
  transcription stream reconnecting) is checked as a start is: the usage
  limit and the bounds above (revival_refusal). One that kept going is
  not (#341).
"""
import os
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

# Seconds of audio in one streaming chunk, in both apps.
STREAMING_CHUNK_SECONDS = 15

# The rate the byte limits allow for: 192 kbps, 1.5 times the web app's
# (Chrome's default MediaRecorder target is 128 kbps, measured at 16.2 KB a
# second with its container), in case a browser records at a higher default.
BOUND_BYTES_PER_SECOND = 192_000 // 8

# INTRODUCED HEURISTIC: the largest streaming chunk accepted. The largest
# chunk measured from the apps is 486 KB (Chrome, AAC in MP4, two slices in
# one chunk); 8 MB is 16 times that, and holds over 8 minutes of audio at the
# web's rate in case a browser holds slices back and hands them over at once.
MAX_STREAMING_CHUNK_BYTES = 8 * 1024 * 1024

# INTRODUCED HEURISTIC: the most audio of one recording that is turned into
# text. The apps set no maximum; they warn at 59 minutes (#127, which
# reports a recording of 62 minutes). Six hours is far past that and bounds
# what one recording can cost to transcribe.
MAX_TRANSCRIBED_SECONDS = 6 * 60 * 60

# INTRODUCED HEURISTIC: chunks one recording may have (a chunk_index must be
# below it): twice the chunks of the longest transcribed recording, so the
# shorter chunks that pausing, a locked phone or a resume cut never reach it,
# and a recording that runs past six hours keeps its audio. 2,880 chunks;
# 12 hours at 15 s a chunk.
MAX_STREAMING_CHUNKS = 2 * MAX_TRANSCRIBED_SECONDS // STREAMING_CHUNK_SECONDS

# INTRODUCED HEURISTIC: audio bytes one recording may store: all its chunks
# (12 hours) at BOUND_BYTES_PER_SECOND, about 1 GB. Twelve hours from the
# web app are about 700 MB, from the iPhone about 350 MB.
MAX_STREAMING_SESSION_BYTES = (
    MAX_STREAMING_CHUNKS * STREAMING_CHUNK_SECONDS * BOUND_BYTES_PER_SECOND)

# INTRODUCED HEURISTIC: the largest piece of an uploaded file accepted. Both
# apps send 5 MB pieces (utils/chunkedUpload.js, NodeFormModel), the last one
# smaller; 8 MB leaves room for a client with somewhat larger pieces.
MAX_UPLOAD_CHUNK_BYTES = 8 * 1024 * 1024

# INTRODUCED HEURISTIC: the most pieces an upload may announce: a file of the
# largest size (routes/nodes.MAX_AUDIO_BYTES, 200 MB) in 1 MB pieces. The apps
# announce 40 for a 200 MB file.
MAX_UPLOAD_CHUNKS = 200

# Room for the multipart form around a chunk (its fields and boundaries)
# when a request's Content-Length is checked before the form is read.
FORM_OVERHEAD_BYTES = 64 * 1024

# INTRODUCED HEURISTIC: recordings (dictation and Voice) one account may
# have in progress at once; a new one is refused while this many are. In
# progress: being recorded (live, utils/streaming_session: a sign of life
# within 45 s; a left-behind or crashed recording stops counting then, and
# at once when its app released it), or finalizing, until its transcript
# is in. Legitimate use is one device recording, plus a second device or an
# earlier session not released yet.
MAX_RECORDINGS_IN_PROGRESS = 3
# A finalizing recording counts for at most this long after its last
# change: its finalize waits up to 10 minutes for its chunks (the task's
# own limit), so one whose task died stops counting after this.
FINALIZING_COUNTS_FOR = timedelta(minutes=30)

# INTRODUCED HEURISTIC: unfinished recordings one account may hold at all,
# live or not: recording (with audio stored, or live) or finalizing, with a
# sign of life within UNFINISHED_COUNTS_FOR. A recording an app left behind
# (killed mid-recording) stays offered for recovery; it stops counting here
# once it has been quiet that long, and when the user saves, discards or
# continues and finishes it. A started recording with no audio stored (left
# before its first chunk) never counts. Six is twice the recordings in
# progress: room for a few interrupted recordings not recovered yet.
MAX_UNFINISHED_RECORDINGS = 6
UNFINISHED_COUNTS_FOR = timedelta(hours=24)

# INTRODUCED HEURISTIC: a recording whose last sign of life is within this
# keeps going without a check when its audio comes in again (a dropped
# connection, a phone call, an app relaunched); after it, or once its app
# released it, it is checked as a start is (revival_refusal).
CONTINUES_WITHIN = timedelta(minutes=10)

# INTRODUCED HEURISTIC: audio uploads (chunked or in one request) and older
# node-path recordings of one account that may wait for their transcript at
# once. The apps upload one file per press; three leaves room for presses in
# quick succession.
MAX_UPLOADS_IN_PROGRESS = 3
# INTRODUCED HEURISTIC: an upload (or older node-path recording) counts for
# this long after it started, and takes pieces only within it: a 200 MB file
# in six hours is 74 kbit/s, slower than any connection the apps upload on.
# One the apps report as failed (/upload/cleanup) stops counting at once.
UPLOAD_COUNTS_FOR = timedelta(hours=6)

TOO_MANY_RECORDINGS_TEXT = (
    "Other recordings of yours are still in progress. Finish one of them, "
    "then start this one.")
TOO_MANY_UNFINISHED_TEXT = (
    "You have several unfinished recordings. Save or discard one of them, "
    "then start a new one.")
TOO_MANY_UPLOADS_TEXT = (
    "Other audio files of yours are still being uploaded or turned into "
    "text. Wait for one of them to finish, then upload this one.")
UPLOAD_EXPIRED_TEXT = ("This upload took too long to arrive. Please "
                       "upload the file again.")
UPLOAD_FAILED_TEXT = "The upload did not finish. Please upload the file again."
RECORDINGS_BUSY_TEXT = ("Loore is busy starting another recording of "
                        "yours. Try again in a moment.")

# Heuristic, as utils/todo_lock: how long a start waits for another start of
# the same account to finish counting and inserting (well under a second).
LOCK_WAIT_SECONDS = 10
# The first key of the two-key advisory lock: a fixed number naming "an
# account's recordings and uploads", so it cannot collide with another
# advisory lock keyed on a user id (utils/todo_lock uses 477001).
_LOCK_NAMESPACE = 694001
# PostgreSQL's SQLSTATE for "lock_timeout reached" (lock_not_available).
_LOCK_NOT_AVAILABLE = "55P03"

# Why a chunk's audio is kept without its text: the code the transcription
# stream sends with the chunk (``chunk_error``), so the apps tell it apart
# from a failure.
TOO_LONG = "recording_too_long"

# NodeTranscriptChunk.error of a chunk kept without its text.
NOT_TRANSCRIBED_TEXT = ("Saved as audio, not turned into text: the "
                        "recording is longer than six hours.")

# The note at the end of a recording's text: some of it was transcribed,
# none of it was.
NOTE_PART = ("*Note: The recording is longer than six hours, so the rest "
             "of it was not turned into text. Its audio is saved.*")
NOTE_ALL = ("*Note: The recording is longer than six hours, so it was not "
            "turned into text. Its audio is saved.*")


def body_too_large(max_bytes):
    """True when the request says its body is larger than a chunk of
    *max_bytes* in its form can be. Asked before the form is read; a
    request without a Content-Length is measured once it is stored."""
    from flask import request
    length = request.content_length
    return length is not None and length > max_bytes + FORM_OVERHEAD_BYTES


def stored_bytes(folder, leave_out=()):
    """Bytes of the files in *folder*: a recording's or an upload's audio
    so far. Files an upload is still writing (``upload-*``) and the names
    in *leave_out* (the copy a re-sent chunk replaces) are not counted."""
    total = 0
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return 0
    for entry in entries:
        if entry.name.startswith("upload-") or entry.name in leave_out:
            continue
        try:
            if entry.is_file(follow_symlinks=False):
                total += entry.stat(follow_symlinks=False).st_size
        except OSError:
            continue
    return total


def int_in_range(value, low, high):
    """*value* as an int when it is one (not a bool, not a float) with
    low <= value <= high, else None."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if low <= value <= high else None


def _live_since(now):
    from backend.utils.streaming_session import LIVE_SESSION_WINDOW_SEC
    return now - timedelta(seconds=LIVE_SESSION_WINDOW_SEC)


def _sessions(user_id, exclude_draft_id):
    from backend.models import Draft
    query = Draft.query.filter(Draft.user_id == user_id,
                               Draft.session_id.isnot(None))
    if exclude_draft_id is not None:
        query = query.filter(Draft.id != exclude_draft_id)
    return query


def recordings_in_progress(user_id, now=None, exclude_draft_id=None):
    """The account's recordings in progress (MAX_RECORDINGS_IN_PROGRESS):
    live ones, and finalizing ones changed within FINALIZING_COUNTS_FOR."""
    from backend.extensions import db
    from backend.models import Draft
    now = now or datetime.utcnow()
    return _sessions(user_id, exclude_draft_id).filter(
        db.or_(
            db.and_(Draft.streaming_status == "recording",
                    Draft.streaming_heartbeat_at >= _live_since(now)),
            db.and_(Draft.streaming_status == "finalizing",
                    Draft.updated_at >= now - FINALIZING_COUNTS_FOR),
        ),
    ).count()


def unfinished_recordings(user_id, now=None, exclude_draft_id=None):
    """The account's unfinished recordings (MAX_UNFINISHED_RECORDINGS):
    recording or finalizing, live or not, with a sign of life within
    UNFINISHED_COUNTS_FOR; a recording that is neither live nor finalizing
    counts only with audio stored."""
    from backend.extensions import db
    from backend.models import Draft, NodeTranscriptChunk
    now = now or datetime.utcnow()
    has_audio = db.exists().where(
        NodeTranscriptChunk.session_id == Draft.session_id)
    last_sign = db.func.coalesce(Draft.streaming_heartbeat_at,
                                 Draft.updated_at, Draft.created_at)
    return _sessions(user_id, exclude_draft_id).filter(
        Draft.streaming_status.in_(("recording", "finalizing")),
        last_sign >= now - UNFINISHED_COUNTS_FOR,
        db.or_(Draft.streaming_status == "finalizing",
               Draft.streaming_heartbeat_at >= _live_since(now),
               has_audio),
    ).count()


def uploads_in_progress(user_id, now=None):
    """The account's audio entries waiting for their transcript
    (MAX_UPLOADS_IN_PROGRESS): transcription pending or processing, started
    within UPLOAD_COUNTS_FOR."""
    from backend.models import Node
    now = now or datetime.utcnow()
    return Node.query.filter(
        Node.user_id == user_id,
        Node.transcription_status.in_(("pending", "processing")),
        Node.created_at >= now - UPLOAD_COUNTS_FOR,
    ).count()


def upload_expired(node, now=None):
    """True for an upload (or older node-path recording) started more than
    UPLOAD_COUNTS_FOR ago: it no longer counts, and takes no more pieces."""
    now = now or datetime.utcnow()
    return node.created_at is not None and \
        node.created_at < now - UPLOAD_COUNTS_FOR


class RecordingsBusy(Exception):
    """The account's lock was not free within LOCK_WAIT_SECONDS. The
    session was rolled back."""


def lock_account(user_id):
    """Take the account's recordings-and-uploads lock for the rest of the
    current transaction (a PostgreSQL advisory lock keyed on the user, as
    utils/todo_lock): the commit or rollback releases it, also when a
    worker dies. Between it and the commit the caller only counts, inserts
    or stamps, and commits: nothing that waits on the network. Other
    databases (SQLite in the tests) take no lock; the count inside still
    runs. Raises RecordingsBusy after LOCK_WAIT_SECONDS."""
    from backend.extensions import db
    if db.session.get_bind().dialect.name != "postgresql":
        return
    set_lock_timeout = text("SELECT set_config('lock_timeout', :value, true)")
    previous = db.session.execute(
        text("SELECT current_setting('lock_timeout')")).scalar()
    try:
        db.session.execute(set_lock_timeout,
                           {"value": f"{int(LOCK_WAIT_SECONDS * 1000)}ms"})
        db.session.execute(
            text("SELECT pg_advisory_xact_lock(:namespace, :user_id)"),
            {"namespace": _LOCK_NAMESPACE, "user_id": int(user_id)})
    except OperationalError as e:
        if getattr(e.orig, "pgcode", None) != _LOCK_NOT_AVAILABLE:
            raise
        db.session.rollback()
        raise RecordingsBusy() from e
    db.session.execute(set_lock_timeout, {"value": previous})


def _refused(text_, code, status):
    """A refusal: the transaction is rolled back (nothing of the request
    is kept, and the account's lock goes), then the answer."""
    from flask import jsonify
    from backend.extensions import db
    db.session.rollback()
    return jsonify({"error": text_, "code": code}), status


def recording_start_refusal(user_id, exclude_draft_id=None):
    """Take the account's lock and count: the answer for a new recording
    (or one coming back, *exclude_draft_id*) past MAX_RECORDINGS_IN_PROGRESS
    (429 ``too_many_recordings``) or MAX_UNFINISHED_RECORDINGS (429
    ``too_many_unfinished``), else None, the lock held until the caller
    commits."""
    try:
        lock_account(user_id)
    except RecordingsBusy:
        return _refused(RECORDINGS_BUSY_TEXT, "recordings_busy", 503)
    if recordings_in_progress(user_id, exclude_draft_id=exclude_draft_id) \
            >= MAX_RECORDINGS_IN_PROGRESS:
        return _refused(TOO_MANY_RECORDINGS_TEXT, "too_many_recordings", 429)
    if unfinished_recordings(user_id, exclude_draft_id=exclude_draft_id) \
            >= MAX_UNFINISHED_RECORDINGS:
        return _refused(TOO_MANY_UNFINISHED_TEXT, "too_many_unfinished", 429)
    return None


def upload_start_refusal(user_id):
    """Take the account's lock and count: 429 ``too_many_uploads`` past
    MAX_UPLOADS_IN_PROGRESS, else None, the lock held until the caller
    commits."""
    try:
        lock_account(user_id)
    except RecordingsBusy:
        return _refused(RECORDINGS_BUSY_TEXT, "recordings_busy", 503)
    if uploads_in_progress(user_id) >= MAX_UPLOADS_IN_PROGRESS:
        return _refused(TOO_MANY_UPLOADS_TEXT, "too_many_uploads", 429)
    return None


def too_many_uploads_response(user_id):
    """The quick check before an upload's file is stored: 429
    ``too_many_uploads`` past MAX_UPLOADS_IN_PROGRESS, else None. The
    locked count right before the entry is inserted decides
    (upload_start_refusal)."""
    if uploads_in_progress(user_id) < MAX_UPLOADS_IN_PROGRESS:
        return None
    from flask import jsonify
    return jsonify({"error": TOO_MANY_UPLOADS_TEXT,
                    "code": "too_many_uploads"}), 429


def session_continues(draft, now=None):
    """True while a recording keeps going: a sign of life within
    CONTINUES_WITHIN, not released by its app."""
    now = now or datetime.utcnow()
    return (draft.streaming_heartbeat_at is not None
            and draft.streaming_heartbeat_at >= now - CONTINUES_WITHIN)


def revival_refusal(draft, now=None):
    """The answer for a recording that takes audio again after it stopped
    keeping going (session_continues is False): the checks of a start, the
    usage limit (402) and the bounds, with the recording itself not
    counted; None when it may go on (the account's lock then held until
    the caller commits), or when it kept going (#341)."""
    if session_continues(draft, now):
        return None
    from backend.models import User
    from backend.extensions import db
    from backend.utils.spend import spend_cap_response, user_is_capped
    user = db.session.get(User, draft.user_id)
    if user_is_capped(user):
        db.session.rollback()
        return spend_cap_response(user)
    return recording_start_refusal(draft.user_id, exclude_draft_id=draft.id)


def transcribed_seconds(session_id=None, node_id=None):
    """Seconds of a recording's audio already turned into text (the sum of
    NodeTranscriptChunk.audio_seconds), by its session or, for the older
    node path, its node."""
    from sqlalchemy import func
    from backend.extensions import db
    from backend.models import NodeTranscriptChunk
    q = db.session.query(
        func.coalesce(func.sum(NodeTranscriptChunk.audio_seconds), 0.0))
    if session_id is not None:
        q = q.filter(NodeTranscriptChunk.session_id == session_id)
    else:
        q = q.filter(NodeTranscriptChunk.node_id == node_id)
    return float(q.scalar() or 0.0)


def too_long(done_seconds, next_seconds):
    """True when the next *next_seconds* of a recording would take what is
    transcribed of it past MAX_TRANSCRIBED_SECONDS."""
    return done_seconds + next_seconds > MAX_TRANSCRIBED_SECONDS


def mark_untranscribed(chunks):
    """Keep chunk rows' audio without their text: status ``failed`` (the
    finalize waits for them no longer) with NOT_TRANSCRIBED_TEXT as the
    error. Their audio files stay where they are. The caller commits."""
    now = datetime.utcnow()
    for chunk in chunks:
        if chunk is None or chunk.status == "completed":
            continue
        chunk.status = "failed"
        chunk.error = NOT_TRANSCRIBED_TEXT
        chunk.completed_at = now


def untranscribed_code(error):
    """TOO_LONG for a chunk with this error kept without its text, None
    for any other error."""
    return TOO_LONG if error == NOT_TRANSCRIBED_TEXT else None


def untranscribed_note(chunks, has_text):
    """The note for a recording with *chunks* kept without their text
    (nothing when there are none). *has_text*: some of it was
    transcribed."""
    if not chunks:
        return ""
    return NOTE_PART if has_text else NOTE_ALL


def with_note(content, note):
    """*content* with *note* after it (the note alone for no content);
    *content* unchanged when there is no note."""
    if not note:
        return content
    content = (content or "").rstrip()
    return f"{content}\n\n{note}" if content else note

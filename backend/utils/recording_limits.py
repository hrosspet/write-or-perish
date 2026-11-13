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
  its recordings are in progress, and no new audio upload while
  MAX_UPLOADS_IN_PROGRESS of its uploads wait for their transcript.
"""
import os
from datetime import datetime, timedelta

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

# INTRODUCED HEURISTIC: audio uploads (chunked or in one request) and older
# node-path recordings of one account that may wait for their transcript at
# once. The apps upload one file per press; three leaves room for presses in
# quick succession.
MAX_UPLOADS_IN_PROGRESS = 3
# An upload counts for at most this long after it started, so one that was
# never finished (its pieces stopped coming) does not count for ever.
UPLOAD_COUNTS_FOR = timedelta(hours=1)

TOO_MANY_RECORDINGS_TEXT = (
    "Other recordings of yours are still in progress. Finish one of them, "
    "then start this one.")
TOO_MANY_UPLOADS_TEXT = (
    "Other audio files of yours are still being uploaded or turned into "
    "text. Wait for one of them to finish, then upload this one.")

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


def recordings_in_progress(user_id, now=None):
    """The account's recordings in progress (MAX_RECORDINGS_IN_PROGRESS):
    live ones, and finalizing ones changed within FINALIZING_COUNTS_FOR."""
    from backend.extensions import db
    from backend.models import Draft
    from backend.utils.streaming_session import LIVE_SESSION_WINDOW_SEC
    now = now or datetime.utcnow()
    live_since = now - timedelta(seconds=LIVE_SESSION_WINDOW_SEC)
    return Draft.query.filter(
        Draft.user_id == user_id,
        Draft.session_id.isnot(None),
        db.or_(
            db.and_(Draft.streaming_status == "recording",
                    Draft.streaming_heartbeat_at >= live_since),
            db.and_(Draft.streaming_status == "finalizing",
                    Draft.updated_at >= now - FINALIZING_COUNTS_FOR),
        ),
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


def too_many_recordings_response(user_id):
    """429 ``too_many_recordings`` when the account has
    MAX_RECORDINGS_IN_PROGRESS recordings in progress, else None."""
    if recordings_in_progress(user_id) < MAX_RECORDINGS_IN_PROGRESS:
        return None
    from flask import jsonify
    return jsonify({"error": TOO_MANY_RECORDINGS_TEXT,
                    "code": "too_many_recordings"}), 429


def too_many_uploads_response(user_id):
    """429 ``too_many_uploads`` when the account has
    MAX_UPLOADS_IN_PROGRESS uploads waiting for their transcript, else
    None."""
    if uploads_in_progress(user_id) < MAX_UPLOADS_IN_PROGRESS:
        return None
    from flask import jsonify
    return jsonify({"error": TOO_MANY_UPLOADS_TEXT,
                    "code": "too_many_uploads"}), 429


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

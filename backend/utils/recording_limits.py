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

A recording accepted at its start (where the monthly usage limit and the
count below are checked, #341) is stored and transcribed to its end
(Peter, 2026-11-13: "User's recorded message should be stored on Loore's
servers, possibly even transcribed (transcription is cheap). Just further
AI use should be blocked"). Once it holds audio it is never refused more
audio for quiet time or for the usage limit, whatever the gap; only these
limits apply:

- One recording is transcribed up to MAX_TRANSCRIBED_SECONDS of audio, as
  the server counts it (see the end of this note), the same measure the
  cost is recorded with. The
  audio past it is still stored and kept with the recording; its chunks
  end ``failed`` with NOT_TRANSCRIBED_TEXT, so the finalize waits for them
  no longer, and the recording's text ends with a note
  (untranscribed_note).
- Its audio is refused only past bounds no recording made in the apps
  reaches: a chunk over MAX_STREAMING_CHUNK_BYTES, a chunk index from
  MAX_STREAMING_CHUNKS on, or more than MAX_STREAMING_SESSION_BYTES in
  all. Audio accepted before stays.

An account holds at most MAX_OPEN_RECORDINGS open recordings that hold
audio (open_recordings); a new start beyond them is refused. A recording
is active through its start and the chunks it stores; its liveness stamp
(for the takeover between devices) plays no part in these limits. A
recording without audio does not count there: at most MAX_EMPTY_STARTS of
them are started within EMPTY_SESSION_EXPIRES_AFTER, and one older than
that is a new start when its first audio comes. A recording left untouched
for OPEN_COUNTS_FOR is treated as a new start too when it takes audio
again (audio_refusal): the usage limit, then the count. Its audio is kept
either way and can be saved. Each start takes the account's lock and
counts again inside it (lock_account), so two starts at once cannot both
pass. Recordings without audio older than EMPTY_SESSION_REMOVED_AFTER are
removed daily (tasks/recording_cleanup).

An account has at most MAX_UPLOADS_IN_PROGRESS audio uploads waiting for
their transcript with activity within UPLOAD_COUNTS_FOR
(uploads_in_progress). An upload takes pieces as long as it had one within
UPLOAD_COUNTS_FOR. An upload accepted at its start is transcribed (as a
recording is, #341), up to MAX_UPLOAD_TRANSCRIBED_SECONDS (its audio is
cut there before it is sent).

Lengths for these bounds and for the transcription cost are counted from
the decoded audio (utils/audio_processing.decoded_audio_seconds), never
from the container's timestamps. Audio that cannot be decoded is
re-encoded on the server and the re-encoded audio is what is sent and
counted; audio that cannot be re-encoded either is not sent (UNREADABLE).
A recording's audio counts at least STREAMING_CHUNK_SECONDS a chunk.
"""
import os
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

# Seconds of audio in one streaming chunk, in both apps.
STREAMING_CHUNK_SECONDS = 15

# Chunks transcribed together in one batch (20 × 15 s = 5 minutes): the
# chunk route queues a batch once this many are stored, and the finalize and
# transcribe-remaining queue a longer backlog as several batches. So a batch
# holds at most this many parts (a part per resume), the bound the task
# keeps (MAX_PARTS_PER_BATCH in tasks/streaming_transcription).
BATCH_CHUNKS = 20

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

# INTRODUCED HEURISTIC: open recordings (dictation and Voice) with audio one
# account may hold. Open: recording, with audio and active (its start or a
# chunk stored) within OPEN_COUNTS_FOR; or finalizing while its app still
# sends the chunks it announced. Saving, discarding or finishing a recording
# frees its place at once; a recording an app left behind (killed
# mid-recording) frees it after a day without activity, and stays offered
# for recovery. A Voice turn frees its place as soon as its last chunk is
# in. Six is room for one recording, a second device and a few interrupted
# recordings not recovered yet.
MAX_OPEN_RECORDINGS = 6
# INTRODUCED HEURISTIC: a recording with audio counts as open for a day
# after its last activity; one left untouched longer is treated as a new
# start when it takes audio again.
OPEN_COUNTS_FOR = timedelta(hours=24)

# INTRODUCED HEURISTIC: how long a recording with no audio stored yet stays
# open. The apps send their first chunk within 15 s of recording (at once on
# a pause or Stop); fifteen minutes covers a start followed by a long time
# offline. After it, its first audio is checked as a new start.
EMPTY_SESSION_EXPIRES_AFTER = timedelta(minutes=15)
# INTRODUCED HEURISTIC: recordings without audio one account may start
# within EMPTY_SESSION_EXPIRES_AFTER. A start whose microphone fails, or a
# Voice screen opened and left, makes one; thirty in fifteen minutes is far
# past what a person does by hand.
MAX_EMPTY_STARTS = 30
# INTRODUCED HEURISTIC: a recording without audio older than this is removed
# by the daily clean-up: long past EMPTY_SESSION_EXPIRES_AFTER, and long
# enough for a phone that lost its connection before its first chunk got
# through to send it. At most EMPTY_SESSIONS_REMOVED_PER_RUN a run.
EMPTY_SESSION_REMOVED_AFTER = timedelta(days=7)
EMPTY_SESSIONS_REMOVED_PER_RUN = 500

# INTRODUCED HEURISTIC: audio uploads (chunked or in one request) and older
# node-path recordings of one account that may wait for their transcript at
# once. The apps upload one file per press; five leaves room for presses in
# quick succession and for an upload left behind.
MAX_UPLOADS_IN_PROGRESS = 5
# INTRODUCED HEURISTIC: an upload (or older node-path recording) takes
# pieces, and counts, while it had activity (its start, a piece, its
# finalize) within this time. A device that slept mid-upload continues
# where it stopped; after a day the upload ends (410) and the app shows the
# server's words. One the app reports as failed (/upload/cleanup) stops
# counting at once.
UPLOAD_COUNTS_FOR = timedelta(hours=24)

# INTRODUCED HEURISTIC: the most audio of one uploaded file that is turned
# into text, as decoded. A 200 MB file (the largest upload) at the iPhone's
# 64 kbps is about 7 hours; eight leaves room. A longer file is transcribed
# to this length (the audio is cut there before it is sent, so the bound
# holds also when the length cannot be measured); the whole file stays with
# its entry, and its text ends with UPLOAD_NOTE_PART.
MAX_UPLOAD_TRANSCRIBED_SECONDS = 8 * 60 * 60

# At the count, when recovery offers some of the open recordings, and when
# it offers none (they are being recorded elsewhere or saved).
TOO_MANY_RECORDINGS_TEXT = (
    "You have several recordings that are not finished. Save, finish or "
    "discard one of them, then start a new one.")
RECORDINGS_IN_PROGRESS_TEXT = (
    "Several of your recordings are still being recorded or saved. Wait "
    "for one of them to finish, then start a new one.")
TOO_MANY_STARTS_TEXT = (
    "Many recordings were started without any audio in the last few "
    "minutes. Wait a few minutes, then start a new one.")
TOO_MANY_UPLOADS_TEXT = (
    "Other audio files of yours are still being uploaded or turned into "
    "text. Wait for one of them to finish, then upload this one.")
UPLOAD_EXPIRED_TEXT = ("This upload stopped arriving more than a day ago. "
                       "Please upload the file again.")
UPLOAD_FAILED_TEXT = "The upload did not finish. Please upload the file again."
UPLOAD_NOTE_PART = ("*Note: The file is longer than eight hours, so the "
                    "rest of it was not turned into text. Its audio is "
                    "saved.*")
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
# from a failure. TOO_LONG: past MAX_TRANSCRIBED_SECONDS. UNREADABLE: the
# server could neither measure the audio nor re-encode it, so it was not
# sent. INVALID: its chunks decode to more than MAX_CHUNK_AUDIO_SECONDS each
# (the apps treat both as a failed chunk).
TOO_LONG = "recording_too_long"
UNREADABLE = "audio_unreadable"
INVALID = "chunk_invalid"

# INTRODUCED HEURISTIC: the most audio a chunk may decode to, on average over
# the chunks merged and sent together. Both apps cut a chunk every 15 s
# (useStreamingMediaRecorder's timeslice, FMP4SegmentWriter's segments); the
# longest measured is 30 s (Chrome handing two slices over at once). The
# decode stops just past this, so measuring a chunk takes a fraction of a
# second whatever it holds; a part that reaches it is not re-encoded or
# sent.
MAX_CHUNK_AUDIO_SECONDS = 2 * 60

# NodeTranscriptChunk.error of a chunk kept without its text.
NOT_TRANSCRIBED_TEXT = ("Saved as audio, not turned into text: the "
                        "recording is longer than six hours.")
NOT_READ_TEXT = ("Saved as audio, not turned into text: this part of the "
                 "recording could not be read.")
NOT_VALID_TEXT = ("Saved as audio, not turned into text: this part of the "
                  "recording is not valid audio.")
_ERROR_TEXTS = {TOO_LONG: NOT_TRANSCRIBED_TEXT, UNREADABLE: NOT_READ_TEXT,
                INVALID: NOT_VALID_TEXT}

# The note at the end of a recording's text: some of it was transcribed,
# none of it was; part of it could not be read.
NOTE_PART = ("*Note: The recording is longer than six hours, so the rest "
             "of it was not turned into text. Its audio is saved.*")
NOTE_ALL = ("*Note: The recording is longer than six hours, so it was not "
            "turned into text. Its audio is saved.*")
NOTE_UNREADABLE = ("*Note: Part of this recording could not be read, so it "
                   "was not turned into text. Its audio is saved.*")
NOTE_INVALID = ("*Note: Part of this recording is not valid audio, so it "
                "was not turned into text. Its audio is saved.*")


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


def _since(now, delta):
    return (now or datetime.utcnow()) - delta


def _has_audio():
    from backend.extensions import db
    from backend.models import Draft, NodeTranscriptChunk
    return db.exists().where(
        NodeTranscriptChunk.session_id == Draft.session_id)


def _active_since(since):
    """A recording started or stored a chunk since *since*. Its liveness
    stamp (``streaming_heartbeat_at``, renewed by its transcription stream)
    is for the takeover between devices and plays no part here; a claim
    runs audio_refusal itself."""
    from backend.extensions import db
    from backend.models import Draft, NodeTranscriptChunk
    recent_chunk = db.exists().where(db.and_(
        NodeTranscriptChunk.session_id == Draft.session_id,
        NodeTranscriptChunk.created_at >= since))
    return db.or_(Draft.created_at >= since, recent_chunk)


def _open_query(user_id, now=None, exclude_draft_id=None):
    """The account's open recordings (MAX_OPEN_RECORDINGS): recording with
    audio and active within OPEN_COUNTS_FOR; finalizing with fewer chunks
    stored than it announced, active within OPEN_COUNTS_FOR. Recordings
    without audio are not counted (MAX_EMPTY_STARTS)."""
    from backend.extensions import db
    from backend.models import Draft, NodeTranscriptChunk
    now = now or datetime.utcnow()
    stored = (db.select(db.func.count(NodeTranscriptChunk.id))
              .where(NodeTranscriptChunk.session_id == Draft.session_id)
              .scalar_subquery())
    active = _active_since(_since(now, OPEN_COUNTS_FOR))
    query = Draft.query.filter(Draft.user_id == user_id,
                               Draft.session_id.isnot(None))
    if exclude_draft_id is not None:
        query = query.filter(Draft.id != exclude_draft_id)
    return query.filter(db.or_(
        db.and_(Draft.streaming_status == "recording", _has_audio(), active),
        db.and_(Draft.streaming_status == "finalizing",
                Draft.streaming_total_chunks.isnot(None),
                stored < Draft.streaming_total_chunks, active),
    ))


def open_recordings(user_id, now=None, exclude_draft_id=None):
    """How many open recordings the account holds (_open_query)."""
    return _open_query(user_id, now, exclude_draft_id).count()


def empty_starts(user_id, now=None):
    """Recordings without audio the account started within
    EMPTY_SESSION_EXPIRES_AFTER (MAX_EMPTY_STARTS)."""
    from backend.models import Draft
    return Draft.query.filter(
        Draft.user_id == user_id,
        Draft.session_id.isnot(None),
        Draft.streaming_status == "recording",
        ~_has_audio(),
        Draft.created_at >= _since(now, EMPTY_SESSION_EXPIRES_AFTER),
    ).count()


def _activity():
    from backend.extensions import db
    from backend.models import Node
    return db.func.coalesce(Node.upload_activity_at, Node.created_at)


def uploads_in_progress(user_id, now=None):
    """The account's audio entries waiting for their transcript
    (MAX_UPLOADS_IN_PROGRESS), deleted ones left out, with activity (start,
    a piece, the finalize) within UPLOAD_COUNTS_FOR."""
    from backend.models import Node
    return Node.query.filter(
        Node.user_id == user_id,
        Node.deleted_at.is_(None),
        Node.transcription_status.in_(("pending", "processing")),
        _activity() >= _since(now, UPLOAD_COUNTS_FOR),
    ).count()


def upload_expired(node, now=None):
    """True for an upload (or older node-path recording) not yet handed to
    transcription that has had no activity for UPLOAD_COUNTS_FOR: it no
    longer counts, and takes no more pieces."""
    last = node.upload_activity_at or node.created_at
    return (node.audio_original_url is None and last is not None
            and last < _since(now, UPLOAD_COUNTS_FOR))


def touch_upload(node_id, now=None):
    """An upload received a piece: renew it (UPLOAD_COUNTS_FOR). A Core
    UPDATE that keeps ``updated_at``: a piece is not an edit."""
    from backend.models import Node
    Node.query.filter(Node.id == node_id).update(
        {Node.upload_activity_at: now or datetime.utcnow(),
         Node.updated_at: Node.updated_at},
        synchronize_session=False)


def remove_empty_recordings(now=None, limit=None, root=None):
    """The daily clean-up (tasks/recording_cleanup): delete up to *limit*
    (EMPTY_SESSIONS_REMOVED_PER_RUN) recording rows that never received
    audio and started more than EMPTY_SESSION_REMOVED_AFTER ago, with their
    empty session folders. A start whose microphone failed, or a Voice
    screen opened and left, makes one.

    Never touches a recording that holds audio: a row with a chunk row, a
    session folder that holds a file, or text in the draft is left alone.
    The draft's text is compared as stored (an empty draft is stored
    empty), so nothing is decrypted. Returns how many were removed."""
    import pathlib
    from backend.extensions import db
    from backend.models import Draft
    from backend.utils.audio_storage import AUDIO_STORAGE_ROOT, storage_path
    now = now or datetime.utcnow()
    limit = limit or EMPTY_SESSIONS_REMOVED_PER_RUN
    root = AUDIO_STORAGE_ROOT if root is None else pathlib.Path(root)
    candidates = Draft.query.filter(
        Draft.session_id.isnot(None),
        Draft.streaming_status == "recording",
        ~_has_audio(),
        db.or_(Draft.content.is_(None), Draft.content == ""),
        Draft.created_at < now - EMPTY_SESSION_REMOVED_AFTER,
    ).order_by(Draft.id).limit(limit).all()
    removed = 0
    for draft in candidates:
        try:
            folder = storage_path(root, "drafts", draft.user_id,
                                  draft.session_id)
            if folder.is_symlink():
                continue
            if folder.exists():
                if any(folder.iterdir()):
                    continue    # it holds a file: left alone
                folder.rmdir()
        except (ValueError, OSError):
            continue
        db.session.delete(draft)
        removed += 1
    db.session.commit()
    return removed


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


def recording_start_refusal(user_id, exclude_draft_id=None, new=False):
    """Take the account's lock and count: 429 ``too_many_recordings`` when
    the account already holds MAX_OPEN_RECORDINGS open recordings (the
    recording *exclude_draft_id* not counted), worded by what recovery
    offers; for a *new* recording (an init, without audio yet) also 429
    ``too_many_starts`` past MAX_EMPTY_STARTS. Else None, the lock held
    until the caller commits."""
    from backend.utils.streaming_session import not_live_clause
    try:
        lock_account(user_id)
    except RecordingsBusy:
        return _refused(RECORDINGS_BUSY_TEXT, "recordings_busy", 503)
    open_ = _open_query(user_id, exclude_draft_id=exclude_draft_id)
    if open_.count() >= MAX_OPEN_RECORDINGS:
        from backend.models import Draft
        offered = open_.filter(Draft.streaming_status == "recording",
                               not_live_clause()).count()
        return _refused(TOO_MANY_RECORDINGS_TEXT if offered
                        else RECORDINGS_IN_PROGRESS_TEXT,
                        "too_many_recordings", 429)
    if new and empty_starts(user_id) >= MAX_EMPTY_STARTS:
        return _refused(TOO_MANY_STARTS_TEXT, "too_many_starts", 429)
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


def audio_refusal(draft, now=None):
    """The answer for audio for *draft* (a chunk, a claim), or None when it
    may take it.

    - A recording that holds audio is never refused for quiet time or the
      usage limit (#341) while it had activity within OPEN_COUNTS_FOR. One
      left untouched longer is a new start when it takes audio again: the
      usage limit (402), then the count. Its stored audio stays and can be
      saved either way.
    - A recording without audio is open for EMPTY_SESSION_EXPIRES_AFTER
      from its start (also once its app released it): its first audio must
      fit under the count of open recordings (MAX_OPEN_RECORDINGS; the
      usage limit was asked at its start). After that its first audio is a
      new start: the usage limit (402), then the count.
    Under the account's lock when a check runs (held until the caller
    commits)."""
    from backend.extensions import db
    from backend.models import Draft, NodeTranscriptChunk
    now = now or datetime.utcnow()
    has_audio = db.session.query(NodeTranscriptChunk.id).filter(
        NodeTranscriptChunk.session_id == draft.session_id).first() is not None
    if has_audio:
        active = Draft.query.filter(
            Draft.id == draft.id,
            _active_since(_since(now, OPEN_COUNTS_FOR))).count()
        if active:
            return None
    elif (draft.created_at
            and draft.created_at >= _since(now, EMPTY_SESSION_EXPIRES_AFTER)):
        return recording_start_refusal(draft.user_id,
                                       exclude_draft_id=draft.id)
    from backend.models import User
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


def mark_untranscribed(chunks, reason=TOO_LONG):
    """Keep chunk rows' audio without their text: status ``failed`` (the
    finalize waits for them no longer) with the text for *reason*
    (NOT_TRANSCRIBED_TEXT, NOT_READ_TEXT) as the error. Their audio files
    stay where they are. The caller commits."""
    now = datetime.utcnow()
    for chunk in chunks:
        if chunk is None or chunk.status == "completed":
            continue
        chunk.status = "failed"
        chunk.error = _ERROR_TEXTS[reason]
        chunk.completed_at = now


def untranscribed_code(error):
    """TOO_LONG or UNREADABLE for a chunk with this error kept without its
    text, None for any other error."""
    for code, text_ in _ERROR_TEXTS.items():
        if error == text_:
            return code
    return None


def untranscribed_note(chunks, has_text):
    """The note for a recording with *chunks* kept without their text
    (nothing when there are none). *has_text*: some of it was
    transcribed."""
    codes = {untranscribed_code(chunk.error) for chunk in chunks}
    notes = []
    if TOO_LONG in codes:
        notes.append(NOTE_PART if has_text else NOTE_ALL)
    if UNREADABLE in codes:
        notes.append(NOTE_UNREADABLE)
    if INVALID in codes:
        notes.append(NOTE_INVALID)
    return "\n\n".join(notes)


def with_note(content, note):
    """*content* with *note* after it (the note alone for no content);
    *content* unchanged when there is no note."""
    if not note:
        return content
    content = (content or "").rstrip()
    return f"{content}\n\n{note}" if content else note

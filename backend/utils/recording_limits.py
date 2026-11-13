"""Limits on recording uploads and on transcription per recording.

Audio reaches the server in two ways:

- A streaming recording (dictation and Voice:
  ``/api/drafts/streaming/<session>/audio-chunk``; the older
  ``/api/nodes/<id>/audio-chunk``) arrives in chunks while it is recorded.
  Both apps cut a chunk every 15 s of audio.
- An audio file (``/api/nodes/upload/init|chunk|finalize``) arrives in
  5 MB pieces, after an init that names its size and its piece count.

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

A streaming recording is never cut short by the server:

- Its transcription stops at the monthly usage limit (utils/spend) and
  at MAX_TRANSCRIBED_SECONDS of audio. The audio from there on is still
  stored and kept with the recording; its chunks end ``failed`` with a
  text of their own (NOT_TRANSCRIBED_TEXTS), so the finalize waits for
  them no longer, and the recording's text ends with a note saying what
  was not turned into text and why (untranscribed_note). Peter,
  2026-11-13: "User's recorded message should be stored on Loore's
  servers … Just further AI use should be blocked".
- Its audio is refused only past bounds no recording made in the apps
  reaches: a chunk over MAX_STREAMING_CHUNK_BYTES, a chunk index from
  MAX_STREAMING_CHUNKS on, or more than MAX_STREAMING_SESSION_BYTES in
  all. Audio accepted before stays.
"""
import os

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

# Why a chunk's audio is kept without its text: the codes the transcription
# stream sends with the chunk (``chunk_error``), so the apps tell it apart
# from a failure. The first is the spend cap's own error code (utils/spend).
SPEND_CAP = "monthly_spend_limit_reached"
TOO_LONG = "recording_too_long"

# NodeTranscriptChunk.error of a chunk kept without its text, by reason.
NOT_TRANSCRIBED_TEXTS = {
    SPEND_CAP: ("Saved as audio, not turned into text: you've reached your "
                "monthly usage limit."),
    TOO_LONG: ("Saved as audio, not turned into text: the recording is "
               "longer than six hours."),
}

# The note at the end of a recording's text, by reason: (some of it was
# transcribed, none of it was).
_NOTES = {
    SPEND_CAP: (
        "*Note: The rest of this recording was not turned into text, "
        "because you've reached your monthly usage limit. Its audio is "
        "saved.*",
        "*Note: This recording was not turned into text, because you've "
        "reached your monthly usage limit. Its audio is saved.*",
    ),
    TOO_LONG: (
        "*Note: The recording is longer than six hours, so the rest of it "
        "was not turned into text. Its audio is saved.*",
        "*Note: The recording is longer than six hours, so it was not "
        "turned into text. Its audio is saved.*",
    ),
}


def refusal(code, message, status):
    """A refused upload: (body, HTTP status), for jsonify."""
    return {"error": message, "code": code}, status


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


def untranscribed_reason(user_id, done_seconds, next_seconds):
    """Why the next *next_seconds* of a recording are not sent to be
    transcribed, or None when they may be: the user has reached the
    monthly usage limit (SPEND_CAP; read right before the provider call,
    as the other paid calls do), or the recording would pass
    MAX_TRANSCRIBED_SECONDS with them (TOO_LONG)."""
    from backend.utils.spend import user_is_capped
    if user_id is not None and user_is_capped(user_id):
        return SPEND_CAP
    if done_seconds + next_seconds > MAX_TRANSCRIBED_SECONDS:
        return TOO_LONG
    return None


def mark_untranscribed(chunks, reason):
    """Keep chunk rows' audio without their text: status ``failed`` (the
    finalize waits for them no longer) with the reason's text as the
    error. Their audio files stay where they are. The caller commits."""
    from datetime import datetime
    now = datetime.utcnow()
    for chunk in chunks:
        if chunk is None or chunk.status == "completed":
            continue
        chunk.status = "failed"
        chunk.error = NOT_TRANSCRIBED_TEXTS[reason]
        chunk.completed_at = now


def untranscribed_code(error):
    """The reason (SPEND_CAP, TOO_LONG) a chunk with this error was kept
    without its text, or None for any other error."""
    for code, text in NOT_TRANSCRIBED_TEXTS.items():
        if error == text:
            return code
    return None


def untranscribed_note(chunks, has_text):
    """The note for a recording whose *chunks* were kept without their
    text: one line per reason, in the order of the first chunk of each.
    *has_text*: some of the recording was transcribed."""
    reasons = []
    for chunk in sorted(chunks, key=lambda c: c.chunk_index):
        code = untranscribed_code(chunk.error)
        if code is not None and code not in reasons:
            reasons.append(code)
    return "\n\n".join(_NOTES[code][0 if has_text else 1]
                       for code in reasons)


def with_note(content, note):
    """*content* with *note* after it (the note alone for no content);
    *content* unchanged when there is no note."""
    if not note:
        return content
    content = (content or "").rstrip()
    return f"{content}\n\n{note}" if content else note

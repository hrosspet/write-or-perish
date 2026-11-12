"""No AI means no AI, for recordings (#665).

Turning a recording into text sends its audio to OpenAI, so a recording
is transcribed only when the entry it becomes lets AI read it
(ai_usage 'chat' or 'train'). The web and iPhone apps disable Record and
Upload for an entry set to None; the server refuses the same, on every
path that takes audio:

- Starting a recording or upload whose entry is set to None gets the 403
  ``{"error", "code": "ai_usage_none", "scope": "entry"}`` the clients
  already handle for Voice (llm_nodes.AIUsageRefused), before any audio
  is stored or any transcription is queued.
- A recording that started while its entry allowed AI, and whose entry
  is set to None before its audio is transcribed (the entry edited in
  another tab, or a Voice turn whose thread was set to None), keeps its
  audio and is not transcribed. Its entry says why.
- Each transcription worker asks again right before it calls the
  provider, so nothing queued before a change reaches it.

The entry's own setting decides, not the account's: the apps let a user
whose Default AI usage is None set one entry to Chat and record it, and
only that entry's audio is sent. Voice mode keeps its own, stricter rule
at start (llm_nodes.voice_turn_refusal).
"""

from backend.utils.privacy import AI_ALLOWED

# The scope of the 403: the entry's own setting decided. The web reads
# any scope but "thread" as the account's (aiUsage.js); the iPhone falls
# back to its default. Both show the "error" text for a recording.
RECORDING_REFUSED_SCOPE = "entry"
RECORDING_REFUSED_MESSAGE = (
    "AI usage for this entry is set to None, so Loore doesn't send its "
    "audio to AI to turn it into text. Set AI usage to Chat or Train to "
    "record or upload audio.")

# Shown where a transcription error is shown (the upload form, the
# recording's chunk list) for a recording kept without its text.
NOT_TRANSCRIBED_TEXT = (
    "This recording is saved with its audio, but not turned into text: "
    "AI usage for its entry is set to None.")
# The entry's text in place of a transcript.
NOT_TRANSCRIBED_CONTENT = (
    "[Voice note – not transcribed, because AI usage is set to None]")
# The note on a streamed recording's text when its chunks were kept
# without being transcribed: all of them, or the ones after the text that
# was transcribed while AI was allowed.
NOT_TRANSCRIBED_NOTE = (
    "*Note: This recording was not turned into text, because AI usage is "
    "set to None. Its audio is saved.*")
PARTLY_NOT_TRANSCRIBED_NOTE = (
    "*Note: Part of this recording was not turned into text, because AI "
    "usage is set to None. Its audio is saved.*")


def may_transcribe(ai_usage):
    """Whether a recording whose entry has *ai_usage* may be sent to the
    provider to be transcribed."""
    return ai_usage in AI_ALLOWED


def recording_refusal(ai_usage):
    """The AIUsageRefused for starting a recording whose entry has
    *ai_usage*, or None when it may be transcribed."""
    if may_transcribe(ai_usage):
        return None
    from backend.utils.llm_nodes import AIUsageRefused
    return AIUsageRefused(RECORDING_REFUSED_MESSAGE, RECORDING_REFUSED_SCOPE)


def recording_refused_response(ai_usage):
    """The 403 answer for starting a recording whose entry has *ai_usage*,
    or None when it may be transcribed."""
    refused = recording_refusal(ai_usage)
    if refused is None:
        return None
    from backend.utils.llm_nodes import ai_usage_refused_response
    return ai_usage_refused_response(refused)


def draft_entry_ai_usage(draft):
    """The ai_usage of the entry a streaming draft's recording becomes, as
    it is now. A Voice turn that continues a thread takes the thread's
    (llm_nodes.reply_ai_usage, as the turn's entry does when it is saved,
    tasks/streaming_transcription._start_server_side_llm_chain); any other
    recording keeps the value it started with."""
    if draft.label == "Voice" and draft.parent_id:
        from backend.models import Node, User
        from backend.utils.llm_nodes import reply_ai_usage
        parent = Node.query.get(draft.parent_id)
        if parent is not None:
            return reply_ai_usage(parent, User.query.get(draft.user_id))
    return draft.ai_usage or "none"


def draft_may_transcribe(draft):
    """Whether a streaming draft's audio may be sent to be transcribed."""
    return may_transcribe(draft_entry_ai_usage(draft))


def voice_note_content(node, filename, text):
    """An uploaded recording's text as its entry shows it: a top-level
    entry is titled after the file (one made in the app, named
    "recording", after the time), a reply is not."""
    if node.parent_id is None and filename:
        import os
        from datetime import datetime
        header, _ = os.path.splitext(filename)
        if header == "recording":
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return f"# {timestamp} Voice note\n\n{text}"
        return f"# {header}\n\n{text}"
    return text


# The texts an uploaded recording's entry holds until its transcript is in
# (routes/nodes.py).
PLACEHOLDER_CONTENTS = (
    "[Voice note – transcription pending]",
    "[Voice note – upload in progress]",
)


def mark_not_transcribed(node, filename=None):
    """Keep an uploaded recording without its text: its entry is set to AI
    usage None, so its audio is never sent to be transcribed. The audio
    stays with the entry; the transcription ends 'failed' with
    NOT_TRANSCRIBED_TEXT, which the upload form and the app show, and an
    entry that still holds a placeholder says why instead. A transcript
    the entry already has is kept. The caller commits."""
    from datetime import datetime
    from backend.utils.tokens import approximate_token_count
    current = (node.get_content() or "").strip()
    if not current or current in PLACEHOLDER_CONTENTS:
        content = voice_note_content(node, filename, NOT_TRANSCRIBED_CONTENT)
        node.set_content(content)
        node.token_count = approximate_token_count(content)
    node.transcription_status = "failed"
    node.transcription_error = NOT_TRANSCRIBED_TEXT
    node.transcription_progress = 0
    node.transcription_completed_at = datetime.utcnow()


def mark_chunks_not_transcribed(chunks):
    """Mark transcript chunk rows as kept without their text: status
    'failed' (so the finalize waits for them no longer) with
    NOT_TRANSCRIBED_TEXT as the error, which tells them apart from a
    provider failure. Their audio files are left where they are. A chunk
    already transcribed keeps its text. The caller commits."""
    from datetime import datetime
    now = datetime.utcnow()
    for chunk in chunks:
        if chunk is None or chunk.status == "completed":
            continue
        chunk.status = "failed"
        chunk.error = NOT_TRANSCRIBED_TEXT
        chunk.completed_at = now


def not_transcribed_note(failed_chunks, has_text):
    """The note for a recording's text when every chunk listed failed only
    because it was not transcribed for AI usage, else None (the caller's
    own note for failed chunks applies). *has_text*: some of the recording
    was transcribed before."""
    if failed_chunks and all(
            getattr(c, "error", None) == NOT_TRANSCRIBED_TEXT
            for c in failed_chunks):
        return PARTLY_NOT_TRANSCRIBED_NOTE if has_text else NOT_TRANSCRIBED_NOTE
    return None

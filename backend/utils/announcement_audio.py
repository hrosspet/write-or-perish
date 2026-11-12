"""The approval announcement as its own audio clip (Peter, 2026-11-12):

"the announcement should be TTSed as a separate chunk and appended to the
queue. So that when it's not pending, it's not voiced out anymore"

A Voice reply whose write calls wait for the user's approval is followed by
one fixed sentence. It is not part of the reply's audio: the players append
this clip after the reply's last chunk while the reply's calls wait
(GET /api/voice/approval-announcement), and drop it once they are decided.

The sentence is fixed, so there are two clips (one change / several),
made once with the reply's TTS model and voice and kept on disk:
``<AUDIO_STORAGE_PATH>/shared/announcements/<variant>-<key>.mp3`` with a
``.json`` holding its duration. *key* hashes the TTS model, the voice and
the text, so a change of any of them makes a new clip on the next request
(the old file stays, unused). The text is no one's content, so the file is
not encrypted. Making a clip is billed to the user whose request made it.

When a clip can't be made (no TTS key, a provider error) the answer has no
clip: the reply plays alone, and the dialog and the record block work as
before.
"""
import hashlib
import json
import logging
import os
import pathlib
import uuid

from flask import current_app

logger = logging.getLogger(__name__)

ANNOUNCEMENT_TEXTS = {
    "one": ("A change to your data is waiting. Approve or reject it on the "
            "screen to continue."),
    "several": ("Changes to your data are waiting. Approve or reject them "
                "on the screen to continue."),
}


def variant_for(count):
    """The clip for *count* waiting calls."""
    return "one" if count == 1 else "several"


class Clip:
    def __init__(self, variant, key, path, duration):
        self.variant = variant
        self.key = key
        self.path = path
        self.duration = duration

    @property
    def url(self):
        """Where the players fetch it (routes/voice.py). *v* changes with
        the clip, so a browser never plays a cached older one."""
        return (f"/api/voice/approval-announcement/{self.variant}.mp3"
                f"?v={self.key}")


def _tts_settings():
    """The reply's TTS model and voice (tasks/tts.py)."""
    from backend.tasks.tts import TTS_MODEL, TTS_VOICE
    return TTS_MODEL, TTS_VOICE


def _synthesize(text, path):
    """One TTS call to *path*, as the reply's chunks are made; returns the
    audio's seconds. Raises when there is no TTS key or the call fails."""
    from openai import OpenAI
    from backend.tasks.tts import synthesize_to_file
    from backend.utils.api_keys import get_openai_chat_key
    api_key = get_openai_chat_key(current_app.config)
    if not api_key:
        raise RuntimeError("no TTS key")
    segment = synthesize_to_file(OpenAI(api_key=api_key), text, path)
    return len(segment) / 1000.0


def _log_cost(user_id, seconds):
    from backend.tasks.tts import log_tts_cost
    log_tts_cost(user_id, seconds)


def _clip_dir():
    root = pathlib.Path(
        os.environ.get("AUDIO_STORAGE_PATH", "data/audio")).resolve()
    return root / "shared" / "announcements"


def clip_key(variant):
    model, voice = _tts_settings()
    raw = "\x00".join(["approval-announcement", model, voice,
                       ANNOUNCEMENT_TEXTS[variant]])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def ensure_clip(variant, user_id):
    """The cached clip of *variant*, made now if there is none yet; None
    when it can't be made (logged)."""
    if variant not in ANNOUNCEMENT_TEXTS:
        return None
    key = clip_key(variant)
    folder = _clip_dir()
    path = folder / f"{variant}-{key}.mp3"
    meta = folder / f"{variant}-{key}.json"
    if path.exists() and meta.exists():
        try:
            duration = float(json.loads(meta.read_text())["duration"])
            return Clip(variant, key, path, duration)
        except (ValueError, KeyError, TypeError, OSError):
            logger.warning("Announcement clip %s: unreadable duration; "
                           "making it again", path.name)
    folder.mkdir(parents=True, exist_ok=True)
    # Written aside and moved in place: two first requests at once each
    # make a whole file, and a reader never sees half of one.
    tmp = folder / f".{variant}-{key}-{uuid.uuid4().hex}.mp3"
    try:
        duration = _synthesize(ANNOUNCEMENT_TEXTS[variant], tmp)
    except Exception as e:  # noqa: BLE001 - the reply plays alone
        tmp.unlink(missing_ok=True)
        logger.warning("Announcement clip %s could not be made: %s",
                       variant, type(e).__name__)
        return None
    _log_cost(user_id, duration)
    meta.write_text(json.dumps({"duration": duration}))
    os.replace(tmp, path)
    return Clip(variant, key, path, duration)

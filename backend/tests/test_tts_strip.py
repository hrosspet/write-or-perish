"""Unit tests for TTS proposal heading-stripping (#158).

``_strip_heading_sections`` decides what a structured Voice proposal speaks:
the intro (before the first ### heading), the ### Note body, and any trailing
commentary the model appends below the structured block (after a single-line
Category / Feedback category value). The structured lists/values are rendered
visually in the proposal card and must NOT be spoken.

Imports the real tts module against stub glue (celery / openai / pydub /
backend.celery_app), then restores it so the rest of the suite is unaffected —
same pattern as test_artifacts.py.
"""
import os
import sys
from unittest.mock import MagicMock

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ENCRYPTION_DISABLED", "true")

# celery.Task must be a real base class so `class TTSTask(Task)` imports.
_celery_stub = MagicMock()
_celery_stub.Task = object

_GLUE = {
    "celery": _celery_stub,
    "celery.utils": MagicMock(),
    "celery.utils.log": MagicMock(),
    "openai": MagicMock(),
    "pydub": MagicMock(),
    "backend.celery_app": MagicMock(),
}
_saved = {k: sys.modules.get(k) for k in _GLUE}
for _k, _v in _GLUE.items():
    sys.modules[_k] = _v
sys.modules.pop("backend.tasks.tts", None)

from backend.tasks.tts import _strip_heading_sections  # noqa: E402

for _k, _v in _saved.items():
    if _v is None:
        sys.modules.pop(_k, None)
    else:
        sys.modules[_k] = _v
sys.modules.pop("backend.tasks.tts", None)


def test_intro_and_note_spoken_lists_not():
    text = ("Nice work today.\n\n### Completed\n- ship it\n\n"
            "### Note\nYou closed three threads.")
    out = _strip_heading_sections(text)
    assert "Nice work today." in out
    assert "You closed three threads." in out
    assert "ship it" not in out


def test_trailing_commentary_after_feedback_category_spoken():
    text = ("That's great to hear.\n\n### Feedback\nLove the voice mode.\n\n"
            "### Feedback category\npraise\n\nLet me know if that captures it.")
    out = _strip_heading_sections(text)
    assert "That's great to hear." in out
    assert "Let me know if that captures it." in out
    # Structured parts are shown in the card, never spoken.
    assert "Love the voice mode." not in out
    assert "praise" not in out


def test_trailing_commentary_after_issue_category_spoken():
    text = ("Here is the issue.\n\n### Issue Title\nAdd dark mode\n\n"
            "### Description\nUsers want it.\n\n### Category\nenhancement\n\n"
            "Want me to file it?")
    out = _strip_heading_sections(text)
    assert "Here is the issue." in out
    assert "Want me to file it?" in out
    assert "enhancement" not in out


def test_plain_text_passes_through():
    text = "Just a normal spoken reply with no proposal."
    assert _strip_heading_sections(text) == text

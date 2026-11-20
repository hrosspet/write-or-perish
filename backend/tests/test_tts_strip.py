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

import pytest

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
    "pydub.exceptions": MagicMock(),
    "backend.celery_app": MagicMock(),
}
_saved = {k: sys.modules.get(k) for k in _GLUE}
for _k, _v in _GLUE.items():
    sys.modules[_k] = _v
sys.modules.pop("backend.tasks.tts", None)

from backend.tasks.tts import (  # noqa: E402
    _node_spoken_text, _strip_heading_sections, _strip_quote_markers,
)

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


def test_trailing_commentary_after_share_type_spoken():
    text = ("Happy to make that shareable.\n\n### Share\nLooking for a "
            "thinking partner on consciousness.\n\n### Share type\nneed\n\n"
            "Saved it as a draft when you confirm.")
    out = _strip_heading_sections(text)
    assert "Happy to make that shareable." in out
    assert "Saved it as a draft when you confirm." in out
    # Structured parts are shown in the card, never spoken.
    assert "thinking partner" not in out
    assert "need" not in out


def test_share_fence_block_never_spoken():
    """Fenced :::share blocks (the current share syntax) are shown in the
    card, never spoken — including ### headings inside the fence."""
    text = ("Happy to make that shareable.\n\n"
            ":::share need\n"
            "Looking for a thinking partner on consciousness.\n\n"
            "### With its own heading\nInner body line.\n"
            ":::\n\n"
            "Saved as a draft when you confirm.")
    out = _strip_heading_sections(text)
    assert "Happy to make that shareable." in out
    assert "Saved as a draft when you confirm." in out
    assert "thinking partner" not in out
    assert "Inner body line." not in out


def test_multiple_share_fences_stripped():
    text = ("Two pieces.\n\n:::share insight\nALPHA\n:::\n\n"
            "And another:\n\n:::share exploration\nBETA\n:::\n\nDone.")
    out = _strip_heading_sections(text)
    assert "Two pieces." in out
    assert "And another:" in out
    assert "Done." in out
    assert "ALPHA" not in out and "BETA" not in out


def test_quote_markers_never_spoken():
    """{quote:ID} / {quote_ext:ID} render as cards visually; TTS must not
    read the literal marker aloud (#208 quote-as-response)."""
    text = ("Found it. {quote_ext:836} That's the guide by @blissbrah.\n"
            "Also see {quote:123} from March.")
    out = _strip_quote_markers(text)
    assert "quote_ext" not in out and "{quote:" not in out
    assert "Found it. That's the guide by @blissbrah." in out
    assert "Also see from March." in out  # prose flows, no double spaces


def test_quote_marker_only_content_becomes_empty():
    assert _strip_quote_markers("{quote_ext:5}") == ""
    assert _strip_quote_markers("") == ""


# ── Links (#461) ────────────────────────────────────────────────────────

_PR = "https://github.com/hrosspet/write-or-perish/pull/460"
_PROPOSAL_META = '[{"name": "propose_github_issue"}]'


def test_node_text_speaks_links_and_addresses():
    text = (f"Merged [459]({_PR[:-3]}459) and {{quote:12}} more.\n\n"
            f"{_PR}\n\nhttps://www.example.com/a/b and <https://x.org/y>.")
    assert _node_spoken_text(text) == (
        "Merged 459 and more.\n\nPR 460\n\n"
        "a link to example.com and a link to x.org.")


def test_node_text_proposal_reply_speaks_links_in_its_prose():
    text = (f"I'd file this, see [the PR]({_PR}).\n\n"
            "### Issue Title\nBug\n### Description\nIt breaks.\n"
            f"### Category\nbug\nMore at {_PR}.")
    assert _node_spoken_text(text, _PROPOSAL_META) == (
        "I'd file this, see the PR.\n\nMore at PR 460.")


def test_node_text_with_only_an_image_is_empty():
    """Nothing left to speak: the task skips TTS as for empty text."""
    assert _node_spoken_text("![](https://x.com/p.png)") == ""
    assert _node_spoken_text(None) == ""


# ── Code blocks (#646) ──────────────────────────────────────────────────

def test_node_text_speaks_a_code_block_as_one_phrase():
    from backend.utils.spoken_links import CODE_BLOCK_SPOKEN
    text = ("Use `os.path` here:\n\n```python\nimport os\n"
            "print(os.path.join('a', 'b'))\n```\n\nThat prints a/b.")
    assert _node_spoken_text(text) == (
        f"Use `os.path` here:\n\n{CODE_BLOCK_SPOKEN}\n\nThat prints a/b.")


def test_node_text_list_item_fence_keeps_the_prose_after_it():
    from backend.utils.spoken_links import CODE_BLOCK_SPOKEN
    text = ("Do this:\n\n1. ```bash\n   pip install x\n   ```\n2. Run it.\n\n"
            "Then read the docs.")
    assert _node_spoken_text(text) == (
        f"Do this:\n\n1. {CODE_BLOCK_SPOKEN}\n2. Run it.\n\n"
        "Then read the docs.")


def test_node_text_proposal_reply_leaves_code_out_of_its_prose():
    from backend.utils.spoken_links import CODE_BLOCK_SPOKEN
    text = ("I'd file this; it fails here:\n\n```\nTraceback (most recent "
            "call last):\n  File \"x.py\"\n```\n\n"
            "### Issue Title\nBug\n### Description\nIt breaks.\n"
            "### Category\nbug\nThanks.")
    assert _node_spoken_text(text, _PROPOSAL_META) == (
        f"I'd file this; it fails here:\n\n{CODE_BLOCK_SPOKEN}\n\nThanks.")


@pytest.mark.parametrize("text", [
    f"A link [459]({_PR[:-3]}459) mid-sentence.",
    f"Intro.\n\n## Fix [460]({_PR})\nBody.",
    f"Merged:\n{_PR}\nThat's it.",
    "Read https://www.example.com/a/b?c=d, then rest.",
    "Autolink <https://example.org/x/y> here.",
    # Code blocks (#646): one phrase each, no chapter from a comment.
    # Parity only, so the first and the third are controls (before #646
    # both paths read the code, and cut the same chapters at comments).
    "Here:\n\n```python\n# setup\nimport os\n## not a chapter\n```\n\n"
    "Done. Next steps follow.",
    "Steps:\n1. Install:\n    ```bash\n    pip install x\n    ```\n2. Run.",
    "## A\nIntro.\n\n~~~\n# x\n~~~\n\n## B\nEnd.",
    "Steps:\n1. ```bash\n   pip install x\n   ```\n2. Run it.\n\nThen more.",
])
def test_node_text_matches_the_streamed_reply(text):
    """The batch task and a reply spoken while written say the same,
    however the stream is cut (here: every split into two deltas)."""
    from backend.utils.audio_processing import section_aware_chunk_text
    from backend.utils.tts_stream_text import ChunkPlanner, SpokenTextProjector

    def streamed(pieces):
        proj, planner = SpokenTextProjector(), ChunkPlanner()
        for piece in pieces:
            planner.add(proj.feed(piece))
        planner.add(proj.close())
        planner.close()
        chunks = []
        while (c := planner.take(10_000)) is not None:
            chunks.append((c.text, c.section_title, c.section_index))
        return chunks

    batch = section_aware_chunk_text(_node_spoken_text(text))
    assert "https" not in str(batch)
    for cut in range(1, len(text)):
        assert streamed([text[:cut], text[cut:]]) == batch, cut

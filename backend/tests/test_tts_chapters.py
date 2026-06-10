"""Tests for section-aware TTS chunking + chapters (#145, #140)."""
import os
import sys
from unittest.mock import MagicMock

os.environ["ENCRYPTION_DISABLED"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("TWITTER_API_KEY", "fake")
os.environ.setdefault("TWITTER_API_SECRET", "fake")

sys.modules.setdefault("celery", MagicMock())
sys.modules.setdefault("celery.utils", MagicMock())
sys.modules.setdefault("celery.utils.log", MagicMock())
sys.modules.setdefault("celery.result", MagicMock())
sys.modules.setdefault("ffmpeg", MagicMock())

import pytest  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

from backend.utils.audio_processing import (  # noqa: E402
    MIN_FIRST_CHUNK_CHARS, section_aware_chunk_text, split_sections,
)

LONG = ("This is a reasonably long sentence used to pad sections so the "
        "chunker has real material to work with. " * 12)


# ── split_sections ───────────────────────────────────────────────────────

def test_split_sections_h1_h2_are_chapters():
    text = f"Preamble text.\n\n# Alpha\n{LONG}\n## Beta\n{LONG}\n### gamma\nstays"
    sections = split_sections(text)
    titles = [t for t, _ in sections]
    assert titles == [None, "Alpha", "Beta"]
    # h3 stays inside Beta's body
    assert "### gamma" in sections[2][1]


def test_split_sections_no_headings():
    assert split_sections("just text") == [(None, "just text")]
    assert split_sections("") == []


def test_split_sections_heading_line_not_in_body():
    sections = split_sections("# Title\nBody here")
    assert sections == [("Title", "Body here")]


# ── section_aware_chunk_text ─────────────────────────────────────────────

def test_chunks_never_cross_sections():
    text = f"# One\n{LONG}\n# Two\n{LONG}"
    chunks = section_aware_chunk_text(text)
    for _text, title, idx in chunks:
        assert title in ("One", "Two")
    # Spoken titles (v2): each section's first chunk opens with the
    # bare title, sentence-terminated, blank-line-separated.
    firsts = {}
    for c, _t, i in chunks:
        firsts.setdefault(i, c)
    assert firsts[0].startswith("One.\n\n")
    assert firsts[1].startswith("Two.\n\n")
    # Contiguous non-decreasing section indices
    indices = [idx for _, _, idx in chunks]
    assert indices == sorted(indices)
    assert set(indices) == {0, 1}


def test_first_chunk_stays_small_globally():
    text = f"# One\n{LONG}\n# Two\n{LONG}"
    chunks = section_aware_chunk_text(text)
    assert len(chunks[0][0]) <= 320  # fast-start budget
    assert any(len(c) > 1000 for c, _, _ in chunks[1:])  # later are big


def test_no_heading_text_single_section_matches_legacy_sizing():
    chunks = section_aware_chunk_text(LONG * 3)
    assert all(title is None and idx == 0 for _, title, idx in chunks)
    assert len(chunks) >= 3


def test_empty_sections_skipped():
    chunks = section_aware_chunk_text("# Empty\n# Full\ncontent here")
    assert [(t, i) for _, t, i in chunks] == [("Full", 1)]


def test_140_tiny_first_sentence_falls_back_to_word_split():
    # First sentence is tiny ("Hi.") — sentence split would give a
    # sub-MIN chunk; the fallback splits word-aware near the budget.
    text = "Hi. " + ("word " * 400)
    chunks = section_aware_chunk_text(text)
    first = chunks[0][0]
    assert len(first) >= MIN_FIRST_CHUNK_CHARS
    assert not first.endswith("wor")  # never mid-word


# ── chapters endpoint ────────────────────────────────────────────────────

def test_chapters_endpoint(tmp_path):
    from flask import Flask
    from backend.extensions import db as _db
    from backend.models import User, Node, TTSChunk
    from flask_login import LoginManager

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    _db.init_app(app)
    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from backend.routes.nodes import nodes_bp
    app.register_blueprint(nodes_bp, url_prefix="/api/nodes")

    with app.app_context():
        _db.create_all()
        user = User(username="tester")
        _db.session.add(user)
        _db.session.flush()
        node = Node(user_id=user.id, human_owner_id=user.id,
                    node_type="text")
        node.set_content("x")
        _db.session.add(node)
        _db.session.flush()
        specs = [
            (0, None, None, 10.0),
            (1, 0, "Intro part", None),     # legacy row w/o duration
            (2, 1, "Chapter A", 20.0),
            (3, 1, "Chapter A", 30.0),
            (4, 2, "Chapter B", 5.0),
        ]
        # Row 0 simulates pre-#145 chunks (no section metadata)
        for idx, s_idx, s_title, dur in specs:
            _db.session.add(TTSChunk(
                node_id=node.id, chunk_index=idx, status='completed',
                section_index=s_idx, section_title=s_title, duration=dur))
        _db.session.commit()

        client = app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
            sess["_fresh"] = True

        body = client.get(
            f"/api/nodes/{node.id}/tts-chapters").get_json()
        chapters = body["chapters"]
        assert [c["title"] for c in chapters] == [
            "Intro part", "Chapter A", "Chapter B"]
        assert [c["chunk_index"] for c in chapters] == [1, 2, 4]
        # Start times accumulate prior durations (None counts as 0)
        assert [c["start_time"] for c in chapters] == [10.0, 10.0, 60.0]

        # Single/no sections → empty list (no chapter UI)
        node2 = Node(user_id=user.id, human_owner_id=user.id,
                     node_type="text")
        node2.set_content("y")
        _db.session.add(node2)
        _db.session.flush()
        _db.session.add(TTSChunk(node_id=node2.id, chunk_index=0,
                                 status='completed', duration=5.0))
        _db.session.commit()
        assert client.get(
            f"/api/nodes/{node2.id}/tts-chapters"
        ).get_json()["chapters"] == []

        _db.session.rollback()
        _db.drop_all()


def test_spoken_title_keeps_existing_punctuation():
    chunks = section_aware_chunk_text("# Ready?\nBody text here.")
    assert chunks[0][0].startswith("Ready?\n\n")
    # No markdown markers in the spoken text
    assert "#" not in chunks[0][0]

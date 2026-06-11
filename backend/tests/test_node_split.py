"""Tests for the per-node content cap and serial-chain splitting.

A single node above the chunk budget stalls chunked profile generation
(the budget window admits it alone, the resolver can't fit it, the
export returns None and the resume cursor never advances). The cap +
split keep every node below the budget while preserving content
losslessly across a serial parent→child chain.
"""
import os
import sys
from datetime import datetime
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

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

for _mod in ["backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

from backend.extensions import db  # noqa: E402
from backend.models import User, Node  # noqa: E402
from backend.utils.node_split import (  # noqa: E402
    NODE_CHAR_CAP, split_text_at_cap, split_node_into_chain,
)


@pytest.fixture
def app():
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    db.init_app(app)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


# ── split_text_at_cap ───────────────────────────────────────────────────

def test_short_text_untouched():
    assert split_text_at_cap("hello\nworld", cap=100) == ["hello\nworld"]


def test_exact_cap_untouched():
    text = "x" * 100
    assert split_text_at_cap(text, cap=100) == [text]


def test_lossless_newline_split():
    lines = [f"line {i} " + "x" * 30 for i in range(100)]
    text = "\n".join(lines)
    segments = split_text_at_cap(text, cap=500)
    assert len(segments) > 1
    assert "".join(segments) == text
    for seg in segments:
        assert len(seg) <= 500
    # No segment starts mid-line: every boundary fell on a newline,
    # so each earlier segment ends with one.
    for seg in segments[:-1]:
        assert seg.endswith("\n")


def test_single_long_line_hard_cut():
    text = "x" * 1200  # no newlines at all
    segments = split_text_at_cap(text, cap=500)
    assert segments == ["x" * 500, "x" * 500, "x" * 200]
    assert "".join(segments) == text


def test_mixed_long_line_and_newlines():
    text = "short\n" + "y" * 800 + "\ntail"
    segments = split_text_at_cap(text, cap=500)
    assert "".join(segments) == text
    for seg in segments:
        assert len(seg) <= 500


def test_default_cap_value():
    assert NODE_CHAR_CAP == 100_000


# ── split_node_into_chain ───────────────────────────────────────────────

def _user(username="alice"):
    u = User(username=username, approved=True, plan="alpha")
    db.session.add(u)
    db.session.flush()
    return u


def _node(user, content, parent_id=None, **kw):
    n = Node(user_id=user.id, human_owner_id=user.id, parent_id=parent_id,
             node_type=kw.pop("node_type", "user"),
             privacy_level="private", ai_usage="chat",
             token_count=len(content) // 4, **kw)
    n.set_content(content)
    n.created_at = kw.get("created_at", datetime(2026, 6, 1, 12, 0, 0))
    db.session.add(n)
    db.session.flush()
    return n


def test_chain_split_preserves_content_and_order(app):
    user = _user()
    lines = "\n".join(f"row {i} " + "z" * 40 for i in range(60))
    node = _node(user, lines)
    reply = _node(user, "a reply", parent_id=node.id)
    db.session.commit()

    parts = split_node_into_chain(
        node, segments=split_text_at_cap(lines, cap=600))
    db.session.commit()

    assert len(parts) >= 1
    # Lossless reassembly across the chain
    chain_text = node.get_content() + "".join(
        p.get_content() for p in parts)
    assert chain_text == lines
    # Serial chain: each part is the child of the previous
    prev = node
    for p in parts:
        assert p.parent_id == prev.id
        assert p.created_at > prev.created_at
        assert p.node_type == node.node_type
        assert p.ai_usage == node.ai_usage
        assert p.privacy_level == node.privacy_level
        prev = p
    # Pre-existing reply re-parented onto the chain tip
    assert reply.parent_id == parts[-1].id
    # Original keeps its id and the first segment only
    assert node.token_count == len(node.get_content()) // 4


def test_voice_node_split_keeps_audio_on_head(app):
    """Splitting a transcribed voice node leaves the audio linkage on
    the head node only — playback plays the full recording from the
    chain head; parts carry transcript text, not audio."""
    user = _user()
    transcript = "\n".join(f"spoken sentence {i}" for i in range(200))
    node = _node(user, transcript)
    node.audio_original_url = "/media/nodes/1/original.webm"
    db.session.commit()

    parts = split_node_into_chain(
        node, segments=split_text_at_cap(transcript, cap=800))
    db.session.commit()

    assert len(parts) >= 1
    assert node.audio_original_url == "/media/nodes/1/original.webm"
    for p in parts:
        assert p.audio_original_url is None
    chain_text = node.get_content() + "".join(
        p.get_content() for p in parts)
    assert chain_text == transcript


def test_chain_split_noop_below_cap(app):
    user = _user()
    node = _node(user, "small content")
    db.session.commit()
    assert split_node_into_chain(node) == []
    assert node.get_content() == "small content"


# ── API: create auto-splits, edit rejects ───────────────────────────────

@pytest.fixture
def client(app, monkeypatch):
    from flask_login import LoginManager
    import backend.routes.nodes as nodes_module

    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    app.register_blueprint(nodes_module.nodes_bp, url_prefix="/api/nodes")
    with app.app_context():
        user = _user("poster")
        db.session.commit()
        uid = user.id
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(uid)
        sess["_fresh"] = True
    return client


def test_create_node_auto_splits(app, client, monkeypatch):
    monkeypatch.setattr(
        "backend.utils.context_artifacts.sync_context_artifacts",
        lambda *a, **k: None)
    big = "\n".join(f"line {i} " + "q" * 90 for i in range(1500))
    assert len(big) > NODE_CHAR_CAP
    res = client.post("/api/nodes/", json={
        "content": big, "ai_usage": "chat", "privacy_level": "private"})
    assert res.status_code == 201
    data = res.get_json()
    assert data["split_into"] >= 2
    with app.app_context():
        first = db.session.get(Node, data["id"])
        chain = [first]
        cur = first
        while True:
            child = Node.query.filter_by(parent_id=cur.id).first()
            if child is None:
                break
            chain.append(child)
            cur = child
        assert len(chain) == data["split_into"]
        assert "".join(n.get_content() for n in chain) == big
        for n in chain:
            assert len(n.get_content()) <= NODE_CHAR_CAP
        assert chain[-1].id == data["tip_id"]


def test_create_small_node_not_split(app, client, monkeypatch):
    monkeypatch.setattr(
        "backend.utils.context_artifacts.sync_context_artifacts",
        lambda *a, **k: None)
    res = client.post("/api/nodes/", json={
        "content": "just a note", "ai_usage": "chat",
        "privacy_level": "private"})
    assert res.status_code == 201
    data = res.get_json()
    assert data["split_into"] == 1
    assert data["tip_id"] == data["id"]


def test_update_node_rejects_oversized(app, client, monkeypatch):
    monkeypatch.setattr(
        "backend.utils.context_artifacts.sync_context_artifacts",
        lambda *a, **k: None)
    res = client.post("/api/nodes/", json={
        "content": "v1", "ai_usage": "chat", "privacy_level": "private"})
    nid = res.get_json()["id"]
    res = client.put(f"/api/nodes/{nid}", json={
        "content": "x" * (NODE_CHAR_CAP + 1)})
    assert res.status_code == 422
    assert res.get_json()["char_cap"] == NODE_CHAR_CAP

"""The tts-stream SSE of a node whose TTS has finished.

A client can reach it after the TTS completed: its stream was cut, or
(voice) it closed the stream as stalled a moment before the last chunk
was sent. The stream then replays the chunks the client doesn't have and
ends with all_complete, as a live one would have.
"""
import json
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

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

from backend.extensions import db as _db  # noqa: E402
from backend.models import Node, TTSChunk, User  # noqa: E402


@pytest.fixture
def app():
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

    from backend.routes.sse import sse_bp
    app.register_blueprint(sse_bp, url_prefix="/api/sse")

    with app.app_context():
        _db.create_all()
        _db.session.add(User(username="tester"))
        _db.session.commit()
        yield app
        _db.session.rollback()
        _db.drop_all()


@pytest.fixture
def client(app):
    client = app.test_client()
    user = User.query.first()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return client


def _node(tts_status, audio_url=None, chunks=0):
    user = User.query.first()
    node = Node(user_id=user.id, node_type="llm",
                tts_task_status=tts_status, audio_tts_url=audio_url)
    node.set_content("The reply, spoken.")
    _db.session.add(node)
    _db.session.flush()
    for i in range(chunks):
        _db.session.add(TTSChunk(
            node_id=node.id, chunk_index=i, status="completed",
            audio_url=f"/media/n/tts_chunk_{i}.mp3?v=1", duration=2.0))
    _db.session.commit()
    return node.id


def _events(resp):
    """[(event, data)] of an SSE body."""
    out = []
    for block in resp.get_data(as_text=True).strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        out.append((lines.get("event"), json.loads(lines["data"])))
    return out


def test_completed_node_replays_the_missing_chunks_then_ends(client):
    node_id = _node("completed", "/media/n/tts.mp3?v=1", chunks=2)
    resp = client.get(f"/api/sse/nodes/{node_id}/tts-stream?last_chunk=0")
    assert resp.mimetype == "text/event-stream"
    events = _events(resp)
    assert [e for e, _ in events] == ["chunk_ready", "all_complete"]
    assert events[0][1]["chunk_index"] == 1
    assert events[1][1]["tts_url"] == "/media/n/tts.mp3?v=1"
    assert events[1][1]["continuation_node_id"] is None


def test_reconnect_without_last_chunk_replays_every_chunk(client):
    node_id = _node("completed", "/media/n/tts.mp3?v=1", chunks=2)
    events = _events(client.get(f"/api/sse/nodes/{node_id}/tts-stream"))
    assert [(e, d.get("chunk_index")) for e, d in events] == [
        ("chunk_ready", 0), ("chunk_ready", 1), ("all_complete", None)]


def test_completed_without_audio_still_ends_the_stream(client):
    # A voice reply that was all proposal card: nothing spoken.
    node_id = _node("completed")
    events = _events(client.get(f"/api/sse/nodes/{node_id}/tts-stream"))
    assert [e for e, _ in events] == ["all_complete"]
    assert events[0][1]["tts_url"] is None


def test_node_with_no_tts_run_answers_without_a_stream(client):
    node_id = _node(None, "/media/n/tts.mp3?v=1")
    assert client.get(
        f"/api/sse/nodes/{node_id}/tts-stream").get_json()["status"] \
        == "completed"
    node_id = _node("failed")
    assert client.get(
        f"/api/sse/nodes/{node_id}/tts-stream").status_code == 400

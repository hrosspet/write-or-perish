"""Tests for the Claude import analyze endpoint.

The client extracts conversations.json from the Claude export zip in the
browser and uploads only that file, so the analyze endpoint now reads the
JSON field directly (no zip handling).

Covers POST /api/import/claude/analyze:
- request shape validation (missing field, empty filename)
- JSON parse errors
- empty-conversations case
- happy path (single + multiple conversations)
- login required
"""

import io
import json
import os
import sys
from unittest.mock import MagicMock

# ── Environment ──────────────────────────────────────────────────────────
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
from flask import Flask  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login          # noqa: E402
from backend.extensions import db as _db         # noqa: E402
from backend.models import User                  # noqa: E402
import backend.models as _real_backend_models    # noqa: E402


def _make_app():
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

    from backend.routes.import_data import import_bp
    app.register_blueprint(import_bp, url_prefix="/api")

    return app


@pytest.fixture
def app():
    _affected = lambda k: (  # noqa: E731
        k == "flask_login"
        or k.startswith("backend.routes")
        or k == "backend.models"
    )
    saved = {k: sys.modules[k] for k in list(sys.modules) if _affected(k)}

    sys.modules["flask_login"] = _real_flask_login
    sys.modules["backend.models"] = _real_backend_models
    for _k in [k for k in list(sys.modules) if k.startswith("backend.routes")]:
        del sys.modules[_k]

    app = _make_app()
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()

    for k in [k for k in list(sys.modules) if _affected(k)]:
        if k not in saved:
            del sys.modules[k]
    for k, mod in saved.items():
        sys.modules[k] = mod


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True


def _make_user(username, **kwargs):
    u = User(username=username, approved=True, plan="alpha", **kwargs)
    _db.session.add(u)
    _db.session.flush()
    return u


def _post_conversations(client, raw_conversations):
    payload = json.dumps(raw_conversations).encode("utf-8")
    data = {
        "conversations_file": (io.BytesIO(payload), "conversations.json"),
    }
    return client.post(
        "/api/import/claude/analyze",
        data=data,
        content_type="multipart/form-data",
    )


# ── Fixtures ─────────────────────────────────────────────────────────────

def _msg(text="hello", sender="human", created_at="2023-11-14T22:13:20Z"):
    return {
        "text": text,
        "sender": sender,
        "created_at": created_at,
    }


def _conv(name="First chat", created_at="2023-11-14T22:13:20Z",
          chat_messages=None):
    if chat_messages is None:
        chat_messages = [
            _msg(text="hello world", sender="human"),
            _msg(text="hi there, friend", sender="assistant"),
        ]
    return {
        "name": name,
        "created_at": created_at,
        "chat_messages": chat_messages,
    }


# ── POST /api/import/claude/analyze ──────────────────────────────────────

class TestAnalyzeClaudeImport:
    def test_rejects_missing_field(self, app):
        client = app.test_client()
        alice = _make_user("alice")
        _db.session.commit()
        _login(client, alice.id)

        resp = client.post(
            "/api/import/claude/analyze",
            data={},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400
        assert "conversations_file" in resp.get_json()["error"]

    def test_rejects_empty_filename(self, app):
        client = app.test_client()
        alice = _make_user("alice")
        _db.session.commit()
        _login(client, alice.id)

        data = {
            "conversations_file": (io.BytesIO(b"[]"), ""),
        }
        resp = client.post(
            "/api/import/claude/analyze",
            data=data,
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400

    def test_rejects_invalid_json(self, app):
        client = app.test_client()
        alice = _make_user("alice")
        _db.session.commit()
        _login(client, alice.id)

        data = {
            "conversations_file": (
                io.BytesIO(b"{not json"),
                "conversations.json",
            ),
        }
        resp = client.post(
            "/api/import/claude/analyze",
            data=data,
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400
        body = resp.get_json()
        assert "parse" in body["error"].lower()
        assert "details" in body

    def test_empty_array_returns_400(self, app):
        client = app.test_client()
        alice = _make_user("alice")
        _db.session.commit()
        _login(client, alice.id)

        resp = _post_conversations(client, [])
        assert resp.status_code == 400
        assert "No conversations" in resp.get_json()["error"]

    def test_conversation_without_messages_is_skipped(self, app):
        client = app.test_client()
        alice = _make_user("alice")
        _db.session.commit()
        _login(client, alice.id)

        # A conversation whose only message has blank text yields no
        # usable messages → endpoint reports no conversations.
        resp = _post_conversations(
            client,
            [_conv(chat_messages=[_msg(text="   ")])],
        )
        assert resp.status_code == 400
        assert "No conversations" in resp.get_json()["error"]

    def test_happy_path_single_conversation(self, app):
        client = app.test_client()
        alice = _make_user("alice")
        _db.session.commit()
        _login(client, alice.id)

        resp = _post_conversations(
            client,
            [_conv(name="First chat")],
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["total_conversations"] == 1
        assert body["total_messages"] == 2
        assert body["total_tokens"] > 0
        assert body["total_size"] > 0
        conv = body["conversations"][0]
        assert conv["name"] == "First chat"
        assert conv["message_count"] == 2
        senders = [m["sender"] for m in conv["messages"]]
        assert senders == ["human", "assistant"]
        texts = [m["text"] for m in conv["messages"]]
        assert texts == ["hello world", "hi there, friend"]

    def test_happy_path_multiple_conversations(self, app):
        client = app.test_client()
        alice = _make_user("alice")
        _db.session.commit()
        _login(client, alice.id)

        resp = _post_conversations(
            client,
            [
                _conv(name="A"),
                _conv(name="B"),
            ],
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["total_conversations"] == 2
        assert body["total_messages"] == 4
        names = [c["name"] for c in body["conversations"]]
        assert names == ["A", "B"]

    def test_blank_messages_filtered_within_conversation(self, app):
        client = app.test_client()
        alice = _make_user("alice")
        _db.session.commit()
        _login(client, alice.id)

        resp = _post_conversations(
            client,
            [
                _conv(
                    name="Mixed",
                    chat_messages=[
                        _msg(text="real question", sender="human"),
                        _msg(text="   ", sender="assistant"),
                        _msg(text="real answer", sender="assistant"),
                    ],
                ),
            ],
        )
        assert resp.status_code == 200
        body = resp.get_json()
        # Blank message dropped → 2 usable messages remain.
        assert body["total_messages"] == 2
        conv = body["conversations"][0]
        assert conv["message_count"] == 2
        texts = [m["text"] for m in conv["messages"]]
        assert texts == ["real question", "real answer"]

    def test_requires_login(self, app):
        client = app.test_client()
        resp = _post_conversations(client, [])
        # flask_login redirects unauthenticated requests (302/401).
        assert resp.status_code in (302, 401)

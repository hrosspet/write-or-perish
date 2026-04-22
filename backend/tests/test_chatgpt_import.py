"""Tests for ChatGPT import: linearization + analyze endpoint.

Covers:
- _linearize_chatgpt_messages: graph traversal, role filtering, empty/branching
  cases, create_time parsing.
- POST /api/import/chatgpt/analyze: request shape validation, JSON parse errors,
  empty-conversations case, happy path with realistic mapping graph.
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
        "/api/import/chatgpt/analyze",
        data=data,
        content_type="multipart/form-data",
    )


# ── _linearize_chatgpt_messages ──────────────────────────────────────────

class TestLinearizeChatgptMessages:
    def _linearize(self):
        from backend.routes.import_data import _linearize_chatgpt_messages
        return _linearize_chatgpt_messages

    def test_empty_mapping_returns_empty(self):
        assert self._linearize()({}) == []

    def test_no_root_returns_empty(self):
        # Every node has a parent → no root found
        mapping = {
            "a": {"parent": "b", "children": [], "message": None},
            "b": {"parent": "a", "children": [], "message": None},
        }
        assert self._linearize()(mapping) == []

    def test_simple_user_assistant_chain(self):
        mapping = {
            "root": {
                "parent": None,
                "children": ["u1"],
                "message": None,
            },
            "u1": {
                "parent": "root",
                "children": ["a1"],
                "message": {
                    "author": {"role": "user"},
                    "content": {"parts": ["hello"]},
                    "create_time": 1700000000,
                    "metadata": {},
                },
            },
            "a1": {
                "parent": "u1",
                "children": [],
                "message": {
                    "author": {"role": "assistant"},
                    "content": {"parts": ["hi there"]},
                    "create_time": 1700000001,
                    "metadata": {"model_slug": "gpt-4o"},
                },
            },
        }
        msgs = self._linearize()(mapping)
        assert [m["role"] for m in msgs] == ["user", "assistant"]
        assert [m["text"] for m in msgs] == ["hello", "hi there"]
        assert msgs[1]["model"] == "gpt-4o"
        assert msgs[0]["created_at"].startswith("2023-")

    def test_branching_follows_first_child(self):
        # u1 has two assistant children; linearizer picks the first.
        mapping = {
            "root": {"parent": None, "children": ["u1"], "message": None},
            "u1": {
                "parent": "root",
                "children": ["a_first", "a_second"],
                "message": {
                    "author": {"role": "user"},
                    "content": {"parts": ["q"]},
                    "create_time": None,
                },
            },
            "a_first": {
                "parent": "u1",
                "children": [],
                "message": {
                    "author": {"role": "assistant"},
                    "content": {"parts": ["first branch"]},
                    "create_time": None,
                },
            },
            "a_second": {
                "parent": "u1",
                "children": [],
                "message": {
                    "author": {"role": "assistant"},
                    "content": {"parts": ["second branch"]},
                    "create_time": None,
                },
            },
        }
        msgs = self._linearize()(mapping)
        assert [m["text"] for m in msgs] == ["q", "first branch"]

    def test_skips_non_user_assistant_roles(self):
        mapping = {
            "root": {"parent": None, "children": ["s1"], "message": None},
            "s1": {
                "parent": "root",
                "children": ["u1"],
                "message": {
                    "author": {"role": "system"},
                    "content": {"parts": ["you are helpful"]},
                    "create_time": None,
                },
            },
            "u1": {
                "parent": "s1",
                "children": ["t1"],
                "message": {
                    "author": {"role": "user"},
                    "content": {"parts": ["hi"]},
                    "create_time": None,
                },
            },
            "t1": {
                "parent": "u1",
                "children": [],
                "message": {
                    "author": {"role": "tool"},
                    "content": {"parts": ["tool output"]},
                    "create_time": None,
                },
            },
        }
        msgs = self._linearize()(mapping)
        assert [m["role"] for m in msgs] == ["user"]

    def test_skips_empty_text(self):
        mapping = {
            "root": {"parent": None, "children": ["u1"], "message": None},
            "u1": {
                "parent": "root",
                "children": [],
                "message": {
                    "author": {"role": "user"},
                    "content": {"parts": ["   "]},
                    "create_time": None,
                },
            },
        }
        assert self._linearize()(mapping) == []

    def test_non_string_parts_are_dropped(self):
        # Multimodal exports can include dict parts; we keep only strings.
        mapping = {
            "root": {"parent": None, "children": ["u1"], "message": None},
            "u1": {
                "parent": "root",
                "children": [],
                "message": {
                    "author": {"role": "user"},
                    "content": {
                        "parts": [
                            {"content_type": "image_asset_pointer"},
                            "caption text",
                        ]
                    },
                    "create_time": None,
                },
            },
        }
        msgs = self._linearize()(mapping)
        assert len(msgs) == 1
        assert msgs[0]["text"] == "caption text"

    def test_invalid_create_time_falls_back_to_empty(self):
        mapping = {
            "root": {"parent": None, "children": ["u1"], "message": None},
            "u1": {
                "parent": "root",
                "children": [],
                "message": {
                    "author": {"role": "user"},
                    "content": {"parts": ["hi"]},
                    "create_time": "not-a-number",
                },
            },
        }
        msgs = self._linearize()(mapping)
        assert msgs[0]["created_at"] == ""


# ── POST /api/import/chatgpt/analyze ─────────────────────────────────────

def _conv(title="t", mapping=None, create_time=1700000000,
          default_model_slug="gpt-4o"):
    return {
        "title": title,
        "create_time": create_time,
        "default_model_slug": default_model_slug,
        "mapping": mapping or {},
    }


def _simple_mapping():
    return {
        "root": {"parent": None, "children": ["u1"], "message": None},
        "u1": {
            "parent": "root",
            "children": ["a1"],
            "message": {
                "author": {"role": "user"},
                "content": {"parts": ["hello world"]},
                "create_time": 1700000000,
                "metadata": {},
            },
        },
        "a1": {
            "parent": "u1",
            "children": [],
            "message": {
                "author": {"role": "assistant"},
                "content": {"parts": ["hi there, friend"]},
                "create_time": 1700000001,
                "metadata": {"model_slug": "gpt-4o"},
            },
        },
    }


class TestAnalyzeChatgptImport:
    def test_rejects_missing_field(self, app):
        client = app.test_client()
        alice = _make_user("alice")
        _db.session.commit()
        _login(client, alice.id)

        resp = client.post(
            "/api/import/chatgpt/analyze",
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
            "/api/import/chatgpt/analyze",
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
            "/api/import/chatgpt/analyze",
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

    def test_conversation_without_mapping_is_skipped(self, app):
        client = app.test_client()
        alice = _make_user("alice")
        _db.session.commit()
        _login(client, alice.id)

        resp = _post_conversations(
            client,
            [_conv(mapping={})],
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
            [_conv(title="First chat", mapping=_simple_mapping())],
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["total_conversations"] == 1
        assert body["total_messages"] == 2
        assert body["total_tokens"] > 0
        assert body["total_size"] > 0
        conv = body["conversations"][0]
        assert conv["name"] == "First chat"
        assert conv["default_model"] == "gpt-4o"
        assert conv["message_count"] == 2
        assert conv["created_at"].startswith("2023-")
        roles = [m["role"] for m in conv["messages"]]
        assert roles == ["user", "assistant"]

    def test_happy_path_multiple_conversations(self, app):
        client = app.test_client()
        alice = _make_user("alice")
        _db.session.commit()
        _login(client, alice.id)

        resp = _post_conversations(
            client,
            [
                _conv(title="A", mapping=_simple_mapping()),
                _conv(title="B", mapping=_simple_mapping()),
            ],
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["total_conversations"] == 2
        assert body["total_messages"] == 4
        names = [c["name"] for c in body["conversations"]]
        assert names == ["A", "B"]

    def test_requires_login(self, app):
        client = app.test_client()
        resp = _post_conversations(client, [])
        # flask_login redirects unauthenticated requests (302/401).
        assert resp.status_code in (302, 401)

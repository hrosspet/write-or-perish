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
from backend.models import User, Node            # noqa: E402
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


def _reset_login_cache():
    """Clear Flask-Login's per-context user cache.

    The ``app`` fixture wraps the whole test in a single application
    context, so ``flask.g._login_user`` (set by Flask-Login on the first
    authenticated request) leaks into subsequent requests and pins
    ``current_user`` to the first user. Pop it so the next request
    re-loads the identity from its own session cookie.
    """
    from flask import g
    g.pop("_login_user", None)


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


def _confirm_conversations(client, conversations, **extra):
    """POST analyzed conversations to the ChatGPT confirm endpoint."""
    body = {"conversations": conversations}
    body.update(extra)
    return client.post(
        "/api/import/chatgpt/confirm",
        data=json.dumps(body),
        content_type="application/json",
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


# ── POST /api/import/chatgpt/confirm (dedup) ─────────────────────────────

def _analyzed_conv(name="First chat", created_at="2023-11-14T22:13:20",
                   messages=None):
    """Build a confirm-shaped conversation (mirrors analyze output)."""
    if messages is None:
        messages = [
            {
                "text": "hello world",
                "role": "user",
                "created_at": "2023-11-14T22:13:20",
                "model": "",
                "mapping_id": "u1",
            },
            {
                "text": "hi there, friend",
                "role": "assistant",
                "created_at": "2023-11-14T22:13:21",
                "model": "gpt-4o",
                "mapping_id": "a1",
            },
        ]
    return {
        "name": name,
        "created_at": created_at,
        "default_model": "gpt-4o",
        "messages": messages,
        "message_count": len(messages),
        "token_count": sum(len(m["text"]) // 4 for m in messages),
    }


class TestConfirmChatgptImportDedup:
    def _count_nodes(self, user_id):
        return Node.query.filter_by(human_owner_id=user_id).count()

    def test_reimport_same_archive_creates_zero_new_nodes(self, app):
        client = app.test_client()
        alice = _make_user("alice")
        _db.session.commit()
        _login(client, alice.id)

        convs = [_analyzed_conv()]

        # First import: both messages become nodes.
        r1 = _confirm_conversations(client, convs)
        assert r1.status_code == 201
        b1 = r1.get_json()
        assert b1["created"] == 2
        assert b1["skipped"] == 0
        assert b1["nodes_created"] == 2
        assert self._count_nodes(alice.id) == 2

        # Re-import the identical archive: zero new nodes.
        r2 = _confirm_conversations(client, convs)
        assert r2.status_code == 201
        b2 = r2.get_json()
        assert b2["created"] == 0
        assert b2["skipped"] == 2
        assert self._count_nodes(alice.id) == 2

    def test_overlapping_snapshot_adds_only_new_messages(self, app):
        client = app.test_client()
        alice = _make_user("alice")
        _db.session.commit()
        _login(client, alice.id)

        first = [_analyzed_conv()]
        r1 = _confirm_conversations(client, first)
        assert r1.status_code == 201
        assert r1.get_json()["created"] == 2
        assert self._count_nodes(alice.id) == 2

        # A later snapshot of the same conversation: the original two
        # messages plus one genuinely-new follow-up.
        extended_messages = [
            {
                "text": "hello world",
                "role": "user",
                "created_at": "2023-11-14T22:13:20",
                "model": "",
                "mapping_id": "u1",
            },
            {
                "text": "hi there, friend",
                "role": "assistant",
                "created_at": "2023-11-14T22:13:21",
                "model": "gpt-4o",
                "mapping_id": "a1",
            },
            {
                "text": "one more question",
                "role": "user",
                "created_at": "2023-11-14T22:14:00",
                "model": "",
                "mapping_id": "u2",
            },
        ]
        second = [_analyzed_conv(messages=extended_messages)]
        r2 = _confirm_conversations(client, second)
        assert r2.status_code == 201
        b2 = r2.get_json()
        assert b2["created"] == 1
        assert b2["skipped"] == 2
        # No new thread was started: the follow-up extends the old one.
        assert b2["thread_count"] == 0
        assert self._count_nodes(alice.id) == 3

        # The new message must chain onto the existing copy of the
        # message that precedes it — not become an orphaned root.
        new_node = Node.query.filter_by(
            human_owner_id=alice.id, source_key="chatgpt:u2"
        ).one()
        prev_node = Node.query.filter_by(
            human_owner_id=alice.id, source_key="chatgpt:a1"
        ).one()
        assert new_node.parent_id == prev_node.id

    def test_renamed_conversation_still_dedups(self, app):
        # Renaming a conversation between exports must not change the
        # dedup keys (they are based on the mapping id alone).
        client = app.test_client()
        alice = _make_user("alice")
        _db.session.commit()
        _login(client, alice.id)

        r1 = _confirm_conversations(client, [_analyzed_conv(name="Old")])
        assert r1.get_json()["created"] == 2

        r2 = _confirm_conversations(client, [_analyzed_conv(name="New")])
        b2 = r2.get_json()
        assert b2["created"] == 0
        assert b2["skipped"] == 2
        assert self._count_nodes(alice.id) == 2

    def test_dedup_is_scoped_per_user(self, app):
        alice = _make_user("alice")
        bob = _make_user("bob")
        _db.session.commit()

        convs = [_analyzed_conv()]

        alice_client = app.test_client()
        _login(alice_client, alice.id)
        ra = _confirm_conversations(alice_client, convs)
        assert ra.get_json()["created"] == 2

        # Bob importing the same archive gets his own copy. Clear the
        # leaked Flask-Login cache so current_user re-loads as Bob.
        _reset_login_cache()
        bob_client = app.test_client()
        _login(bob_client, bob.id)
        rb = _confirm_conversations(bob_client, convs)
        assert rb.get_json()["created"] == 2
        assert rb.get_json()["skipped"] == 0
        assert self._count_nodes(alice.id) == 2
        assert self._count_nodes(bob.id) == 2

    def test_intra_request_duplicates_are_deduped(self, app):
        # Same conversation appears twice within one payload.
        client = app.test_client()
        alice = _make_user("alice")
        _db.session.commit()
        _login(client, alice.id)

        convs = [_analyzed_conv(), _analyzed_conv()]
        r = _confirm_conversations(client, convs)
        assert r.status_code == 201
        b = r.get_json()
        assert b["created"] == 2
        assert b["skipped"] == 2
        assert self._count_nodes(alice.id) == 2

    def test_source_key_persisted_on_created_nodes(self, app):
        client = app.test_client()
        alice = _make_user("alice")
        _db.session.commit()
        _login(client, alice.id)

        _confirm_conversations(client, [_analyzed_conv()])
        keys = [
            n.source_key
            for n in Node.query.filter_by(human_owner_id=alice.id).all()
        ]
        assert all(k and k.startswith("chatgpt:") for k in keys)


# ── POST /api/import/confirm (markdown zip, dedup) ───────────────────────

def _confirm_files(client, files, **extra):
    """POST analyzed markdown files to the generic confirm endpoint."""
    body = {"files": files}
    body.update(extra)
    return client.post(
        "/api/import/confirm",
        data=json.dumps(body),
        content_type="application/json",
    )


def _md_file(name, content, modified_at):
    return {
        "filename_without_ext": name,
        "content": content,
        "modified_at": modified_at,
    }


class TestConfirmMarkdownImportDedup:
    def _count_nodes(self, user_id):
        return Node.query.filter_by(human_owner_id=user_id).count()

    def test_reimport_separate_nodes_skips_existing(self, app):
        client = app.test_client()
        alice = _make_user("alice")
        _db.session.commit()
        _login(client, alice.id)

        files = [
            _md_file("a", "file a content", "2024-01-01T12:00:00"),
            _md_file("b", "file b content", "2024-01-02T12:00:00"),
        ]

        r1 = _confirm_files(client, files, import_type="separate_nodes")
        assert r1.status_code == 201
        b1 = r1.get_json()
        assert b1["created"] == 2
        assert b1["skipped"] == 0
        assert self._count_nodes(alice.id) == 2

        r2 = _confirm_files(client, files, import_type="separate_nodes")
        assert r2.status_code == 201
        b2 = r2.get_json()
        assert b2["created"] == 0
        assert b2["skipped"] == 2
        assert b2["thread_count"] == 0
        assert self._count_nodes(alice.id) == 2

    def test_single_thread_overlap_chains_onto_existing(self, app):
        client = app.test_client()
        alice = _make_user("alice")
        _db.session.commit()
        _login(client, alice.id)

        files = [
            _md_file("a", "file a content", "2024-01-01T12:00:00"),
            _md_file("b", "file b content", "2024-01-02T12:00:00"),
        ]
        r1 = _confirm_files(client, files, import_type="single_thread")
        assert r1.get_json()["created"] == 2

        # Re-import with one extra file: the new node must chain onto
        # the existing copy of "b", not start a fresh root.
        files_plus = files + [
            _md_file("c", "file c content", "2024-01-03T12:00:00"),
        ]
        r2 = _confirm_files(client, files_plus, import_type="single_thread")
        b2 = r2.get_json()
        assert b2["created"] == 1
        assert b2["skipped"] == 2
        assert self._count_nodes(alice.id) == 3

        node_b = Node.query.filter(
            Node.human_owner_id == alice.id,
            Node.content.contains("file b content"),
        ).one()
        node_c = Node.query.filter(
            Node.human_owner_id == alice.id,
            Node.content.contains("file c content"),
        ).one()
        assert node_c.parent_id == node_b.id


# ── POST confirm endpoints: soft-deleted content (restore-or-skip) ───────

class TestConfirmImportDeletedContent:
    """Imports colliding with soft-deleted nodes prompt restore-or-skip."""

    def _count_nodes(self, user_id):
        return Node.query.filter_by(human_owner_id=user_id).count()

    def _soft_delete_all(self, user_id, wipe_content=False):
        """Soft-delete all of the user's nodes (optionally as wiped
        tombstones, mirroring the cleanup task's content wipe)."""
        from datetime import datetime
        for node in Node.query.filter_by(human_owner_id=user_id).all():
            node.deleted_at = datetime.utcnow()
            if wipe_content:
                node.content = None
        _db.session.commit()

    def test_reimport_of_deleted_content_returns_409(self, app):
        client = app.test_client()
        alice = _make_user("alice")
        _db.session.commit()
        _login(client, alice.id)

        convs = [_analyzed_conv()]
        assert _confirm_conversations(client, convs).status_code == 201
        self._soft_delete_all(alice.id)

        r = _confirm_conversations(client, convs)
        assert r.status_code == 409
        body = r.get_json()
        assert body["error"] == "deleted_content_matches"
        assert body["deleted_matches"] == 2
        # Nothing changed: no new nodes, originals still deleted.
        assert self._count_nodes(alice.id) == 2
        assert all(
            n.deleted_at is not None
            for n in Node.query.filter_by(human_owner_id=alice.id)
        )

    def test_on_deleted_skip_keeps_nodes_deleted(self, app):
        client = app.test_client()
        alice = _make_user("alice")
        _db.session.commit()
        _login(client, alice.id)

        convs = [_analyzed_conv()]
        _confirm_conversations(client, convs)
        self._soft_delete_all(alice.id)

        r = _confirm_conversations(client, convs, on_deleted="skip")
        assert r.status_code == 201
        body = r.get_json()
        assert body["created"] == 0
        assert body["skipped"] == 2
        assert body["restored"] == 0
        assert self._count_nodes(alice.id) == 2
        assert all(
            n.deleted_at is not None
            for n in Node.query.filter_by(human_owner_id=alice.id)
        )

    def test_on_deleted_restore_undeletes_wiped_tombstones(self, app):
        client = app.test_client()
        alice = _make_user("alice")
        _db.session.commit()
        _login(client, alice.id)

        convs = [_analyzed_conv()]
        _confirm_conversations(client, convs)
        original_ids = sorted(
            n.id for n in Node.query.filter_by(human_owner_id=alice.id)
        )
        # Wiped tombstones: content already cleared by the cleanup task.
        self._soft_delete_all(alice.id, wipe_content=True)

        r = _confirm_conversations(client, convs, on_deleted="restore")
        assert r.status_code == 201
        body = r.get_json()
        assert body["created"] == 0
        assert body["skipped"] == 0
        assert body["restored"] == 2
        # Same rows, un-deleted, content refilled from the archive.
        nodes = Node.query.filter_by(human_owner_id=alice.id).all()
        assert sorted(n.id for n in nodes) == original_ids
        assert all(n.deleted_at is None for n in nodes)
        assert all(n.content for n in nodes)

    def test_overlap_snapshot_chains_onto_restored_node(self, app):
        client = app.test_client()
        alice = _make_user("alice")
        _db.session.commit()
        _login(client, alice.id)

        _confirm_conversations(client, [_analyzed_conv()])
        self._soft_delete_all(alice.id)

        extended = _analyzed_conv(messages=[
            {
                "text": "hello world",
                "role": "user",
                "created_at": "2023-11-14T22:13:20",
                "model": "",
                "mapping_id": "u1",
            },
            {
                "text": "hi there, friend",
                "role": "assistant",
                "created_at": "2023-11-14T22:13:21",
                "model": "gpt-4o",
                "mapping_id": "a1",
            },
            {
                "text": "one more question",
                "role": "user",
                "created_at": "2023-11-14T22:14:00",
                "model": "",
                "mapping_id": "u2",
            },
        ])
        r = _confirm_conversations(client, [extended], on_deleted="restore")
        assert r.status_code == 201
        body = r.get_json()
        assert body["created"] == 1
        assert body["restored"] == 2
        assert body["skipped"] == 0

        new_node = Node.query.filter_by(
            human_owner_id=alice.id, source_key="chatgpt:u2"
        ).one()
        prev_node = Node.query.filter_by(
            human_owner_id=alice.id, source_key="chatgpt:a1"
        ).one()
        assert prev_node.deleted_at is None
        assert new_node.parent_id == prev_node.id

    def test_markdown_reimport_restore_only_deleted_file(self, app):
        # One deleted file out of two: 409 reports 1 match; restore
        # un-deletes it while the alive duplicate is just skipped.
        client = app.test_client()
        alice = _make_user("alice")
        _db.session.commit()
        _login(client, alice.id)

        from datetime import datetime
        files = [
            _md_file("a", "file a content", "2024-01-01T12:00:00"),
            _md_file("b", "file b content", "2024-01-02T12:00:00"),
        ]
        _confirm_files(client, files, import_type="separate_nodes")
        node_a = Node.query.filter(
            Node.human_owner_id == alice.id,
            Node.content.contains("file a content"),
        ).one()
        node_a.deleted_at = datetime.utcnow()
        _db.session.commit()

        r1 = _confirm_files(client, files, import_type="separate_nodes")
        assert r1.status_code == 409
        assert r1.get_json()["deleted_matches"] == 1

        r2 = _confirm_files(client, files, import_type="separate_nodes",
                            on_deleted="restore")
        assert r2.status_code == 201
        body = r2.get_json()
        assert body["created"] == 0
        assert body["restored"] == 1
        assert body["skipped"] == 1
        assert Node.query.get(node_a.id).deleted_at is None


# ── POST confirm endpoints: settings update on re-import ─────────────────

class TestConfirmImportSettingsUpdate:
    """Re-importing with different privacy/ai_usage updates the
    already-imported nodes instead of being a pure no-op."""

    def _nodes(self, user_id):
        return Node.query.filter_by(human_owner_id=user_id).all()

    def test_reimport_with_new_settings_updates_existing_nodes(self, app):
        client = app.test_client()
        alice = _make_user("alice")
        _db.session.commit()
        _login(client, alice.id)

        convs = [_analyzed_conv()]
        r1 = _confirm_conversations(client, convs)  # private / none
        assert r1.get_json()["created"] == 2

        r2 = _confirm_conversations(
            client, convs, privacy_level="public", ai_usage="chat"
        )
        assert r2.status_code == 201
        b2 = r2.get_json()
        assert b2["created"] == 0
        assert b2["updated"] == 2
        assert b2["skipped"] == 0
        nodes = self._nodes(alice.id)
        assert len(nodes) == 2
        assert all(n.privacy_level == "public" for n in nodes)
        assert all(n.ai_usage == "chat" for n in nodes)

    def test_reimport_same_settings_is_pure_skip(self, app):
        client = app.test_client()
        alice = _make_user("alice")
        _db.session.commit()
        _login(client, alice.id)

        convs = [_analyzed_conv()]
        _confirm_conversations(client, convs)
        b2 = _confirm_conversations(client, convs).get_json()
        assert b2["updated"] == 0
        assert b2["skipped"] == 2

    def test_settings_update_leaves_deleted_nodes_untouched(self, app):
        # With on_deleted="skip", deleted matches keep their old
        # settings (and stay deleted); only alive duplicates update.
        from datetime import datetime
        client = app.test_client()
        alice = _make_user("alice")
        _db.session.commit()
        _login(client, alice.id)

        convs = [_analyzed_conv()]
        _confirm_conversations(client, convs)
        deleted_node = Node.query.filter_by(
            human_owner_id=alice.id, source_key="chatgpt:u1"
        ).one()
        deleted_node.deleted_at = datetime.utcnow()
        _db.session.commit()

        r = _confirm_conversations(
            client, convs, on_deleted="skip",
            privacy_level="public", ai_usage="chat",
        )
        b = r.get_json()
        assert b["updated"] == 1
        assert b["skipped"] == 1
        assert b["restored"] == 0

        deleted_node = Node.query.filter_by(
            human_owner_id=alice.id, source_key="chatgpt:u1"
        ).one()
        alive_node = Node.query.filter_by(
            human_owner_id=alice.id, source_key="chatgpt:a1"
        ).one()
        assert deleted_node.deleted_at is not None
        assert deleted_node.privacy_level == "private"
        assert deleted_node.ai_usage == "none"
        assert alive_node.privacy_level == "public"
        assert alive_node.ai_usage == "chat"

    def test_restore_applies_new_settings(self, app):
        from datetime import datetime
        client = app.test_client()
        alice = _make_user("alice")
        _db.session.commit()
        _login(client, alice.id)

        convs = [_analyzed_conv()]
        _confirm_conversations(client, convs)
        for node in self._nodes(alice.id):
            node.deleted_at = datetime.utcnow()
        _db.session.commit()

        r = _confirm_conversations(
            client, convs, on_deleted="restore",
            privacy_level="public", ai_usage="chat",
        )
        b = r.get_json()
        assert b["restored"] == 2
        assert b["updated"] == 0
        nodes = self._nodes(alice.id)
        assert all(n.deleted_at is None for n in nodes)
        assert all(n.privacy_level == "public" for n in nodes)
        assert all(n.ai_usage == "chat" for n in nodes)

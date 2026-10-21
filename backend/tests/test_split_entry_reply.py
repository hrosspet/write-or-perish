"""A long entry's reply goes under its last part.

An entry above the per-node cap is saved as a serial chain of parts
(utils/node_split). The reply's context walks up from its parent, so a
reply under the first part reads only the first part, and it sits in the
middle of the entry in the thread. The web's inline form asked for the
reply under the first part (the create response's ``id``, not its
``tip_id``).

Covers: split_chain_tip (what counts as a part), POST /nodes/<id>/llm on
any part replying under the last one, the transcription status naming
the last part, and the thread view telling the owner where the entry
ends.

Routes run in a minimal Flask app (sqlite in-memory, ENCRYPTION_DISABLED)
with the completion task module stubbed, so nothing reaches a model.
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta
from unittest.mock import MagicMock

os.environ["ENCRYPTION_DISABLED"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("TWITTER_API_KEY", "fake")
os.environ.setdefault("TWITTER_API_SECRET", "fake")
# Routes read AUDIO_STORAGE_PATH at import; keep their writes out of the repo.
os.environ.setdefault(
    "AUDIO_STORAGE_PATH", tempfile.mkdtemp(prefix="wop-split-reply-test-"))

sys.modules.setdefault("celery", MagicMock())
sys.modules.setdefault("celery.utils", MagicMock())
sys.modules.setdefault("celery.utils.log", MagicMock())
sys.modules.setdefault("celery.result", MagicMock())
sys.modules.setdefault("ffmpeg", MagicMock())

import pytest  # noqa: E402
from flask import Flask, g  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login  # noqa: E402
from backend.extensions import db as _db  # noqa: E402
from backend.models import User, Node  # noqa: E402
import backend.models as _real_backend_models  # noqa: E402
from backend.utils.node_split import (  # noqa: E402
    NODE_CHAR_CAP, split_chain_tip, split_node_into_chain,
)


def _make_app():
    from flask_login import LoginManager

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["DEFAULT_LLM_MODEL"] = "gpt-5"
    app.config["SUPPORTED_MODELS"] = {
        "gpt-5": {"provider": "openai", "api_model": "gpt-5"},
    }
    _db.init_app(app)

    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from backend.routes.nodes import nodes_bp
    app.register_blueprint(nodes_bp, url_prefix="/api/nodes")
    return app


@pytest.fixture
def task_mod():
    """The completion task module, stubbed: create_llm_placeholder's lazy
    import picks this up, so an enqueued reply is only a recorded call."""
    saved = sys.modules.get("backend.tasks.llm_completion")
    mod = MagicMock()
    result = MagicMock()
    result.id = "fake-task-id"
    mod.generate_llm_response.delay.return_value = result
    sys.modules["backend.tasks.llm_completion"] = mod
    yield mod
    if saved is None:
        sys.modules.pop("backend.tasks.llm_completion", None)
    else:
        sys.modules["backend.tasks.llm_completion"] = saved


@pytest.fixture
def app(task_mod, monkeypatch):
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
    import backend.utils.privacy as privacy
    # Another test file can leave a mocked current_user in the privacy
    # module; the routes' access checks need the real one.
    monkeypatch.setattr(privacy, "current_user", _real_flask_login.current_user)
    monkeypatch.setattr(
        "backend.utils.context_artifacts.sync_context_artifacts",
        lambda *a, **k: None)
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


def _user(username="alice"):
    u = User(username=username, twitter_id=f"{username}-tw", approved=True,
             plan="alpha", default_ai_usage="chat")
    _db.session.add(u)
    _db.session.flush()
    return u


def _client(app, user):
    g.pop("_login_user", None)
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return c


def _node(user, parent=None, content="words", node_type="user",
          created_at=None, privacy_level="private"):
    n = Node(user_id=user.id, human_owner_id=user.id,
             parent_id=parent.id if parent else None, node_type=node_type,
             privacy_level=privacy_level, ai_usage="chat")
    n.set_content(content)
    n.created_at = created_at or datetime(2026, 10, 21, 9, 0, 0)
    _db.session.add(n)
    _db.session.flush()
    return n


def _split_entry(user, parent=None, parts=3, **kw):
    """An entry saved as *parts* parts; returns [head, ..., tip]."""
    head = _node(user, parent, content="x", **kw)
    rest = split_node_into_chain(
        head, segments=[f"part {i}\n" for i in range(parts)])
    _db.session.commit()
    return [head, *rest]


def _llm_children(node_id):
    return Node.query.filter_by(parent_id=node_id, node_type="llm").all()


# ── split_chain_tip ───────────────────────────────────────────────────────

class TestSplitChainTip:
    def test_an_entry_that_was_not_split_is_its_own_tip(self, app):
        alice = _user()
        entry = _node(alice)
        assert split_chain_tip(entry) is entry

    def test_every_part_leads_to_the_last(self, app):
        alice = _user()
        chain = _split_entry(alice)
        for part in chain:
            assert split_chain_tip(part).id == chain[-1].id

    def test_a_reply_the_user_wrote_later_is_not_a_part(self, app):
        alice = _user()
        chain = _split_entry(alice, parts=2)
        _node(alice, chain[-1], content="my follow-up",
              created_at=chain[-1].created_at + timedelta(minutes=5))
        assert split_chain_tip(chain[0]).id == chain[-1].id

    def test_an_ai_reply_is_not_a_part(self, app):
        alice = _user()
        bot = _user("gpt-5")
        entry = _node(alice)
        reply = _node(bot, entry, node_type="llm",
                      created_at=entry.created_at + timedelta(milliseconds=1))
        reply.human_owner_id = alice.id
        _db.session.flush()
        assert split_chain_tip(entry) is entry

    def test_a_deleted_part_ends_the_walk(self, app):
        alice = _user()
        chain = _split_entry(alice)
        chain[1].deleted_at = datetime.utcnow()
        _db.session.commit()
        assert split_chain_tip(chain[0]).id == chain[0].id


# ── POST /nodes/<id>/llm ─────────────────────────────────────────────────

class TestReplyUnderTheLastPart:
    @pytest.mark.parametrize("asked_on", [0, 1, 2])
    def test_asked_on_any_part_the_reply_goes_under_the_last(
            self, app, task_mod, asked_on):
        alice = _user()
        chain = _split_entry(alice)
        resp = _client(app, alice).post(
            f"/api/nodes/{chain[asked_on].id}/llm", json={})
        assert resp.status_code == 202, resp.get_json()

        reply = _db.session.get(Node, resp.get_json()["node_id"])
        assert reply.parent_id == chain[-1].id
        # Its context walks up through every part.
        above, current = [], reply.parent
        while current is not None:
            above.append(current.id)
            current = current.parent
        assert above == [n.id for n in reversed(chain)]
        assert all(not _llm_children(n.id) for n in chain[:-1])

    def test_an_entry_that_was_not_split_is_answered_under_itself(
            self, app, task_mod):
        alice = _user()
        entry = _node(alice)
        _db.session.commit()
        resp = _client(app, alice).post(f"/api/nodes/{entry.id}/llm", json={})
        assert resp.status_code == 202, resp.get_json()
        reply = _db.session.get(Node, resp.get_json()["node_id"])
        assert reply.parent_id == entry.id

    def test_a_long_entry_written_inline_then_answered(self, app, task_mod):
        # The inline form's flow: POST /nodes/ under a thread, then the
        # reply. A client that still names the first part gets the reply
        # under the last one too.
        alice = _user()
        thread = _node(alice, content="earlier entry")
        _db.session.commit()
        client = _client(app, alice)
        big = "\n".join(f"line {i} " + "q" * 90 for i in range(1500))
        assert len(big) > NODE_CHAR_CAP
        created = client.post("/api/nodes/", json={
            "content": big, "parent_id": thread.id,
            "ai_usage": "chat", "privacy_level": "private"}).get_json()
        assert created["split_into"] >= 2
        assert created["tip_id"] != created["id"]

        for asked_on in (created["tip_id"], created["id"]):
            resp = client.post(f"/api/nodes/{asked_on}/llm", json={})
            assert resp.status_code == 202, resp.get_json()
            reply = _db.session.get(Node, resp.get_json()["node_id"])
            assert reply.parent_id == created["tip_id"]


# ── GET /nodes/<id>/transcription-status ─────────────────────────────────

class TestTranscriptionStatusNamesTheLastPart:
    def test_a_split_transcript(self, app):
        alice = _user()
        chain = _split_entry(alice)
        chain[0].transcription_status = "completed"
        _db.session.commit()
        data = _client(app, alice).get(
            f"/api/nodes/{chain[0].id}/transcription-status").get_json()
        assert data["node_id"] == chain[0].id
        assert data["tip_id"] == chain[-1].id

    def test_a_transcript_that_was_not_split(self, app):
        alice = _user()
        entry = _node(alice)
        entry.transcription_status = "completed"
        _db.session.commit()
        data = _client(app, alice).get(
            f"/api/nodes/{entry.id}/transcription-status").get_json()
        assert data["tip_id"] == entry.id


# ── GET /nodes/<id> ──────────────────────────────────────────────────────

class TestThreadViewNamesTheLastPart:
    def test_the_owner_on_an_earlier_part_is_told_the_last(self, app):
        alice = _user()
        chain = _split_entry(alice)
        client = _client(app, alice)
        for part in chain[:-1]:
            data = client.get(f"/api/nodes/{part.id}").get_json()
            assert data["split_tip_id"] == chain[-1].id

    def test_nothing_on_the_last_part_or_an_entry_not_split(self, app):
        alice = _user()
        chain = _split_entry(alice)
        entry = _node(alice, content="short")
        _db.session.commit()
        client = _client(app, alice)
        assert "split_tip_id" not in client.get(
            f"/api/nodes/{chain[-1].id}").get_json()
        assert "split_tip_id" not in client.get(
            f"/api/nodes/{entry.id}").get_json()

    def test_another_viewer_is_not_told(self, app):
        alice = _user()
        bob = _user("bob")
        chain = _split_entry(alice, privacy_level="public")
        resp = _client(app, bob).get(f"/api/nodes/{chain[0].id}")
        assert resp.status_code == 200
        assert "split_tip_id" not in resp.get_json()

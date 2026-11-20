"""Payments on (#438): an account without AI replies keeps its writing and
gets no reply. Payments off: every route replies as before.

Same minimal app as test_no_replies_for_none.py (sqlite in memory, the
completion task stubbed so nothing reaches a model), with
PAYMENTS_ENABLED set per test.
"""
import os
import sys
import tempfile
from unittest.mock import MagicMock

os.environ["ENCRYPTION_DISABLED"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("TWITTER_API_KEY", "fake")
os.environ.setdefault("TWITTER_API_SECRET", "fake")
os.environ.setdefault(
    "AUDIO_STORAGE_PATH", tempfile.mkdtemp(prefix="wop-audio-pay-test-"))

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
from backend.models import (  # noqa: E402
    Node, NodeContextArtifact, User, UserPrompt)
import backend.models as _real_backend_models  # noqa: E402


def _make_app(payments):
    from flask_login import LoginManager

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["PAYMENTS_ENABLED"] = payments
    app.config["DEFAULT_LLM_MODEL"] = "gpt-5"
    app.config["READ_DEFAULT_MODEL"] = "gpt-5"
    app.config["SUPPORTED_MODELS"] = {
        "gpt-5": {"provider": "openai", "api_model": "gpt-5", "read": True},
    }
    _db.init_app(app)
    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return _db.session.get(User, int(user_id))

    from backend.routes.nodes import nodes_bp
    from backend.routes.textmode import textmode_bp
    from backend.routes.voice import voice_bp
    from backend.routes.profile import profile_bp
    from backend.routes.external import external_bp
    app.register_blueprint(nodes_bp, url_prefix="/api/nodes")
    app.register_blueprint(textmode_bp, url_prefix="/api/textmode")
    app.register_blueprint(voice_bp, url_prefix="/api/voice")
    app.register_blueprint(profile_bp, url_prefix="/api/profile")
    app.register_blueprint(external_bp, url_prefix="/api/external")

    from backend.utils.subscriptions import SubscriptionRequired

    @app.errorhandler(SubscriptionRequired)
    def _handle(exc):  # as backend/__init__.py registers it
        from flask import jsonify
        _db.session.rollback()
        return jsonify({"error": "subscription_required",
                        "message": exc.message}), 402
    return app


@pytest.fixture
def task_mod():
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


def _app(payments):
    _affected = lambda k: (  # noqa: E731
        k == "flask_login" or k.startswith("backend.routes")
        or k == "backend.models")
    saved = {k: sys.modules[k] for k in list(sys.modules) if _affected(k)}
    sys.modules["flask_login"] = _real_flask_login
    sys.modules["backend.models"] = _real_backend_models
    for _k in [k for k in list(sys.modules) if k.startswith("backend.routes")]:
        del sys.modules[_k]
    app = _make_app(payments)
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


@pytest.fixture
def on(task_mod):
    yield from _app(True)


@pytest.fixture
def off(task_mod):
    yield from _app(False)


def _user(username="alice", plan="free", **kw):
    u = User(username=username, twitter_id=f"{username}-tw", approved=True,
             plan=plan, **kw)
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


def _node(user, parent=None, content="words"):
    n = Node(user_id=user.id, human_owner_id=user.id,
             parent_id=parent.id if parent else None, node_type="user",
             privacy_level="private", ai_usage="chat")
    n.set_content(content)
    _db.session.add(n)
    _db.session.flush()
    return n


def _prompt_root(user, prompt_key="textmode"):
    prompt = UserPrompt(user_id=user.id, prompt_key=prompt_key, title="P",
                        generated_by="default")
    prompt.set_content("system prompt body")
    _db.session.add(prompt)
    _db.session.flush()
    root = _node(user, content="")
    root.prompt_key = prompt_key
    _db.session.add(NodeContextArtifact(
        node_id=root.id, artifact_type="prompt", artifact_id=prompt.id))
    _db.session.flush()
    return root


def _llm_count():
    return Node.query.filter_by(node_type="llm").count()


class TestPaymentsOff:
    def test_a_free_account_gets_its_reply(self, off, task_mod):
        alice = _user()
        leaf = _node(alice)
        _db.session.commit()
        resp = _client(off, alice).post(f"/api/nodes/{leaf.id}/llm", json={})
        assert resp.status_code == 202, resp.get_json()
        task_mod.generate_llm_response.delay.assert_called_once()

    def test_voice_turn_replies(self, off, task_mod):
        alice = _user()
        _db.session.commit()
        resp = _client(off, alice).post("/api/voice/",
                                        json={"content": "spoken"})
        assert resp.status_code == 202, resp.get_json()
        assert "subscription_required" not in resp.get_json()


class TestPaymentsOn:
    def test_reply_refused_with_a_402(self, on, task_mod):
        alice = _user()
        leaf = _node(alice)
        _db.session.commit()
        resp = _client(on, alice).post(f"/api/nodes/{leaf.id}/llm", json={})
        assert resp.status_code == 402
        data = resp.get_json()
        assert data["error"] == "subscription_required"
        assert "Your writing is saved." in data["message"]
        assert _llm_count() == 0
        task_mod.generate_llm_response.delay.assert_not_called()

    def test_ended_subscription_says_so(self, on, task_mod):
        alice = _user(subscription_status="ended",
                      subscription_level="standard")
        leaf = _node(alice)
        _db.session.commit()
        resp = _client(on, alice).post(f"/api/nodes/{leaf.id}/llm", json={})
        assert resp.status_code == 402
        assert "subscription has ended" in resp.get_json()["message"]

    @pytest.mark.parametrize("kw", [
        {"plan": "alpha"}, {"plan": "pro"},
        {"subscription_status": "active", "subscription_level": "standard"},
        {"subscription_status": "trialing",
         "subscription_level": "discounted"},
        {"is_admin": True}])
    def test_paid_accounts_reply(self, on, task_mod, kw):
        alice = _user(**kw)
        leaf = _node(alice)
        _db.session.commit()
        resp = _client(on, alice).post(f"/api/nodes/{leaf.id}/llm", json={})
        assert resp.status_code == 202, resp.get_json()
        task_mod.generate_llm_response.delay.assert_called_once()

    def test_text_message_is_kept_without_a_reply(self, on, task_mod):
        alice = _user()
        root = _prompt_root(alice)
        msg = _node(alice, root)
        _db.session.commit()
        before = Node.query.count()
        resp = _client(on, alice).post(
            f"/api/textmode/{root.id}/message",
            json={"content": "hello", "parent_id": msg.id})
        assert resp.status_code == 202, resp.get_json()
        data = resp.get_json()
        assert data["subscription_required"] is True
        assert data["subscription_message"]
        assert "llm_node_id" not in data
        assert Node.query.count() == before + 1
        assert _llm_count() == 0

    def test_text_start_is_kept_without_a_reply(self, on, task_mod):
        alice = _user()
        _db.session.commit()
        resp = _client(on, alice).post("/api/textmode/start",
                                       json={"content": "hello"})
        assert resp.status_code == 202, resp.get_json()
        data = resp.get_json()
        assert data["subscription_required"] is True
        assert data["user_node_id"]
        assert _llm_count() == 0
        task_mod.generate_llm_response.delay.assert_not_called()

    def test_from_node_is_kept_without_a_reply(self, on, task_mod):
        alice = _user()
        below = _node(alice)
        _db.session.commit()
        resp = _client(on, alice).post(
            f"/api/textmode/from-node/{below.id}",
            json={"content": "a note", "ai_usage": "chat"})
        assert resp.status_code == 202, resp.get_json()
        assert resp.get_json()["subscription_required"] is True
        assert _llm_count() == 0

    def test_voice_turn_refused_before_anything_is_written(self, on,
                                                           task_mod):
        alice = _user()
        _db.session.commit()
        before = Node.query.count()
        resp = _client(on, alice).post("/api/voice/",
                                       json={"content": "spoken"})
        assert resp.status_code == 402
        assert resp.get_json()["error"] == "subscription_required"
        assert Node.query.count() == before


# ── Reading stays, optional model calls stop ───────────────────────────────

def _recorded_node(user, tmp_root):
    """A private entry with its original recording (and a speech file)."""
    n = _node(user)
    n.audio_original_url = f"/media/nodes/{user.id}/{n.id}/original.webm"
    n.audio_tts_url = f"/media/nodes/{user.id}/{n.id}/tts.mp3"
    folder = tmp_root / "nodes" / str(user.id) / str(n.id)
    folder.mkdir(parents=True)
    (folder / "original.webm").write_bytes(b"webm")
    (folder / "tts.mp3").write_bytes(b"mp3")
    _db.session.commit()
    return n


@pytest.fixture
def audio_root(tmp_path, monkeypatch):
    import backend.routes.nodes as nodes_mod
    monkeypatch.setattr(nodes_mod, "AUDIO_STORAGE_ROOT", tmp_path)
    return tmp_path


class TestOwnRecordings:
    def test_payments_on_the_owner_plays_their_recording(self, on,
                                                         audio_root):
        alice = _user(subscription_status="ended",
                      subscription_level="standard")
        n = _recorded_node(alice, audio_root)
        r = _client(on, alice).get(f"/api/nodes/{n.id}/audio")
        assert r.status_code == 200, r.get_json()
        data = r.get_json()
        assert data["original_url"].endswith("original.webm")
        # No speech for an account without Voice mode.
        assert data["tts_url"] is None

    def test_payments_off_unchanged(self, off, audio_root):
        alice = _user()
        n = _recorded_node(alice, audio_root)
        assert _client(off, alice).get(
            f"/api/nodes/{n.id}/audio").status_code == 403

    def test_chunks_and_download_follow(self, on, audio_root):
        alice = _user()
        n = _node(alice)
        n.streaming_transcription = True
        _db.session.commit()
        c = _client(on, alice)
        # Not refused for Voice mode: there are simply no chunks here.
        assert c.get(f"/api/nodes/{n.id}/audio-chunks").status_code == 404
        assert c.get(f"/api/nodes/{n.id}/audio-download").status_code == 404

    def test_chunks_and_download_refused_while_off(self, off, audio_root):
        alice = _user()
        n = _node(alice)
        n.streaming_transcription = True
        _db.session.commit()
        c = _client(off, alice)
        assert c.get(f"/api/nodes/{n.id}/audio-chunks").status_code == 403
        assert c.get(f"/api/nodes/{n.id}/audio-download").status_code == 403


class TestOptionalCalls:
    def test_no_new_profile_speech_without_the_paid_set(self, on):
        from backend.models import UserProfile
        alice = _user()
        p = UserProfile(user_id=alice.id, generated_by="m", content="x")
        _db.session.add(p)
        _db.session.commit()
        r = _client(on, alice).post(f"/api/profile/{p.id}/tts")
        assert r.status_code == 402
        assert r.get_json()["error"] == "subscription_required"

    def test_no_new_reference_speech_without_the_paid_set(self, on):
        from backend.models import ExternalItem
        alice = _user()
        item = ExternalItem(user_id=alice.id, source="x", external_id="1",
                            content="a saved post")
        _db.session.add(item)
        _db.session.commit()
        r = _client(on, alice).post(f"/api/external/items/{item.id}/tts")
        assert r.status_code == 402

    def test_paid_accounts_still_reach_speech(self, on):
        from backend.models import UserProfile
        alice = _user(plan="alpha")
        p = UserProfile(user_id=alice.id, generated_by="m", content="x")
        _db.session.add(p)
        _db.session.commit()
        r = _client(on, alice).post(f"/api/profile/{p.id}/tts")
        assert r.status_code != 402

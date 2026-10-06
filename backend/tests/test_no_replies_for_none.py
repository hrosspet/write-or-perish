"""No AI reply where AI may not read; Voice mode explains instead.

The rule (Peter, 2026-10-01): content marked ai_usage 'none' is never sent
to a model, and a voice thread whose ai_usage is 'none' gets no AI reply.
A reply is generated only when the node it answers, every node above it
that the model would read, and the reply itself are 'chat' or 'train' (the
web's contextAllowsAi rule). Every route that starts a reply answers 403
``{"code": "ai_usage_none"}`` otherwise; Voice refuses to start recording;
a Voice recording that ends in that state is kept as an entry with a
warning and no reply.

Routes run in a minimal Flask app (sqlite in-memory, ENCRYPTION_DISABLED)
with the completion task module stubbed, so nothing reaches a model. The
finalize task body runs with the celery glue stubbed, as in
test_spend_cap_recording.py. The task-level checks are in
test_no_replies_for_none_task.py.
"""
import os
import sys
import tempfile
from datetime import datetime
from unittest.mock import MagicMock

os.environ["ENCRYPTION_DISABLED"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("TWITTER_API_KEY", "fake")
os.environ.setdefault("TWITTER_API_SECRET", "fake")
# Routes read AUDIO_STORAGE_PATH at import; keep their writes out of the repo.
os.environ.setdefault(
    "AUDIO_STORAGE_PATH", tempfile.mkdtemp(prefix="wop-audio-none-test-"))

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
    User, Node, Draft, NodeTranscriptChunk, NodeContextArtifact, UserPrompt,
)
import backend.models as _real_backend_models  # noqa: E402

CODE = "ai_usage_none"


# ── App ──────────────────────────────────────────────────────────────────

def _make_app():
    from flask_login import LoginManager

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["DEFAULT_LLM_MODEL"] = "gpt-5"
    app.config["READ_DEFAULT_MODEL"] = "gpt-5"
    app.config["SUPPORTED_MODELS"] = {
        "gpt-5": {"provider": "openai", "api_model": "gpt-5", "read": True},
    }
    _db.init_app(app)

    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from backend.routes.nodes import nodes_bp
    from backend.routes.textmode import textmode_bp
    from backend.routes.voice import voice_bp
    from backend.routes.drafts import drafts_bp
    app.register_blueprint(nodes_bp, url_prefix="/api/nodes")
    app.register_blueprint(textmode_bp, url_prefix="/api/textmode")
    app.register_blueprint(voice_bp, url_prefix="/api/voice")
    app.register_blueprint(drafts_bp, url_prefix="/api/drafts")
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
def app(task_mod, tmp_path, monkeypatch):
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
    import backend.routes.drafts as drafts_mod
    import backend.utils.audio_storage as audio_storage
    monkeypatch.setattr(drafts_mod, "AUDIO_STORAGE_ROOT", tmp_path)
    monkeypatch.setattr(audio_storage, "AUDIO_STORAGE_ROOT", tmp_path)
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


def _user(username="alice", default_ai_usage="chat"):
    u = User(username=username, twitter_id=f"{username}-tw", approved=True,
             plan="alpha", default_ai_usage=default_ai_usage)
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


def _node(user, parent=None, ai_usage="chat", content="words",
          node_type="user", human_owner=None, llm_model=None,
          privacy_level="private"):
    n = Node(user_id=user.id, human_owner_id=(human_owner or user).id,
             parent_id=parent.id if parent else None, node_type=node_type,
             llm_model=llm_model, privacy_level=privacy_level,
             ai_usage=ai_usage)
    n.set_content(content)
    _db.session.add(n)
    _db.session.flush()
    return n


def _llm_user():
    u = User.query.filter_by(username="gpt-5").first()
    return u or _user("gpt-5")


def _reply(parent, owner, ai_usage="chat", content="a reply"):
    return _node(_llm_user(), parent, ai_usage=ai_usage, content=content,
                 node_type="llm", human_owner=owner, llm_model="gpt-5")


def _prompt_root(user, prompt_key="voice", ai_usage="chat"):
    """A system node carrying an agentic prompt, as the Voice / Text mode
    routes build it (the thread's ai_usage, an empty body, a linked
    prompt record)."""
    prompt = UserPrompt(user_id=user.id, prompt_key=prompt_key, title="P",
                        generated_by="default")
    prompt.set_content("system prompt body")
    _db.session.add(prompt)
    _db.session.flush()
    root = _node(user, ai_usage=ai_usage, content="")
    root.prompt_key = prompt_key
    _db.session.add(NodeContextArtifact(
        node_id=root.id, artifact_type="prompt", artifact_id=prompt.id))
    _db.session.flush()
    return root


def _refused(resp, scope=None):
    assert resp.status_code == 403, resp.get_data(as_text=True)
    data = resp.get_json()
    assert data["code"] == CODE
    assert data["error"]
    if scope is not None:
        assert data["scope"] == scope
    return data


def _llm_count():
    return Node.query.filter_by(node_type="llm").count()


# ── The rule ─────────────────────────────────────────────────────────────

class TestReplyRule:
    def test_the_node_above_and_the_reply_must_allow_ai(self, app):
        from backend.utils.llm_nodes import reply_refusal
        alice = _user()
        root = _node(alice, ai_usage="none", content="kept from AI")
        mid = _node(alice, root, ai_usage="chat")
        leaf = _node(alice, mid, ai_usage="train")
        _db.session.commit()

        assert reply_refusal(root, alice.id) is not None
        assert reply_refusal(leaf, alice.id) is not None  # a none ancestor
        root.ai_usage = "chat"
        _db.session.commit()
        assert reply_refusal(leaf, alice.id) is None
        assert reply_refusal(leaf, alice.id, "none") is not None
        assert reply_refusal(leaf, alice.id, "train") is None

    def test_a_deleted_none_node_is_not_sent_so_it_does_not_refuse(self, app):
        # The task sends a notice in place of a deleted node, never its text.
        from backend.utils.llm_nodes import reply_refusal
        alice = _user()
        root = _node(alice, ai_usage="none")
        root.deleted_at = datetime.utcnow()
        leaf = _node(alice, root, ai_usage="chat")
        _db.session.commit()
        assert reply_refusal(leaf, alice.id) is None

    def test_nodes_the_requester_cannot_see_are_not_sent(self, app):
        # The walk stops where the context builder stops: above a node the
        # requester cannot see, nothing reaches the model.
        from backend.utils.llm_nodes import reply_refusal
        alice = _user()
        bob = _user("bob")
        hidden = _node(alice, ai_usage="none")
        bobs = _node(bob, hidden, ai_usage="chat")
        _db.session.commit()
        assert reply_refusal(bobs, bob.id) is None
        assert reply_refusal(hidden, alice.id) is not None

    def test_create_llm_placeholder_refuses_before_any_write(self, app):
        from backend.utils.llm_nodes import (
            AIUsageRefused, create_llm_placeholder,
        )
        alice = _user()
        root = _node(alice, ai_usage="none")
        leaf = _node(alice, root, ai_usage="chat")
        _db.session.commit()
        with pytest.raises(AIUsageRefused):
            create_llm_placeholder(leaf.id, "gpt-5", alice.id,
                                   ai_usage="chat", enqueue=False)
        with pytest.raises(AIUsageRefused):
            create_llm_placeholder(root.id, "gpt-5", alice.id,
                                   ai_usage="chat", enqueue=False)
        assert _llm_count() == 0

    @pytest.mark.parametrize("prompt_key", ["voice", "textmode"])
    def test_system_prompt_threads_still_reply(self, app, prompt_key):
        # The agentic root carries the thread's ai_usage like any node.
        from backend.utils.llm_nodes import create_llm_placeholder
        alice = _user()
        root = _prompt_root(alice, prompt_key)
        msg = _node(alice, root, ai_usage="chat")
        _db.session.commit()
        node, _ = create_llm_placeholder(msg.id, "gpt-5", alice.id,
                                         ai_usage="chat", enqueue=False)
        assert node.ai_usage == "chat"

    def test_read_threads_still_reply(self, app):
        # A read's prompt and recommendation are 'chat' by construction
        # (ca_feed.FEED_AI_USAGE); a chat turn under them replies.
        from backend.models import FeedRender
        from backend.utils.ca_feed import FEED_AI_USAGE
        from backend.utils.llm_nodes import create_llm_placeholder
        alice = _user(default_ai_usage="train")
        entry = _node(alice, ai_usage="train")
        prompt = _node(alice, entry, ai_usage=FEED_AI_USAGE, content="")
        prompt.prompt_key = "read_thread"
        read = _reply(prompt, alice, ai_usage=FEED_AI_USAGE, content="picks")
        _db.session.add(FeedRender(node_id=read.id))
        note = _node(alice, read, ai_usage="train", content="why #2?")
        _db.session.commit()
        node, _ = create_llm_placeholder(note.id, "gpt-5", alice.id,
                                         ai_usage="train", enqueue=False)
        assert node.id

    def test_continuation_chains_still_reply(self, app):
        # A turn's interim step and its continuation carry the reply's
        # ai_usage (llm_completion copies it); the next turn under them
        # replies.
        from backend.utils.llm_nodes import create_llm_placeholder
        alice = _user()
        root = _prompt_root(alice, "voice")
        msg = _node(alice, root, ai_usage="chat")
        interim = _reply(msg, alice, content="on it")
        cont = _reply(interim, alice, content="the answer")
        interim.continuation_node_id = cont.id
        nxt = _node(alice, cont, ai_usage="chat", content="and then?")
        _db.session.commit()
        node, _ = create_llm_placeholder(nxt.id, "gpt-5", alice.id,
                                         ai_usage="chat", enqueue=False)
        assert node.id

    def test_an_imported_conversation_follows_its_setting(self, app):
        # An import stamps one ai_usage on every node, replies included.
        from backend.utils.llm_nodes import reply_refusal
        alice = _user()
        for usage, refused in (("chat", False), ("none", True)):
            q = _node(alice, ai_usage=usage, content="imported question")
            a = _reply(q, alice, ai_usage=usage, content="imported answer")
            _db.session.commit()
            assert (reply_refusal(a, alice.id, usage) is not None) is refused


# ── POST /nodes/<id>/llm ─────────────────────────────────────────────────

class TestLlmResponseRoute:
    def test_refused_on_a_none_node(self, app, task_mod):
        alice = _user()
        node = _node(alice, ai_usage="none")
        _db.session.commit()
        _refused(_client(app, alice).post(f"/api/nodes/{node.id}/llm",
                                          json={}), scope="thread")
        assert _llm_count() == 0
        task_mod.generate_llm_response.delay.assert_not_called()

    def test_refused_under_a_none_ancestor(self, app, task_mod):
        alice = _user()
        root = _node(alice, ai_usage="none")
        reply = _reply(root, alice, ai_usage="chat")
        leaf = _node(alice, reply, ai_usage="chat")
        _db.session.commit()
        _refused(_client(app, alice).post(f"/api/nodes/{leaf.id}/llm",
                                          json={}))
        assert _llm_count() == 1  # only the one that was there
        task_mod.generate_llm_response.delay.assert_not_called()

    def test_refused_under_a_none_reply(self, app, task_mod):
        # A reply marked 'none' (imported, or set by its owner) is not
        # sent either.
        alice = _user()
        root = _node(alice, ai_usage="chat")
        reply = _reply(root, alice, ai_usage="none")
        leaf = _node(alice, reply, ai_usage="chat")
        _db.session.commit()
        _refused(_client(app, alice).post(f"/api/nodes/{leaf.id}/llm",
                                          json={}))
        task_mod.generate_llm_response.delay.assert_not_called()

    def test_a_chat_thread_replies(self, app, task_mod):
        alice = _user()
        root = _node(alice, ai_usage="train")
        leaf = _node(alice, root, ai_usage="chat")
        _db.session.commit()
        resp = _client(app, alice).post(f"/api/nodes/{leaf.id}/llm", json={})
        assert resp.status_code == 202, resp.get_json()
        task_mod.generate_llm_response.delay.assert_called_once()


# ── Text mode ────────────────────────────────────────────────────────────

class TestTextMode:
    def test_message_refused_before_anything_is_written(self, app, task_mod):
        alice = _user()
        root = _prompt_root(alice, "textmode", ai_usage="none")
        msg = _node(alice, root, ai_usage="none")
        _db.session.commit()
        before = Node.query.count()
        resp = _client(app, alice).post(
            f"/api/textmode/{root.id}/message",
            json={"content": "hello", "parent_id": msg.id})
        _refused(resp)
        assert Node.query.count() == before
        task_mod.generate_llm_response.delay.assert_not_called()

    def test_message_in_a_chat_thread_replies(self, app, task_mod):
        alice = _user()
        root = _prompt_root(alice, "textmode")
        msg = _node(alice, root, ai_usage="chat")
        _db.session.commit()
        resp = _client(app, alice).post(
            f"/api/textmode/{root.id}/message",
            json={"content": "hello", "parent_id": msg.id})
        assert resp.status_code == 202, resp.get_json()
        task_mod.generate_llm_response.delay.assert_called_once()

    def test_from_node_without_a_reply_still_saves(self, app, task_mod):
        # auto_generate off starts no reply, so a 'none' entry above does
        # not stop the message.
        alice = _user()
        root = _node(alice, ai_usage="none")
        below = _node(alice, root, ai_usage="chat")
        _db.session.commit()
        resp = _client(app, alice).post(
            f"/api/textmode/from-node/{below.id}",
            json={"content": "a note", "ai_usage": "chat",
                  "auto_generate": False})
        assert resp.status_code == 202, resp.get_json()
        assert "llm_node_id" not in resp.get_json()
        task_mod.generate_llm_response.delay.assert_not_called()


# ── Voice ────────────────────────────────────────────────────────────────

class TestVoiceFromNode:
    def test_refused_on_a_none_thread(self, app, task_mod):
        alice = _user()
        root = _prompt_root(alice, "voice", ai_usage="none")
        msg = _node(alice, root, ai_usage="none")
        _db.session.commit()
        before = Node.query.count()
        resp = _client(app, alice).post(f"/api/voice/from-node/{msg.id}",
                                        json={})
        _refused(resp, scope="thread")
        assert Node.query.count() == before
        task_mod.generate_llm_response.delay.assert_not_called()

    def test_refused_on_a_none_reply_without_a_new_reply(self, app):
        # The branch that only resumes an existing reply is refused too:
        # the Voice screen explains instead.
        alice = _user()
        root = _prompt_root(alice, "voice", ai_usage="none")
        msg = _node(alice, root, ai_usage="none")
        reply = _reply(msg, alice, ai_usage="none")
        _db.session.commit()
        _refused(_client(app, alice).post(
            f"/api/voice/from-node/{reply.id}", json={}), scope="thread")

    def test_a_chat_thread_starts(self, app, task_mod):
        alice = _user(default_ai_usage="none")
        root = _prompt_root(alice, "voice")
        msg = _node(alice, root, ai_usage="chat")
        _db.session.commit()
        resp = _client(app, alice).post(f"/api/voice/from-node/{msg.id}",
                                        json={})
        assert resp.status_code == 202, resp.get_json()
        task_mod.generate_llm_response.delay.assert_called_once()


class TestLegacyVoicePost:
    def test_fresh_turn_refused_for_a_none_account(self, app, task_mod):
        alice = _user(default_ai_usage="none")
        _db.session.commit()
        resp = _client(app, alice).post("/api/voice/", json={
            "content": "spoken words"})
        _refused(resp, scope="account")
        assert Node.query.count() == 0
        task_mod.generate_llm_response.delay.assert_not_called()

    def test_fresh_turn_refused_when_the_recording_is_none(self, app, task_mod):
        alice = _user()
        _db.session.commit()
        _refused(_client(app, alice).post("/api/voice/", json={
            "content": "spoken words", "ai_usage": "none"}), scope="account")
        assert Node.query.count() == 0

    def test_continued_turn_refused_on_a_none_thread(self, app, task_mod):
        alice = _user()
        root = _prompt_root(alice, "voice", ai_usage="none")
        msg = _node(alice, root, ai_usage="none")
        reply = _reply(msg, alice, ai_usage="none")
        _db.session.commit()
        before = Node.query.count()
        _refused(_client(app, alice).post("/api/voice/", json={
            "content": "more words", "parent_id": reply.id}), scope="thread")
        assert Node.query.count() == before
        task_mod.generate_llm_response.delay.assert_not_called()

    def test_chat_turn_replies(self, app, task_mod):
        alice = _user()
        _db.session.commit()
        resp = _client(app, alice).post("/api/voice/", json={
            "content": "spoken words"})
        assert resp.status_code == 202, resp.get_json()
        task_mod.generate_llm_response.delay.assert_called_once()


class TestVoiceAvailability:
    def test_account_none_blocks_a_fresh_thread(self, app):
        alice = _user(default_ai_usage="none")
        _db.session.commit()
        data = _client(app, alice).get("/api/voice/availability").get_json()
        assert data["allowed"] is False
        assert data["code"] == CODE
        assert data["scope"] == "account"
        assert "Voice mode needs AI" in data["error"]

    def test_account_chat_allows_a_fresh_thread(self, app):
        alice = _user()
        _db.session.commit()
        data = _client(app, alice).get("/api/voice/availability").get_json()
        assert data == {"allowed": True}

    def test_thread_decides_when_continuing(self, app):
        alice = _user(default_ai_usage="none")
        chat_root = _prompt_root(alice, "voice")
        chat_reply = _reply(_node(alice, chat_root), alice)
        none_root = _node(alice, ai_usage="none")
        none_leaf = _node(alice, none_root, ai_usage="chat")
        _db.session.commit()
        c = _client(app, alice)
        data = c.get(f"/api/voice/availability?parent={chat_reply.id}"
                     ).get_json()
        assert data == {"allowed": True}
        data = c.get(f"/api/voice/availability?parent={none_leaf.id}"
                     ).get_json()
        assert data["allowed"] is False
        assert data["scope"] == "thread"

    def test_a_node_the_user_cannot_see_is_404(self, app):
        alice = _user()
        bob = _user("bob")
        hidden = _node(alice, ai_usage="none")
        _db.session.commit()
        resp = _client(app, bob).get(
            f"/api/voice/availability?parent={hidden.id}")
        assert resp.status_code == 404
        resp = _client(app, bob).get("/api/voice/availability?parent=nope")
        assert resp.status_code == 400


class TestStreamingInit:
    def _init(self, app, user, **body):
        return _client(app, user).post("/api/drafts/streaming/init",
                                       json=body)

    def test_voice_refused_for_a_none_account(self, app):
        alice = _user(default_ai_usage="none")
        _db.session.commit()
        _refused(self._init(app, alice, label="Voice", ai_usage="none"),
                 scope="account")
        assert Draft.query.count() == 0

    def test_voice_refused_for_a_none_recording(self, app):
        alice = _user()
        _db.session.commit()
        _refused(self._init(app, alice, label="Voice", ai_usage="none"),
                 scope="account")
        assert Draft.query.count() == 0

    def test_voice_refused_on_a_none_thread(self, app):
        alice = _user()
        root = _node(alice, ai_usage="none")
        leaf = _node(alice, root, ai_usage="chat")
        _db.session.commit()
        _refused(self._init(app, alice, label="Voice", ai_usage="chat",
                            parent_id=leaf.id), scope="thread")
        assert Draft.query.count() == 0

    def test_voice_on_a_chat_thread_starts(self, app):
        alice = _user(default_ai_usage="none")
        root = _prompt_root(alice, "voice")
        leaf = _node(alice, root, ai_usage="chat")
        _db.session.commit()
        resp = self._init(app, alice, label="Voice", ai_usage="none",
                          parent_id=leaf.id)
        assert resp.status_code == 201, resp.get_json()

    def test_voice_with_chat_starts(self, app):
        alice = _user()
        _db.session.commit()
        resp = self._init(app, alice, label="Voice", ai_usage="chat")
        assert resp.status_code == 201, resp.get_json()

    def test_dictation_is_not_affected(self, app):
        # A recording that is not a Voice turn asks for no reply.
        alice = _user(default_ai_usage="none")
        _db.session.commit()
        resp = self._init(app, alice, ai_usage="none")
        assert resp.status_code == 201, resp.get_json()


class TestSaveAsNode:
    def test_reply_skipped_under_a_none_thread_entry_kept(self, app, task_mod):
        alice = _user()
        root = _node(alice, ai_usage="none")
        leaf = _node(alice, root, ai_usage="chat")
        draft = Draft(user_id=alice.id, parent_id=leaf.id,
                      session_id="sess-save", streaming_status="completed",
                      privacy_level="private", ai_usage="chat")
        draft.set_content("recorded words")
        _db.session.add(draft)
        _db.session.commit()
        resp = _client(app, alice).post(
            "/api/drafts/streaming/sess-save/save-as-node",
            json={"auto_generate": True})
        assert resp.status_code == 201, resp.get_json()
        data = resp.get_json()
        assert data["llm_error_code"] == CODE
        assert "llm_node_id" not in data
        assert Node.query.get(data["id"]).get_content() == "recorded words"
        task_mod.generate_llm_response.delay.assert_not_called()


# ── The finalize task ────────────────────────────────────────────────────

@pytest.fixture
def st(monkeypatch, tmp_path):
    """backend.tasks.streaming_transcription with the celery glue stubbed,
    so task bodies run directly against an in-memory app."""
    _GLUE = ("backend.celery_app", "backend.tasks.streaming_transcription",
             "backend.tasks.llm_completion")
    saved = {k: sys.modules.get(k) for k in _GLUE}
    fake_celery_app = MagicMock()

    def _passthrough_task(*args, **kwargs):
        def deco(f):
            return f
        return deco
    fake_celery_app.celery.task = _passthrough_task
    sys.modules["backend.celery_app"] = fake_celery_app
    sys.modules.pop("backend.tasks.streaming_transcription", None)
    fake_llm = MagicMock()
    sys.modules["backend.tasks.llm_completion"] = fake_llm
    import backend.tasks.streaming_transcription as module

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["TESTING"] = True
    app.config["SUPPORTED_MODELS"] = {
        "gpt-5": {"provider": "openai", "api_model": "gpt-5"},
        "claude-test": {"provider": "anthropic", "api_model": "c"},
    }
    _db.init_app(app)
    fake_celery_app.flask_app = app
    module.flask_app = app
    monkeypatch.setenv("AUDIO_STORAGE_PATH", str(tmp_path))
    module._fake_llm = fake_llm

    with app.app_context():
        _db.create_all()
        yield module
        _db.session.rollback()
        _db.drop_all()

    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


def _voice_draft(user, session_id, ai_usage, text=None, parent=None):
    draft = Draft(user_id=user.id, session_id=session_id,
                  parent_id=parent.id if parent else None, label="Voice",
                  streaming_status="finalizing", ai_usage=ai_usage,
                  streaming_mime_type="audio/webm")
    draft.set_content("")
    _db.session.add(draft)
    if text is not None:
        chunk = NodeTranscriptChunk(session_id=session_id, chunk_index=0,
                                    status="completed",
                                    completed_at=datetime.utcnow())
        chunk.set_text(text)
        _db.session.add(chunk)
    _db.session.commit()
    return draft


class TestVoiceFinalize:
    def _finalize(self, st, user, session_id, parent=None, chunks=1,
                  model="gpt-5"):
        return st.finalize_draft_streaming(
            MagicMock(), session_id, chunks, label="Voice", user_id=user.id,
            parent_id=parent.id if parent else None, model=model)

    def test_fresh_none_recording_is_saved_without_a_reply(self, st):
        user = _user(default_ai_usage="none")
        _voice_draft(user, "sess-none", "none", text="spoken words")

        result = self._finalize(st, user, "sess-none")

        assert result["status"] == "completed"
        draft = Draft.query.filter_by(session_id="sess-none").one()
        assert draft.streaming_status == "completed"
        assert draft.llm_node_id is None
        assert draft.streaming_warning == st.VOICE_REPLY_SKIPPED_AI_USAGE
        entry = Node.query.one()  # no Voice prompt node, no reply
        assert entry.parent_id is None
        assert entry.get_content() == "spoken words"
        assert entry.ai_usage == "none"
        st._fake_llm.generate_llm_response.si.assert_not_called()

    def test_continued_none_thread_saves_the_turn_under_it(self, st):
        user = _user()
        root = _node(user, ai_usage="none")
        leaf = _node(user, root, ai_usage="none")
        _voice_draft(user, "sess-thread", "chat", text="more words",
                     parent=leaf)

        self._finalize(st, user, "sess-thread", parent=leaf)

        draft = Draft.query.filter_by(session_id="sess-thread").one()
        assert draft.streaming_warning == st.VOICE_REPLY_SKIPPED_AI_USAGE
        entry = Node.query.filter_by(parent_id=leaf.id).one()
        assert entry.get_content() == "more words"
        assert _llm_count() == 0
        st._fake_llm.generate_llm_response.si.assert_not_called()

    def test_chat_recording_gets_its_reply(self, st):
        # The control: same path, the chain starts.
        user = _user()
        _voice_draft(user, "sess-chat", "chat", text="spoken words")

        self._finalize(st, user, "sess-chat")

        draft = Draft.query.filter_by(session_id="sess-chat").one()
        assert draft.streaming_warning is None
        assert draft.llm_node_id is not None
        st._fake_llm.generate_llm_response.si.assert_called_once()

    def test_none_recording_is_not_prewarmed(self, st):
        # The warm would send the prompt and the transcript so far to the
        # model; a recording that gets no reply must not pay for it.
        user = _user(default_ai_usage="none")
        draft = _voice_draft(user, "sess-warm", "none")
        draft.set_content("x" * 600)
        _db.session.commit()

        self._finalize(st, user, "sess-warm", chunks=0, model="claude-test")

        st._fake_llm.prewarm_anthropic_cache.delay.assert_not_called()
        assert Node.query.count() == 0

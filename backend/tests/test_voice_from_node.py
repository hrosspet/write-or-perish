"""Tests for POST /voice/from-node/<node_id>.

Verifies the 4-case behavior matrix:
  prompt present + user node  → processing (LLM placeholder created)
  prompt present + LLM node   → processing (TTS playback)
  no prompt      + user node  → processing (system prompt + LLM placeholder)
  no prompt      + LLM node   → processing (system prompt created)

Also verifies authorization and ancestor-walking prompt detection.
"""

import os
import sys
from unittest.mock import MagicMock

# ── Environment ──────────────────────────────────────────────────────────
os.environ["ENCRYPTION_DISABLED"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("TWITTER_API_KEY", "fake")
os.environ.setdefault("TWITTER_API_SECRET", "fake")

# Mock optional heavy deps that may not be installed locally
sys.modules.setdefault("celery", MagicMock())
sys.modules.setdefault("celery.utils", MagicMock())
sys.modules.setdefault("celery.utils.log", MagicMock())
sys.modules.setdefault("celery.result", MagicMock())
sys.modules.setdefault("ffmpeg", MagicMock())

# Pre-mock the LLM task module so the lazy import inside
# create_llm_placeholder_node gets our mock instead of triggering
# the full celery/ffmpeg import chain.
_mock_llm_task_module = MagicMock()
_mock_task_result = MagicMock()
_mock_task_result.id = "fake-task-id"
_mock_llm_task_module.generate_llm_response.delay.return_value = (
    _mock_task_result
)
sys.modules["backend.tasks.llm_completion"] = _mock_llm_task_module

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

# ── Force-import real modules ────────────────────────────────────────────
# Only evict specific mocks that other test files may have installed.
# Do NOT blanket-remove all backend.* mocks — this file legitimately mocks
# backend.tasks.llm_completion above and that must be preserved.
for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login          # noqa: E402
from backend.extensions import db as _db         # noqa: E402
from backend.models import User, Node, NodeContextArtifact, UserPrompt  # noqa: E402
import backend.models as _real_backend_models    # noqa: E402


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

    from backend.routes.voice import voice_bp
    app.register_blueprint(voice_bp, url_prefix="/api/voice")

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


def _make_node(user, parent_id=None, content="hello", node_type="user",
               llm_model=None, ai_usage="chat", human_owner=None):
    n = Node(
        user_id=user.id,
        human_owner_id=(human_owner or user).id,
        parent_id=parent_id,
        node_type=node_type,
        llm_model=llm_model,
        privacy_level="private",
        ai_usage=ai_usage,
    )
    n.set_content(content)
    _db.session.add(n)
    _db.session.flush()
    return n


def _make_prompt_node(user, prompt_key, parent_id=None):
    """Create a system prompt node linked via NodeContextArtifact."""
    from backend.models import NodeContextArtifact
    from backend.utils.prompts import get_user_prompt_record
    record = get_user_prompt_record(user.id, prompt_key)
    n = Node(
        user_id=user.id,
        human_owner_id=user.id,
        parent_id=parent_id,
        node_type="user",
        privacy_level="private",
        ai_usage="chat",
    )
    _db.session.add(n)
    _db.session.flush()
    _db.session.add(NodeContextArtifact(
        node_id=n.id, artifact_type="prompt", artifact_id=record.id,
    ))
    _db.session.flush()
    return n


# ── Tests: Voice from-node ─────────────────────────────────────────────

class TestVoiceFromNodeMatrix:
    """Test the 4-case behavior matrix for voice."""

    def test_prompt_present_user_node_returns_processing(self, app):
        """User node in a thread with prompt → create LLM child → processing."""
        client = app.test_client()

        alice = _make_user("alice")

        # Build chain: prompt_node → user_node
        prompt_node = _make_prompt_node(alice, "voice")
        user_node = _make_node(alice, parent_id=prompt_node.id,
                               content="my thoughts")
        _db.session.commit()

        _login(client, alice.id)
        resp = client.post(
            f"/api/voice/from-node/{user_node.id}",
            json={"model": "gpt-5"},
        )

        assert resp.status_code == 202
        data = resp.get_json()
        assert data["mode"] == "processing"
        assert "llm_node_id" in data

        # Verify LLM placeholder was created as child of user_node
        llm_node = Node.query.get(data["llm_node_id"])
        assert llm_node is not None
        assert llm_node.parent_id == user_node.id
        assert llm_node.node_type == "llm"

    def test_prompt_present_llm_node_returns_processing(self, app):
        """LLM node in a thread with prompt → processing mode (TTS playback)."""
        client = app.test_client()

        alice = _make_user("alice")
        llm_user = _make_user("gpt-5", twitter_id="llm-gpt-5")

        # Build chain: prompt_node → user_node → llm_node
        prompt_node = _make_prompt_node(alice, "voice")
        user_node = _make_node(alice, parent_id=prompt_node.id,
                               content="my thoughts")
        llm_node = _make_node(llm_user, parent_id=user_node.id,
                              content="AI response", node_type="llm",
                              llm_model="gpt-5", human_owner=alice)
        _db.session.commit()

        _login(client, alice.id)
        resp = client.post(
            f"/api/voice/from-node/{llm_node.id}",
            json={"model": "gpt-5"},
        )

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["mode"] == "processing"
        assert data["llm_node_id"] == llm_node.id
        assert data["parent_id"] == llm_node.id

    def test_no_prompt_user_node_creates_system_prompt_and_processing(
        self, app
    ):
        """User node with no prompt in chain → system prompt + LLM → processing."""
        client = app.test_client()

        alice = _make_user("alice")
        # Plain node — no prompt in ancestors
        user_node = _make_node(alice, content="just some text")
        _db.session.commit()

        _login(client, alice.id)
        resp = client.post(
            f"/api/voice/from-node/{user_node.id}",
            json={"model": "gpt-5"},
        )

        assert resp.status_code == 202
        data = resp.get_json()
        assert data["mode"] == "processing"

        # Verify chain: user_node → system_prompt_node → llm_node
        llm_node = Node.query.get(data["llm_node_id"])
        system_node = Node.query.get(llm_node.parent_id)
        assert system_node.parent_id == user_node.id
        assert system_node.node_type == "user"
        assert system_node.is_system_prompt
        assert system_node.content is None
        prompt_artifact = NodeContextArtifact.query.filter_by(
            node_id=system_node.id, artifact_type="prompt",
        ).first()
        assert prompt_artifact is not None
        linked_prompt = UserPrompt.query.get(prompt_artifact.artifact_id)
        assert linked_prompt.prompt_key == "voice"

    def test_no_prompt_llm_node_creates_system_prompt_and_processing(
        self, app
    ):
        """LLM node with no prompt → system prompt child → processing (TTS)."""
        client = app.test_client()

        alice = _make_user("alice")
        llm_user = _make_user("gpt-5", twitter_id="llm-gpt-5")

        # LLM node without any prompt ancestor (e.g. from converse)
        user_node = _make_node(alice, content="some text")
        llm_node = _make_node(llm_user, parent_id=user_node.id,
                              content="AI reply", node_type="llm",
                              llm_model="gpt-5", human_owner=alice)
        _db.session.commit()

        _login(client, alice.id)
        resp = client.post(
            f"/api/voice/from-node/{llm_node.id}",
            json={"model": "gpt-5"},
        )

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["mode"] == "processing"
        assert data["llm_node_id"] == llm_node.id

        # Verify system prompt was created as child of llm_node
        system_node = Node.query.get(data["parent_id"])
        assert system_node.parent_id == llm_node.id
        assert system_node.is_system_prompt
        assert system_node.content is None
        linked_prompt = system_node.get_artifact("prompt")
        assert linked_prompt.prompt_key == "voice"


class TestVoiceFromNodeAncestorWalking:
    """Test that prompt detection walks the full ancestor chain."""

    def test_prompt_detected_at_root(self, app):
        """Prompt at root of a deep chain is found."""
        client = app.test_client()

        alice = _make_user("alice")

        # Deep chain: prompt → n1 → n2 → n3
        prompt_node = _make_prompt_node(alice, "voice")
        n1 = _make_node(alice, parent_id=prompt_node.id, content="a")
        n2 = _make_node(alice, parent_id=n1.id, content="b")
        n3 = _make_node(alice, parent_id=n2.id, content="c")
        _db.session.commit()

        _login(client, alice.id)
        resp = client.post(
            f"/api/voice/from-node/{n3.id}",
            json={"model": "gpt-5"},
        )

        # Should detect prompt → processing mode (user node with prompt)
        assert resp.status_code == 202
        assert resp.get_json()["mode"] == "processing"

    def test_prompt_detected_mid_chain(self, app):
        """Prompt in middle of chain (from prior resume) is found."""
        client = app.test_client()

        alice = _make_user("alice")

        # Chain: regular → prompt → user_node
        regular = _make_node(alice, content="original text")
        prompt_node = _make_prompt_node(alice, "voice",
                                        parent_id=regular.id)
        user_node = _make_node(alice, parent_id=prompt_node.id,
                               content="reflecting")
        _db.session.commit()

        _login(client, alice.id)
        resp = client.post(
            f"/api/voice/from-node/{user_node.id}",
            json={"model": "gpt-5"},
        )

        assert resp.status_code == 202
        assert resp.get_json()["mode"] == "processing"


class TestVoiceFromNodeAuth:
    """Test authorization for from-node endpoint."""

    def test_unauthorized_user_rejected(self, app):
        """User cannot start voice from someone else's node."""
        client = app.test_client()

        alice = _make_user("alice")
        bob = _make_user("bob")
        alice_node = _make_node(alice, content="alice's thoughts")
        _db.session.commit()

        _login(client, bob.id)
        resp = client.post(
            f"/api/voice/from-node/{alice_node.id}",
            json={"model": "gpt-5"},
        )

        assert resp.status_code == 403

    def test_nonexistent_node_404(self, app):
        """Requesting from-node on non-existent node returns 404."""
        client = app.test_client()
        alice = _make_user("alice")
        _db.session.commit()

        _login(client, alice.id)
        resp = client.post(
            "/api/voice/from-node/99999",
            json={"model": "gpt-5"},
        )

        assert resp.status_code == 404

    def test_unauthenticated_rejected(self, app):
        """Unauthenticated user is rejected."""
        client = app.test_client()
        resp = client.post(
            "/api/voice/from-node/1",
            json={"model": "gpt-5"},
        )
        assert resp.status_code in (401, 302)


class TestVoiceFromNodeAiUsageInheritance:
    """Test that ai_usage is inherited from the target node."""

    def test_inherits_ai_usage_from_target_node(self, app):
        """New nodes should inherit ai_usage from the target node."""
        client = app.test_client()

        alice = _make_user("alice")
        # Node with ai_usage="train"
        user_node = _make_node(alice, content="trainable content",
                               ai_usage="train")
        _db.session.commit()

        _login(client, alice.id)
        resp = client.post(
            f"/api/voice/from-node/{user_node.id}",
            json={"model": "gpt-5"},
        )

        assert resp.status_code == 202
        data = resp.get_json()

        # The system prompt node should inherit ai_usage from the target
        llm_node = Node.query.get(data["llm_node_id"])
        system_node = Node.query.get(llm_node.parent_id)
        assert system_node.ai_usage == "train"

    def test_under_a_read_reply_takes_the_thread_not_the_read(self, app):
        """A read's nodes are 'chat' for the tweets they quote (#362):
        a voice session started from a read reply takes the user's node
        above the read, and so does a message sent under it."""
        client = app.test_client()
        alice = _make_user("alice", default_ai_usage="none")
        gpt = _make_user("gpt-5")
        root = _make_node(alice, content="mine", ai_usage="train")
        prompt = _make_prompt_node(alice, "read_thread", parent_id=root.id)
        read = _make_node(gpt, parent_id=prompt.id, content="picks",
                          node_type="llm", llm_model="gpt-5",
                          human_owner=alice)
        from backend.models import FeedRender
        _db.session.add(FeedRender(node_id=read.id))  # a finished read
        _db.session.commit()

        _login(client, alice.id)
        resp = client.post(f"/api/voice/from-node/{read.id}",
                           json={"model": "gpt-5"})
        assert resp.status_code == 200, resp.get_json()
        system_node = Node.query.get(resp.get_json()["parent_id"])
        assert system_node.parent_id == read.id
        assert system_node.ai_usage == "train"

        resp = client.post("/api/voice/", json={
            "content": "why #2?", "model": "gpt-5", "parent_id": read.id})
        assert resp.status_code == 202, resp.get_json()
        data = resp.get_json()
        entry = Node.query.get(data["user_node_id"])
        assert entry.parent_id == read.id
        assert entry.ai_usage == "train"
        # Its AI answer takes the thread's setting too (voice review
        # 2026-09-29).
        assert Node.query.get(data["llm_node_id"]).ai_usage == "train"


class TestVoiceFromNodeAgenticAncestryBridge:
    """Verify that a `textmode` prompt in ancestry counts as an agentic
    prompt being present — switching from text mode to voice mode must
    NOT re-attach a voice prompt node (the two keys share agentic.txt).
    """

    def _count_prompt_ancestors(self, node_id):
        """Walk up from node_id counting nodes that link a UserPrompt."""
        count = 0
        current = Node.query.get(node_id)
        while current is not None:
            prompt = current.get_artifact("prompt")
            if prompt is not None:
                count += 1
            if current.parent_id is None:
                break
            current = Node.query.get(current.parent_id)
        return count

    def test_textmode_prompt_satisfies_voice_check_on_user_node(self, app):
        """Textmode prompt + user node → voice endpoint creates an LLM
        placeholder directly, does NOT append a second prompt node."""
        client = app.test_client()
        alice = _make_user("alice")

        textmode_root = _make_prompt_node(alice, "textmode")
        user_msg = _make_node(
            alice, parent_id=textmode_root.id, content="typed",
        )
        _db.session.commit()

        before = self._count_prompt_ancestors(user_msg.id)
        assert before == 1  # just the textmode root

        _login(client, alice.id)
        resp = client.post(
            f"/api/voice/from-node/{user_msg.id}",
            json={"model": "gpt-5"},
        )
        assert resp.status_code == 202
        data = resp.get_json()

        llm_node = Node.query.get(data["llm_node_id"])
        # LLM should be child of user_msg (no new prompt node in between)
        assert llm_node.parent_id == user_msg.id
        assert self._count_prompt_ancestors(llm_node.id) == 1

    def test_textmode_prompt_satisfies_voice_check_on_llm_node(self, app):
        """Textmode prompt + LLM node → voice endpoint re-plays (mode =
        processing, llm_node_id = the existing node) without appending
        a new prompt."""
        client = app.test_client()
        alice = _make_user("alice")
        llm_user = _make_user("gpt-5", twitter_id="llm-gpt-5")

        textmode_root = _make_prompt_node(alice, "textmode")
        user_msg = _make_node(
            alice, parent_id=textmode_root.id, content="typed",
        )
        llm_node = _make_node(
            llm_user, parent_id=user_msg.id, content="reply",
            node_type="llm", llm_model="gpt-5", human_owner=alice,
        )
        _db.session.commit()

        before = self._count_prompt_ancestors(llm_node.id)
        assert before == 1

        _login(client, alice.id)
        resp = client.post(
            f"/api/voice/from-node/{llm_node.id}",
            json={"model": "gpt-5"},
        )
        # Existing LLM + prompt present → 200 processing (TTS playback)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["mode"] == "processing"
        assert data["llm_node_id"] == llm_node.id
        # No new prompt node should have been appended anywhere in the tree
        assert self._count_prompt_ancestors(llm_node.id) == 1


# ── Tests: a deleted node (#480) ───────────────────────────────────────

class TestVoiceFromDeletedNode:
    """A page opened before its node was deleted can still ask to start
    Voice there. The answer is POST /nodes/'s 410 for a deleted parent,
    and nothing is written or sent to a model."""

    @pytest.fixture
    def llm_task(self, monkeypatch):
        """The reply task, mocked here so a call is counted (and never
        reaches a provider) whatever another test module left in
        sys.modules."""
        module = MagicMock()
        module.generate_llm_response.delay.return_value = MagicMock(
            id="fake-task-id")
        monkeypatch.setitem(
            sys.modules, "backend.tasks.llm_completion", module)
        return module.generate_llm_response

    @staticmethod
    def _delete(node):
        from datetime import datetime
        node.deleted_at = datetime.utcnow()

    def _start(self, app, user, node):
        client = app.test_client()
        _login(client, user.id)
        before = (Node.query.count(), NodeContextArtifact.query.count())
        resp = client.post(f"/api/voice/from-node/{node.id}",
                           json={"model": "gpt-5"})
        after = (Node.query.count(), NodeContextArtifact.query.count())
        return resp, before, after

    def _assert_refused(self, resp, before, after, llm_task):
        assert resp.status_code == 410, resp.get_json()
        assert resp.get_json() == {"error": "Parent node has been deleted"}
        assert after == before          # no prompt node, no reply
        llm_task.delay.assert_not_called()

    def test_deleted_entry_in_a_plain_thread(self, app, llm_task):
        """Was 202 with a Voice prompt and a billed reply under it."""
        alice = _make_user("alice")
        entry = _make_node(alice, content="gone")
        _make_node(alice, parent_id=entry.id, content="kept reply")
        self._delete(entry)
        _db.session.commit()
        self._assert_refused(*self._start(app, alice, entry), llm_task)

    def test_deleted_entry_in_an_agentic_thread(self, app, llm_task):
        """Was a 500 (the placeholder's own deleted-parent error)."""
        alice = _make_user("alice")
        prompt = _make_prompt_node(alice, "voice")
        entry = _make_node(alice, parent_id=prompt.id, content="gone")
        self._delete(entry)
        _db.session.commit()
        self._assert_refused(*self._start(app, alice, entry), llm_task)

    def test_deleted_ai_reply_in_an_agentic_thread(self, app, llm_task):
        """Was 200 pointing the Voice screen at the deleted reply."""
        alice = _make_user("alice")
        gpt = _make_user("gpt-5", twitter_id="llm-gpt-5")
        prompt = _make_prompt_node(alice, "voice")
        entry = _make_node(alice, parent_id=prompt.id, content="said")
        reply = _make_node(gpt, parent_id=entry.id, content="gone",
                           node_type="llm", llm_model="gpt-5",
                           human_owner=alice)
        self._delete(reply)
        _db.session.commit()
        self._assert_refused(*self._start(app, alice, reply), llm_task)

    def test_deleted_ai_reply_without_a_prompt(self, app, llm_task):
        """Was 200 with a Voice prompt attached under the deleted reply."""
        alice = _make_user("alice")
        gpt = _make_user("gpt-5", twitter_id="llm-gpt-5")
        entry = _make_node(alice, content="said")
        reply = _make_node(gpt, parent_id=entry.id, content="gone",
                           node_type="llm", llm_model="gpt-5",
                           human_owner=alice)
        self._delete(reply)
        _db.session.commit()
        self._assert_refused(*self._start(app, alice, reply), llm_task)

    def test_deleted_before_ai_usage(self, app, llm_task):
        """A deleted node in a thread kept away from AI gets the 410, not
        the 403 that would open the Voice screen on the deleted node."""
        alice = _make_user("alice")
        entry = _make_node(alice, content="gone", ai_usage="none")
        self._delete(entry)
        _db.session.commit()
        self._assert_refused(*self._start(app, alice, entry), llm_task)

    def test_live_entry_below_a_deleted_one_still_starts(self, app,
                                                         llm_task):
        """Only the node Voice starts from is checked: a thread with a
        deleted entry higher up goes on as before."""
        alice = _make_user("alice")
        prompt = _make_prompt_node(alice, "voice")
        gone = _make_node(alice, parent_id=prompt.id, content="gone")
        entry = _make_node(alice, parent_id=gone.id, content="here")
        self._delete(gone)
        _db.session.commit()
        resp, before, after = self._start(app, alice, entry)
        assert resp.status_code == 202, resp.get_json()
        reply = Node.query.get(resp.get_json()["llm_node_id"])
        assert reply.parent_id == entry.id
        assert after[0] == before[0] + 1
        llm_task.delay.assert_called_once()


# ── Tests: voice turn timing (#371 step 0) ─────────────────────────────

class TestVoiceTiming:
    @pytest.fixture
    def turn(self, app, monkeypatch):
        from backend.tests.test_voice_timing import FakeRedis
        from backend.utils import voice_timing
        fake = FakeRedis()
        monkeypatch.setattr(voice_timing, "_redis", lambda: fake)
        alice = _make_user("alice")
        llm_user = _make_user("gpt-5", twitter_id="llm-gpt-5")
        user_msg = _make_node(alice, content="spoken")
        llm_node = _make_node(
            llm_user, parent_id=user_msg.id, content="reply",
            node_type="llm", llm_model="gpt-5", human_owner=alice)
        _db.session.commit()
        voice_timing.mark(llm_node.id, "chunk_published", t=1000.0)
        return alice, llm_node

    def test_browser_marks_join_the_backend_record(self, app, turn):
        alice, llm_node = turn
        client = app.test_client()
        _login(client, alice.id)
        resp = client.post("/api/voice/timing", json={
            "node_id": llm_node.id,
            # Browser clock 2 s behind the server's.
            "marks": {"chunk_ready": 998_500, "playing": 998_800,
                      "not_a_stage": 1, "rec_stop": "soon"},
            "offset_ms": 2000, "rtt_ms": 40,
        })
        assert resp.status_code == 200
        rec = resp.get_json()
        assert rec["marks"] == {"chunk_published": 1000.0,
                                "chunk_ready": 1000.5, "playing": 1000.8}
        assert rec["stages"] == {"g": 0.5, "h": 0.3}
        assert rec["facts"]["clock_rtt_ms"] == "40"

        listing = client.get("/api/voice/timing").get_json()
        assert [t["node_id"] for t in listing["turns"]] == [llm_node.id]
        assert listing["median"] == {"g": 0.5, "h": 0.3}
        assert listing["stages"]["g"] == "chunk_published -> chunk_ready"

    def test_someone_elses_node_is_refused(self, app, turn):
        _, llm_node = turn
        bob = _make_user("bob")
        _db.session.commit()
        client = app.test_client()
        _login(client, bob.id)
        resp = client.post("/api/voice/timing", json={
            "node_id": llm_node.id, "marks": {"playing": 1}})
        assert resp.status_code == 404
        assert client.get("/api/voice/timing").get_json()["turns"] == []

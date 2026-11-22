"""Where the Text-mode prompt node goes when a reply is recorded under
an existing node (POST /drafts/streaming/<session>/save-as-node with
`agentic`, the reply box under a gleaning), and at which level.

A Text-mode session starts only under a node of the user's own: the
prompt node is attached under it and the message hangs under that.
Under someone else's node the message is a plain reply, directly under
that node. The prompt node is shared only when the node above it and
the message under it both are; otherwise it is private.

Route-level tests; the generation task is mocked.
"""
import os
import sys
import tempfile
import uuid
from unittest.mock import MagicMock

os.environ["ENCRYPTION_DISABLED"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("TWITTER_API_KEY", "fake")
os.environ.setdefault("TWITTER_API_SECRET", "fake")
# Keep the (best-effort, no-op here) draft→node audio move out of the repo.
os.environ["AUDIO_STORAGE_PATH"] = tempfile.mkdtemp(prefix="wop-audio-test-")

sys.modules.setdefault("celery", MagicMock())
sys.modules.setdefault("celery.utils", MagicMock())
sys.modules.setdefault("celery.utils.log", MagicMock())
sys.modules.setdefault("celery.result", MagicMock())
sys.modules.setdefault("ffmpeg", MagicMock())

_mock_llm_task_module = MagicMock()
_mock_task_result = MagicMock()
_mock_task_result.id = "fake-task-id"
_mock_llm_task_module.generate_llm_response.delay.return_value = (
    _mock_task_result
)

import pytest  # noqa: E402
from flask import Flask, g  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login          # noqa: E402
from backend.extensions import db as _db         # noqa: E402
from backend.models import (                     # noqa: E402
    User, Node, Draft, NodeContextArtifact, UserPrompt,
)
import backend.models as _real_backend_models    # noqa: E402

# Anna's own version of the Text-mode prompt.
PROMPT_TEXT = "Anna's own instructions. Profile: {user_profile}"


def _make_app():
    from flask_login import LoginManager

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["SHARE_V1"] = True
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
    from backend.routes.drafts import drafts_bp
    from backend.routes.commons import commons_bp
    app.register_blueprint(nodes_bp, url_prefix="/api/nodes")
    app.register_blueprint(drafts_bp, url_prefix="/api/drafts")
    app.register_blueprint(commons_bp, url_prefix="/api/commons")
    return app


@pytest.fixture
def app():
    _affected = lambda k: (  # noqa: E731
        k == "flask_login"
        or k.startswith("backend.routes")
        or k == "backend.models"
        or k == "backend.tasks.llm_completion"
    )
    saved = {k: sys.modules[k] for k in list(sys.modules) if _affected(k)}

    sys.modules["flask_login"] = _real_flask_login
    sys.modules["backend.models"] = _real_backend_models
    sys.modules["backend.tasks.llm_completion"] = _mock_llm_task_module
    for _k in [k for k in list(sys.modules) if k.startswith("backend.routes")]:
        del sys.modules[_k]
    _mock_llm_task_module.generate_llm_response.delay.reset_mock()

    app = _make_app()
    with app.app_context():
        _db.create_all()
        ben = User(username="ben", default_ai_usage="chat", approved=True,
                   plan="alpha", public_sharing_enabled=True)
        anna = User(username="anna", default_ai_usage="chat", approved=True,
                    plan="alpha", public_sharing_enabled=True)
        _db.session.add_all([ben, anna])
        _db.session.flush()
        prompt = UserPrompt(user_id=anna.id, prompt_key="textmode",
                            title="Text mode", generated_by="user")
        prompt.set_content(PROMPT_TEXT)
        _db.session.add(prompt)
        _db.session.commit()
        yield app
        _db.session.remove()
        _db.drop_all()

    for k in [k for k in list(sys.modules) if _affected(k)]:
        del sys.modules[k]
    sys.modules.update(saved)


def _user(username):
    return User.query.filter_by(username=username).first()


def _client(app, username=None):
    # Flask-Login caches the user on g: a second client in one test would
    # otherwise act as the first.
    g.pop("_login_user", None)
    client = app.test_client()
    if username is not None:
        with client.session_transaction() as sess:
            sess["_user_id"] = str(_user(username).id)
            sess["_fresh"] = True
    return client


def _gleaning(owner, privacy):
    """*owner*'s private entry with an AI reply under it at *privacy*."""
    user = _user(owner)
    entry = Node(user_id=user.id, human_owner_id=user.id, node_type="user",
                 privacy_level="private", ai_usage="chat")
    entry.set_content("an entry")
    _db.session.add(entry)
    llm_user = User.query.filter_by(username="gpt-5").first()
    if llm_user is None:
        llm_user = User(twitter_id="llm-gpt-5", username="gpt-5")
        _db.session.add(llm_user)
    _db.session.flush()
    node = Node(user_id=llm_user.id, human_owner_id=user.id,
                parent_id=entry.id, node_type="llm", llm_model="gpt-5",
                llm_task_status="completed", privacy_level=privacy,
                ai_usage="chat")
    node.set_content("Picks.")
    _db.session.add(node)
    _db.session.commit()
    return node


def _record(app, username, parent, **body):
    """*username* saves a finished recording made under *parent*."""
    draft = Draft(user_id=_user(username).id, node_id=None,
                  parent_id=parent.id, session_id=str(uuid.uuid4()),
                  streaming_status="completed", streaming_total_chunks=1,
                  streaming_completed_chunks=1, privacy_level="private",
                  ai_usage="chat")
    draft.set_content("recorded words")
    _db.session.add(draft)
    _db.session.commit()
    return _client(app, username).post(
        f"/api/drafts/streaming/{draft.session_id}/save-as-node", json=body)


def _prompt_nodes():
    return Node.query.filter(Node.prompt_key.isnot(None)).all()


def _public_thread(app, node_id):
    return _client(app).get(f"/api/commons/node/{node_id}")


# ── Under someone else's node: a plain reply ──────────────────────────


def test_a_reply_recorded_under_someone_elses_public_node_is_a_plain_reply(
        app):
    gleaning = _gleaning("ben", "public")

    resp = _record(app, "anna", gleaning, agentic=True, auto_generate=True,
                   privacy_level="public", ai_usage="chat")

    assert resp.status_code == 201, resp.get_json()
    data = resp.get_json()
    message = _db.session.get(Node, data["user_node_id"])
    assert message.parent_id == gleaning.id
    assert message.privacy_level == "public"
    assert "conversation_id" not in data
    assert _prompt_nodes() == []
    assert NodeContextArtifact.query.count() == 0

    # The thread as anyone reads it: the reply under the node, and
    # nothing of Anna's prompt.
    thread = _public_thread(app, gleaning.id)
    assert thread.status_code == 200
    replies = thread.get_json()["thread"]["children"]
    assert [r["id"] for r in replies] == [message.id]
    assert replies[0]["content"] == "recorded words"
    assert "Anna's own instructions" not in thread.get_data(as_text=True)

    # The node's owner sees the reply directly under the node.
    page = _client(app, "ben").get(f"/api/nodes/{gleaning.id}")
    assert page.status_code == 200
    assert [c["id"] for c in page.get_json()["children"]] == [message.id]
    assert "Anna's own instructions" not in page.get_data(as_text=True)


def test_a_client_that_sends_no_level_gets_a_plain_reply_there_too(app):
    """An installed iPhone build sends `agentic` without the form's
    settings: the recording's own apply, and the reply is still asked."""
    gleaning = _gleaning("ben", "public")

    resp = _record(app, "anna", gleaning, agentic=True, auto_generate=True)

    assert resp.status_code == 201, resp.get_json()
    data = resp.get_json()
    message = _db.session.get(Node, data["user_node_id"])
    assert message.parent_id == gleaning.id
    assert message.privacy_level == "private"
    assert _prompt_nodes() == []
    assert _db.session.get(Node, data["llm_node_id"]).parent_id == message.id


# ── Under one's own node: a session, as before ────────────────────────


def test_a_reply_recorded_under_ones_own_node_starts_a_session(app):
    gleaning = _gleaning("anna", "private")

    resp = _record(app, "anna", gleaning, agentic=True, auto_generate=True,
                   privacy_level="private", ai_usage="chat")

    assert resp.status_code == 201, resp.get_json()
    data = resp.get_json()
    prompt = _db.session.get(Node, data["conversation_id"])
    message = _db.session.get(Node, data["user_node_id"])
    assert prompt.parent_id == gleaning.id
    assert prompt.get_prompt_key() == "textmode"
    assert prompt.privacy_level == "private"
    assert message.parent_id == prompt.id
    assert _db.session.get(Node, data["llm_node_id"]).parent_id == message.id

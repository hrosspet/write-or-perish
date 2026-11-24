"""The privacy of a Text-mode message written under an existing node
(POST /textmode/from-node/<id>, the reply box under a gleaning).

The reply form shows a privacy selector that starts at the node's level.
The message is saved at the level the form sent, whatever the node's
level is now (Peter, 2026-11-24: "The privacy form is clear and it should
be adhered to"); nothing is refused for a difference between the two. A
client that sends no level gets the node's. The prompt node attached
under the node is given the more private of the two levels, and from
that it is Public only for a Public level under a Public default
(session_helpers.prompt_entry_privacy); an automatic reply follows the
message's level.

Route-level tests; the generation task is mocked.
"""
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
from backend.models import User, Node  # noqa: E402
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

    from backend.routes.nodes import nodes_bp
    from backend.routes.textmode import textmode_bp
    app.register_blueprint(nodes_bp, url_prefix="/api/nodes")
    app.register_blueprint(textmode_bp, url_prefix="/api/textmode")
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
        ben = User(username="ben", default_ai_usage="chat",
                   public_sharing_enabled=True)
        anna = User(username="anna", default_ai_usage="chat",
                    public_sharing_enabled=True)
        _db.session.add_all([ben, anna])
        _db.session.commit()
        yield app
        _db.session.remove()
        _db.drop_all()

    for k in [k for k in list(sys.modules) if _affected(k)]:
        del sys.modules[k]
    sys.modules.update(saved)


def _client(app, username):
    # Flask-Login caches the user on g: a second client in one test would
    # otherwise act as the first.
    g.pop("_login_user", None)
    client = app.test_client()
    user = User.query.filter_by(username=username).first()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return client


def _user(username):
    return User.query.filter_by(username=username).first()


def _node(username, content, parent=None, privacy="public", ai_usage="chat"):
    user = _user(username)
    node = Node(user_id=user.id, human_owner_id=user.id,
                parent_id=parent.id if parent else None,
                node_type="user", privacy_level=privacy, ai_usage=ai_usage)
    node.set_content(content)
    _db.session.add(node)
    _db.session.commit()
    return node


def _llm_node(parent, owner, content="An answer.", status="completed",
              privacy="private"):
    llm_user = User.query.filter_by(username="gpt-5").first()
    if llm_user is None:
        llm_user = User(twitter_id="llm-gpt-5", username="gpt-5")
        _db.session.add(llm_user)
        _db.session.flush()
    node = Node(user_id=llm_user.id, human_owner_id=_user(owner).id,
                parent_id=parent.id, node_type="llm", llm_model="gpt-5",
                llm_task_status=status, privacy_level=privacy,
                ai_usage="chat")
    node.set_content(content)
    _db.session.add(node)
    _db.session.commit()
    return node


def _gleaning(privacy):
    """Ben's entry with an AI reply under it at *privacy*."""
    entry = _node("ben", "an entry", privacy="private")
    return _llm_node(entry, "ben", content="Picks.", privacy=privacy)


def _post(app, node, **body):
    return _client(app, "ben").post(
        f"/api/textmode/from-node/{node.id}",
        json={"content": "what I make of it", **body})


def _message(resp):
    return _db.session.get(Node, resp.get_json()["user_node_id"])


# ── The form's level is the message's level ───────────────────────────


@pytest.mark.parametrize("node_level", ["public", "circles"])
def test_private_chosen_under_a_shared_node_is_saved_private(
        app, node_level):
    """The form still shows Private (it was opened before the node was
    made public, or the user chose Private): the message is saved
    private, the prompt node above it is private, and the automatic
    reply comes, private, since it answers a private message (#644: a
    private branch under a public node is not a shared thread)."""
    gleaning = _gleaning(node_level)

    resp = _post(app, gleaning, privacy_level="private", auto_generate=True)

    assert resp.status_code == 202, resp.get_json()
    data = resp.get_json()
    assert _message(resp).privacy_level == "private"
    prompt = _db.session.get(Node, data["prompt_node_id"])
    assert prompt.privacy_level == "private"
    assert "llm_error_code" not in data
    reply = _db.session.get(Node, data["llm_node_id"])
    assert reply.privacy_level == "private"


def test_circles_chosen_under_a_public_node_is_saved_circles(app):
    """The prompt node is private: a Text-mode prompt entry is Public
    only for a Public message (session_helpers.prompt_entry_privacy)."""
    gleaning = _gleaning("public")

    resp = _post(app, gleaning, privacy_level="circles")

    assert resp.status_code == 202, resp.get_json()
    data = resp.get_json()
    assert _message(resp).privacy_level == "circles"
    prompt = _db.session.get(Node, data["prompt_node_id"])
    assert prompt.privacy_level == "private"
    assert "llm_node_id" not in data
    assert data["llm_error_code"]


@pytest.mark.parametrize("default", ["private", "public"])
def test_public_chosen_under_a_private_node_is_saved_public(app, default):
    """As a typed reply sent to POST /nodes/ is. The reply is not made
    by itself for a public message; the prompt node attached under the
    private node stays private, also under a Public default."""
    _user("ben").default_privacy_level = default
    _db.session.commit()
    gleaning = _gleaning("private")

    resp = _post(app, gleaning, privacy_level="public")

    assert resp.status_code == 202, resp.get_json()
    data = resp.get_json()
    assert _message(resp).privacy_level == "public"
    assert "llm_node_id" not in data
    assert data["llm_error_code"]
    assert _mock_llm_task_module.generate_llm_response.delay.call_count == 0
    prompt = _db.session.get(Node, data["prompt_node_id"])
    assert prompt.privacy_level == "private"


@pytest.mark.parametrize("default", ["private", "public"])
def test_public_chosen_under_a_public_node_is_saved_public(app, default):
    """The prompt node is Public only under a Public default
    (session_helpers.prompt_entry_privacy)."""
    _user("ben").default_privacy_level = default
    _db.session.commit()
    gleaning = _gleaning("public")

    resp = _post(app, gleaning, privacy_level="public")

    assert resp.status_code == 202, resp.get_json()
    data = resp.get_json()
    assert _message(resp).privacy_level == "public"
    assert "llm_node_id" not in data
    prompt = _db.session.get(Node, data["prompt_node_id"])
    assert prompt.privacy_level == default


def test_private_chosen_under_a_private_node_gets_a_private_reply(app):
    gleaning = _gleaning("private")

    resp = _post(app, gleaning, privacy_level="private", ai_usage="chat")

    assert resp.status_code == 202, resp.get_json()
    data = resp.get_json()
    message = _message(resp)
    assert (message.privacy_level, message.ai_usage) == ("private", "chat")
    reply = _db.session.get(Node, data["llm_node_id"])
    assert reply.privacy_level == "private"


def test_a_level_that_does_not_exist_is_refused(app):
    gleaning = _gleaning("private")
    before = Node.query.count()

    resp = _post(app, gleaning, privacy_level="everyone")

    assert resp.status_code == 400
    assert Node.query.count() == before


# ── A client that sends no level ──────────────────────────────────────


@pytest.mark.parametrize("level", ["private", "circles", "public"])
def test_without_a_level_the_message_takes_the_nodes(app, level):
    gleaning = _gleaning(level)

    resp = _post(app, gleaning)

    assert resp.status_code == 202, resp.get_json()
    assert _message(resp).privacy_level == level
    # An automatic reply only in a private thread, and private.
    data = resp.get_json()
    if level == "private":
        reply = _db.session.get(Node, data["llm_node_id"])
        assert reply.privacy_level == "private"
    else:
        assert "llm_node_id" not in data


def test_a_null_level_reads_as_none_sent(app):
    gleaning = _gleaning("private")

    resp = _post(app, gleaning, privacy_level=None)

    assert resp.status_code == 202, resp.get_json()
    assert _message(resp).privacy_level == "private"


# ── The neighbouring routes, for comparison ───────────────────────────


def test_typed_reply_route_keeps_its_rule_from_main(app):
    """POST /nodes/ (the same form's path for a note with AI usage None)
    keeps the rule it had before this route took the form's level (#228:
    a reply under a public post must be public). Not changed here."""
    public = _gleaning("public")
    private = _gleaning("private")
    client = _client(app, "ben")

    refused = client.post("/api/nodes/", json={
        "content": "a note", "parent_id": public.id,
        "privacy_level": "private", "ai_usage": "none"})
    assert refused.status_code == 400
    assert refused.get_json()["code"] == "public_reply_required"

    saved = client.post("/api/nodes/", json={
        "content": "a note", "parent_id": private.id,
        "privacy_level": "public", "ai_usage": "none"})
    assert saved.status_code == 201, saved.get_json()
    assert _db.session.get(
        Node, saved.get_json()["id"]).privacy_level == "public"


def test_textmode_message_takes_its_parents_level(app):
    """POST /textmode/<id>/message has no form with a selector: the
    message takes the level of the node it is written under."""
    root = _node("ben", "a conversation", privacy="private")
    client = _client(app, "ben")

    resp = client.post(f"/api/textmode/{root.id}/message", json={
        "content": "next", "parent_id": root.id,
        "privacy_level": "public"})

    assert resp.status_code == 202, resp.get_json()
    assert _message(resp).privacy_level == "private"


# ── A level that is not text, and a deleted node ──────────────────────


@pytest.mark.parametrize("level", [["public"], {"level": "public"}, [], {},
                                   0, 1, True])
def test_a_level_that_is_not_text_is_refused(app, level):
    gleaning = _gleaning("private")
    before = Node.query.count()

    resp = _post(app, gleaning, privacy_level=level)

    assert resp.status_code == 400, resp.get_json()
    assert Node.query.count() == before


@pytest.mark.parametrize("body", [{}, {"privacy_level": "public"},
                                  {"privacy_level": "private"}])
def test_nothing_is_written_under_a_deleted_node(app, body):
    """As POST /nodes/ answers for a deleted parent: 410."""
    from datetime import datetime
    gleaning = _gleaning("private")
    gleaning.deleted_at = datetime.utcnow()
    _db.session.commit()
    before = Node.query.count()

    resp = _post(app, gleaning, **body)

    assert resp.status_code == 410, resp.get_json()
    assert Node.query.count() == before

"""AI replies in shared threads, and quotes in public nodes (Peter,
2026-11-12).

"all AI replies in a public thread are private by default, autogenerate
is off in public threads and circles and can't be turned on, and only the
user can change an AI reply to that circle/public (after the reply being
generated and the user supposedly having read it and decided it to share
it)"

"If it's about private quotes in public nodes, that shouldn't be allowed.
If a user wants to publish a private node with a quote of another private
node, a dialog stating that this is not possible should be displayed.
Informing the user that they first need to make the quoted node public,
too."

Route-level tests; the generation task is mocked (test_forum's pattern).
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
from backend.models import User, Node, ShareDraft  # noqa: E402
import backend.models as _real_backend_models    # noqa: E402


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

    from backend.routes.share import share_bp
    from backend.routes.nodes import nodes_bp
    from backend.routes.textmode import textmode_bp
    app.register_blueprint(share_bp, url_prefix="/api/share")
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


def _llm_children(parent):
    return Node.query.filter_by(parent_id=parent.id, node_type="llm").all()


# ── AI replies are private at creation ────────────────────────────────


def test_ai_reply_in_my_public_thread_with_someone_elses_reply_is_private(app):
    """Ben's own public thread holds Anna's public reply. The AI reply Ben
    asks for under his public answer to her is private: only Ben reads
    it, whatever it was built from, until he changes it."""
    root = _node("ben", "ben's public note")
    annas = _node("anna", "anna replies in public", parent=root)
    bens = _node("ben", "ben answers anna", parent=annas)

    resp = _client(app, "ben").post(f"/api/nodes/{bens.id}/llm", json={})

    assert resp.status_code == 202, resp.get_json()
    reply = _db.session.get(Node, resp.get_json()["node_id"])
    assert reply.privacy_level == "private"
    assert _client(app, "anna").get(
        f"/api/nodes/{reply.id}").status_code == 404
    assert _client(app, "ben").get(
        f"/api/nodes/{reply.id}").status_code == 200


def test_ai_reply_under_a_circles_node_is_private(app):
    note = _node("ben", "for my circles", privacy="circles")

    resp = _client(app, "ben").post(f"/api/nodes/{note.id}/llm", json={})

    assert resp.status_code == 202
    reply = _db.session.get(Node, resp.get_json()["node_id"])
    assert reply.privacy_level == "private"


def test_textmode_reply_in_my_public_thread_is_private(app):
    """The LLM Response button in a public Text-mode thread: private."""
    note = _node("ben", "a public entry")

    resp = _client(app, "ben").post(
        f"/api/textmode/from-node/{note.id}",
        json={"content": "and more", "auto_generate": False})
    assert resp.status_code == 202, resp.get_json()
    entry = resp.get_json()["user_node_id"]
    resp = _client(app, "ben").post(f"/api/nodes/{entry}/llm", json={})

    assert resp.status_code == 202
    reply = _db.session.get(Node, resp.get_json()["node_id"])
    assert reply.privacy_level == "private"


# ── Auto-generate is off in shared threads ─────────────────────────────


@pytest.mark.parametrize("privacy", ["public", "circles"])
def test_the_automatic_request_is_refused_under_a_shared_node(app, privacy):
    note = _node("ben", "shared", privacy=privacy)

    resp = _client(app, "ben").post(
        f"/api/nodes/{note.id}/llm", json={"auto_generate": True})

    assert resp.status_code == 409
    assert resp.get_json()["code"] == "auto_generate_off"
    assert _llm_children(note) == []
    _mock_llm_task_module.generate_llm_response.delay.assert_not_called()


def test_the_automatic_request_still_replies_under_a_private_node(app):
    note = _node("ben", "mine", privacy="private")

    resp = _client(app, "ben").post(
        f"/api/nodes/{note.id}/llm", json={"auto_generate": True})

    assert resp.status_code == 202
    assert len(_llm_children(note)) == 1


def test_llm_response_the_users_own_request_works_in_a_public_thread(app):
    note = _node("ben", "public")

    resp = _client(app, "ben").post(f"/api/nodes/{note.id}/llm", json={})

    assert resp.status_code == 202


def test_a_public_textmode_entry_is_saved_without_a_reply(app):
    resp = _client(app, "ben").post("/api/textmode/start", json={
        "content": "a shared thought", "privacy_level": "public",
        "ai_usage": "chat", "auto_generate": True})

    data = resp.get_json()
    assert resp.status_code == 202, data
    assert "llm_node_id" not in data
    assert data["llm_error_code"] == "auto_generate_off"
    entry = _db.session.get(Node, data["user_node_id"])
    assert entry.get_content() == "a shared thought"
    assert _llm_children(entry) == []
    _mock_llm_task_module.generate_llm_response.delay.assert_not_called()


def test_a_private_textmode_entry_still_gets_its_reply(app):
    resp = _client(app, "ben").post("/api/textmode/start", json={
        "content": "a private thought", "privacy_level": "private",
        "ai_usage": "chat", "auto_generate": True})

    data = resp.get_json()
    assert resp.status_code == 202, data
    assert data.get("llm_node_id")
    assert "llm_error_code" not in data


def test_a_textmode_message_under_my_public_node_gets_no_reply(app):
    note = _node("ben", "a public entry")

    resp = _client(app, "ben").post(
        f"/api/textmode/from-node/{note.id}", json={"content": "more"})

    data = resp.get_json()
    assert resp.status_code == 202, data
    assert "llm_node_id" not in data
    assert data["llm_error_code"] == "auto_generate_off"
    assert _db.session.get(Node, data["user_node_id"]).get_content() == "more"


def test_a_public_upload_with_auto_generate_keeps_the_upload_only(app):
    from backend.routes.nodes import _upload_reply_options
    with app.test_request_context():
        g.pop("_login_user", None)
        agentic, model, err, auto_off = _upload_reply_options(
            {"agentic": "true", "auto_generate": "true"}, None, "chat",
            "public")
    assert err is None
    assert agentic is True
    assert model is None
    assert auto_off is not None and auto_off.code == "auto_generate_off"


# ── Only the owner shares an AI reply, once it is written ──────────────


def test_a_reply_being_written_is_not_shared(app):
    note = _node("ben", "public")
    reply = _llm_node(note, "ben", status="processing")

    resp = _client(app, "ben").put(f"/api/nodes/{reply.id}", json={
        "content": reply.get_content(), "privacy_level": "public"})

    assert resp.status_code == 409
    assert resp.get_json()["code"] == "reply_in_progress"
    assert _db.session.get(Node, reply.id).privacy_level == "private"


def test_the_owner_shares_a_written_reply(app):
    note = _node("ben", "public")
    reply = _llm_node(note, "ben")

    resp = _client(app, "ben").put(f"/api/nodes/{reply.id}", json={
        "content": reply.get_content(), "privacy_level": "public"})

    assert resp.status_code == 200, resp.get_json()
    assert _client(app, "anna").get(
        f"/api/nodes/{reply.id}").status_code == 200


def test_a_cascade_leaves_a_reply_being_written_private(app):
    root = _node("ben", "root", privacy="private")
    entry = _node("ben", "entry", parent=root, privacy="private")
    pending = _llm_node(entry, "ben", status="pending")

    resp = _client(app, "ben").put(f"/api/nodes/{root.id}", json={
        "content": "root", "privacy_level": "public",
        "apply_to_descendants": True})

    assert resp.status_code == 200, resp.get_json()
    assert _db.session.get(Node, entry.id).privacy_level == "public"
    assert _db.session.get(Node, pending.id).privacy_level == "private"


def test_someone_else_cannot_change_my_reply(app):
    note = _node("ben", "public")
    reply = _llm_node(note, "ben")

    resp = _client(app, "anna").put(f"/api/nodes/{reply.id}", json={
        "content": reply.get_content(), "privacy_level": "public"})

    assert resp.status_code == 403


# ── A public node quotes only public nodes ─────────────────────────────


def test_publishing_a_node_that_quotes_a_private_one_is_refused(app):
    secret = _node("ben", "my private draft of the idea", privacy="private")
    note = _node("ben", f"see {{quote:{secret.id}}}", privacy="private")

    resp = _client(app, "ben").put(f"/api/nodes/{note.id}", json={
        "content": note.get_content(), "privacy_level": "public"})

    data = resp.get_json()
    assert resp.status_code == 409, data
    assert data["code"] == "quotes_not_public"
    assert [q["id"] for q in data["quoted"]] == [secret.id]
    assert data["quoted"][0]["can_open"] is True
    assert data["quoted"][0]["preview"] == "my private draft of the idea"
    assert _db.session.get(Node, note.id).privacy_level == "private"


def test_an_ai_reply_quoting_a_private_entry_is_not_published(app):
    secret = _node("ben", "private entry", privacy="private")
    note = _node("ben", "a question", privacy="private")
    reply = _llm_node(note, "ben", content=f"You wrote {{quote:{secret.id}}}")

    resp = _client(app, "ben").put(f"/api/nodes/{reply.id}", json={
        "content": reply.get_content(), "privacy_level": "public"})

    assert resp.status_code == 409
    assert resp.get_json()["code"] == "quotes_not_public"
    assert _db.session.get(Node, reply.id).privacy_level == "private"


def test_a_quote_of_a_public_node_is_fine(app):
    shown = _node("ben", "already public")
    note = _node("ben", f"see {{quote:{shown.id}}}", privacy="private")

    resp = _client(app, "ben").put(f"/api/nodes/{note.id}", json={
        "content": note.get_content(), "privacy_level": "public"})

    assert resp.status_code == 200, resp.get_json()


def test_a_private_node_quoted_inside_a_public_quote_is_found(app):
    secret = _node("ben", "deep private", privacy="private")
    middle = _node("ben", f"public, quoting {{quote:{secret.id}}}")
    note = _node("ben", f"see {{quote:{middle.id}}}", privacy="private")

    resp = _client(app, "ben").put(f"/api/nodes/{note.id}", json={
        "content": note.get_content(), "privacy_level": "public"})

    assert resp.status_code == 409
    assert [q["id"] for q in resp.get_json()["quoted"]] == [secret.id]


def test_a_deleted_quoted_node_does_not_block(app):
    from datetime import datetime
    gone = _node("ben", "gone", privacy="private")
    gone.deleted_at = datetime.utcnow()
    _db.session.commit()
    note = _node("ben", f"see {{quote:{gone.id}}}", privacy="private")

    resp = _client(app, "ben").put(f"/api/nodes/{note.id}", json={
        "content": note.get_content(), "privacy_level": "public"})

    assert resp.status_code == 200, resp.get_json()


def test_a_quote_of_a_node_made_public_in_the_same_change_is_fine(app):
    root = _node("ben", "root", privacy="private")
    _node("ben", f"quoting the root {{quote:{root.id}}}", parent=root,
          privacy="private")

    resp = _client(app, "ben").put(f"/api/nodes/{root.id}", json={
        "content": "root", "privacy_level": "public",
        "apply_to_descendants": True})

    assert resp.status_code == 200, resp.get_json()


def test_a_cascade_with_a_private_quote_changes_nothing(app):
    secret = _node("ben", "elsewhere", privacy="private")
    root = _node("ben", "root", privacy="private")
    child = _node("ben", f"quoting {{quote:{secret.id}}}", parent=root,
                  privacy="private")

    resp = _client(app, "ben").put(f"/api/nodes/{root.id}", json={
        "content": "root", "privacy_level": "public",
        "apply_to_descendants": True})

    assert resp.status_code == 409
    assert [q["id"] for q in resp.get_json()["quoted"]] == [secret.id]
    assert _db.session.get(Node, root.id).privacy_level == "private"
    assert _db.session.get(Node, child.id).privacy_level == "private"


def test_a_new_public_reply_quoting_a_private_node_is_refused(app):
    secret = _node("ben", "private", privacy="private")
    thread = _node("anna", "a public thread")

    resp = _client(app, "ben").post("/api/nodes/", json={
        "content": f"answer {{quote:{secret.id}}}", "parent_id": thread.id,
        "privacy_level": "public", "ai_usage": "chat"})

    assert resp.status_code == 409
    assert resp.get_json()["code"] == "quotes_not_public"
    assert Node.query.filter_by(parent_id=thread.id).count() == 0


def test_a_new_public_textmode_entry_quoting_a_private_node_is_refused(app):
    secret = _node("ben", "private", privacy="private")

    resp = _client(app, "ben").post("/api/textmode/start", json={
        "content": f"look {{quote:{secret.id}}}", "privacy_level": "public",
        "ai_usage": "chat", "auto_generate": False})

    assert resp.status_code == 409
    assert resp.get_json()["code"] == "quotes_not_public"


def test_an_edit_of_a_public_node_adding_a_private_quote_is_refused(app):
    secret = _node("ben", "private", privacy="private")
    note = _node("ben", "public words")

    resp = _client(app, "ben").put(f"/api/nodes/{note.id}", json={
        "content": f"public words {{quote:{secret.id}}}"})

    assert resp.status_code == 409
    assert _db.session.get(Node, note.id).get_content() == "public words"


def test_an_edit_of_a_public_node_keeps_the_quotes_it_already_had(app):
    """A public node published before this rule stays editable."""
    secret = _node("ben", "private", privacy="private")
    note = _node("ben", f"old words {{quote:{secret.id}}}")

    resp = _client(app, "ben").put(f"/api/nodes/{note.id}", json={
        "content": f"new words {{quote:{secret.id}}}"})

    assert resp.status_code == 200, resp.get_json()


def test_making_a_quoted_node_private_later_is_not_refused(app):
    quoted = _node("ben", "public for now")
    _node("ben", f"quoting {{quote:{quoted.id}}}")

    resp = _client(app, "ben").put(f"/api/nodes/{quoted.id}", json={
        "content": "public for now", "privacy_level": "private"})

    assert resp.status_code == 200, resp.get_json()


def test_a_share_quoting_a_private_node_stays_a_draft(app):
    secret = _node("ben", "private", privacy="private")
    share = ShareDraft(user_id=_user("ben").id, share_type="insight",
                       status="draft")
    share.set_content(f"a thought {{quote:{secret.id}}}")
    _db.session.add(share)
    _db.session.commit()

    resp = _client(app, "ben").post(f"/api/share/{share.id}/publish")

    assert resp.status_code == 409
    assert resp.get_json()["code"] == "quotes_not_public"
    assert _db.session.get(ShareDraft, share.id).status == "draft"


def test_a_quote_of_someone_elses_private_node_is_named_without_its_text(app):
    theirs = _node("anna", "anna's private", privacy="private")
    note = _node("ben", f"see {{quote:{theirs.id}}}", privacy="private")

    resp = _client(app, "ben").put(f"/api/nodes/{note.id}", json={
        "content": note.get_content(), "privacy_level": "public"})

    data = resp.get_json()
    assert resp.status_code == 409
    assert data["quoted"] == [{"id": theirs.id, "can_open": False}]

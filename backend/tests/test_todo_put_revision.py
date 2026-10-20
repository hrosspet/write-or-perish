"""The Todo editor's Save must not drop changes it didn't make (#476).

PUT /api/todo/ saves the editor's text as a new version. The web editor
sends the revision of the version it was opened on (base_revision); when
the newest version is no longer that one (a todo merge, a tick or another
Save landed while the editor was open), nothing is written and the answer
is 409 with the newest version. The editor then offers "Save mine anyway"
(sent with the newest revision) or "Show the newest list". A PUT without
base_revision (Create, older clients) saves as before.

Same harness as test_todo_patch_revision: in-memory SQLite,
ENCRYPTION_DISABLED, celery mocked so the modules import.
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

sys.modules.setdefault("celery", MagicMock())
sys.modules.setdefault("celery.utils", MagicMock())
sys.modules.setdefault("celery.utils.log", MagicMock())
sys.modules.setdefault("celery.result", MagicMock())

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

from backend.extensions import db as _db  # noqa: E402
from backend.models import User, UserTodo  # noqa: E402


@pytest.fixture
def app():
    import backend.celery_app  # noqa: F401
    from flask_login import LoginManager
    from backend.routes.todo import todo_bp

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    _db.init_app(app)
    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    app.register_blueprint(todo_bp, url_prefix="/api/todo")
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()


def _user(name="alice"):
    user = User(username=name, plan="alpha", twitter_id=None, approved=True,
                default_ai_usage="chat")
    _db.session.add(user)
    _db.session.commit()
    return user


def _todo(user_id, content, generated_by="user"):
    todo = UserTodo(user_id=user_id, generated_by=generated_by,
                    ai_usage="chat")
    todo.set_content(content)
    _db.session.add(todo)
    _db.session.commit()
    return todo


def _client(app, user):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return client


def _contents(user_id):
    return [t.get_content() for t in UserTodo.query.filter_by(
        user_id=user_id).order_by(UserTodo.id).all()]


OLD = "## Today\n- [ ] call mom"
MERGED = "## Today\n- [ ] call mom\n- [ ] buy milk"
MINE = "## Today\n- [ ] call mom tonight"


def test_a_save_from_a_stale_editor_is_refused_and_nothing_is_lost(app):
    """The scenario of #476: the editor was opened on the old list, a todo
    merge saved a new version, then the user changes a line and saves."""
    user = _user()
    _todo(user.id, OLD)
    client = _client(app, user)
    opened = client.get("/api/todo/").get_json()["todo"]
    _todo(user.id, MERGED, generated_by="voice_session")

    res = client.put("/api/todo/", json={
        "content": MINE, "generated_by": "user",
        "base_revision": opened["revision"]})

    assert res.status_code == 409
    body = res.get_json()
    assert body["code"] == "todo_changed"
    assert body["error"]
    # The answer carries the newest list, so the editor can show it.
    assert body["todo"]["content"] == MERGED
    assert body["todo"]["version_number"] == 2
    # Nothing was written: the merge's tasks are still the newest list.
    assert _contents(user.id) == [OLD, MERGED]


def test_save_mine_anyway_saves_the_users_text_on_top(app):
    """"Save mine anyway" sends the same text with the newest revision: the
    user's text becomes the newest version, and the merge's version stays
    in history."""
    user = _user()
    _todo(user.id, OLD)
    client = _client(app, user)
    opened = client.get("/api/todo/").get_json()["todo"]
    _todo(user.id, MERGED, generated_by="voice_session")
    refused = client.put("/api/todo/", json={
        "content": MINE, "base_revision": opened["revision"]}).get_json()

    res = client.put("/api/todo/", json={
        "content": MINE, "generated_by": "user",
        "base_revision": refused["todo"]["revision"]})

    assert res.status_code == 200
    saved = res.get_json()["todo"]
    assert saved["content"] == MINE
    assert saved["version_number"] == 3
    assert _contents(user.id) == [OLD, MERGED, MINE]


def test_a_save_after_a_tick_elsewhere_is_refused(app):
    """A tick on another device edits the newest version in place; the
    editor opened before it would drop the tick."""
    user = _user()
    _todo(user.id, MERGED)
    client = _client(app, user)
    opened = client.get("/api/todo/").get_json()["todo"]
    ticked = client.patch("/api/todo/", json={
        "content": "## Today\n- [x] call mom\n- [ ] buy milk",
        "base_revision": opened["revision"]})
    assert ticked.status_code == 200

    res = client.put("/api/todo/", json={
        "content": "## Today\n- [ ] call mom\n- [ ] buy milk\n- [ ] bake",
        "base_revision": opened["revision"]})

    assert res.status_code == 409
    assert res.get_json()["todo"]["content"] == (
        "## Today\n- [x] call mom\n- [ ] buy milk")
    assert _contents(user.id) == ["## Today\n- [x] call mom\n- [ ] buy milk"]


def test_a_save_on_the_newest_version_saves(app):
    user = _user()
    _todo(user.id, OLD)
    client = _client(app, user)
    opened = client.get("/api/todo/").get_json()["todo"]

    res = client.put("/api/todo/", json={
        "content": MINE, "base_revision": opened["revision"]})

    assert res.status_code == 200
    assert res.get_json()["todo"]["revision"] != opened["revision"]
    assert _contents(user.id) == [OLD, MINE]


def test_a_save_without_a_revision_saves_as_before(app):
    """Create, and clients from before #476, send only the content."""
    user = _user()
    _todo(user.id, OLD)
    _todo(user.id, MERGED, generated_by="voice_session")

    res = _client(app, user).put("/api/todo/", json={"content": MINE})

    assert res.status_code == 200
    assert _contents(user.id) == [OLD, MERGED, MINE]

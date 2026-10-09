"""A stale web Todo page must not overwrite a newer todo list (#430).

PATCH /api/todo/ edits the latest version in place. The web page sends the
revision of the list it applied its change to (base_revision); when the
latest version is no longer that text (a todo merge saved a new version, or
another device or tab edited it in place), the route writes nothing and
answers 409 with the latest version. A request without base_revision (the
shipped iPhone app) overwrites as before.

Same harness as test_todo_merge_ai_usage: in-memory SQLite,
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


def test_get_returns_the_revision(app):
    user = _user()
    _todo(user.id, OLD)

    todo = _client(app, user).get("/api/todo/").get_json()["todo"]

    assert todo["content"] == OLD
    assert isinstance(todo["revision"], str) and todo["revision"]


def test_patch_with_the_current_revision_saves(app):
    user = _user()
    _todo(user.id, OLD)
    client = _client(app, user)
    loaded = client.get("/api/todo/").get_json()["todo"]

    res = client.patch("/api/todo/", json={
        "content": "## Today\n- [x] call mom",
        "base_revision": loaded["revision"]})

    assert res.status_code == 200
    saved = res.get_json()["todo"]
    assert saved["content"] == "## Today\n- [x] call mom"
    # The in-place write gives the version a new revision, which the next
    # save sends.
    assert saved["revision"] != loaded["revision"]
    assert _contents(user.id) == ["## Today\n- [x] call mom"]


def test_patch_after_a_merge_saved_a_newer_version_is_refused(app):
    """The scenario of #430: the page loaded the old list, a todo merge
    saved a new version, then the user ticks an item on the page."""
    user = _user()
    _todo(user.id, OLD)
    client = _client(app, user)
    loaded = client.get("/api/todo/").get_json()["todo"]
    _todo(user.id, MERGED, generated_by="voice_session")

    res = client.patch("/api/todo/", json={
        "content": "## Today\n- [x] call mom",
        "base_revision": loaded["revision"]})

    assert res.status_code == 409
    body = res.get_json()
    assert body["code"] == "todo_changed"
    assert body["error"]
    # The answer carries the newest list, so the page can apply its tick
    # to it and save again.
    assert body["todo"]["content"] == MERGED
    assert body["todo"]["version_number"] == 2
    # Nothing was written: the merge's version and the history are intact.
    assert _contents(user.id) == [OLD, MERGED]

    retry = client.patch("/api/todo/", json={
        "content": "## Today\n- [x] call mom\n- [ ] buy milk",
        "base_revision": body["todo"]["revision"]})
    assert retry.status_code == 200
    assert _contents(user.id) == [OLD, "## Today\n- [x] call mom\n- [ ] buy milk"]


def test_patch_after_an_in_place_edit_elsewhere_is_refused(app):
    """Another tab or device ticked an item in place: same version id, new
    text. A save based on the text before that is refused."""
    user = _user()
    _todo(user.id, MERGED)
    client = _client(app, user)
    loaded = client.get("/api/todo/").get_json()["todo"]

    other = client.patch("/api/todo/", json={
        "content": "## Today\n- [ ] call mom\n- [x] buy milk",
        "base_revision": loaded["revision"]})
    assert other.status_code == 200

    res = client.patch("/api/todo/", json={
        "content": "## Today\n- [x] call mom\n- [ ] buy milk",
        "base_revision": loaded["revision"]})

    assert res.status_code == 409
    assert res.get_json()["todo"]["content"] == (
        "## Today\n- [ ] call mom\n- [x] buy milk")
    assert _contents(user.id) == ["## Today\n- [ ] call mom\n- [x] buy milk"]


def test_patch_without_a_revision_overwrites_as_before(app):
    """The shipped iPhone app sends only the content."""
    user = _user()
    _todo(user.id, OLD)
    _todo(user.id, MERGED, generated_by="voice_session")

    res = _client(app, user).patch("/api/todo/", json={
        "content": "## Today\n- [x] call mom"})

    assert res.status_code == 200
    assert _contents(user.id) == [OLD, "## Today\n- [x] call mom"]


def test_put_and_revert_return_a_fresh_revision(app):
    user = _user()
    first = _todo(user.id, OLD)
    client = _client(app, user)
    loaded = client.get("/api/todo/").get_json()["todo"]

    put = client.put("/api/todo/", json={"content": MERGED}).get_json()["todo"]
    reverted = client.post(f"/api/todo/revert/{first.id}").get_json()["todo"]

    # A revert copies the old version's stored text into a new version; it
    # still gets its own revision, so a page holding the old one is refused.
    revisions = {loaded["revision"], put["revision"], reverted["revision"]}
    assert len(revisions) == 3
    assert reverted["content"] == OLD
    res = client.patch("/api/todo/", json={
        "content": "## Today\n- [x] call mom",
        "base_revision": loaded["revision"]})
    assert res.status_code == 409

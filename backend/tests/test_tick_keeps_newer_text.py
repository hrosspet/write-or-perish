"""A tick saves only over the text the page showed (#795).

A checklist tick (and the row "+") on an entry or an artifact sent the
page's whole text, and the server stored it. A page left open while the
text changed elsewhere (another tab, the phone, the AI updating an
artifact) put its older text back with one box changed, undoing the newer
edits. The page now also sends the text it showed before the tick
(``base_content``); the server saves only when that is still the stored
text, and otherwise saves nothing and answers 409 ``content_changed``. The
page then undoes the tick and asks the person to reload.

A request without ``base_content`` (an installed iPhone build, a tab
opened before the change) is saved as before.
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

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login  # noqa: E402
from backend.extensions import db as _db  # noqa: E402
from backend.models import (  # noqa: E402
    User, Node, NodeContextArtifact, NodeVersion, UserArtifact,
)
import backend.models as _real_backend_models  # noqa: E402


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

    from backend.routes.nodes import nodes_bp
    from backend.routes.artifacts import artifacts_bp
    app.register_blueprint(nodes_bp, url_prefix="/nodes")
    app.register_blueprint(artifacts_bp, url_prefix="/artifacts")
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


@pytest.fixture
def alice(app):
    u = User(username="alice", twitter_id="alice-twitter-id")
    _db.session.add(u)
    _db.session.commit()
    return u


@pytest.fixture
def client(app, alice):
    c = app.test_client()
    with c.session_transaction() as session:
        session["_user_id"] = str(alice.id)
        session["_fresh"] = True
    return c


STATUS = "## Status\n- [ ] deploy\n- [ ] write the note\n- [ ] call the bank"


def _entry(user, content=STATUS):
    node = Node(user_id=user.id, human_owner_id=user.id, node_type="user",
                privacy_level="private", ai_usage="none", token_count=1)
    node.set_content(content)
    _db.session.add(node)
    _db.session.commit()
    return node


def _stored(node_id):
    _db.session.expire_all()
    return Node.query.get(node_id).get_content()


def _versions(node_id):
    return NodeVersion.query.filter_by(node_id=node_id).count()


def _tick(text, label):
    return text.replace(f"- [ ] {label}", f"- [x] {label}")


# ── Entries ──────────────────────────────────────────────────────────────

def test_a_tick_on_a_page_showing_the_stored_text_is_saved(client, alice):
    entry = _entry(alice)
    r = client.put(f"/nodes/{entry.id}", json={
        "content": _tick(STATUS, "deploy"), "base_content": STATUS})
    assert r.status_code == 200
    assert _stored(entry.id) == _tick(STATUS, "deploy")


def test_a_tick_from_a_second_tab_does_not_undo_the_first_tabs_edit(
        client, alice):
    entry = _entry(alice)
    # Tab A edits the entry: a new line and a reworded one.
    newer = ("## Status\n- [ ] deploy on Monday\n- [ ] write the note\n"
             "- [ ] call the bank\n- [ ] book the room")
    assert client.put(f"/nodes/{entry.id}",
                      json={"content": newer}).status_code == 200
    versions = _versions(entry.id)

    # Tab B still shows the older text and ticks a box in it.
    r = client.put(f"/nodes/{entry.id}", json={
        "content": _tick(STATUS, "call the bank"), "base_content": STATUS})
    assert r.status_code == 409
    assert r.get_json()["code"] == "content_changed"
    # Nothing saved: tab A's text stays, and no version was added.
    assert _stored(entry.id) == newer
    assert _versions(entry.id) == versions


def test_a_tick_on_a_line_reworded_elsewhere_saves_nothing(client, alice):
    entry = _entry(alice)
    reworded = STATUS.replace("write the note", "write the long note")
    client.put(f"/nodes/{entry.id}", json={"content": reworded})

    r = client.put(f"/nodes/{entry.id}", json={
        "content": _tick(STATUS, "write the note"), "base_content": STATUS})
    assert r.status_code == 409
    assert _stored(entry.id) == reworded


def test_a_tick_already_made_elsewhere_still_needs_the_current_text(
        client, alice):
    """The phone ticked the same box: the stale page's text differs from
    the stored one in nothing but that box, and it is still refused, as
    the page can't know what else changed."""
    entry = _entry(alice)
    ticked = _tick(STATUS, "deploy")
    client.put(f"/nodes/{entry.id}", json={"content": ticked})

    r = client.put(f"/nodes/{entry.id}", json={
        "content": ticked, "base_content": STATUS})
    assert r.status_code == 409
    assert _stored(entry.id) == ticked


def test_quick_ticks_each_build_on_the_text_the_last_one_saved(
        client, alice):
    entry = _entry(alice)
    one = _tick(STATUS, "deploy")
    two = _tick(one, "call the bank")
    assert client.put(f"/nodes/{entry.id}", json={
        "content": one, "base_content": STATUS}).status_code == 200
    assert client.put(f"/nodes/{entry.id}", json={
        "content": two, "base_content": one}).status_code == 200
    assert _stored(entry.id) == two


def test_a_request_without_base_content_is_saved_as_before(client, alice):
    """Installed iPhone builds and tabs opened before this change send
    only the text: it is stored as it was before."""
    entry = _entry(alice)
    client.put(f"/nodes/{entry.id}",
               json={"content": STATUS + "\n- [ ] newer line"})
    r = client.put(f"/nodes/{entry.id}",
                   json={"content": _tick(STATUS, "deploy")})
    assert r.status_code == 200
    assert _stored(entry.id) == _tick(STATUS, "deploy")


def test_someone_elses_entry_is_refused_before_the_text_check(
        app, client, alice):
    bob = User(username="bob", twitter_id="bob-twitter-id")
    _db.session.add(bob)
    _db.session.commit()
    entry = _entry(bob)
    r = client.put(f"/nodes/{entry.id}", json={
        "content": _tick(STATUS, "deploy"), "base_content": "other"})
    assert r.status_code == 403


# ── Artifacts ────────────────────────────────────────────────────────────

LIST = "- [ ] a\n- [ ] b\n- [ ] c"


def _artifact(user, content=LIST, generated_by="user", kind="dev-map"):
    row = UserArtifact(user_id=user.id, kind=kind, title="Dev Map",
                       generated_by=generated_by, tokens_used=0)
    row.set_content(content)
    _db.session.add(row)
    _db.session.commit()
    return row


def _latest(user, kind="dev-map"):
    _db.session.expire_all()
    return UserArtifact.latest_for(user.id, kind)


def _count(user, kind="dev-map"):
    return UserArtifact.version_count(user.id, kind)


def test_an_artifact_tick_over_the_shown_text_edits_in_place(
        client, alice):
    _artifact(alice)
    r = client.patch("/artifacts/dev-map", json={
        "content": _tick(LIST, "a"), "base_content": LIST})
    assert r.status_code == 200
    assert _latest(alice).get_content() == _tick(LIST, "a")
    assert _count(alice) == 1


def test_an_artifact_tick_from_a_second_tab_saves_nothing(client, alice):
    """Both tabs show v1. Tab A ticks a (an in-place edit: still v1, so
    a version number can't tell them apart); tab B then ticks b."""
    _artifact(alice)
    assert client.patch("/artifacts/dev-map", json={
        "content": _tick(LIST, "a"), "base_content": LIST,
    }).status_code == 200

    r = client.patch("/artifacts/dev-map", json={
        "content": _tick(LIST, "b"), "base_content": LIST})
    assert r.status_code == 409
    assert r.get_json()["code"] == "content_changed"
    assert _latest(alice).get_content() == _tick(LIST, "a")
    assert _count(alice) == 1


def test_an_artifact_tick_after_the_ai_updated_it_saves_nothing(
        client, alice):
    _artifact(alice)
    # The AI wrote a new version while the page showed v1.
    ai_text = "- [ ] a\n- [ ] b, by Friday\n- [ ] c\n- [ ] d"
    _artifact(alice, ai_text, generated_by="claude-opus-5.5")

    r = client.patch("/artifacts/dev-map", json={
        "content": _tick(LIST, "c"), "base_content": LIST})
    assert r.status_code == 409
    latest = _latest(alice)
    assert latest.get_content() == ai_text
    assert latest.generated_by == "claude-opus-5.5"
    # No user version was made from the older text.
    assert _count(alice) == 2


def test_an_artifact_tick_on_the_ai_version_shown_makes_a_user_version(
        client, alice):
    """The page shows the AI's latest version: the tick is saved as a new
    user version, as before (the AI's version stays as it was)."""
    _artifact(alice)
    ai_text = "- [ ] a\n- [ ] b\n- [ ] c\n- [ ] d"
    ai_row = _artifact(alice, ai_text, generated_by="claude-opus-5.5")
    ai_id = ai_row.id

    r = client.patch("/artifacts/dev-map", json={
        "content": _tick(ai_text, "d"), "base_content": ai_text})
    assert r.status_code == 200
    art = r.get_json()["artifact"]
    assert art["generated_by"] == "user"
    assert art["version_number"] == 3
    _db.session.expire_all()
    assert UserArtifact.query.get(ai_id).get_content() == ai_text


def test_an_artifact_tick_on_a_pinned_version_still_makes_a_new_one(
        client, alice):
    row = _artifact(alice)
    node = Node(user_id=alice.id, node_type="user")
    node.set_content("session")
    _db.session.add(node)
    _db.session.flush()
    _db.session.add(NodeContextArtifact(
        node_id=node.id, artifact_type="user_artifact", artifact_id=row.id))
    _db.session.commit()
    pinned_id = row.id

    r = client.patch("/artifacts/dev-map", json={
        "content": _tick(LIST, "a"), "base_content": LIST})
    assert r.status_code == 200
    assert r.get_json()["artifact"]["version_number"] == 2
    _db.session.expire_all()
    assert UserArtifact.query.get(pinned_id).get_content() == LIST


def test_an_artifact_plus_on_a_stale_page_saves_nothing(client, alice):
    _artifact(alice)
    newer = LIST + "\n- [ ] e"
    client.patch("/artifacts/dev-map", json={"content": newer})

    r = client.patch("/artifacts/dev-map", json={
        "content": "- [ ] a\n- [ ] new\n- [ ] b\n- [ ] c",
        "base_content": LIST})
    assert r.status_code == 409
    assert _latest(alice).get_content() == newer


def test_quick_artifact_ticks_each_build_on_the_last_saved_text(
        client, alice):
    _artifact(alice)
    one = _tick(LIST, "a")
    two = _tick(one, "b")
    assert client.patch("/artifacts/dev-map", json={
        "content": one, "base_content": LIST}).status_code == 200
    assert client.patch("/artifacts/dev-map", json={
        "content": two, "base_content": one}).status_code == 200
    assert _latest(alice).get_content() == two


def test_an_artifact_patch_without_base_content_is_saved_as_before(
        client, alice):
    _artifact(alice)
    client.patch("/artifacts/dev-map", json={"content": LIST + "\n- [ ] e"})
    r = client.patch("/artifacts/dev-map",
                     json={"content": _tick(LIST, "a")})
    assert r.status_code == 200
    assert _latest(alice).get_content() == _tick(LIST, "a")

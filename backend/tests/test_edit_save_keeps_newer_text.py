"""An edit form's Save never puts the form's older text over a newer one.

The edit form of an entry and of an artifact sent its whole text, and the
server stored it. A form opened on a page left open while the text changed
elsewhere (another tab, the phone, the AI, a tick) put its older text back,
undoing the newer change. The forms now also send ``base_content``: the
text they were opened on. The server saves only while that is still the
stored text; otherwise it saves nothing and answers 409 ``content_changed``,
and the form keeps the person's text and says what to do. A Save whose text
is untouched (only privacy, AI usage or the description changed) keeps the
stored text, whatever it is now.

Edit drafts now carry the same base, so a draft written on an older text is
offered beside the entry's current text when the form opens again, instead
of being loaded and saved over it.

A request without ``base_content`` (installed iPhone builds, tabs opened
before this change) is saved as before.
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
    User, Node, NodeVersion, Draft, UserArtifact,
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
    from backend.routes.drafts import drafts_bp
    app.register_blueprint(nodes_bp, url_prefix="/nodes")
    app.register_blueprint(artifacts_bp, url_prefix="/artifacts")
    app.register_blueprint(drafts_bp, url_prefix="/drafts")
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


TEXT = "## Plan\nCall the bank on Monday.\n- [ ] deploy\n- [ ] write the note"


def _entry(user, content=TEXT):
    node = Node(user_id=user.id, human_owner_id=user.id, node_type="user",
                privacy_level="private", ai_usage="none", token_count=1)
    node.set_content(content)
    _db.session.add(node)
    _db.session.commit()
    return node


def _stored(node_id):
    _db.session.expire_all()
    return Node.query.get(node_id).get_content()


def _shown(client, node_id):
    """The entry's text as the page got it (what the form opens on)."""
    return client.get(f"/nodes/{node_id}").get_json()["content"]


# ── The entry's edit form ────────────────────────────────────────────────

def test_a_save_on_the_text_the_form_was_opened_on_is_saved(client, alice):
    entry = _entry(alice)
    edited = TEXT.replace("Monday", "Tuesday")
    r = client.put(f"/nodes/{entry.id}", json={
        "content": edited, "base_content": _shown(client, entry.id),
        "privacy_level": "private", "ai_usage": "none"})
    assert r.status_code == 200
    assert _stored(entry.id) == edited


def test_a_save_from_a_form_opened_before_a_tick_saves_nothing(
        client, alice):
    """The form was opened; the phone ticked a box; the form saves its
    edit, made on the text without the tick."""
    entry = _entry(alice)
    opened_on = _shown(client, entry.id)
    ticked = TEXT.replace("- [ ] deploy", "- [x] deploy")
    assert client.put(f"/nodes/{entry.id}", json={
        "content": ticked, "base_content": opened_on}).status_code == 200
    versions = NodeVersion.query.filter_by(node_id=entry.id).count()

    r = client.put(f"/nodes/{entry.id}", json={
        "content": opened_on.replace("Monday", "Tuesday"),
        "base_content": opened_on,
        "privacy_level": "private", "ai_usage": "none"})
    assert r.status_code == 409
    assert r.get_json()["code"] == "content_changed"
    assert _stored(entry.id) == ticked
    assert NodeVersion.query.filter_by(node_id=entry.id).count() == versions


def test_a_save_after_the_text_was_edited_elsewhere_saves_nothing(
        client, alice):
    entry = _entry(alice)
    opened_on = _shown(client, entry.id)
    newer = TEXT + "\n- [ ] book the room"
    client.put(f"/nodes/{entry.id}", json={"content": newer})

    r = client.put(f"/nodes/{entry.id}", json={
        "content": opened_on + "\nA new paragraph.",
        "base_content": opened_on})
    assert r.status_code == 409
    assert _stored(entry.id) == newer


def test_a_settings_only_save_keeps_the_newer_text(client, alice):
    """Only the privacy changed in the form: it has no text of its own to
    save, so the entry keeps the text it has now, and the setting is saved."""
    entry = _entry(alice)
    opened_on = _shown(client, entry.id)
    newer = TEXT + "\n- [ ] book the room"
    client.put(f"/nodes/{entry.id}", json={"content": newer})

    r = client.put(f"/nodes/{entry.id}", json={
        "content": opened_on, "base_content": opened_on,
        "privacy_level": "circles", "ai_usage": "none"})
    assert r.status_code == 200
    assert _stored(entry.id) == newer
    assert r.get_json()["node"]["content"] == newer
    _db.session.expire_all()
    assert Node.query.get(entry.id).privacy_level == "circles"


@pytest.mark.parametrize("text", [
    "line one\r\nline two\r\n",
    "trailing spaces   \n\n\n",
    "“typographic quotes” and ‘apostrophes’",
    "a {quote:12} placeholder and a {user_profile} one",
    "a link https://example.com/?a=1&b=2 and an é with \U0001F33F",
    "",
])
def test_the_first_save_of_a_form_opened_fresh_never_conflicts(
        client, alice, text):
    """The form sends back, as its base, the text the page got: whatever
    it holds, the first Save is saved."""
    entry = _entry(alice, content=text or "x")
    if not text:
        entry.set_content("")
        _db.session.commit()
    opened_on = _shown(client, entry.id)
    r = client.put(f"/nodes/{entry.id}", json={
        "content": "edited", "base_content": opened_on})
    assert r.status_code == 200
    assert _stored(entry.id) == "edited"


def test_an_edit_save_without_a_base_is_saved_as_before(client, alice):
    entry = _entry(alice)
    client.put(f"/nodes/{entry.id}", json={"content": TEXT + "\nnewer"})
    r = client.put(f"/nodes/{entry.id}", json={"content": "older form"})
    assert r.status_code == 200
    assert _stored(entry.id) == "older form"


# ── The artifacts page's edit form ───────────────────────────────────────

LIST = "- [ ] a\n- [ ] b"


def _artifact(user, content=LIST, generated_by="user", kind="dev-map"):
    row = UserArtifact(user_id=user.id, kind=kind, title="Dev Map",
                       description="Ideas", generated_by=generated_by,
                       tokens_used=0)
    row.set_content(content)
    _db.session.add(row)
    _db.session.commit()
    return row


def _latest(user, kind="dev-map"):
    _db.session.expire_all()
    return UserArtifact.latest_for(user.id, kind)


def _count(user, kind="dev-map"):
    return UserArtifact.version_count(user.id, kind)


def test_an_artifact_save_on_the_shown_text_makes_a_version(client, alice):
    _artifact(alice)
    r = client.put("/artifacts/dev-map", json={
        "content": LIST + "\n- [ ] c", "base_content": LIST,
        "description": "Ideas", "generated_by": "user"})
    assert r.status_code == 200
    assert _latest(alice).get_content() == LIST + "\n- [ ] c"
    assert _count(alice) == 2


def test_an_artifact_save_after_a_tick_in_another_tab_saves_nothing(
        client, alice):
    """A tick in place keeps the version (v1): only the text tells."""
    _artifact(alice)
    assert client.patch("/artifacts/dev-map", json={
        "content": "- [x] a\n- [ ] b", "base_content": LIST,
    }).status_code == 200

    r = client.put("/artifacts/dev-map", json={
        "content": LIST + "\n- [ ] c", "base_content": LIST,
        "description": "Ideas", "generated_by": "user"})
    assert r.status_code == 409
    assert r.get_json()["code"] == "content_changed"
    assert _latest(alice).get_content() == "- [x] a\n- [ ] b"
    assert _count(alice) == 1


def test_an_artifact_save_after_the_ai_wrote_a_version_saves_nothing(
        client, alice):
    _artifact(alice)
    _artifact(alice, LIST + "\n- [ ] the AI's line",
              generated_by="claude-opus-5.5")

    r = client.put("/artifacts/dev-map", json={
        "content": "- [ ] a, rewritten\n- [ ] b", "base_content": LIST,
        "description": "Ideas", "generated_by": "user"})
    assert r.status_code == 409
    assert _latest(alice).get_content() == LIST + "\n- [ ] the AI's line"
    assert _count(alice) == 2


def test_a_description_only_save_keeps_the_newer_text(client, alice):
    _artifact(alice)
    _artifact(alice, LIST + "\n- [ ] the AI's line",
              generated_by="claude-opus-5.5")
    r = client.put("/artifacts/dev-map", json={
        "content": LIST, "base_content": LIST,
        "description": "Ideas, sorted", "generated_by": "user"})
    assert r.status_code == 200
    latest = _latest(alice)
    assert latest.get_content() == LIST + "\n- [ ] the AI's line"
    assert latest.description == "Ideas, sorted"


def test_writing_an_empty_artifact_that_the_ai_filled_meanwhile_saves_nothing(
        client, alice):
    """The page showed memory empty ("Write Memory"); the AI wrote v1."""
    _artifact(alice, "- has a dog", generated_by="claude-opus-5.5",
              kind="memory")
    r = client.put("/artifacts/memory", json={
        "content": "- lives in Prague", "base_content": "",
        "description": "Facts", "generated_by": "user"})
    assert r.status_code == 409
    assert _latest(alice, "memory").get_content() == "- has a dog"


def test_writing_an_empty_artifact_creates_it(client, alice):
    r = client.put("/artifacts/memory", json={
        "content": "- lives in Prague", "base_content": "",
        "description": "Facts", "generated_by": "user"})
    assert r.status_code == 200
    assert _latest(alice, "memory").get_content() == "- lives in Prague"


def test_an_artifact_save_without_a_base_is_saved_as_before(client, alice):
    _artifact(alice)
    client.patch("/artifacts/dev-map", json={"content": "- [x] a\n- [ ] b"})
    r = client.put("/artifacts/dev-map", json={
        "content": LIST + "\n- [ ] c", "description": "Ideas",
        "generated_by": "user"})
    assert r.status_code == 200
    assert _latest(alice).get_content() == LIST + "\n- [ ] c"


# ── Edit drafts ──────────────────────────────────────────────────────────

def _autosave(client, entry, content, base):
    r = client.post("/drafts/", json={
        "content": content, "node_id": entry.id, "base_content": base})
    assert r.status_code == 200
    return r.get_json()["id"]


def _draft(client, entry):
    return client.get(f"/drafts/?node_id={entry.id}").get_json()


def test_a_draft_on_the_current_text_is_loaded_as_before(client, alice):
    entry = _entry(alice)
    opened_on = _shown(client, entry.id)
    _autosave(client, entry, opened_on + "\nmore", opened_on)
    got = _draft(client, entry)
    assert got["content"] == opened_on + "\nmore"
    assert "older_than_entry" not in got


def test_a_refused_saves_draft_is_offered_when_the_form_opens_again(
        client, alice):
    """The form typed on, after the entry changed elsewhere and after its
    Save was refused: its draft was saved after the entry's change, but it
    is still written on the older text, so it is offered, not loaded."""
    entry = _entry(alice)
    opened_on = _shown(client, entry.id)
    _autosave(client, entry, opened_on + "\nmy paragraph", opened_on)
    newer = TEXT.replace("- [ ] deploy", "- [x] deploy")
    client.put(f"/nodes/{entry.id}", json={"content": newer})
    # The form keeps typing (its autosave lands after the entry's change).
    first = _autosave(client, entry, opened_on + "\nmy paragraph, longer",
                      opened_on)
    assert client.put(f"/nodes/{entry.id}", json={
        "content": opened_on + "\nmy paragraph, longer",
        "base_content": opened_on}).status_code == 409

    got = _draft(client, entry)
    assert got["older_than_entry"] is True
    assert got["content"] == opened_on + "\nmy paragraph, longer"
    # One row: the same form's typing went into the same draft.
    assert got["id"] == first
    assert Draft.query.filter_by(node_id=entry.id).count() == 1
    assert _stored(entry.id) == newer


def test_typing_on_the_newer_text_keeps_the_older_draft_beside_it(
        client, alice):
    entry = _entry(alice)
    opened_on = _shown(client, entry.id)
    older_id = _autosave(client, entry, opened_on + "\nolder", opened_on)
    newer = TEXT + "\n- [ ] book the room"
    client.put(f"/nodes/{entry.id}", json={"content": newer})

    # The form opened again on the newer text; the person types there.
    mine = _autosave(client, entry, newer + "\nnew typing", newer)
    assert mine != older_id
    got = _draft(client, entry)
    assert got["id"] == mine
    assert "older_than_entry" not in got
    assert got["older_draft"]["id"] == older_id
    assert got["older_draft"]["content"] == opened_on + "\nolder"


def test_a_drafts_base_is_kept_out_of_plain_sight(client, alice):
    """Encrypted like the draft's text; never in GET's answer."""
    entry = _entry(alice)
    opened_on = _shown(client, entry.id)
    _autosave(client, entry, "typed", opened_on)
    row = Draft.query.filter_by(node_id=entry.id).one()
    assert row.get_base_content() == opened_on
    assert "base_content" not in _draft(client, entry)

"""An edit draft older than its entry's last change is marked (#761).

The iPhone's edit form loaded the edit draft in place of the entry's text
whenever one existed. A draft left behind (the form closed without saving)
and an entry changed afterwards (a checkbox ticked on the thread page, an
edit on another device) meant that Save put the older text over the newer
one. GET /drafts/?node_id= now says when the draft was last saved before
the entry changed, so the form shows the entry's text and offers the draft.
The draft itself is kept: it holds text the person typed.
"""
import os
import sys
from datetime import datetime, timedelta
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
from backend.models import User, Node, Draft  # noqa: E402
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
    from backend.routes.drafts import drafts_bp
    app.register_blueprint(nodes_bp, url_prefix="/nodes")
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


def _entry(user, content="- [ ] milk\n- [ ] bread"):
    node = Node(user_id=user.id, human_owner_id=user.id, node_type="user",
                privacy_level="private", ai_usage="none", token_count=1)
    node.set_content(content)
    _db.session.add(node)
    _db.session.commit()
    return node


def _age_draft(draft_id, seconds):
    """The draft was last saved *seconds* ago (requests in a test run
    within microseconds of each other)."""
    draft = Draft.query.get(draft_id)
    draft.updated_at = datetime.utcnow() - timedelta(seconds=seconds)
    _db.session.commit()


def test_a_draft_left_before_a_checkbox_was_ticked_is_marked_and_kept(
        client, alice):
    entry = _entry(alice)
    # The edit form autosaves, and the person leaves without saving.
    saved = client.post("/drafts/", json={
        "content": "- [ ] milk\n- [ ] bread\n- [ ] eggs", "node_id": entry.id})
    assert saved.status_code == 200
    _age_draft(saved.get_json()["id"], 60)

    # A checkbox is ticked on the thread page: the entry is saved.
    ticked = client.put(f"/nodes/{entry.id}",
                        json={"content": "- [x] milk\n- [ ] bread"})
    assert ticked.status_code == 200

    got = client.get(f"/drafts/?node_id={entry.id}")
    assert got.status_code == 200
    body = got.get_json()
    assert body["older_than_entry"] is True
    assert body["entry_updated_at"].endswith("Z")
    # The person's unsaved text is still there, to use or discard.
    assert body["content"] == "- [ ] milk\n- [ ] bread\n- [ ] eggs"


def test_a_draft_saved_after_the_entry_changed_is_not_marked(client, alice):
    entry = _entry(alice)
    entry.updated_at = datetime.utcnow() - timedelta(minutes=5)
    _db.session.commit()
    client.post("/drafts/", json={"content": "typed later", "node_id": entry.id})

    body = client.get(f"/drafts/?node_id={entry.id}").get_json()

    assert body["content"] == "typed later"
    assert "older_than_entry" not in body


def test_a_new_entrys_draft_is_never_marked(client, alice):
    parent = _entry(alice)
    saved = client.post("/drafts/",
                        json={"content": "a reply", "parent_id": parent.id})
    _age_draft(saved.get_json()["id"], 60)
    client.put(f"/nodes/{parent.id}", json={"content": "the parent, edited"})

    body = client.get(f"/drafts/?parent_id={parent.id}").get_json()

    assert body["content"] == "a reply"
    assert "older_than_entry" not in body


def _left_edit_then_tick(client, entry):
    """Steps 1-2 of #761: an edit left unsaved, then a checkbox ticked."""
    saved = client.post("/drafts/", json={
        "content": "- [ ] milk\n- [ ] bread\n- [ ] eggs", "node_id": entry.id})
    _age_draft(saved.get_json()["id"], 60)
    assert client.put(f"/nodes/{entry.id}",
                      json={"content": "- [x] milk\n- [ ] bread"}).status_code == 200
    return saved.get_json()["id"]


def test_typing_after_the_mark_keeps_the_older_edit_beside_it(client, alice):
    # #766 review: typing in the entry's text must not overwrite the older
    # edit; both stay until the person chooses.
    entry = _entry(alice)
    older_id = _left_edit_then_tick(client, entry)

    typed = client.post("/drafts/", json={
        "content": "- [x] milk\n- [x] bread", "node_id": entry.id})
    assert typed.status_code == 200
    assert typed.get_json()["id"] != older_id
    assert Draft.query.get(older_id).get_content() == (
        "- [ ] milk\n- [ ] bread\n- [ ] eggs")

    # More typing updates the new row, not the older one.
    client.post("/drafts/", json={
        "content": "- [x] milk\n- [x] bread\n- [ ] tea", "node_id": entry.id})
    assert Draft.query.filter_by(node_id=entry.id).count() == 2

    body = client.get(f"/drafts/?node_id={entry.id}").get_json()
    # Every client gets the newest draft, as before.
    assert body["id"] == typed.get_json()["id"]
    assert body["content"] == "- [x] milk\n- [x] bread\n- [ ] tea"
    assert "older_than_entry" not in body
    # The older edit comes back beside it.
    assert body["older_draft"]["id"] == older_id
    assert body["older_draft"]["content"] == "- [ ] milk\n- [ ] bread\n- [ ] eggs"
    assert body["older_draft"]["updated_at"].endswith("Z")
    assert body["entry_updated_at"].endswith("Z")


def test_discarding_the_older_edit_deletes_only_that_row(client, alice):
    entry = _entry(alice)
    older_id = _left_edit_then_tick(client, entry)
    typed = client.post("/drafts/", json={"content": "typed", "node_id": entry.id})

    resp = client.delete(f"/drafts/?node_id={entry.id}&draft_id={older_id}")

    assert resp.status_code == 200
    assert Draft.query.get(older_id) is None
    body = client.get(f"/drafts/?node_id={entry.id}").get_json()
    assert body["id"] == typed.get_json()["id"]
    assert "older_draft" not in body
    # Another entry's draft id is not this entry's to delete.
    other = _entry(alice)
    other_draft = client.post("/drafts/", json={"content": "x", "node_id": other.id})
    assert client.delete(
        f"/drafts/?node_id={entry.id}&draft_id={other_draft.get_json()['id']}"
    ).status_code == 404
    assert Draft.query.get(other_draft.get_json()["id"]) is not None


def test_saving_the_entry_deletes_both(client, alice):
    entry = _entry(alice)
    _left_edit_then_tick(client, entry)
    client.post("/drafts/", json={"content": "typed", "node_id": entry.id})

    assert client.delete(f"/drafts/?node_id={entry.id}").status_code == 200

    assert Draft.query.filter_by(node_id=entry.id).count() == 0


def test_every_older_edit_is_kept_and_offered_newest_first(client, alice):
    # A second round: the typing that replaced an older edit itself becomes
    # older than the entry, and the person types again. #766 kept only the
    # newest older edit and deleted the earlier one; since the #799 review
    # an autosave deletes no row (it may hold typing the person has not
    # seen): the newest older edit is offered, the earlier one after it.
    entry = _entry(alice)
    first_id = _left_edit_then_tick(client, entry)
    second = client.post("/drafts/", json={"content": "second", "node_id": entry.id})
    second_id = second.get_json()["id"]
    _age_draft(second_id, 30)
    client.put(f"/nodes/{entry.id}", json={"content": "- [x] milk\n- [x] bread"})

    marked = client.get(f"/drafts/?node_id={entry.id}").get_json()
    assert marked["id"] == second_id and marked["older_than_entry"] is True

    third = client.post("/drafts/", json={"content": "third", "node_id": entry.id})

    assert Draft.query.get(first_id) is not None
    assert Draft.query.get(second_id).get_content() == "second"
    body = client.get(f"/drafts/?node_id={entry.id}").get_json()
    assert body["id"] == third.get_json()["id"]
    assert body["older_draft"]["id"] == second_id
    # Discarded, the next older edit is offered.
    client.delete(f"/drafts/?node_id={entry.id}&draft_id={second_id}")
    body = client.get(f"/drafts/?node_id={entry.id}").get_json()
    assert body["older_draft"]["id"] == first_id


def test_an_older_edit_with_the_newest_drafts_own_text_is_not_offered(
        client, alice):
    # "Use my edit": the older text saved again as the newest draft.
    entry = _entry(alice)
    _left_edit_then_tick(client, entry)
    client.post("/drafts/", json={
        "content": "- [ ] milk\n- [ ] bread\n- [ ] eggs", "node_id": entry.id})

    body = client.get(f"/drafts/?node_id={entry.id}").get_json()

    assert body["content"] == "- [ ] milk\n- [ ] bread\n- [ ] eggs"
    assert "older_draft" not in body
    assert "older_than_entry" not in body


def test_a_new_entrys_drafts_are_updated_in_place_as_before(client, alice):
    parent = _entry(alice)
    first = client.post("/drafts/", json={"content": "a", "parent_id": parent.id})
    _age_draft(first.get_json()["id"], 60)
    client.put(f"/nodes/{parent.id}", json={"content": "parent edited"})
    again = client.post("/drafts/", json={"content": "ab", "parent_id": parent.id})

    assert again.get_json()["id"] == first.get_json()["id"]
    assert Draft.query.filter_by(parent_id=parent.id).count() == 1


def test_the_edit_forms_save_clears_the_entrys_draft(client, alice):
    # The form's Save: PUT, then DELETE of the context's drafts.
    entry = _entry(alice)
    client.post("/drafts/", json={"content": "the edit", "node_id": entry.id})
    assert client.put(f"/nodes/{entry.id}",
                      json={"content": "the edit"}).status_code == 200
    assert client.delete(f"/drafts/?node_id={entry.id}").status_code == 200

    assert client.get(f"/drafts/?node_id={entry.id}").status_code == 404

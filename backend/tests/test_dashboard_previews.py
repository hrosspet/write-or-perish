"""The owner's dashboard (GET /api/dashboard/) shows a session's card by
the first entry under its system prompt root. That entry is the first one
the owner may see: not soft-deleted, and not another user's entry the
owner cannot open. The same rule as GET /api/dashboard/<username>.

Same harness as test_log_dashboard_privacy (minimal app, sqlite)."""
from datetime import datetime, timedelta

from backend.tests.test_log_dashboard_privacy import (  # noqa: F401 - fixture
    _db, _login, app,
)
from backend.models import Node, User

T0 = datetime(2026, 1, 1, 12, 0, 0)


def _node(user, parent=None, text="", privacy="private", at=None, **kw):
    n = Node(user_id=user.id, human_owner_id=user.id,
             parent_id=parent.id if parent else None, node_type="user",
             privacy_level=privacy, ai_usage="chat", created_at=at or T0,
             **kw)
    n.set_content(text)
    _db.session.add(n)
    _db.session.flush()
    return n


def _world():
    alice = User(username="alice", approved=True)
    bob = User(username="bob", approved=True)
    _db.session.add_all([alice, bob])
    _db.session.flush()
    root = _node(alice, text="SYSTEM PROMPT", privacy="public",
                 prompt_key="voice", pinned_at=T0)
    root.pinned_by = alice.id
    return alice, bob, root


def _cards(flask_app, user_id):
    client = flask_app.test_client()
    _login(client, user_id)
    body = client.get("/api/dashboard/").get_json()
    return body["nodes"] + body["pinned_nodes"]


def test_preview_skips_another_users_entry_the_owner_cannot_open(app):  # noqa: F811
    alice, bob, root = _world()
    _node(bob, root, "BOB PRIVATE WORDS", at=T0 + timedelta(minutes=1))
    _node(alice, root, "alice's own words", at=T0 + timedelta(minutes=2))
    _db.session.commit()

    cards = _cards(app, alice.id)
    assert len(cards) == 2
    for card in cards:
        assert "BOB PRIVATE WORDS" not in card["preview"]
        assert card["preview"] == "alice's own words"


def test_preview_skips_a_deleted_entry(app):  # noqa: F811
    alice, bob, root = _world()
    _node(alice, root, "DELETED WORDS", at=T0 + timedelta(minutes=1),
          deleted_at=T0 + timedelta(days=1))
    _node(alice, root, "alice's own words", at=T0 + timedelta(minutes=2))
    _db.session.commit()

    for card in _cards(app, alice.id):
        assert card["preview"] == "alice's own words"


def test_preview_shows_another_users_public_entry(app):  # noqa: F811
    alice, bob, root = _world()
    _node(bob, root, "bob public words", privacy="public",
          at=T0 + timedelta(minutes=1))
    _db.session.commit()

    for card in _cards(app, alice.id):
        assert card["preview"] == "bob public words"


def test_with_nothing_visible_below_the_card_shows_the_root(app):  # noqa: F811
    alice, bob, root = _world()
    _node(bob, root, "BOB PRIVATE WORDS", at=T0 + timedelta(minutes=1))
    _db.session.commit()

    for card in _cards(app, alice.id):
        assert "BOB PRIVATE WORDS" not in card["preview"]
        assert card["id"] == root.id

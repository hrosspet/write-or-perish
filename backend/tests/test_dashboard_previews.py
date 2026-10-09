"""The signed-in user's GET /api/dashboard/ lists no thread cards (#481):
no client shows them, and each card's preview was a decryption on every
app load. It carries the user and the newest profile version.

Same harness as test_log_dashboard_privacy (minimal app, sqlite)."""
from datetime import datetime, timedelta
from unittest import mock

from backend.tests.test_log_dashboard_privacy import (  # noqa: F401 - fixture
    _db, _login, app,
)
from backend.models import Node, User, UserProfile

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


# What the clients read from `user`: the web app keeps the whole object
# (UserContext), the iPhone app decodes it as CurrentUser.
USER_FIELDS = {
    "id", "username", "description", "accepted_terms_at",
    "terms_up_to_date", "approved", "email", "is_admin", "plan",
    "voice_mode_enabled", "craft_mode", "preferred_model",
    "profile_generation_task_id", "profile_batch_pending",
    "default_privacy_level", "default_ai_usage", "twitter_login",
    "twitter_handle", "pending_email", "pending_email_expired",
    "prefill_consent", "prefilled_handle", "has_own_entries", "timezone",
    "spend_blocked", "share_v1_enabled", "share_v1_available",
    "public_sharing_enabled", "external_content_available",
    "external_content_enabled",
}
# What the Profile page reads from `latest_profile` (web and iPhone).
PROFILE_FIELDS = {
    "id", "content", "generated_by", "tokens_used", "created_at",
    "source_tokens_used", "source_origin_stats", "source_data_cutoff",
    "generation_type", "has_tts", "ai_usage",
}
CARD_KEYS = {"nodes", "pinned_nodes", "has_more", "page", "total_nodes"}


def _owner_with_threads_and_profile():
    """Alice: a pinned voice session with her entry under it, a plain
    top-level entry, and a profile."""
    alice, bob, root = _world()
    _node(alice, root, "alice's own words", at=T0 + timedelta(minutes=1))
    _node(alice, text="a plain entry", at=T0 + timedelta(hours=1))
    profile = UserProfile(user_id=alice.id, generated_by="user",
                          tokens_used=0)
    profile.set_content("PROFILE TEXT")
    _db.session.add(profile)
    _db.session.commit()
    return alice


def _own_dashboard(flask_app, user):
    client = flask_app.test_client()
    _login(client, user.id)
    resp = client.get("/api/dashboard/")
    assert resp.status_code == 200
    return resp.get_json()


def _counting_decrypt():
    """decrypt_content where Node and UserProfile look it up, wrapped in a
    mock that counts the calls."""
    namespace = Node.get_content.__globals__
    decrypt = mock.Mock(side_effect=namespace["decrypt_content"])
    return decrypt, mock.patch.dict(namespace, {"decrypt_content": decrypt})


def test_own_dashboard_decrypts_no_entry(app):  # noqa: F811
    alice = _owner_with_threads_and_profile()
    decrypt, patched = _counting_decrypt()
    with patched, mock.patch.object(
            Node, "get_content", autospec=True,
            side_effect=Node.get_content) as node_content:
        body = _own_dashboard(app, alice)

    assert node_content.call_count == 0
    # The one decryption left is the profile the Profile page shows.
    assert decrypt.call_count == 1
    assert body["latest_profile"]["content"] == "PROFILE TEXT"


def test_own_dashboard_without_a_profile_decrypts_nothing(app):  # noqa: F811
    alice, bob, root = _world()
    _node(alice, root, "alice's own words", at=T0 + timedelta(minutes=1))
    _db.session.commit()
    decrypt, patched = _counting_decrypt()
    with patched:
        body = _own_dashboard(app, alice)

    assert decrypt.call_count == 0
    assert body["latest_profile"] is None


def test_own_dashboard_lists_no_cards(app):  # noqa: F811
    alice = _owner_with_threads_and_profile()
    body = _own_dashboard(app, alice)
    assert not CARD_KEYS & set(body)


def test_own_dashboard_keeps_what_the_clients_read(app):  # noqa: F811
    alice = _owner_with_threads_and_profile()
    body = _own_dashboard(app, alice)
    assert USER_FIELDS <= set(body["user"])
    assert body["user"]["id"] == alice.id
    assert body["user"]["username"] == "alice"
    assert PROFILE_FIELDS <= set(body["latest_profile"])
    assert body["latest_profile"]["content"] == "PROFILE TEXT"

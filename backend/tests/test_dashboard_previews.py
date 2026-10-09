"""The signed-in user's GET /api/dashboard/ (#481): no thread cards, and
an app load decrypts nothing.

It lists no cards: no client shows them, and each card's preview was a
decryption on every app load. The app-load calls (web UserContext, iPhone
AppState) read only `user` and send ?profile=0, which leaves out the
profile and its decryption; the Profile page (and older iPhone builds)
call without it and get `latest_profile`.

Same harness as test_log_dashboard_privacy (minimal app, sqlite)."""
import sys
from contextlib import ExitStack
from datetime import datetime, timedelta
from unittest import mock

from backend.tests.test_log_dashboard_privacy import (  # noqa: F401 - fixture
    _db, _login, app,
)
from backend.models import Node, User, UserProfile

T0 = datetime(2026, 1, 1, 12, 0, 0)
APP_LOAD = "/api/dashboard/?profile=0"
PROFILE_PAGE = "/api/dashboard/"


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


def _get(flask_app, user, url):
    client = flask_app.test_client()
    _login(client, user.id)
    resp = client.get(url)
    assert resp.status_code == 200
    return resp.get_json()


def _decryptions(flask_app, user, url):
    """GET *url* as *user*, counting content decryptions: decrypt_content
    is wrapped in a counting mock in every module that holds it (the models
    and anything else that imported it), and Node.get_content is counted
    on its own. Returns (body, decryptions, entry reads)."""
    real = Node.get_content.__globals__["decrypt_content"]
    decrypt = mock.Mock(side_effect=real)
    holders = [m for m in list(sys.modules.values())
               if getattr(m, "decrypt_content", None) is real]
    with ExitStack() as stack:
        for module in holders:
            stack.enter_context(
                mock.patch.object(module, "decrypt_content", decrypt))
        node_content = stack.enter_context(mock.patch.object(
            Node, "get_content", autospec=True,
            side_effect=Node.get_content))
        body = _get(flask_app, user, url)
    return body, decrypt.call_count, node_content.call_count


def test_app_load_decrypts_nothing(app):  # noqa: F811
    alice = _owner_with_threads_and_profile()
    body, decryptions, entries = _decryptions(app, alice, APP_LOAD)

    assert decryptions == 0
    assert entries == 0
    assert "latest_profile" not in body
    assert USER_FIELDS <= set(body["user"])
    assert body["user"]["username"] == "alice"


def test_app_load_keeps_the_flag_through_the_slash_redirect(app):  # noqa: F811
    # The web app calls /api/dashboard (no slash) and follows the 308.
    alice = _owner_with_threads_and_profile()
    client = app.test_client()
    _login(client, alice.id)
    resp = client.get("/api/dashboard?profile=0", follow_redirects=True)
    assert resp.status_code == 200
    assert "latest_profile" not in resp.get_json()


def test_profile_page_request_decrypts_only_the_profile(app):  # noqa: F811
    alice = _owner_with_threads_and_profile()
    body, decryptions, entries = _decryptions(app, alice, PROFILE_PAGE)

    assert entries == 0
    assert decryptions == 1
    assert body["latest_profile"]["content"] == "PROFILE TEXT"


def test_without_a_profile_nothing_is_decrypted(app):  # noqa: F811
    alice, bob, root = _world()
    _node(alice, root, "alice's own words", at=T0 + timedelta(minutes=1))
    _db.session.commit()
    body, decryptions, entries = _decryptions(app, alice, PROFILE_PAGE)

    assert decryptions == 0
    assert body["latest_profile"] is None


def test_no_cards_on_either_request(app):  # noqa: F811
    alice = _owner_with_threads_and_profile()
    for url in (APP_LOAD, PROFILE_PAGE):
        assert not CARD_KEYS & set(_get(app, alice, url)), url


def test_profile_page_request_keeps_what_the_clients_read(app):  # noqa: F811
    alice = _owner_with_threads_and_profile()
    body = _get(app, alice, PROFILE_PAGE)
    assert USER_FIELDS <= set(body["user"])
    assert body["user"]["id"] == alice.id
    assert body["user"]["username"] == "alice"
    assert PROFILE_FIELDS <= set(body["latest_profile"])
    assert body["latest_profile"]["content"] == "PROFILE TEXT"

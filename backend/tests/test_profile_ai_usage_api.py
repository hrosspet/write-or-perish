"""#346: a profile's ai_usage comes only from the account setting when
its text is written. The profile API does not accept it on create or
edit (no UI sends it). An edit made while the account is set to 'none'
is saved as a new version marked 'none'."""
from backend.tests.test_tts_invalidation import (  # noqa: F401
    app, alice, _login, _db,
)
from backend.models import UserProfile


def test_create_takes_ai_usage_from_the_account(app, alice):  # noqa: F811
    client = app.test_client()
    _login(client, alice)

    resp = client.post("/profile/", json={"content": "about me",
                                          "ai_usage": "none"})
    assert resp.status_code in (200, 201)
    assert UserProfile.query.one().ai_usage == "chat"

    alice.default_ai_usage = "none"
    _db.session.commit()
    resp = client.post("/profile/", json={"content": "about me, later",
                                          "ai_usage": "chat"})
    assert resp.status_code in (200, 201)
    latest = UserProfile.query.order_by(UserProfile.id.desc()).first()
    assert latest.ai_usage == "none"


def test_edit_ignores_ai_usage(app, alice):  # noqa: F811
    profile = UserProfile(user_id=alice.id, generated_by="user",
                          tokens_used=0, ai_usage="chat")
    profile.set_content("original")
    _db.session.add(profile)
    _db.session.commit()
    client = app.test_client()
    _login(client, alice)

    resp = client.put(f"/profile/{profile.id}",
                      json={"content": "edited", "ai_usage": "none"})
    assert resp.status_code == 200
    refreshed = UserProfile.query.get(profile.id)
    assert refreshed.get_content() == "edited"
    assert refreshed.ai_usage == "chat"


def _profile(user, ai_usage="chat", content="generated text"):
    profile = UserProfile(user_id=user.id, generated_by="m", tokens_used=0,
                          ai_usage=ai_usage, source_tokens_used=5000)
    profile.set_content(content)
    _db.session.add(profile)
    _db.session.commit()
    return profile


def test_edit_while_account_none_saves_a_none_version(app, alice):  # noqa: F811
    profile = _profile(alice)
    alice.default_ai_usage = "none"
    _db.session.commit()
    client = app.test_client()
    _login(client, alice)

    resp = client.put(f"/profile/{profile.id}",
                      json={"content": "written while none"})
    assert resp.status_code == 200
    new_id = resp.get_json()["profile"]["id"]
    assert new_id != profile.id

    old = UserProfile.query.get(profile.id)
    assert old.get_content() == "generated text" and old.ai_usage == "chat"
    new = UserProfile.query.get(new_id)
    assert new.get_content() == "written while none"
    assert new.ai_usage == "none"
    assert new.parent_profile_id == profile.id
    assert new.source_tokens_used == 5000


def test_edit_in_place_when_no_text_moves_into_a_readable_row(app, alice):  # noqa: F811
    """Account 'chat', or a row that is already 'none': edited in place."""
    readable = _profile(alice)
    client = app.test_client()
    _login(client, alice)
    resp = client.put(f"/profile/{readable.id}", json={"content": "v2"})
    assert resp.get_json()["profile"]["id"] == readable.id

    hidden = _profile(alice, ai_usage="none")
    alice.default_ai_usage = "none"
    _db.session.commit()
    resp = client.put(f"/profile/{hidden.id}", json={"content": "v3"})
    assert resp.get_json()["profile"]["id"] == hidden.id
    assert UserProfile.query.count() == 2

"""#346: a profile's ai_usage comes only from the account setting at
creation. The profile API does not accept it on create or edit (no UI
sends it), so a profile can only end up 'none' by being created while
the account is set to 'none'."""
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

"""The speech routes' access check after the payments train's merge
(PR #680, "Conflicts and how each was resolved": backend/routes/voice.py).

#675 (the fixes train) had ``_announcement_access_refusal`` check Voice
mode only; #649 added, while payments are on, the paid feature set (402).
The merge kept #649's body, so #675's new refusal-clip route (GET
/api/voice/refusal/<code>.mp3) gets the payments check too, like the
approval-announcement routes. Payments on throughout, except the last
control.

Today Voice mode is the paid set for every account that is not an admin
(User.has_voice_mode is on_paid_plan; ai_replies_allowed is on_paid_plan
while payments are on), so a real account never reaches the 402: it gets
the 403 first. The 402 tests switch Voice mode on by hand to reach the
second check, the line the merge kept:
``return refuse_without_paid_features(current_user)``.
"""
import pytest

from backend.extensions import db as _db
from backend.models import Node, User
from backend.tests.test_retrieval_loop import _llm_task_mod
from backend.tests.test_tool_approval_routes import _client
from backend.tests.test_voice_approval_routes import (  # noqa: F401
    _make_app, clips)
from backend.utils.announcement_audio import ANNOUNCEMENT_TEXTS
from backend.utils.voice_refusals import SPEND_CAP_CODE, SPOKEN_REFUSALS


@pytest.fixture
def app(monkeypatch, tmp_path):
    import sys
    monkeypatch.setitem(sys.modules, "backend.tasks.llm_completion",
                        _llm_task_mod)
    app = _make_app()
    app.config["PAYMENTS_ENABLED"] = True
    # The app's gate (backend/__init__.py), as in production.
    from backend.utils.subscriptions import (
        block_accounts_without_subscription)
    app.before_request(block_accounts_without_subscription)
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()


def _paid(name="paula"):
    u = User(username=name, approved=True, plan="free",
             subscription_status="active", subscription_level="standard",
             subscription_ever_paid=True)
    _db.session.add(u)
    _db.session.commit()
    return u


def _lapsed(name="lou"):
    """Had a subscription that ended: let in (reading, writing, export),
    without the paid set, so without Voice mode."""
    u = User(username=name, approved=True, plan="free",
             subscription_status="ended", subscription_level="standard",
             subscription_ever_paid=True)
    _db.session.add(u)
    _db.session.commit()
    return u


def _reply(user):
    """A reply of *user*'s with no waiting change (the announcement's
    count is 0)."""
    bot = User(username="gpt-5", twitter_id="llm-gpt-5")
    _db.session.add(bot)
    _db.session.flush()
    node = Node(user_id=bot.id, human_owner_id=user.id, node_type="llm",
                llm_model="gpt-5", llm_task_status="completed",
                privacy_level="private", ai_usage="chat")
    node.set_content("a reply")
    _db.session.add(node)
    _db.session.commit()
    return node


def _routes(node_id):
    return [f"/api/voice/refusal/{SPEND_CAP_CODE}.mp3",
            f"/api/voice/approval-announcement?node={node_id}",
            "/api/voice/approval-announcement/one.mp3"]


def test_a_paid_account_gets_every_clip(app, clips):  # noqa: F811
    """Control: the paid set and Voice mode, nothing refused."""
    u = _paid()
    node = _reply(u)
    client = _client(app, u)
    refusal, info, announcement = (client.get(p) for p in _routes(node.id))
    assert refusal.status_code == 200
    assert refusal.mimetype == "audio/mpeg"
    assert info.status_code == 200 and info.get_json()["count"] == 0
    assert announcement.status_code == 200
    assert clips["calls"] == [SPOKEN_REFUSALS[SPEND_CAP_CODE],
                              ANNOUNCEMENT_TEXTS["one"]]


def test_no_voice_mode_is_a_403_on_every_route(app, clips):  # noqa: F811
    """Control (both versions check Voice mode first): an account whose
    subscription ended has no Voice mode."""
    u = _lapsed()
    node = _reply(u)
    client = _client(app, u)
    for path in _routes(node.id):
        r = client.get(path)
        assert r.status_code == 403, (path, r.get_data(as_text=True))
        assert r.get_json()["error"] == \
            "Voice mode not enabled for this account"
    assert clips["calls"] == []


def test_without_the_paid_set_a_402_on_every_route(
        app, clips, monkeypatch):  # noqa: F811
    """Guards the merged line ``return refuse_without_paid_features(
    current_user)``: Voice mode switched on by hand for an account without
    the paid set. Merged the other way (#675's Voice-mode check only), the
    refusal clip and the announcements are made, and billed, for it."""
    monkeypatch.setattr(User, "has_voice_mode", property(lambda self: True))
    u = _lapsed()
    node = _reply(u)
    client = _client(app, u)
    for path in _routes(node.id):
        r = client.get(path)
        assert r.status_code == 402, (path, r.get_data(as_text=True))
        assert r.get_json()["error"] == "subscription_required"
    assert clips["calls"] == []
    assert clips["costs"] == []


def test_an_account_never_let_in_is_stopped_by_the_gate(
        app, clips):  # noqa: F811
    """Control: never subscribed, so not let in: the app's gate answers
    402 no_subscription before the route runs (#649)."""
    u = User(username="nell", approved=True, plan="free")
    _db.session.add(u)
    _db.session.commit()
    client = _client(app, u)
    for path in _routes(1):
        r = client.get(path)
        assert r.status_code == 402
        assert r.get_json()["error"] == "no_subscription"
    assert clips["calls"] == []


def test_payments_off_no_payments_check(app, clips, monkeypatch):  # noqa: F811
    """Control: with payments off only Voice mode counts (#675's rule)."""
    app.config["PAYMENTS_ENABLED"] = False
    monkeypatch.setattr(User, "has_voice_mode", property(lambda self: True))
    u = _lapsed()
    r = _client(app, u).get(f"/api/voice/refusal/{SPEND_CAP_CODE}.mp3")
    assert r.status_code == 200

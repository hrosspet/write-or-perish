"""Calls to the paid X API that an account starts are bounded per day
(utils/billing_limits):

- POST /api/external/twitter/sync ("Sync now" for X bookmarks) queues at
  most X_BOOKMARK_SYNCS_PER_DAY syncs a day; past them it answers 429
  ``too_many_syncs`` with a message the web and iPhone apps show. The
  nightly sync is not counted.
- POST /api/referrals/invite for an X handle looks the handle up at most
  X_INVITE_LOOKUPS_PER_DAY times a day, unknown handles included; past
  them it answers 429 before any lookup. Email invites are not counted.

Requests of one account are counted under its row lock, one after the
other (test_billing_limits_postgres.py shows that lock on PostgreSQL).
"""
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from backend.tests.test_payments_gate import (  # noqa: F401
    real_app, _user, _client, _db)
from backend.tests.test_payments import _client as _pay_client
from backend.tests.test_invites import x_lookups  # noqa: F401
from backend.tests.test_referrals import app, gateway  # noqa: F401
from backend.models import BillingRequest, Invite, User
from backend.utils import billing_limits

SYNCS = billing_limits.X_BOOKMARK_SYNCS_PER_DAY
LOOKUPS = billing_limits.X_INVITE_LOOKUPS_PER_DAY


# ── "Sync now" for X bookmarks ───────────────────────────────────────────

def _connect_x(user):
    from backend.models import ExternalAccount
    account = ExternalAccount(user_id=user.id, provider="twitter",
                              external_user_id="42", handle="x")
    account.set_tokens("access-token", "refresh-token")
    _db.session.add(account)
    _db.session.commit()


def _stub_delay(monkeypatch):
    import backend.tasks.external_sync as sync_mod
    delay = MagicMock()
    delay.return_value.id = "task-id"
    monkeypatch.setattr(sync_mod.sync_twitter_bookmarks, "delay", delay,
                        raising=False)
    return delay


def _member(name):
    u = _user(name, subscription_status="active",
              subscription_level="standard", subscription_ever_paid=True)
    _connect_x(u)
    return u


def test_syncs_by_hand_stop_at_the_days_limit(real_app, monkeypatch):  # noqa: F811
    u = _member("member")
    delay = _stub_delay(monkeypatch)
    c = _client(real_app, u)

    answers = [c.post("/api/external/twitter/sync") for _ in range(SYNCS + 3)]

    assert [a.status_code for a in answers] == [202] * SYNCS + [429] * 3
    assert answers[-1].get_json() == {
        "error": billing_limits.TOO_MANY_X_SYNCS_MESSAGE,
        "code": "too_many_syncs"}
    assert delay.call_count == SYNCS


def test_a_day_later_syncs_by_hand_work_again(real_app, monkeypatch):  # noqa: F811
    u = _member("member")
    delay = _stub_delay(monkeypatch)
    c = _client(real_app, u)
    for _ in range(SYNCS):
        assert c.post("/api/external/twitter/sync").status_code == 202
    assert c.post("/api/external/twitter/sync").status_code == 429

    BillingRequest.query.filter_by(user_id=u.id).update(
        {BillingRequest.created_at: datetime.utcnow() - timedelta(days=1,
                                                                  minutes=1)})
    _db.session.commit()

    assert c.post("/api/external/twitter/sync").status_code == 202
    assert delay.call_count == SYNCS + 1


def test_each_account_has_its_own_count(real_app, monkeypatch):  # noqa: F811
    first, second = _member("first"), _member("second")
    _stub_delay(monkeypatch)
    c = _client(real_app, first)
    for _ in range(SYNCS):
        c.post("/api/external/twitter/sync")
    assert c.post("/api/external/twitter/sync").status_code == 429
    assert _client(real_app, second).post(
        "/api/external/twitter/sync").status_code == 202


# ── Invites for an X handle ──────────────────────────────────────────────

@pytest.fixture
def no_broker(monkeypatch):
    """The cost row's cap check is queued to Celery: not from a test.
    (Another test file may have loaded the task as a plain function.)"""
    from backend.tasks import spend_monitor
    monkeypatch.setattr(spend_monitor.enforce_user_cap, "apply_async",
                        MagicMock(), raising=False)


def _inviter(name="mal"):
    u = User(username=name, approved=True, plan="free",
             email=f"{name}@example.com",
             subscription_level="standard", subscription_status="ended",
             subscription_cancelled_by_owner=True,
             subscription_trial_used=True)
    _db.session.add(u)
    _db.session.commit()
    return u


def test_invites_made_and_withdrawn_stop_at_the_days_lookups(
        app, x_lookups, no_broker):  # noqa: F811
    c = _pay_client(app, _inviter())
    codes = []
    for _ in range(LOOKUPS + 5):
        made = c.post("/api/referrals/invite", json={"to": "@ben_x"})
        codes.append(made.status_code)
        if made.status_code == 201:
            assert c.delete("/api/referrals/invite").status_code == 200

    assert codes == [201] * LOOKUPS + [429] * 5
    assert len(x_lookups.x_reads) == LOOKUPS
    refused = c.post("/api/referrals/invite", json={"to": "@ben_x"})
    assert refused.get_json() == {
        "error": billing_limits.TOO_MANY_X_LOOKUPS_MESSAGE}


def test_unknown_handles_count_too(app, x_lookups, no_broker):  # noqa: F811
    c = _pay_client(app, _inviter())
    codes = [c.post("/api/referrals/invite",
                    json={"to": f"@nobody_{i}"}).status_code
             for i in range(LOOKUPS + 5)]

    assert codes == [400] * LOOKUPS + [429] * 5
    assert len(x_lookups.x_reads) == LOOKUPS
    assert Invite.query.count() == 0


def test_an_email_invite_is_not_counted(app, x_lookups, no_broker):  # noqa: F811
    user = _inviter()
    c = _pay_client(app, user)
    for i in range(LOOKUPS):
        assert c.post("/api/referrals/invite",
                      json={"to": f"@nobody_{i}"}).status_code == 400
    assert c.post("/api/referrals/invite",
                  json={"to": "friend@example.com"}).status_code == 201
    assert len(x_lookups.x_reads) == LOOKUPS

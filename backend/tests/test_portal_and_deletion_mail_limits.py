"""Hourly limits on the billing page and on the deletion email.

- POST /api/billing/portal (Stripe's billing page): BILLING_PORTALS_PER_HOUR
  an account an hour (utils/billing_limits, "portal"); past it 429 with
  its own message and no call to Stripe; an hour later it opens again;
  an account the admin blocked still opens it within the limit.
- POST /api/account/delete with an email address: the confirmation email
  is held to send_limits' account and client limits (5 an hour, 10 a
  day); past them 429 with the wait message and nothing sent, and the
  link sent before keeps working. A failed send gives its count back.
  An account without an email is scheduled at once, as before.
"""
from datetime import datetime, timedelta

import pytest

from backend.extensions import db as _db
from backend.models import BillingRequest
from backend.tests import test_payments as tp
from backend.utils import billing_limits, send_limits, stripe_billing

# The account-deletion tests' fixtures (their `app` is the deletion test
# app; the payments app here is `pay_app`).
from backend.tests.test_account_deletion import (  # noqa: F401
    app, mail, stubs, world)
from backend.tests.test_user_purge import _client as _purge_client


@pytest.fixture
def gateway(monkeypatch):
    gw = tp.FakeGateway()
    monkeypatch.setattr(stripe_billing, "get_gateway", lambda *a, **k: gw)
    return gw


@pytest.fixture
def pay_app(gateway):
    app, ctx = tp._app_fixture(True)
    yield app
    _db.session.remove()
    _db.drop_all()
    ctx.pop()


def _portal(c):
    return c.post("/api/billing/portal")


def test_billing_page_opens_are_limited_an_hour(pay_app, gateway):
    user = tp._user("ann", stripe_customer_id="cus_ann")
    c = tp._client(pay_app, user)
    most = billing_limits.limit_for("portal", pay_app.config)
    assert most == 20
    for _ in range(most):
        assert _portal(c).status_code == 200
    r = _portal(c)
    assert r.status_code == 429
    assert r.get_json()["error"] == "too_many_requests"
    assert r.get_json()["message"] == billing_limits.TOO_MANY_PORTALS_MESSAGE
    assert gateway.names().count("create_portal_session") == most
    # An hour later it opens again.
    for row in BillingRequest.query.all():
        row.created_at -= timedelta(hours=1, minutes=1)
    _db.session.commit()
    assert _portal(c).status_code == 200


def test_a_blocked_account_still_opens_the_billing_page(pay_app, gateway):
    user = tp._user("bea", stripe_customer_id="cus_bea")
    user.approved = False
    user.deactivated_at = datetime.utcnow()
    _db.session.commit()
    c = tp._client(pay_app, user)
    assert _portal(c).status_code == 200
    assert BillingRequest.query.filter_by(kind="portal").count() == 1


def test_no_customer_is_answered_before_counting(pay_app, gateway):
    c = tp._client(pay_app, tp._user("cal"))
    assert _portal(c).status_code == 409
    assert BillingRequest.query.count() == 0


def _ask(c):
    return c.post("/api/account/delete", json={"confirm": "alice"})


def test_deletion_emails_are_limited(app, world, stubs, mail):
    c = _purge_client(app, world.alice)
    before = len(mail)
    most = send_limits.ACCOUNT_LIMITS[0][1]
    codes = [_ask(c).status_code for _ in range(most)]
    assert codes == [202] * most
    sent = len(mail) - before
    assert sent == most
    link_hash = world.alice.account_deletion_token_hash
    r = _ask(c)
    assert r.status_code == 429
    assert r.get_json()["error"] == send_limits.WAIT_MESSAGE
    assert len(mail) - before == most            # nothing more sent
    # The link sent last still works.
    assert world.alice.account_deletion_token_hash == link_hash


def test_a_failed_send_gives_its_count_back(app, world, stubs, mail,
                                            monkeypatch):
    from backend.utils import email as email_mod
    c = _purge_client(app, world.alice)

    def fail(*a, **k):
        raise RuntimeError("relay down")

    real = email_mod.send_account_deletion_link_email
    monkeypatch.setattr(email_mod, "send_account_deletion_link_email", fail)
    most = send_limits.ACCOUNT_LIMITS[0][1]
    for _ in range(most + 2):
        assert _ask(c).status_code == 502
    monkeypatch.setattr(email_mod, "send_account_deletion_link_email", real)
    assert _ask(c).status_code == 202

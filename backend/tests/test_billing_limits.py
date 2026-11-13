"""Limits on starting a checkout and on the sync (#697).

With nobody approving accounts, any new account reaches the billing
routes, and each checkout started and each sync calls Stripe. An account
may start CHECKOUT_STARTS_PER_HOUR checkouts and make
BILLING_SYNCS_PER_HOUR syncs an hour; going back to the same level while
its session is still open gets that session again.

The payments test app (test_payments.py), Stripe replaced by its stub.
"""
from datetime import datetime, timedelta

import pytest

from backend.extensions import db as _db
from backend.models import BillingRequest, CheckoutSession
from backend.tests import test_payments as tp
from backend.tests.test_payments import _client, _user
from backend.utils import stripe_billing


class SessionGateway(tp.FakeGateway):
    """The stub, with a session's address as Stripe gives it back."""

    def retrieve_checkout_session(self, sid):
        session = super().retrieve_checkout_session(sid)
        if session.get("status") == "open":
            return {**session, "url": f"https://checkout.example/{sid}"}
        return session


@pytest.fixture
def gateway(monkeypatch):
    gw = SessionGateway()
    monkeypatch.setattr(stripe_billing, "get_gateway", lambda *a, **k: gw)
    stripe_billing._price_cache.clear()
    for level in ("standard", "supporter", "discounted"):
        gw.prices.append({
            "id": f"price_{level}", "active": True,
            "lookup_key": stripe_billing.PRICE_LOOKUP_KEYS[level]})
    return gw


@pytest.fixture
def app(gateway):
    app, ctx = tp._app_fixture(True)
    app.config["CHECKOUT_STARTS_PER_HOUR"] = 3
    app.config["BILLING_SYNCS_PER_HOUR"] = 2
    yield app
    _db.session.remove()
    _db.drop_all()
    ctx.pop()


def _checkout(c, level="standard"):
    return c.post("/api/billing/checkout", json={"level": level})


def test_the_same_level_again_gets_its_open_session(app, gateway):
    c = _client(app, _user())
    first = _checkout(c).get_json()["url"]
    again = _checkout(c).get_json()["url"]
    assert again == first
    assert gateway.names().count("create_checkout_session") == 1
    assert "expire_checkout_session" not in gateway.names()
    assert CheckoutSession.query.count() == 1


def test_another_level_gets_a_new_one_and_the_old_expires(app, gateway):
    c = _client(app, _user())
    first = _checkout(c, "standard").get_json()["url"]
    other = _checkout(c, "supporter").get_json()["url"]
    assert other != first
    assert gateway.names().count("create_checkout_session") == 2
    assert gateway.names().count("expire_checkout_session") == 1
    states = sorted(r.state for r in CheckoutSession.query.all())
    assert states == ["expired", "open"]


def test_an_old_or_closed_session_is_not_given_again(app, gateway):
    c = _client(app, _user())
    _checkout(c)
    row = CheckoutSession.query.one()
    row.created_at = datetime.utcnow() - timedelta(hours=2)
    _db.session.commit()
    _checkout(c)
    assert gateway.names().count("create_checkout_session") == 2
    # Closed at Stripe meanwhile (expired there): a new one.
    row = CheckoutSession.query.filter_by(state="open").one()
    gateway.sessions[row.id]["status"] = "expired"
    _checkout(c)
    assert gateway.names().count("create_checkout_session") == 3


def test_checkouts_an_hour_are_limited(app, gateway):
    c = _client(app, _user())
    for level in ("standard", "supporter", "standard"):
        assert _checkout(c, level).status_code == 200
    r = _checkout(c, "supporter")
    assert r.status_code == 429
    assert r.get_json()["error"] == "too_many_requests"
    assert "try again in an hour" in r.get_json()["message"]
    assert gateway.names().count("create_checkout_session") == 3
    # An hour later it may again.
    for row in BillingRequest.query.all():
        row.created_at -= timedelta(hours=1, minutes=1)
    _db.session.commit()
    assert _checkout(c, "supporter").status_code == 200


def test_the_limit_is_per_account(app, gateway):
    a = _client(app, _user("ann"))
    for level in ("standard", "supporter", "standard"):
        _checkout(a, level)
    assert _checkout(_client(app, _user("ben")), "standard").status_code \
        == 200


def test_a_failed_checkout_counts_too(app, gateway):
    c = _client(app, _user())
    gateway.fail = True
    for _ in range(3):
        assert _checkout(c).status_code == 503
    gateway.fail = False
    assert _checkout(c).status_code == 429


def test_syncs_an_hour_are_limited(app, gateway):
    c = _client(app, _user())
    for _ in range(2):
        assert c.post("/api/billing/sync", json={}).status_code == 200
    r = c.post("/api/billing/sync", json={})
    assert r.status_code == 429
    # The page falls back to reading the state, which calls nobody.
    assert c.get("/api/billing").status_code == 200


def test_requests_arriving_together_are_counted_one_after_the_other(
        app, monkeypatch):
    """Two requests of one account at the same moment: the count and the
    new row are one step under the account's row lock, so the second is
    counted after the first's row. Simulated here: while this request
    waits for the lock, the other one (holding it) adds its row and
    commits. sqlite ignores the lock itself; test_billing_limits_postgres
    runs real threads against Postgres."""
    from backend.utils import billing_limits
    u = _user()
    for _ in range(2):   # the limit is 3 here
        _db.session.add(BillingRequest(user_id=u.id, kind="checkout"))
    _db.session.commit()
    real_lock = billing_limits._lock_account
    order = []

    def other_request_first(user):
        order.append("lock asked")
        _db.session.add(BillingRequest(user_id=user.id, kind="checkout"))
        _db.session.commit()   # the other request's row, then its lock ends
        real_lock(user)
        order.append("lock held")
    monkeypatch.setattr(billing_limits, "_lock_account", other_request_first)

    with pytest.raises(billing_limits.TooManyBillingRequests):
        billing_limits.check_and_count(u, "checkout")
    assert order == ["lock asked", "lock held"]
    assert BillingRequest.query.filter_by(user_id=u.id).count() == 3


def test_the_counts_go_with_the_account(app, gateway):
    from backend.utils.account_deletion import references_to_user
    u = _user()
    _checkout(_client(app, u))
    assert references_to_user(u.id).get("billing_request.user_id") == 1

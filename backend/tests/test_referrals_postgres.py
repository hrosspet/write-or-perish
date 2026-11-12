"""Referrals (#439) on Postgres: the friend's two payment events of one
invoice (invoice.paid and invoice.payment_succeeded) delivered at the same
moment give one free week to each side, not two.

sqlite ignores SELECT ... FOR UPDATE, so this runs only against a
throwaway Postgres database named by LOORE_TEST_POSTGRES_URL (it creates
and drops every table in it); skipped otherwise.
"""
import os
import threading
import time

import pytest

PG_URL = os.environ.get("LOORE_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not PG_URL, reason="needs LOORE_TEST_POSTGRES_URL (a throwaway database)")

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ENCRYPTION_DISABLED", "true")

from backend.extensions import db as _db  # noqa: E402
from backend.models import Referral, User, UserNotification  # noqa: E402
from backend.tests import test_payments as tp  # noqa: E402
from backend.tests import test_referrals as tr  # noqa: E402
from backend.utils import stripe_billing  # noqa: E402


class SlowGateway(tr.ReferralGateway):
    """Reading a subscription and making a credit take a moment, as calls
    to Stripe do, so the two deliveries overlap."""

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()

    def _call(self, name, *args):
        with self._lock:
            super()._call(name, *args)

    def retrieve_subscription(self, sub_id):
        time.sleep(0.3)
        return super().retrieve_subscription(sub_id)

    def create_balance_transaction(self, customer_id, params,
                                   idempotency_key=None):
        time.sleep(0.3)
        # Without the idempotency key: only Loore's own lock may keep the
        # second delivery from making a second credit.
        return super().create_balance_transaction(customer_id, params, None)


@pytest.fixture
def pg_app(monkeypatch):
    gw = SlowGateway()
    monkeypatch.setattr(stripe_billing, "get_gateway", lambda *a, **k: gw)
    app = tp._make_app(payments=True, db_uri=PG_URL)
    app.config["REFERRALS_ENABLED"] = True
    with app.app_context():
        _db.drop_all()
        _db.create_all()
    yield app, gw
    with app.app_context():
        _db.session.remove()
        _db.drop_all()


def test_two_payment_events_at_once_give_one_week_each(pg_app):
    app, gw = pg_app
    with app.app_context():
        inviter = User(username="ines", approved=True, plan="free",
                       stripe_customer_id="cus_i",
                       stripe_subscription_id="sub_i",
                       subscription_status="active",
                       subscription_level="standard")
        friend = User(username="fred", approved=True, plan="free",
                      stripe_customer_id="cus_f",
                      stripe_subscription_id="sub_f",
                      subscription_status="trialing",
                      subscription_level="standard")
        _db.session.add_all([inviter, friend])
        _db.session.commit()
        _db.session.add(Referral(inviter_id=inviter.id, friend_id=friend.id))
        _db.session.commit()
    invoice = tr._paid(gw)
    results = []

    def deliver(etype, event_id):
        with app.app_context():
            r = tp._post_event(app, tp._event(etype, invoice,
                                              event_id=event_id))
            results.append(r.status_code)
            _db.session.remove()

    threads = [threading.Thread(target=deliver, args=args) for args in (
        ("invoice.paid", "evt_a"), ("invoice.payment_succeeded", "evt_b"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [200, 200]
    customers = sorted(t["customer"] for t in gw.credits())
    assert customers == ["cus_f", "cus_i"]
    # One notice each: the second delivery never got past the reward.
    with app.app_context():
        notes = sorted(n.user_id for n in UserNotification.query.all())
        assert len(notes) == 2 and len(set(notes)) == 2

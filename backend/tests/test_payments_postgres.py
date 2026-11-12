"""Payments (#438) on Postgres: two subscriptions of one account whose
events arrive at the same moment end with one kept and one cancelled.

sqlite ignores SELECT ... FOR UPDATE, so this runs only against a
throwaway Postgres database named by LOORE_TEST_POSTGRES_URL (it creates
and drops every table in it); skipped otherwise. Two threads post the
first event of each subscription while the stub gateway holds both inside
settle_subscription long enough for them to overlap.
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
from backend.models import User  # noqa: E402
from backend.tests import test_payments as tp  # noqa: E402
from backend.utils import stripe_billing  # noqa: E402


class SlowGateway(tp.FakeGateway):
    """Reading a subscription takes a moment, as a call to Stripe does,
    so the two webhook requests are inside settle_subscription together."""

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()

    def _call(self, name, *args):
        with self._lock:
            super()._call(name, *args)

    def retrieve_subscription(self, sub_id):
        time.sleep(0.4)
        return super().retrieve_subscription(sub_id)


@pytest.fixture
def pg_app(monkeypatch):
    gw = SlowGateway()
    monkeypatch.setattr(stripe_billing, "get_gateway", lambda *a, **k: gw)
    app = tp._make_app(payments=True, db_uri=PG_URL)
    with app.app_context():
        _db.drop_all()
        _db.create_all()
    yield app, gw
    with app.app_context():
        _db.session.remove()
        _db.drop_all()


def test_two_subscriptions_arriving_together(pg_app):
    app, gw = pg_app
    with app.app_context():
        u = User(username="alice", approved=True, stripe_customer_id="cus_1")
        _db.session.add(u)
        _db.session.commit()
        uid = u.id
    gw.subscriptions["sub_old"] = tp.subscription(
        "sub_old", created=tp.NOW - 60)
    gw.subscriptions["sub_new"] = tp.subscription(
        "sub_new", created=tp.NOW, latest_invoice="in_new")
    gw.invoices["in_new"] = {
        "id": "in_new", "amount_paid": 3000,
        "payments": {"data": [{"payment": {"payment_intent": "pi_new"}}]}}

    codes = []

    def deliver(sub_id, event_id):
        with app.app_context():
            r = tp._post_event(app, tp._event(
                "customer.subscription.created", {"id": sub_id}, event_id))
            codes.append(r.status_code)

    threads = [threading.Thread(target=deliver, args=("sub_old", "evt_a")),
               threading.Thread(target=deliver, args=("sub_new", "evt_b"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert codes == [200, 200]
    with app.app_context():
        user = _db.session.get(User, uid)
        assert user.stripe_subscription_id == "sub_old"
    assert gw.subscriptions["sub_old"]["status"] == "active"
    assert gw.subscriptions["sub_new"]["status"] == "canceled"
    assert [c[1] for c in gw.calls if c[0] == "cancel_subscription"] == [
        "sub_new"]
    assert len(gw.refunds["pi_new"]) == 1

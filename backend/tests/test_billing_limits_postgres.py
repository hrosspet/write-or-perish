"""The checkout limit (#697) on Postgres: requests of one account that
arrive together are counted one after the other, so no more than the
limit pass.

sqlite ignores SELECT ... FOR UPDATE, so this runs only against a
throwaway Postgres database named by LOORE_TEST_POSTGRES_URL (it creates
and drops every table in it); skipped otherwise, like
test_payments_postgres.py. Six threads ask at once; each holds its count
open for a moment, as a slow database would.
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
from backend.models import BillingRequest, User  # noqa: E402
from backend.tests import test_payments as tp  # noqa: E402
from backend.utils import billing_limits  # noqa: E402


@pytest.fixture
def pg_app():
    app = tp._make_app(payments=True, db_uri=PG_URL)
    app.config["CHECKOUT_STARTS_PER_HOUR"] = 3
    with app.app_context():
        _db.drop_all()
        _db.create_all()
    yield app
    with app.app_context():
        _db.session.remove()
        _db.drop_all()


def test_six_at_once_and_only_the_limit_passes(pg_app, monkeypatch):
    real_limit = billing_limits.limit_for

    def slow_limit(kind, config=None):
        time.sleep(0.2)   # between the count and the new row
        return real_limit(kind, config)
    monkeypatch.setattr(billing_limits, "limit_for", slow_limit)
    with pg_app.app_context():
        u = User(username="ann", approved=True)
        _db.session.add(u)
        _db.session.commit()
        user_id = u.id
    passed, refused = [], []

    def ask():
        with pg_app.app_context():
            user = _db.session.get(User, user_id)
            try:
                billing_limits.check_and_count(user, "checkout")
                passed.append(1)
            except billing_limits.TooManyBillingRequests:
                refused.append(1)
            finally:
                _db.session.remove()

    threads = [threading.Thread(target=ask) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert (len(passed), len(refused)) == (3, 3)
    with pg_app.app_context():
        assert BillingRequest.query.filter_by(user_id=user_id).count() == 3

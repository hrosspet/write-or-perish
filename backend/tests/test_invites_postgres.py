"""Invites (#439) on Postgres: two things at once.

An invite's state changes only while it is open, making an invite and
recording its use take the same per-inviter lock, and the database holds
one open invite per inviter and one used invite per inviter and month.
Here two requests really run at once, in two threads, each pausing at the
point where the other could get in between.

sqlite runs one request at a time and ignores SELECT ... FOR UPDATE, so
this runs only against a throwaway Postgres database named by
LOORE_TEST_POSTGRES_URL (it creates and drops every table in it); skipped
otherwise, as test_referrals_postgres.py.
"""
import os
import threading
import time
from datetime import datetime, timedelta

import pytest

PG_URL = os.environ.get("LOORE_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not PG_URL, reason="needs LOORE_TEST_POSTGRES_URL (a throwaway database)")

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ENCRYPTION_DISABLED", "true")

from backend.extensions import db as _db  # noqa: E402
from backend.models import Invite, Referral, User  # noqa: E402
from backend.tests import test_payments as tp  # noqa: E402
from backend.utils import referrals  # noqa: E402

NOW = datetime(2026, 11, 13, 12, 0)
PAUSE = 0.4


@pytest.fixture
def pg_app():
    app = tp._make_app(payments=True, db_uri=PG_URL)
    app.config["REFERRALS_ENABLED"] = True
    with app.app_context():
        _db.drop_all()
        _db.create_all()
    yield app
    with app.app_context():
        _db.session.remove()
        _db.drop_all()


def _setup(app, invite_at=NOW):
    with app.app_context():
        ana = User(username="ana", approved=True, plan="alpha",
                   email="ana@example.com")
        fred = User(username="fred", approved=False)
        _db.session.add_all([ana, fred])
        _db.session.commit()
        invite = referrals.create_invite(ana, "fred@example.com",
                                         now=invite_at)
        ids = (ana.id, fred.id, invite.id, invite.code)
        _db.session.remove()
        return ids


def _run(app, *jobs):
    out = {}

    def run(name, fn):
        with app.app_context():
            try:
                out[name] = fn()
            except Exception as e:  # noqa: BLE001
                out[name] = e
            finally:
                _db.session.remove()

    threads = [threading.Thread(target=run, args=job) for job in jobs]
    for t in threads:
        t.start()
        time.sleep(PAUSE / 4)
    for t in threads:
        t.join()
    return out


def _paused(monkeypatch, name, when=lambda *a, **k: True):
    """Make referrals.<name> wait PAUSE before doing its work, for the
    calls *when* picks."""
    real = getattr(referrals, name)

    def slow(*a, **k):
        if when(*a, **k):
            time.sleep(PAUSE)
        return real(*a, **k)
    monkeypatch.setattr(referrals, name, slow)


def _state(app, invite_id):
    with app.app_context():
        state = _db.session.get(Invite, invite_id).state
        used = Referral.query.count()
        _db.session.remove()
        return state, used


@pytest.mark.parametrize("slow, winner", [
    (referrals.WITHDRAWN, "sign_up"), (referrals.USED, "withdraw")])
def test_a_withdrawal_and_the_sign_up_at_once(pg_app, monkeypatch, slow,
                                              winner):
    """Both read the invite as open; one pauses before writing, so the
    other writes first. The first write wins and the late one changes
    nothing. Never "withdrawn" with a referral, which would free the month
    for a second rewarded invite."""
    ana_id, fred_id, invite_id, code = _setup(pg_app)
    _paused(monkeypatch, "_end_if_open",
            when=lambda _id, state, _now: state == slow)

    def withdraw():
        return referrals.withdraw_invite(_db.session.get(User, ana_id), NOW)

    def sign_up():
        return referrals.redeem_invite(_db.session.get(User, fred_id), code,
                                       True, email="fred@example.com",
                                       now=NOW)

    # The slow one starts first, so both have read the invite as open
    # before either writes.
    jobs = [("withdraw", withdraw), ("sign_up", sign_up)]
    if slow == referrals.USED:
        jobs.reverse()
    out = _run(pg_app, *jobs)
    state, referrals_made = _state(pg_app, invite_id)
    if winner == "sign_up":
        assert (state, referrals_made, out["withdraw"]) == ("used", 1, False)
    else:
        assert (state, referrals_made, out["withdraw"]) == (
            "withdrawn", 0, True)
        assert out["sign_up"] is None
    with pg_app.app_context():
        ana = _db.session.get(User, ana_id)
        if state == "used":
            with pytest.raises(referrals.InviteRefused):
                referrals.create_invite(ana, "cy@example.com", now=NOW)


def test_two_makes_at_once_after_an_unswept_expiry(pg_app, monkeypatch):
    """The open invite expired and the hourly sweep has not run: two
    "Make invite" requests at once make one invite; the other is told
    there is one out already."""
    ana_id, _fred_id, old_id, _code = _setup(pg_app)
    later = NOW + timedelta(days=31)
    # Both pass the checks before the lock; the first then pauses while it
    # holds the lock (after expiring the old invite, before writing), and
    # the second waits for it.
    _paused(monkeypatch, "expire_due",
            when=lambda *a, commit=True, **k: not commit)

    def make(to):
        return lambda: referrals.create_invite(
            _db.session.get(User, ana_id), to, now=later).id

    out = _run(pg_app, ("a", make("cy@example.com")),
               ("b", make("dee@example.com")))
    made = [v for v in out.values() if isinstance(v, int)]
    refused = [v for v in out.values()
               if isinstance(v, referrals.InviteRefused)]
    assert len(made) == 1 and len(refused) == 1, out
    assert refused[0].status == 409
    with pg_app.app_context():
        assert Invite.query.filter_by(state="open").count() == 1
        assert _db.session.get(Invite, old_id).state == "expired"
        _db.session.remove()


def test_the_database_holds_one_open_invite(pg_app):
    """Without any lock: a second open invite of one inviter is refused by
    the partial unique index the model declares."""
    from sqlalchemy.exc import IntegrityError
    ana_id, *_ = _setup(pg_app)
    with pg_app.app_context():
        _db.session.add(Invite(inviter_id=ana_id, code="z" * 16,
                               kind="email", month="2026-11", state="open",
                               expires_at=NOW + timedelta(days=1)))
        with pytest.raises(IntegrityError):
            _db.session.commit()
        _db.session.rollback()
        _db.session.remove()

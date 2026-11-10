"""The spend cap's carry (#594): a month's spending above the user's limit
counts toward the next month.

Peter, 2026-10-22: $50 limit, $60 spent in October: November starts with
$10 counted. An overflow larger than a month's limit keeps the user capped
into the following month. A limit of 0 clears the carry; raising a limit
gives the room at once.

Patterned after test_user_spend_cap.py: sqlite in-memory, minimal Flask
app, logic tested directly with an explicit ``now``.
"""
import os
import sys
from datetime import date, datetime
from unittest.mock import MagicMock

# ── Environment ──────────────────────────────────────────────────────────
os.environ["ENCRYPTION_DISABLED"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("TWITTER_API_KEY", "fake")
os.environ.setdefault("TWITTER_API_SECRET", "fake")

sys.modules.setdefault("celery", MagicMock())
sys.modules.setdefault("celery.utils", MagicMock())
sys.modules.setdefault("celery.utils.log", MagicMock())
sys.modules.setdefault("celery.result", MagicMock())

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

for _mod in ["backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

from backend.extensions import db as _db  # noqa: E402
from backend.models import User, APICostLog  # noqa: E402
from backend.utils.spend import (  # noqa: E402
    clear_spend_carry, enforce_user_spend_cap, reconcile_user_spend_block,
    roll_spend_carries, roll_spend_carry, rolled_spend_carry,
    spend_cap_lift_date, spend_cap_payload, user_is_capped,
)

OCT = datetime(2026, 10, 15, 12, 0, 0)
OCT_END = datetime(2026, 10, 31, 23, 59, 0)
NOV_1 = datetime(2026, 11, 1, 0, 7, 0)
NOV = datetime(2026, 11, 20, 12, 0, 0)
DEC_1 = datetime(2026, 12, 1, 0, 7, 0)
JAN_1 = datetime(2027, 1, 1, 0, 7, 0)
FEB_1 = datetime(2027, 2, 1, 0, 7, 0)


def _config(limit=50.0):
    return {
        "PER_USER_MONTHLY_LIMIT_USD": limit,
        "SPEND_ALERT_EMAIL": "alerts@example.com",
    }


@pytest.fixture
def app():
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["TESTING"] = True
    app.config.update(_config())
    _db.init_app(app)
    with app.app_context():
        _db.create_all()
        _db.session.add(User(username="tester"))
        _db.session.commit()
        yield app
        _db.session.rollback()
        _db.drop_all()


@pytest.fixture
def user(app):
    """A user whose carry was last rolled in October (the month the
    examples overflow), with a $50 limit of their own."""
    u = User.query.filter_by(username="tester").first()
    u.monthly_spend_limit_usd = 50.0
    u.spend_carry_month = "2026-10"
    u.spend_carry_microdollars = 0
    _db.session.commit()
    return u


def _log_cost(user_id, usd, created_at):
    row = APICostLog(user_id=user_id, model_id="claude-opus-4.6",
                     request_type="llm",
                     cost_microdollars=int(round(usd * 1_000_000)))
    row.created_at = created_at
    _db.session.add(row)
    _db.session.commit()
    return row


def _carry_usd(u):
    return (u.spend_carry_microdollars or 0) / 1_000_000


def _enforce(u, now, limit=50.0):
    return enforce_user_spend_cap(u.id, _config(limit), now=now,
                                  send_email=MagicMock())


# ── Peter's example ──────────────────────────────────────────────────────

def test_sixty_of_fifty_in_october_leaves_forty_in_november(user):
    _log_cost(user.id, 60.0, OCT)
    assert roll_spend_carry(user, _config(), now=NOV_1) is True
    _db.session.commit()
    assert user.spend_carry_month == "2026-11"
    assert _carry_usd(user) == 10.0
    assert user_is_capped(user, now=NOV_1) is False   # $10 < $50

    _log_cost(user.id, 39.99, NOV)
    assert _enforce(user, NOV)["blocked"] is False     # $49.99 counted
    _log_cost(user.id, 0.01, NOV)
    result = _enforce(user, NOV)                       # $50 counted
    assert result["blocked"] is True
    assert result["spend_usd"] == 40.0                 # the real spend
    assert result["carried_usd"] == 10.0
    assert user_is_capped(user, now=NOV) is True


def test_the_block_email_names_the_carry(user):
    _log_cost(user.id, 60.0, OCT)
    _log_cost(user.id, 45.0, NOV)
    send = MagicMock()
    enforce_user_spend_cap(user.id, _config(), now=NOV, send_email=send)
    kwargs = send.call_args.kwargs
    assert kwargs["spend_usd"] == 45.0
    assert kwargs["carried_usd"] == 10.0


def test_an_october_under_the_limit_carries_nothing(user):
    _log_cost(user.id, 49.0, OCT)
    roll_spend_carry(user, _config(), now=NOV_1)
    assert _carry_usd(user) == 0
    assert user.spend_carry_month == "2026-11"


# ── An overflow above a whole limit ──────────────────────────────────────

def test_an_overflow_above_a_full_limit_caps_the_next_month(user):
    """$130 of $50 in October: November is capped from the start, and
    December starts with $30 carried."""
    _log_cost(user.id, 130.0, OCT)
    assert user_is_capped(user, now=NOV_1) is False   # the October block expired
    roll_spend_carry(user, _config(), now=NOV_1)
    _db.session.commit()
    assert _carry_usd(user) == 80.0
    assert user.spend_blocked_month == "2026-11"
    assert user_is_capped(user, now=NOV_1) is True
    assert spend_cap_lift_date(user, _config(), now=NOV) == date(2026, 12, 1)

    roll_spend_carry(user, _config(), now=DEC_1)
    _db.session.commit()
    assert _carry_usd(user) == 30.0
    assert user_is_capped(user, now=DEC_1) is False
    assert _enforce(user, DEC_1)["blocked"] is False


def test_the_lift_date_and_message_while_capped_in_october(app, user):
    """Capped in October with $130 spent: the cap lifts on 1 December, not
    1 November, and the 402 says so. One month's overflow keeps the usual
    sentence."""
    _log_cost(user.id, 130.0, OCT)
    assert _enforce(user, OCT)["blocked"] is True
    assert spend_cap_lift_date(user, _config(), now=OCT) == date(2026, 12, 1)
    payload = spend_cap_payload(user, now=OCT)
    assert payload["error"] == "monthly_spend_limit_reached"
    assert payload["lifts_on"] == "2026-12-01"
    assert payload["message"].endswith("It resets on December 1.")

    # $60 of $50: lifts at the start of next month, the text unchanged.
    APICostLog.query.delete()
    _db.session.commit()
    _log_cost(user.id, 60.0, OCT)
    payload = spend_cap_payload(user, now=OCT)
    assert payload["lifts_on"] == "2026-11-01"
    assert payload["message"] == (
        "You've reached your monthly usage limit for the free alpha. "
        "It resets at the start of next month.")


@pytest.mark.parametrize("october, lifts", [
    (50.0, date(2026, 11, 1)),     # at the limit: nothing carries
    (99.99, date(2026, 11, 1)),    # $49.99 carried: under the limit
    (100.0, date(2026, 12, 1)),    # $50 carried: November capped
    (150.0, date(2027, 1, 1)),     # $100 carried: November and December
])
def test_the_lift_date_counts_whole_limits(user, october, lifts):
    _log_cost(user.id, october, OCT)
    assert spend_cap_lift_date(user, _config(), now=OCT) == lifts


def test_a_payload_without_a_user_keeps_the_usual_text(app):
    payload = spend_cap_payload(None)
    assert payload["lifts_on"] is None
    assert payload["message"].endswith("It resets at the start of next month.")


# ── Limits changed during the month ──────────────────────────────────────

def test_a_limit_raised_mid_month_and_not_reached_carries_nothing(user):
    """Capped at $50 on 20 October, raised to $100, October ends at $90:
    the limit in force when the month ends decides, so nothing carries."""
    _log_cost(user.id, 50.0, OCT)
    assert _enforce(user, OCT)["blocked"] is True
    user.monthly_spend_limit_usd = 100.0
    assert reconcile_user_spend_block(user, _config(), now=OCT)["blocked"] \
        is False
    _db.session.commit()
    _log_cost(user.id, 40.0, OCT_END)
    roll_spend_carry(user, _config(), now=NOV_1)
    assert _carry_usd(user) == 0
    assert user_is_capped(user, now=NOV_1) is False


def test_raising_the_limit_gives_the_room_at_once(user):
    """$80 carried into November caps the user; a limit of $100 leaves
    $20 at once."""
    _log_cost(user.id, 130.0, OCT)
    roll_spend_carry(user, _config(), now=NOV_1)
    _db.session.commit()
    assert user_is_capped(user, now=NOV) is True
    user.monthly_spend_limit_usd = 100.0
    state = reconcile_user_spend_block(user, _config(), now=NOV)
    _db.session.commit()
    assert state["blocked"] is False
    assert state["carried_usd"] == 80.0
    assert user_is_capped(user, now=NOV) is False
    _log_cost(user.id, 20.0, NOV)
    assert _enforce(user, NOV, limit=100.0)["status"] == "blocked"


def test_a_limit_of_zero_clears_the_carry(user):
    _log_cost(user.id, 130.0, OCT)
    roll_spend_carry(user, _config(), now=NOV_1)
    _db.session.commit()
    assert _carry_usd(user) == 80.0
    user.monthly_spend_limit_usd = 0.0
    state = reconcile_user_spend_block(user, _config(), now=NOV)
    _db.session.commit()
    assert state["blocked"] is False
    assert _carry_usd(user) == 0
    # Back to $50: the carry stays cleared.
    user.monthly_spend_limit_usd = 50.0
    state = reconcile_user_spend_block(user, _config(), now=NOV)
    _db.session.commit()
    assert state["blocked"] is False
    assert state["carried_usd"] == 0
    assert _carry_usd(user) == 0


def test_a_limit_set_on_an_uncapped_user_carries_nothing_from_before(user):
    """Spending while the user had no limit never carries: the hourly roll
    keeps an uncapped user's month current, so a limit set later starts
    from 0."""
    user.monthly_spend_limit_usd = 0.0
    _db.session.commit()
    _log_cost(user.id, 500.0, OCT)
    roll_spend_carries(_config(), now=NOV_1)
    assert user.spend_carry_month == "2026-11"
    user.monthly_spend_limit_usd = 50.0
    state = reconcile_user_spend_block(user, _config(), now=NOV)
    assert state["carried_usd"] == 0
    assert state["blocked"] is False


def test_the_cap_check_of_an_uncapped_user_writes_nothing(user):
    """The cap check runs before every profile step: for a user with no
    limit it neither rolls nor writes."""
    user.monthly_spend_limit_usd = 0.0
    _db.session.commit()
    assert _enforce(user, NOV, limit=0)["status"] == "disabled"
    assert user.spend_carry_month == "2026-10"


def test_a_rollover_with_no_limit_carries_nothing(user):
    user.monthly_spend_limit_usd = 0.0
    _log_cost(user.id, 500.0, OCT)
    roll_spend_carry(user, _config(), now=NOV_1)
    assert _carry_usd(user) == 0
    assert user_is_capped(user, now=NOV_1) is False


# ── The admin's clear action ─────────────────────────────────────────────

def test_clearing_the_carry_lifts_a_cap_it_caused(user):
    _log_cost(user.id, 130.0, OCT)
    roll_spend_carry(user, _config(), now=NOV_1)
    _db.session.commit()
    _log_cost(user.id, 5.0, NOV)
    assert user_is_capped(user, now=NOV) is True
    state = clear_spend_carry(user, _config(), now=NOV)
    _db.session.commit()
    assert state == {"blocked": False, "spend_usd": 5.0, "carried_usd": 0.0,
                     "limit_usd": 50.0, "month": "2026-11"}
    assert _carry_usd(user) == 0
    assert user_is_capped(user, now=NOV) is False
    # The clear holds: a later roll in the month changes nothing.
    assert roll_spend_carry(user, _config(), now=NOV) is False
    # Cost records are not changed.
    assert APICostLog.query.count() == 2


def test_clearing_keeps_a_cap_the_month_reached_itself(user):
    _log_cost(user.id, 130.0, OCT)
    roll_spend_carry(user, _config(), now=NOV_1)
    _log_cost(user.id, 55.0, NOV)
    state = clear_spend_carry(user, _config(), now=NOV)
    assert state["blocked"] is True
    assert user_is_capped(user, now=NOV) is True


# ── An inactive user, several months ─────────────────────────────────────

def test_an_inactive_users_carry_shrinks_one_limit_a_month(user):
    """$175 of $50 in October and nothing since: $125 into November, $75
    into December, $25 into January, nothing into February."""
    _log_cost(user.id, 175.0, OCT)
    expected = [(NOV_1, 125.0, True), (DEC_1, 75.0, True),
                (JAN_1, 25.0, False), (FEB_1, 0.0, False)]
    for now, carry, capped in expected:
        roll_spend_carry(user, _config(), now=now)
        _db.session.commit()
        assert _carry_usd(user) == carry, now
        assert user_is_capped(user, now=now) is capped, now


def test_a_roll_months_late_advances_month_by_month(user):
    """Not rolled since October (the hourly task did not run): rolling in
    February counts each month on its own, with its own spend."""
    _log_cost(user.id, 175.0, OCT)
    _log_cost(user.id, 30.0, datetime(2026, 12, 10))
    # Nov: 125; Dec: 125 + 0 - 50 = 75; Jan: 75 + 30 - 50 = 55; Feb: 5.
    assert rolled_spend_carry(user, _config(), now=JAN_1) == 55_000_000
    roll_spend_carry(user, _config(), now=FEB_1)
    _db.session.commit()
    assert user.spend_carry_month == "2027-02"
    assert _carry_usd(user) == 5.0
    assert user_is_capped(user, now=FEB_1) is False


def test_a_user_never_rolled_starts_with_nothing_carried(app):
    """No month before the first roll is charged afterwards."""
    u = User.query.filter_by(username="tester").first()
    assert u.spend_carry_month is None
    _log_cost(u.id, 500.0, OCT)
    roll_spend_carry(u, _config(), now=NOV_1)
    assert u.spend_carry_month == "2026-11"
    assert _carry_usd(u) == 0


# ── Idempotent ────────────────────────────────────────────────────────────

def test_the_rollover_is_idempotent(user):
    _log_cost(user.id, 60.0, OCT)
    assert roll_spend_carry(user, _config(), now=NOV_1) is True
    _db.session.commit()
    # Again in the same month, also after spending: nothing changes.
    _log_cost(user.id, 20.0, NOV)
    assert roll_spend_carry(user, _config(), now=NOV) is False
    assert _carry_usd(user) == 10.0
    assert enforce_user_spend_cap(user.id, _config(), now=NOV,
                                  send_email=MagicMock())["carried_usd"] == 10.0
    assert _carry_usd(user) == 10.0


def test_the_hourly_roll_touches_each_user_once_a_month(app, user):
    other = User(username="other")              # never rolled
    _db.session.add(other)
    _db.session.commit()
    _log_cost(user.id, 130.0, OCT)
    first = roll_spend_carries(_config(), now=NOV_1)
    assert first["users"] == 2
    assert first["capped_by_carry"] == 1
    assert _carry_usd(user) == 80.0
    assert user_is_capped(user, now=NOV_1) is True
    assert other.spend_carry_month == "2026-11"
    assert _carry_usd(other) == 0

    again = roll_spend_carries(_config(), now=NOV)
    assert again["users"] == 0
    assert _carry_usd(user) == 80.0


# ── The default limit ────────────────────────────────────────────────────

def test_a_null_override_uses_the_default_limit(user):
    user.monthly_spend_limit_usd = None
    _db.session.commit()
    _log_cost(user.id, 70.0, OCT)
    roll_spend_carry(user, _config(limit=50.0), now=NOV_1)
    assert _carry_usd(user) == 20.0
    # With no default limit either, nothing carries.
    user.spend_carry_month = "2026-10"
    user.spend_carry_microdollars = 0
    roll_spend_carry(user, _config(limit=0), now=NOV_1)
    assert _carry_usd(user) == 0

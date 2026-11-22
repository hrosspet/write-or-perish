"""The usage ceiling of a subscribed account is counted per paid period.

A subscription paid on 2027-01-20 for the period to 2027-02-20 has its
level's ceiling once for that period: $49 in January and $2 more in
February reach a $50 ceiling. The next period starts from zero (plus
what the last one spent above the ceiling), the first paid period after
a trial counts from the trial's start, and accounts whose ceiling is not
their subscription's (a plan set by hand, an admin's limit, a
subscription whose period start is not stored yet) keep the calendar
month.
"""
import os
import sys
from datetime import date, datetime, timezone
from unittest.mock import MagicMock

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
from backend.utils import spend, stripe_billing  # noqa: E402
from backend.utils.spend import (  # noqa: E402
    ceiling_period_start, counted_spend_microdollars,
    enforce_user_spend_cap, get_user_spend_limit_usd,
    reconcile_user_spend_block, spend_cap_lift_date, spend_cap_payload,
    user_is_capped,
)

PAID = datetime(2027, 1, 20, 10, 0, 0)         # the period's start
PERIOD_END = datetime(2027, 2, 20, 10, 0, 0)   # what the payment covers
NEXT_END = datetime(2027, 3, 20, 10, 0, 0)
JAN_LATE = datetime(2027, 1, 30, 12, 0, 0)
FEB_EARLY = datetime(2027, 2, 10, 12, 0, 0)    # inside the same period
FEB_LATE = datetime(2027, 2, 22, 12, 0, 0)     # the next period

CONFIG = {
    "PAYMENTS_ENABLED": True,
    "PER_USER_MONTHLY_LIMIT_USD": 10.0,
    "SUBSCRIPTION_SPEND_LIMIT_USD_STANDARD": 50.0,
    "SUBSCRIPTION_SPEND_LIMIT_USD_SUPPORTER": 100.0,
    "SUBSCRIPTION_SPEND_LIMIT_USD_DISCOUNTED": 50.0,
    "SUBSCRIPTION_TRIAL_SPEND_LIMIT_USD": 30.0,
    "SPEND_ALERT_EMAIL": "alerts@example.com",
}


@pytest.fixture
def app():
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["TESTING"] = True
    app.config.update(CONFIG)
    _db.init_app(app)
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.rollback()
        _db.drop_all()


def _log_cost(user_id, usd, created_at):
    _db.session.add(APICostLog(
        user_id=user_id, model_id="claude-opus-4.6", request_type="llm",
        cost_microdollars=int(usd * 1_000_000), created_at=created_at))
    _db.session.commit()


def _subscriber(name="payer", level="standard", **kw):
    fields = dict(
        username=name, plan="free", subscription_level=level,
        subscription_status="active", subscription_ever_paid=True,
        subscription_period_start=PAID, subscription_period_end=PERIOD_END)
    fields.update(kw)
    u = User(**fields)
    _db.session.add(u)
    _db.session.commit()
    return u


def _mailbox():
    emails = []

    def send(**kw):
        emails.append(kw)
    return emails, send


def _stamp(value):
    return int(value.replace(tzinfo=timezone.utc).timestamp())


def _stripe_sub(user, start, end, status="active", level="standard"):
    return {
        "id": "sub_1", "object": "subscription", "customer": "cus_1",
        "status": status, "cancel_at_period_end": False, "cancel_at": None,
        "trial_start": None, "trial_end": None,
        "metadata": {"loore_user_id": str(user.id)},
        "items": {"data": [{
            "current_period_start": _stamp(start),
            "current_period_end": _stamp(end),
            "price": {"id": f"price_{level}",
                      "lookup_key":
                          stripe_billing.PRICE_LOOKUP_KEYS[level]},
        }]},
    }


def _stripe_subscriber(**kw):
    return _subscriber(stripe_subscription_id="sub_1",
                       stripe_customer_id="cus_1", **kw)


def _renew(user, start, end, level="standard"):
    """Stripe's event for the next period (and, with *level*, a change
    of level that arrives with it)."""
    stripe_billing.apply_subscription(
        _stripe_sub(user, start, end, level=level), CONFIG)
    _db.session.commit()


# ── One period, one ceiling ──────────────────────────────────────────────

@pytest.mark.parametrize("level", ["standard", "discounted"])
def test_a_period_across_two_months_has_the_ceiling_once(app, level):
    u = _subscriber(level=level)
    assert get_user_spend_limit_usd(u, CONFIG) == 50.0
    assert ceiling_period_start(u) == PAID
    emails, send = _mailbox()

    _log_cost(u.id, 49.0, JAN_LATE)
    r = enforce_user_spend_cap(u.id, CONFIG, now=JAN_LATE, send_email=send)
    assert r["status"] == "ok" and r["blocked"] is False

    # February, same period: January's $49 still counts.
    r = enforce_user_spend_cap(u.id, CONFIG, now=FEB_EARLY, send_email=send)
    assert r["status"] == "ok" and r["spend_usd"] == 49.0
    assert not user_is_capped(u, now=FEB_EARLY)

    _log_cost(u.id, 2.0, FEB_EARLY)
    r = enforce_user_spend_cap(u.id, CONFIG, now=FEB_EARLY, send_email=send)
    assert r["status"] == "blocked" and r["spend_usd"] == 51.0
    assert user_is_capped(u, now=FEB_EARLY)
    assert len(emails) == 1 and emails[0]["spend_usd"] == 51.0

    # Asked again: still blocked, one email.
    r = enforce_user_spend_cap(u.id, CONFIG, now=FEB_EARLY, send_email=send)
    assert r["status"] == "already_blocked"
    assert len(emails) == 1


def test_spend_before_the_period_does_not_count(app):
    u = _subscriber()
    _log_cost(u.id, 45.0, datetime(2027, 1, 5, 12, 0, 0))   # before it
    _log_cost(u.id, 45.0, JAN_LATE)
    emails, send = _mailbox()
    r = enforce_user_spend_cap(u.id, CONFIG, now=JAN_LATE, send_email=send)
    # $90 in January, $45 of it in this period: under its ceiling.
    assert r["status"] == "ok" and r["spend_usd"] == 45.0
    assert not user_is_capped(u, now=JAN_LATE)
    assert counted_spend_microdollars(u, CONFIG, now=JAN_LATE) == 45_000_000


def test_the_block_ends_when_the_next_period_starts(app):
    u = _subscriber()
    emails, send = _mailbox()
    _log_cost(u.id, 50.0, JAN_LATE)
    enforce_user_spend_cap(u.id, CONFIG, now=JAN_LATE, send_email=send)
    assert user_is_capped(u, now=FEB_EARLY)

    u.subscription_period_start = PERIOD_END
    u.subscription_period_end = NEXT_END
    _db.session.commit()

    assert not user_is_capped(u, now=FEB_LATE)
    r = enforce_user_spend_cap(u.id, CONFIG, now=FEB_LATE, send_email=send)
    assert r["status"] == "ok"
    assert r["spend_usd"] == 0 and r["carried_usd"] == 0


def test_what_a_period_spends_above_the_ceiling_counts_in_the_next(app):
    u = _stripe_subscriber()
    emails, send = _mailbox()
    _log_cost(u.id, 58.0, JAN_LATE)
    enforce_user_spend_cap(u.id, CONFIG, now=JAN_LATE, send_email=send)

    _renew(u, PERIOD_END, NEXT_END)
    _log_cost(u.id, 41.0, FEB_LATE)
    r = enforce_user_spend_cap(u.id, CONFIG, now=FEB_LATE, send_email=send)
    assert r["status"] == "ok"
    assert r["carried_usd"] == 8.0 and r["spend_usd"] == 41.0

    _log_cost(u.id, 1.0, FEB_LATE)
    r = enforce_user_spend_cap(u.id, CONFIG, now=FEB_LATE, send_email=send)
    assert r["status"] == "blocked"
    assert len(emails) == 2


def test_a_carry_of_a_whole_ceiling_blocks_the_next_period_at_once(app):
    u = _stripe_subscriber()
    _log_cost(u.id, 154.0, JAN_LATE)
    emails, send = _mailbox()
    enforce_user_spend_cap(u.id, CONFIG, now=JAN_LATE, send_email=send)

    _renew(u, PERIOD_END, NEXT_END)

    assert u.spend_period_carry_microdollars == 104_000_000
    assert user_is_capped(u, now=FEB_LATE)
    # $104 counted against $50: $54 go on into the next period, which
    # they block too; the one after it starts with $4.
    assert spend_cap_lift_date(u, CONFIG, now=FEB_LATE) == date(2027, 4, 20)


# ── The first paid period after a trial ──────────────────────────────────

def test_the_first_paid_period_counts_from_the_trials_start(app):
    trial_start = datetime(2027, 1, 6, 10, 0, 0)
    u = _subscriber(subscription_trial_start=trial_start,
                    subscription_trial_end=PAID)
    assert ceiling_period_start(u) == trial_start
    _log_cost(u.id, 25.0, datetime(2027, 1, 10, 12, 0, 0))   # in the trial
    _log_cost(u.id, 24.0, JAN_LATE)
    emails, send = _mailbox()
    r = enforce_user_spend_cap(u.id, CONFIG, now=JAN_LATE, send_email=send)
    assert r["status"] == "ok" and r["spend_usd"] == 49.0
    _log_cost(u.id, 1.0, FEB_EARLY)
    r = enforce_user_spend_cap(u.id, CONFIG, now=FEB_EARLY, send_email=send)
    assert r["status"] == "blocked"

    # The second paid period counts from its own start.
    u.subscription_period_start = PERIOD_END
    u.subscription_period_end = NEXT_END
    _db.session.commit()
    assert ceiling_period_start(u) == PERIOD_END
    assert not user_is_capped(u, now=FEB_LATE)


def test_a_trial_keeps_its_own_ceiling(app):
    trial_start = datetime(2027, 1, 6, 10, 0, 0)
    u = _subscriber(subscription_status="trialing",
                    subscription_ever_paid=False,
                    subscription_trial_start=trial_start,
                    subscription_trial_end=PAID,
                    subscription_period_start=trial_start,
                    subscription_period_end=PAID)
    _log_cost(u.id, 30.0, datetime(2027, 1, 10, 12, 0, 0))
    emails, send = _mailbox()
    r = enforce_user_spend_cap(u.id, CONFIG, now=JAN_LATE, send_email=send)
    assert r["status"] == "trial_capped"
    assert spend_cap_payload(u)["lifts_with_payment"] is True


# ── What the capped account is told ──────────────────────────────────────

def test_the_text_names_the_day_the_next_period_starts(app):
    u = _subscriber()
    _log_cost(u.id, 50.0, JAN_LATE)
    emails, send = _mailbox()
    enforce_user_spend_cap(u.id, CONFIG, now=JAN_LATE, send_email=send)
    payload = spend_cap_payload(u, now=FEB_EARLY)
    assert payload == {
        "error": "monthly_spend_limit_reached",
        "message": "You've reached your monthly usage limit. It resets on "
                   "February 20.",
        "lifts_on": "2027-02-20",
    }


def test_no_reset_is_named_for_a_subscription_that_ends_with_the_period(app):
    u = _subscriber(subscription_cancel_at_period_end=True)
    _log_cost(u.id, 50.0, JAN_LATE)
    emails, send = _mailbox()
    enforce_user_spend_cap(u.id, CONFIG, now=JAN_LATE, send_email=send)
    assert user_is_capped(u, now=FEB_EARLY)
    payload = spend_cap_payload(u, now=FEB_EARLY)
    assert payload["message"] == "You've reached your monthly usage limit."
    assert payload["lifts_on"] is None


# ── Accounts that keep the calendar month ────────────────────────────────

def _two_months_under_the_limit(u, limit):
    """Just under *limit* in January and again in February: never blocked
    when the ceiling is counted per calendar month."""
    emails, send = _mailbox()
    _log_cost(u.id, limit - 1, JAN_LATE)
    r = enforce_user_spend_cap(u.id, CONFIG, now=JAN_LATE, send_email=send)
    assert r["status"] == "ok"
    _log_cost(u.id, limit - 1, FEB_EARLY)
    r = enforce_user_spend_cap(u.id, CONFIG, now=FEB_EARLY, send_email=send)
    assert r["status"] == "ok" and r["spend_usd"] == limit - 1
    assert r["carried_usd"] == 0 and "period_start" not in r
    assert not user_is_capped(u, now=FEB_EARLY)
    assert emails == []


def test_a_subscription_without_a_stored_period_start_keeps_the_month(app):
    u = _subscriber(subscription_period_start=None)
    assert ceiling_period_start(u) is None
    _two_months_under_the_limit(u, 50.0)


def test_an_admins_limit_keeps_the_month(app):
    u = _subscriber(monthly_spend_limit_usd=20.0)
    assert ceiling_period_start(u) is None
    _two_months_under_the_limit(u, 20.0)


def test_a_plan_set_by_hand_keeps_the_month(app):
    u = _subscriber(plan="alpha")
    assert ceiling_period_start(u) is None
    _two_months_under_the_limit(u, 10.0)


def test_an_account_without_a_subscription_keeps_the_month(app):
    u = User(username="free", plan="free")
    _db.session.add(u)
    _db.session.commit()
    assert ceiling_period_start(u) is None
    _two_months_under_the_limit(u, 10.0)


def test_a_months_block_is_not_read_while_counted_per_period(app):
    """The month's block of an account that then gets a paid period: what
    the period has spent decides."""
    u = _subscriber(subscription_period_start=None)
    u.spend_blocked_month = spend.current_month(FEB_EARLY)
    _db.session.commit()
    assert user_is_capped(u, now=FEB_EARLY)
    u.subscription_period_start = PAID
    _db.session.commit()
    assert not user_is_capped(u, now=FEB_EARLY)


def test_months_under_the_period_rule_carry_nothing_into_the_calendar(app):
    """An account counted per period that an admin then gives a limit:
    the months before carry nothing into the month's count."""
    u = _subscriber()
    _log_cost(u.id, 45.0, datetime(2027, 1, 21, 12, 0, 0))
    emails, send = _mailbox()
    enforce_user_spend_cap(u.id, CONFIG, now=JAN_LATE, send_email=send)
    spend.roll_spend_carries(CONFIG, now=JAN_LATE)
    state = spend.set_user_spend_limit(u, 20.0, CONFIG, now=FEB_EARLY)
    _db.session.commit()
    assert state["carried_usd"] == 0 and state["blocked"] is False
    assert "period_start" not in state


# ── The period's start comes from Stripe ─────────────────────────────────

def test_the_subscription_event_stores_the_periods_start(app):
    u = User(username="new", plan="free")
    _db.session.add(u)
    _db.session.commit()
    stripe_billing.apply_subscription(
        _stripe_sub(u, PAID, PERIOD_END), CONFIG)
    _db.session.commit()
    assert u.subscription_period_start == PAID
    assert u.subscription_period_end == PERIOD_END
    assert ceiling_period_start(u) == PAID


def test_a_renewal_event_lifts_the_last_periods_block(app):
    u = _subscriber(stripe_subscription_id="sub_1",
                    stripe_customer_id="cus_1")
    u.subscription_ever_paid = True
    _log_cost(u.id, 52.0, JAN_LATE)
    emails, send = _mailbox()
    enforce_user_spend_cap(u.id, CONFIG, now=JAN_LATE, send_email=send)
    assert user_is_capped(u, now=FEB_EARLY)

    stripe_billing.apply_subscription(
        _stripe_sub(u, PERIOD_END, NEXT_END), CONFIG)
    _db.session.commit()

    assert u.subscription_period_start == PERIOD_END
    assert not user_is_capped(u, now=FEB_LATE)
    # The $2 above the ceiling were carried when the event arrived.
    assert u.spend_period_start == PERIOD_END
    assert u.spend_period_carry_microdollars == 2_000_000


# ── A change of level, time counted by the month, the admin's clear ──────

MAR_END = datetime(2027, 4, 20, 10, 0, 0)
MAR_LATE = datetime(2027, 3, 25, 12, 0, 0)


def test_a_lower_level_starts_without_the_higher_levels_use(app):
    """Supporter ($100) used $95 of its period, inside its ceiling; the
    change to Standard ($50) arrives with the next period."""
    u = _stripe_subscriber(level="supporter")
    emails, send = _mailbox()
    _log_cost(u.id, 95.0, JAN_LATE)
    r = enforce_user_spend_cap(u.id, CONFIG, now=JAN_LATE, send_email=send)
    assert r["status"] == "ok"

    _renew(u, PERIOD_END, NEXT_END, level="standard")

    assert get_user_spend_limit_usd(u, CONFIG) == 50.0
    assert u.spend_period_carry_microdollars == 0
    assert not user_is_capped(u, now=FEB_LATE)


def test_a_lower_level_carries_only_what_went_above_the_higher_ceiling(app):
    u = _stripe_subscriber(level="supporter")
    emails, send = _mailbox()
    _log_cost(u.id, 100.5, JAN_LATE)
    enforce_user_spend_cap(u.id, CONFIG, now=JAN_LATE, send_email=send)

    _renew(u, PERIOD_END, NEXT_END, level="standard")

    assert u.spend_period_carry_microdollars == 500_000
    assert not user_is_capped(u, now=FEB_LATE)


def test_periods_under_an_admins_limit_count_nothing_after_it_is_removed(app):
    """The admin gives a subscriber $200 a month over two renewals, the
    subscriber uses $45 in each period, then the limit is removed."""
    u = _stripe_subscriber()
    emails, send = _mailbox()
    _log_cost(u.id, 45.0, JAN_LATE)
    enforce_user_spend_cap(u.id, CONFIG, now=JAN_LATE, send_email=send)
    assert u.spend_period_start == PAID

    spend.set_user_spend_limit(u, 200.0, CONFIG, now=FEB_EARLY)
    _db.session.commit()
    _renew(u, PERIOD_END, NEXT_END)
    _log_cost(u.id, 45.0, FEB_LATE)
    _renew(u, NEXT_END, MAR_END)

    state = spend.set_user_spend_limit(u, None, CONFIG, now=MAR_LATE)
    _db.session.commit()
    assert state["carried_usd"] == 0 and state["spend_usd"] == 0
    assert not user_is_capped(u, now=MAR_LATE)


def test_a_period_after_a_plan_set_by_hand_starts_at_zero(app):
    u = _stripe_subscriber()
    emails, send = _mailbox()
    _log_cost(u.id, 80.0, JAN_LATE)
    enforce_user_spend_cap(u.id, CONFIG, now=JAN_LATE, send_email=send)
    u.plan = "alpha"
    _db.session.commit()
    _renew(u, PERIOD_END, NEXT_END)
    u.plan = "free"
    state = reconcile_user_spend_block(u, CONFIG, now=FEB_LATE)
    _db.session.commit()
    assert state["carried_usd"] == 0
    assert not user_is_capped(u, now=FEB_LATE)


def test_the_admins_clear_action_clears_the_periods_carry(app):
    u = _stripe_subscriber()
    emails, send = _mailbox()
    _log_cost(u.id, 154.0, JAN_LATE)
    enforce_user_spend_cap(u.id, CONFIG, now=JAN_LATE, send_email=send)
    _renew(u, PERIOD_END, NEXT_END)
    assert user_is_capped(u, now=FEB_LATE)        # $104 carried

    state = spend.clear_spend_carry(u, CONFIG, now=FEB_LATE)
    _db.session.commit()

    assert state["carried_usd"] == 0 and state["blocked"] is False
    assert not user_is_capped(u, now=FEB_LATE)
    assert u.spend_period_carry_microdollars == 0


def test_the_same_event_twice_carries_once(app):
    u = _stripe_subscriber()
    emails, send = _mailbox()
    _log_cost(u.id, 58.0, JAN_LATE)
    enforce_user_spend_cap(u.id, CONFIG, now=JAN_LATE, send_email=send)
    _renew(u, PERIOD_END, NEXT_END)
    _renew(u, PERIOD_END, NEXT_END)
    assert u.spend_period_carry_microdollars == 8_000_000


# ── Time without access, and events out of order ─────────────────────────

def test_a_carry_is_kept_over_a_late_payment(app):
    """Over the ceiling, then the payment for the next period is late
    (the subscription gives nothing), then it is paid in that period."""
    u = _stripe_subscriber()
    emails, send = _mailbox()
    _log_cost(u.id, 70.0, JAN_LATE)
    enforce_user_spend_cap(u.id, CONFIG, now=JAN_LATE, send_email=send)

    stripe_billing.apply_subscription(
        _stripe_sub(u, PERIOD_END, NEXT_END, status="past_due"), CONFIG)
    _db.session.commit()
    assert ceiling_period_start(u) is None
    assert u.spend_period_start == PAID

    _renew(u, PERIOD_END, NEXT_END)

    assert u.spend_period_start == PERIOD_END
    assert u.spend_period_carry_microdollars == 20_000_000
    assert not user_is_capped(u, now=FEB_LATE)
    _log_cost(u.id, 30.0, FEB_LATE)
    r = enforce_user_spend_cap(u.id, CONFIG, now=FEB_LATE, send_email=send)
    assert r["status"] == "blocked" and r["carried_usd"] == 20.0


def test_an_event_for_an_earlier_period_leaves_the_carry(app):
    u = _stripe_subscriber()
    emails, send = _mailbox()
    _log_cost(u.id, 58.0, JAN_LATE)
    enforce_user_spend_cap(u.id, CONFIG, now=JAN_LATE, send_email=send)
    _renew(u, PERIOD_END, NEXT_END)
    assert u.spend_period_carry_microdollars == 8_000_000

    _renew(u, PAID, PERIOD_END)          # the earlier period's event, late
    assert u.spend_period_start == PERIOD_END
    assert u.spend_period_carry_microdollars == 8_000_000
    enforce_user_spend_cap(u.id, CONFIG, now=FEB_LATE, send_email=send)
    assert u.spend_period_start == PERIOD_END
    assert u.spend_period_carry_microdollars == 8_000_000

    _renew(u, PERIOD_END, NEXT_END)
    assert u.spend_period_carry_microdollars == 8_000_000
    r = enforce_user_spend_cap(u.id, CONFIG, now=FEB_LATE, send_email=send)
    assert r["carried_usd"] == 8.0

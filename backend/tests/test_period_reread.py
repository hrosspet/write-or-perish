"""A subscription whose stored period has ended is read again from Stripe.

A subscription paid on 2027-01-20 for the period to 2027-02-20 renews at
Stripe on 2027-02-20. When the event of that renewal has not arrived, the
account still stores the period that ended. The hourly job reads the
subscription from Stripe and settles it as the event would be: the next
period starts with what the last one spent above the ceiling, a
subscription that ended or whose payment is late is recorded as that.
Stripe is a stub here; nothing reaches it.
"""
import ast
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
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

from backend import celery_queues  # noqa: E402
from backend.extensions import db as _db  # noqa: E402
from backend.models import User, APICostLog  # noqa: E402
from backend.utils import period_reread, stripe_billing  # noqa: E402
from backend.utils.period_reread import reread_ended_periods  # noqa: E402
from backend.utils.spend import (  # noqa: E402
    enforce_user_spend_cap, user_is_capped)

PAID = datetime(2027, 1, 20, 10, 0, 0)         # the period's start
PERIOD_END = datetime(2027, 2, 20, 10, 0, 0)   # what the payment covers
NEXT_END = datetime(2027, 3, 20, 10, 0, 0)
JAN_LATE = datetime(2027, 1, 30, 12, 0, 0)
AN_HOUR_AFTER = PERIOD_END + timedelta(hours=1)
TWO_HOURS_AFTER = PERIOD_END + timedelta(hours=2)
FEB_LATE = datetime(2027, 2, 22, 12, 0, 0)     # inside the next period

CONFIG = {
    "PAYMENTS_ENABLED": True,
    "STRIPE_SECRET_KEY": "sk_test_stub",
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


class StubStripe:
    """Stripe's side: the subscriptions it has, by id. Counts the reads.
    *down*: every read raises, as when Stripe cannot be reached."""

    def __init__(self):
        self.subs = {}
        self.reads = []
        self.down = False
        self.on_read = None

    def retrieve_subscription(self, subscription_id):
        self.reads.append(subscription_id)
        if self.down:
            raise ConnectionError("stub: Stripe cannot be reached")
        if subscription_id not in self.subs:
            error = Exception("stub: no such subscription")
            error.http_status = 404
            raise error
        sub = self.subs[subscription_id]
        if self.on_read is not None:
            self.on_read(subscription_id)
        return sub


@pytest.fixture
def stripe(monkeypatch):
    stub = StubStripe()
    monkeypatch.setattr(stripe_billing, "get_gateway", lambda config=None: stub)
    return stub


def _stamp(value):
    return int(value.replace(tzinfo=timezone.utc).timestamp())


def _stripe_sub(user, start, end, status="active", level="standard"):
    return {
        "id": user.stripe_subscription_id, "object": "subscription",
        "customer": user.stripe_customer_id,
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


def _subscriber(n=1, level="standard", **kw):
    fields = dict(
        username=f"payer{n}", plan="free", subscription_level=level,
        subscription_status="active", subscription_ever_paid=True,
        subscription_period_start=PAID, subscription_period_end=PERIOD_END,
        stripe_subscription_id=f"sub_{n}", stripe_customer_id=f"cus_{n}")
    fields.update(kw)
    u = User(**fields)
    _db.session.add(u)
    _db.session.commit()
    return u


def _use(user, usd, when):
    _db.session.add(APICostLog(
        user_id=user.id, model_id="claude-opus-4.6", request_type="llm",
        cost_microdollars=int(usd * 1_000_000), created_at=when))
    _db.session.commit()
    return enforce_user_spend_cap(user.id, CONFIG, now=when,
                                  send_email=lambda **kw: None)


def _at_the_ceiling(n=1, usd=60.0, **kw):
    """A subscriber who used *usd* of the $50 in the period that ended."""
    u = _subscriber(n, **kw)
    _use(u, usd, JAN_LATE)
    assert user_is_capped(u, now=AN_HOUR_AFTER)
    return u


def _event(stripe, user):
    """Stripe's event of a change to the subscription, as the webhook
    applies it."""
    outcome = stripe_billing.handle_event(
        stripe, {"type": "customer.subscription.updated",
                 "data": {"object": {"id": user.stripe_subscription_id}}},
        CONFIG)
    _db.session.commit()
    return outcome


# ── The event of the renewal is late ─────────────────────────────────────

def test_the_next_period_starts_without_its_event(app, stripe):
    u = _at_the_ceiling()
    stripe.subs["sub_1"] = _stripe_sub(u, PERIOD_END, NEXT_END)

    result = reread_ended_periods(CONFIG, now=AN_HOUR_AFTER)

    assert result["status"] == "ok"
    assert (result["due"], result["asked"], result["changed"]) == (1, 1, 1)
    assert stripe.reads == ["sub_1"]
    assert u.subscription_period_start == PERIOD_END
    assert u.subscription_period_end == NEXT_END
    assert not user_is_capped(u, now=AN_HOUR_AFTER)
    # The new period's numbers: the $10 above the ceiling are in it.
    assert u.spend_period_start == PERIOD_END
    assert u.spend_period_carry_microdollars == 10_000_000
    assert u.spend_period_limit_usd == 50.0


def test_a_period_that_just_ended_is_left_to_its_event(app, stripe):
    u = _at_the_ceiling()
    stripe.subs["sub_1"] = _stripe_sub(u, PERIOD_END, NEXT_END)

    result = reread_ended_periods(
        CONFIG, now=PERIOD_END + timedelta(minutes=5))

    assert result["due"] == 0 and stripe.reads == []
    assert u.subscription_period_end == PERIOD_END


def test_a_period_still_running_is_not_read(app, stripe):
    _subscriber()
    result = reread_ended_periods(CONFIG, now=JAN_LATE)
    assert result["due"] == 0 and stripe.reads == []


def test_a_trial_that_ended_becomes_the_first_paid_period(app, stripe):
    trial_end = datetime(2027, 1, 24, 10, 0, 0)
    u = _subscriber(subscription_status="trialing",
                    subscription_ever_paid=False,
                    subscription_trial_start=datetime(2027, 1, 10, 10),
                    subscription_trial_end=trial_end,
                    subscription_period_start=datetime(2027, 1, 10, 10),
                    subscription_period_end=trial_end)
    sub = _stripe_sub(u, trial_end, datetime(2027, 2, 24, 10))
    sub["trial_start"] = _stamp(datetime(2027, 1, 10, 10))
    sub["trial_end"] = _stamp(trial_end)
    stripe.subs["sub_1"] = sub

    result = reread_ended_periods(
        CONFIG, now=trial_end + timedelta(hours=1))

    assert result["changed"] == 1
    assert u.subscription_status == "active"
    assert u.subscription_period_end == datetime(2027, 2, 24, 10)


# ── The event arrives after the job ──────────────────────────────────────

def test_the_event_after_the_job_carries_nothing_a_second_time(app, stripe):
    u = _at_the_ceiling()
    stripe.subs["sub_1"] = _stripe_sub(u, PERIOD_END, NEXT_END)
    reread_ended_periods(CONFIG, now=AN_HOUR_AFTER)

    assert _event(stripe, u) == "applied"

    assert u.spend_period_start == PERIOD_END
    assert u.spend_period_carry_microdollars == 10_000_000
    assert u.subscription_period_end == NEXT_END
    assert not user_is_capped(u, now=FEB_LATE)


def test_the_event_after_the_job_does_not_lift_the_new_periods_block(
        app, stripe):
    u = _at_the_ceiling()
    stripe.subs["sub_1"] = _stripe_sub(u, PERIOD_END, NEXT_END)
    reread_ended_periods(CONFIG, now=AN_HOUR_AFTER)
    # $10 carried and $45 used: at the ceiling of the new period.
    r = _use(u, 45.0, FEB_LATE)
    assert r["status"] == "blocked"

    _event(stripe, u)

    assert user_is_capped(u, now=FEB_LATE)
    assert u.spend_period_carry_microdollars == 10_000_000


def test_the_job_after_the_event_asks_nothing(app, stripe):
    u = _at_the_ceiling()
    stripe.subs["sub_1"] = _stripe_sub(u, PERIOD_END, NEXT_END)
    _event(stripe, u)
    stripe.reads.clear()

    result = reread_ended_periods(CONFIG, now=AN_HOUR_AFTER)

    assert result["due"] == 0 and stripe.reads == []
    assert u.spend_period_carry_microdollars == 10_000_000


def test_an_event_applied_during_the_read_is_kept(app, stripe):
    """The job reads the renewed subscription; before it takes the
    account's lock, an event brings a later state (the same period at
    the higher level). The later state stays."""
    u = _at_the_ceiling()
    stripe.subs["sub_1"] = _stripe_sub(u, PERIOD_END, NEXT_END)

    def event_meanwhile(_sub_id):
        stripe.on_read = None
        stripe_billing.apply_subscription(
            _stripe_sub(u, PERIOD_END, NEXT_END, level="supporter"), CONFIG)
        _db.session.commit()
    stripe.on_read = event_meanwhile

    result = reread_ended_periods(CONFIG, now=AN_HOUR_AFTER)

    assert result["event_first"] == 1 and result["changed"] == 0
    assert u.subscription_level == "supporter"
    assert u.spend_period_limit_usd == 100.0
    assert u.spend_period_carry_microdollars == 10_000_000


# ── What Stripe says of the subscription ─────────────────────────────────

def test_a_subscription_still_in_the_old_period_changes_nothing(app, stripe):
    u = _at_the_ceiling()
    stripe.subs["sub_1"] = _stripe_sub(u, PAID, PERIOD_END)

    result = reread_ended_periods(CONFIG, now=AN_HOUR_AFTER)

    assert result["unchanged"] == 1 and result["changed"] == 0
    assert user_is_capped(u, now=AN_HOUR_AFTER)
    assert u.spend_period_carry_microdollars == 0
    # Asked again the next hour, once.
    reread_ended_periods(CONFIG, now=TWO_HOURS_AFTER)
    assert stripe.reads == ["sub_1", "sub_1"]


def test_a_cancelled_subscription_is_recorded_as_ended(app, stripe):
    u = _at_the_ceiling(subscription_cancel_at_period_end=True)
    stripe.subs["sub_1"] = _stripe_sub(u, PAID, PERIOD_END,
                                       status="canceled")

    result = reread_ended_periods(CONFIG, now=AN_HOUR_AFTER)

    assert result["changed"] == 1
    assert u.subscription_status == "ended"
    assert not u.subscription_grants_access
    assert not u.subscription_cancel_at_period_end
    # The period it had and its count stay, for a later return.
    assert u.spend_period_start == PAID
    # Never asked again.
    reread_ended_periods(CONFIG, now=TWO_HOURS_AFTER)
    assert stripe.reads == ["sub_1"]


def test_a_late_payment_is_recorded_and_the_carry_waits(app, stripe):
    u = _at_the_ceiling()
    stripe.subs["sub_1"] = _stripe_sub(u, PERIOD_END, NEXT_END,
                                       status="past_due")

    result = reread_ended_periods(CONFIG, now=AN_HOUR_AFTER)

    assert result["changed"] == 1
    assert u.subscription_status == "past_due"
    assert not u.subscription_grants_access
    assert u.spend_period_start == PAID
    assert u.spend_period_carry_microdollars == 0
    # Its stored period end is ahead now: not asked the next hour.
    reread_ended_periods(CONFIG, now=TWO_HOURS_AFTER)
    assert stripe.reads == ["sub_1"]

    # Paid later: the event takes the carry on, once.
    stripe.subs["sub_1"] = _stripe_sub(u, PERIOD_END, NEXT_END)
    _event(stripe, u)
    assert u.subscription_status == "active"
    assert u.spend_period_start == PERIOD_END
    assert u.spend_period_carry_microdollars == 10_000_000
    assert not user_is_capped(u, now=FEB_LATE)


def test_a_late_payment_whose_period_ended_is_read_again(app, stripe):
    u = _subscriber(subscription_status="past_due")
    stripe.subs["sub_1"] = _stripe_sub(u, PAID, PERIOD_END,
                                       status="canceled")

    reread_ended_periods(CONFIG, now=AN_HOUR_AFTER)

    assert u.subscription_status == "ended"


def test_a_lower_level_that_arrives_with_the_renewal(app, stripe):
    """Supporter used $57 of $100; the change to Standard waited for the
    period's end. Nothing is carried into the Standard period."""
    u = _subscriber(level="supporter")
    _use(u, 57.0, JAN_LATE)
    stripe.subs["sub_1"] = _stripe_sub(u, PERIOD_END, NEXT_END,
                                       level="standard")

    reread_ended_periods(CONFIG, now=AN_HOUR_AFTER)

    assert u.subscription_level == "standard"
    assert u.spend_period_carry_microdollars == 0
    assert u.spend_period_limit_usd == 50.0


def test_an_admins_limit_stays_the_ceiling(app, stripe):
    u = _subscriber(monthly_spend_limit_usd=5.0)
    stripe.subs["sub_1"] = _stripe_sub(u, PERIOD_END, NEXT_END)

    reread_ended_periods(CONFIG, now=AN_HOUR_AFTER)

    assert u.subscription_period_end == NEXT_END
    assert u.monthly_spend_limit_usd == 5.0
    assert u.spend_period_start is None


def test_a_subscription_stripe_does_not_have_changes_nothing(app, stripe):
    u1 = _at_the_ceiling(1)
    u2 = _at_the_ceiling(2, subscription_period_end=PERIOD_END
                         + timedelta(minutes=1))
    stripe.subs["sub_2"] = _stripe_sub(u2, PERIOD_END, NEXT_END)

    result = reread_ended_periods(CONFIG, now=AN_HOUR_AFTER)

    assert result["status"] == "ok"
    assert result["unknown"] == 1 and result["changed"] == 1
    assert u1.subscription_status == "active"
    assert u1.subscription_period_end == PERIOD_END
    assert user_is_capped(u1, now=AN_HOUR_AFTER)
    assert not user_is_capped(u2, now=AN_HOUR_AFTER)


def test_a_subscription_of_another_customer_changes_nothing(app, stripe):
    u = _at_the_ceiling()
    sub = _stripe_sub(u, PERIOD_END, NEXT_END)
    sub["customer"] = "cus_other"
    sub["metadata"] = {}
    stripe.subs["sub_1"] = sub

    result = reread_ended_periods(CONFIG, now=AN_HOUR_AFTER)

    assert result["unknown"] == 1
    assert u.subscription_period_end == PERIOD_END


# ── Which accounts, and how many ─────────────────────────────────────────

def test_accounts_without_a_running_subscription_are_not_read(app, stripe):
    _subscriber(1, subscription_status="ended")
    _subscriber(2, subscription_status="none")
    _subscriber(3, stripe_subscription_id=None)
    _subscriber(4, subscription_period_end=None)

    result = reread_ended_periods(CONFIG, now=AN_HOUR_AFTER)

    assert result["due"] == 0 and stripe.reads == []


def test_a_run_reads_a_bounded_number_the_earliest_end_first(
        app, stripe, monkeypatch):
    monkeypatch.setattr(period_reread, "PERIOD_REREAD_MAX_PER_RUN", 2)
    ends = {1: PERIOD_END, 2: PERIOD_END - timedelta(days=2),
            3: PERIOD_END - timedelta(days=1)}
    users = {}
    for n, end in ends.items():
        users[n] = _subscriber(n, subscription_period_end=end)
        stripe.subs[f"sub_{n}"] = _stripe_sub(users[n], end, NEXT_END)

    result = reread_ended_periods(CONFIG, now=AN_HOUR_AFTER)

    assert stripe.reads == ["sub_2", "sub_3"]
    assert result["due"] == 2 and result["more_waiting"] is True
    assert users[1].subscription_period_end == PERIOD_END

    stripe.reads.clear()
    result = reread_ended_periods(CONFIG, now=TWO_HOURS_AFTER)
    assert stripe.reads == ["sub_1"]
    assert result["more_waiting"] is False


# ── Stripe cannot be asked; payments off ─────────────────────────────────

def test_the_run_ends_when_stripe_cannot_be_reached(app, stripe):
    u1 = _at_the_ceiling(1)
    u2 = _at_the_ceiling(2)
    for u in (u1, u2):
        stripe.subs[u.stripe_subscription_id] = _stripe_sub(
            u, PERIOD_END, NEXT_END)
    stripe.down = True

    result = reread_ended_periods(CONFIG, now=AN_HOUR_AFTER)

    assert result["status"] == "stripe_unreachable"
    assert stripe.reads == ["sub_1"]          # one read, not one each
    assert result["asked"] == 1 and result["changed"] == 0
    for u in (u1, u2):
        assert u.subscription_period_end == PERIOD_END
        assert user_is_capped(u, now=AN_HOUR_AFTER)

    # The next hour, Stripe answers.
    stripe.down = False
    result = reread_ended_periods(CONFIG, now=TWO_HOURS_AFTER)
    assert result["status"] == "ok" and result["changed"] == 2
    for u in (u1, u2):
        assert not user_is_capped(u, now=TWO_HOURS_AFTER)


def test_an_account_that_cannot_be_written_waits_and_the_run_goes_on(
        app, stripe, monkeypatch):
    u1 = _at_the_ceiling(1, subscription_period_end=PERIOD_END
                         - timedelta(minutes=1))
    u2 = _at_the_ceiling(2)
    for u in (u1, u2):
        stripe.subs[u.stripe_subscription_id] = _stripe_sub(
            u, PERIOD_END, NEXT_END)
    settle = stripe_billing.settle_subscription

    def failing_once(gateway, sub, config=None, lock=True):
        if sub["id"] == "sub_1":
            raise RuntimeError("stub: not written")
        return settle(gateway, sub, config, lock=lock)
    monkeypatch.setattr(stripe_billing, "settle_subscription", failing_once)

    result = reread_ended_periods(CONFIG, now=AN_HOUR_AFTER)

    assert result["failed"] == 1 and result["changed"] == 1
    assert u1.subscription_period_end < PERIOD_END
    assert u2.subscription_period_end == NEXT_END


def test_nothing_happens_while_payments_are_off(app, monkeypatch):
    u = _at_the_ceiling()

    def no_gateway(config=None):
        raise AssertionError("Stripe must not be asked")
    monkeypatch.setattr(stripe_billing, "get_gateway", no_gateway)

    result = reread_ended_periods(dict(CONFIG, PAYMENTS_ENABLED=False),
                                  now=AN_HOUR_AFTER)

    assert result == {"status": "off"}
    assert u.subscription_period_end == PERIOD_END


def test_without_a_stripe_key_nothing_is_read(app):
    u = _at_the_ceiling()
    result = reread_ended_periods(dict(CONFIG, STRIPE_SECRET_KEY=None),
                                  now=AN_HOUR_AFTER)
    assert result["status"] == "not_configured"
    assert u.subscription_period_end == PERIOD_END


def test_the_log_names_ids_and_states_only(app, stripe, caplog):
    u = _at_the_ceiling()
    u.email = "payer@example.com"
    _db.session.commit()
    stripe.subs["sub_1"] = _stripe_sub(u, PERIOD_END, NEXT_END)

    with caplog.at_level(logging.INFO, logger=period_reread.__name__):
        reread_ended_periods(CONFIG, now=AN_HOUR_AFTER)

    lines = [r.getMessage() for r in caplog.records
             if r.name == period_reread.__name__]
    assert lines == [f"Period read again: user {u.id}, subscription sub_1: "
                     "active -> active"]


# ── The schedule ─────────────────────────────────────────────────────────

BACKEND = Path(__file__).resolve().parents[1]


def test_the_job_runs_hourly_at_a_fixed_minute_on_the_background_queue():
    name = "backend.tasks.spend_monitor.reread_periods"
    assert celery_queues.TASK_QUEUES[name] == celery_queues.BACKGROUND_QUEUE
    tree = ast.parse((BACKEND / "celery_app.py").read_text())
    entry = None
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "beat_schedule":
            for k, v in zip(node.value.keys, node.value.values):
                if k.value == "reread-ended-periods":
                    entry = dict(zip([kk.value for kk in v.keys], v.values))
    assert entry is not None
    assert entry["task"].value == name
    assert entry["schedule"].func.attr == "crontab"
    assert {k.arg: ast.literal_eval(k.value)
            for k in entry["schedule"].keywords} == {"minute": 37}
    assert f"name='{name}'" in (
        BACKEND / "tasks" / "spend_monitor.py").read_text()

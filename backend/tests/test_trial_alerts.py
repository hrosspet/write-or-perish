"""The daily count of trials started and the admin's email.

More than OUTSIDE_TRIALS_EMAIL_PER_DAY accounts that were not prepared in
advance started a trial in 24 hours: one email to the admin, at most once
per UTC day. At or below the number, nothing. An account prepared in
advance is never counted. The email stops nothing: every trial stays as
it is. (The waiting list from the higher number: test_trial_waiting_list.)
"""
import os
import sys
from datetime import datetime, timedelta
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
from backend.models import TrialAlert, User  # noqa: E402
from backend.utils.trial_alerts import (  # noqa: E402
    OUTSIDE_TRIALS_EMAIL_PER_DAY, OUTSIDE_TRIALS_HOLD_PER_DAY,
    check_trials_started, outside_places_since,
    outside_trials_started_since,
)

TRIALS_STARTED_ALERT = OUTSIDE_TRIALS_EMAIL_PER_DAY

NOW = datetime(2027, 1, 20, 12, 0, 0)
CONFIG = {
    "PAYMENTS_ENABLED": True,
    "SUBSCRIPTION_TRIAL_SPEND_LIMIT_USD": 30.0,
    "SPEND_ALERT_EMAIL": "alerts@example.com",
}


@pytest.fixture
def app():
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["TESTING"] = True
    _db.init_app(app)
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.rollback()
        _db.drop_all()


def _trials(count, started=NOW - timedelta(hours=2), prefix="t", **kw):
    for i in range(count):
        _db.session.add(User(
            username=f"{prefix}{i}", plan="free",
            subscription_level="standard", subscription_status="trialing",
            subscription_trial_start=started, **kw))
    _db.session.commit()


def _mailbox():
    emails = []

    def send(**kw):
        emails.append(kw)
    return emails, send


def test_many_trials_in_a_day_email_the_admin_once(app):
    _trials(TRIALS_STARTED_ALERT + 1)
    emails, send = _mailbox()

    result = check_trials_started(CONFIG, now=NOW, send_email=send)

    assert result["alerted"] is True
    assert result["trials"] == TRIALS_STARTED_ALERT + 1
    assert emails == [{
        "to_email": "alerts@example.com",
        "trials": TRIALS_STARTED_ALERT + 1,
        "alert_above": TRIALS_STARTED_ALERT,
        "places": TRIALS_STARTED_ALERT + 1,
        "hold_from": OUTSIDE_TRIALS_HOLD_PER_DAY,
        "trial_limit_usd": 30.0,
    }]
    # Every trial is as it was.
    assert User.query.filter_by(
        subscription_status="trialing").count() == TRIALS_STARTED_ALERT + 1

    # The next runs of the same day: no second email, also with more.
    _trials(5, prefix="later")
    result = check_trials_started(
        CONFIG, now=NOW + timedelta(hours=3), send_email=send)
    assert result["status"] == "already_alerted"
    assert result["alerted"] is False
    assert len(emails) == 1
    assert TrialAlert.query.count() == 1


def test_the_next_day_emails_again_while_the_count_stays_high(app):
    _trials(TRIALS_STARTED_ALERT + 1, started=NOW - timedelta(hours=1))
    emails, send = _mailbox()
    check_trials_started(CONFIG, now=NOW, send_email=send)
    # 13 hours later it is the next UTC day and the trials are still
    # inside the last 24 hours.
    result = check_trials_started(
        CONFIG, now=NOW + timedelta(hours=13), send_email=send)
    assert result["alerted"] is True
    assert len(emails) == 2
    assert sorted(a.day for a in TrialAlert.query.all()) == [
        "2027-01-20", "2027-01-21"]


def test_the_usual_number_of_trials_sends_nothing(app):
    _trials(TRIALS_STARTED_ALERT)
    emails, send = _mailbox()
    result = check_trials_started(CONFIG, now=NOW, send_email=send)
    assert result == {"status": "ok", "trials": TRIALS_STARTED_ALERT,
                      "alert_above": TRIALS_STARTED_ALERT, "alerted": False}
    assert emails == []
    assert TrialAlert.query.count() == 0


def test_only_the_last_24_hours_count(app):
    _trials(TRIALS_STARTED_ALERT, started=NOW - timedelta(hours=30),
            prefix="old")
    _trials(TRIALS_STARTED_ALERT, started=NOW - timedelta(hours=5),
            prefix="new")
    assert outside_trials_started_since(NOW - timedelta(hours=24)) == \
        TRIALS_STARTED_ALERT
    emails, send = _mailbox()
    assert check_trials_started(
        CONFIG, now=NOW, send_email=send)["alerted"] is False
    assert emails == []


def test_the_alpha_testers_free_month_is_not_counted(app):
    _trials(TRIALS_STARTED_ALERT + 10, prefix="alpha",
            alpha_free_granted_at=NOW - timedelta(days=3))
    emails, send = _mailbox()
    result = check_trials_started(CONFIG, now=NOW, send_email=send)
    assert result["trials"] == 0 and emails == []


def test_a_failed_email_is_tried_again_at_the_next_run(app):
    _trials(TRIALS_STARTED_ALERT + 1)
    emails, send = _mailbox()

    def failing(**kw):
        raise RuntimeError("mail server down")

    result = check_trials_started(CONFIG, now=NOW, send_email=failing)
    assert result["status"] == "email_failed" and result["alerted"] is False
    assert TrialAlert.query.count() == 0
    result = check_trials_started(
        CONFIG, now=NOW + timedelta(hours=1), send_email=send)
    assert result["alerted"] is True and len(emails) == 1


def test_nothing_is_counted_while_payments_are_off(app):
    _trials(TRIALS_STARTED_ALERT + 1)
    emails, send = _mailbox()
    result = check_trials_started(
        dict(CONFIG, PAYMENTS_ENABLED=False), now=NOW, send_email=send)
    assert result == {"status": "disabled"}
    assert emails == []


def test_the_email_states_the_numbers(app, monkeypatch):
    from backend.utils import email as email_mod
    sent = []
    monkeypatch.setattr(
        email_mod, "_send",
        lambda to, subject, text, html, *a, **kw: sent.append(
            (to, subject, text, html)))
    email_mod.send_trials_started_email(
        to_email="alerts@example.com", trials=12, alert_above=10,
        trial_limit_usd=30.0, places=15, hold_from=20)
    (to, subject, text, html), = sent
    assert to == "alerts@example.com"
    assert "12 trials" in subject
    assert ("Trials started in the last 24 hours by accounts that were not "
            "prepared in advance: 12") in text
    assert "This email is sent above: 10" in text
    assert "$30.00" in text
    assert ("New accounts let in or on a trial in the last 24 hours: 15. "
            "From 20, a new account that was not prepared in advance waits "
            "for your approval") in text
    assert "12" in html and "From 20" in html


def test_an_account_prepared_in_advance_is_not_counted(app):
    # Pre-filled now (both fields), pre-filled before the date was kept
    # (the handle only), and loaded again after its content was deleted
    # (the date only).
    _trials(TRIALS_STARTED_ALERT + 3, prefix="ca", prefilled_handle="h",
            prefilled_at=NOW - timedelta(days=9))
    _trials(4, prefix="early", prefilled_handle="h")
    _trials(4, prefix="again", prefilled_at=NOW - timedelta(days=2))
    _trials(2, prefix="outside")
    since = NOW - timedelta(hours=24)
    assert outside_trials_started_since(since) == 2
    assert outside_places_since(since) == 2
    emails, send = _mailbox()
    result = check_trials_started(CONFIG, now=NOW, send_email=send)
    assert result["trials"] == 2 and result["alerted"] is False
    assert emails == []


def test_the_number_comes_from_the_settings(app):
    _trials(3)
    emails, send = _mailbox()
    result = check_trials_started(
        dict(CONFIG, OUTSIDE_TRIALS_EMAIL_PER_DAY=2), now=NOW,
        send_email=send)
    assert result["alerted"] is True and result["alert_above"] == 2
    assert emails[0]["alert_above"] == 2


def test_the_number_zero_sends_nothing(app):
    _trials(TRIALS_STARTED_ALERT + 1)
    emails, send = _mailbox()
    result = check_trials_started(
        dict(CONFIG, OUTSIDE_TRIALS_EMAIL_PER_DAY=0), now=NOW,
        send_email=send)
    assert result == {"status": "off"} and emails == []


def test_the_default_numbers(app):
    from backend.config import Config
    assert (Config.OUTSIDE_TRIALS_EMAIL_PER_DAY,
            Config.OUTSIDE_TRIALS_HOLD_PER_DAY) == (10, 20)
    assert (OUTSIDE_TRIALS_EMAIL_PER_DAY, OUTSIDE_TRIALS_HOLD_PER_DAY) \
        == (10, 20)


def test_places_count_trials_and_accounts_let_in_without_one(app):
    since = NOW - timedelta(hours=24)
    _trials(3, prefix="trial")
    # Let in by the gate in the window, no trial yet: a place each.
    for i in range(2):
        _db.session.add(User(username=f"in{i}", plan="free", approved=True,
                             let_in_at=NOW - timedelta(hours=1)))
    # Let in two days ago, approved by the admin (no let_in_at), and one
    # the admin turned off since: no place.
    _db.session.add(User(username="older", plan="free", approved=True,
                         let_in_at=NOW - timedelta(hours=50)))
    _db.session.add(User(username="byhand", plan="free", approved=True))
    _db.session.add(User(username="off", plan="free", approved=False,
                         let_in_at=NOW - timedelta(hours=1),
                         deactivated_at=NOW))
    _db.session.commit()
    assert outside_trials_started_since(since) == 3
    assert outside_places_since(since) == 5

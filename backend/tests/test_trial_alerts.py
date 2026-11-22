"""The daily count of trials started and the admin's email.

More than TRIALS_STARTED_ALERT trials started in 24 hours: one email to
the admin, at most once per UTC day. At or below the number, nothing.
Nothing is stopped: every trial stays as it is.
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
    TRIALS_STARTED_ALERT, check_trials_started, trials_started_since,
)

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
    assert trials_started_since(NOW - timedelta(hours=24)) == \
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
        to_email="alerts@example.com", trials=31, alert_above=25,
        trial_limit_usd=30.0)
    (to, subject, text, html), = sent
    assert to == "alerts@example.com"
    assert "31 trials" in subject
    assert "31" in text and "25" in text and "$30.00" in text
    assert "31" in html

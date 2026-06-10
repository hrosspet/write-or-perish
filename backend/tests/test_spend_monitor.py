"""Tests for API spend monitoring (issue #85).

Covers month-to-date aggregation with provider attribution, threshold
parsing, alert firing + per-month dedupe, email-failure retry semantics,
and the disabled-by-default behavior.

Patterned after test_tts_invalidation.py: sqlite in-memory, minimal
Flask app, no celery import (logic lives in backend/utils/spend.py).
"""
import os
import sys
from datetime import datetime
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
from backend.models import User, APICostLog, SpendAlert  # noqa: E402
from backend.utils.spend import (  # noqa: E402
    check_and_alert, get_month_spend_microdollars, parse_thresholds,
)

SUPPORTED_MODELS = {
    "claude-opus-4.6": {"provider": "anthropic"},
    "gpt-5.5": {"provider": "openai"},
}


def _base_config(limit=100.0, thresholds="0.5,0.8,0.95"):
    return {
        "SUPPORTED_MODELS": SUPPORTED_MODELS,
        "ANTHROPIC_SPEND_LIMIT_USD": limit,
        "SPEND_ALERT_THRESHOLDS": thresholds,
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
        user = User(username="tester")
        _db.session.add(user)
        _db.session.commit()
        yield app
        _db.session.rollback()
        _db.drop_all()


def _log_cost(user_id, model_id, usd, created_at=None):
    row = APICostLog(
        user_id=user_id,
        model_id=model_id,
        request_type="llm",
        cost_microdollars=int(usd * 1_000_000),
    )
    if created_at is not None:
        row.created_at = created_at
    _db.session.add(row)
    _db.session.commit()
    return row


NOW = datetime(2026, 6, 10, 3, 0, 0)


def test_parse_thresholds():
    assert parse_thresholds("0.5,0.8,0.95") == [0.5, 0.8, 0.95]
    assert parse_thresholds(" 0.8 , bogus, 1.5, 0.5 ") == [0.5, 0.8]
    assert parse_thresholds("") == []
    assert parse_thresholds(None) == []


def test_month_spend_provider_attribution(app):
    with app.app_context():
        user_id = User.query.first().id
        _log_cost(user_id, "claude-opus-4.6", 10.0, NOW)
        _log_cost(user_id, "claude-fable-5", 5.0, NOW)  # prefix fallback
        _log_cost(user_id, "gpt-5.5", 3.0, NOW)
        _log_cost(user_id, "gpt-4o-transcribe", 1.0, NOW)
        # Previous month — excluded.
        _log_cost(user_id, "claude-opus-4.6", 99.0, datetime(2026, 5, 31))

        config = _base_config()
        total = get_month_spend_microdollars(config, now=NOW)
        anthropic = get_month_spend_microdollars(
            config, provider="anthropic", now=NOW)
        openai = get_month_spend_microdollars(
            config, provider="openai", now=NOW)

        assert total == 19_000_000
        assert anthropic == 15_000_000
        assert openai == 4_000_000


def test_disabled_when_no_limit(app):
    with app.app_context():
        result = check_and_alert(_base_config(limit=0), now=NOW)
        assert result == {"status": "disabled"}


def test_alerts_fire_once_per_month(app):
    with app.app_context():
        user_id = User.query.first().id
        _log_cost(user_id, "claude-opus-4.6", 85.0, NOW)  # 85% of $100

        sent = []

        def fake_send(**kwargs):
            sent.append(kwargs)

        config = _base_config()
        result = check_and_alert(config, now=NOW, send_email=fake_send)
        # 0.5 and 0.8 crossed; 0.95 not.
        assert result["fired"] == [0.5, 0.8]
        assert len(sent) == 2
        assert sent[0]["to_email"] == "alerts@example.com"
        assert sent[0]["limit_usd"] == 100.0

        # Second run: nothing new fires.
        result2 = check_and_alert(config, now=NOW, send_email=fake_send)
        assert result2["fired"] == []
        assert len(sent) == 2

        # Spend grows past 95% → only the new threshold fires.
        _log_cost(user_id, "claude-opus-4.6", 11.0, NOW)
        result3 = check_and_alert(config, now=NOW, send_email=fake_send)
        assert result3["fired"] == [0.95]
        assert len(sent) == 3

        assert SpendAlert.query.count() == 3


def test_openai_spend_does_not_trigger_anthropic_alerts(app):
    with app.app_context():
        user_id = User.query.first().id
        _log_cost(user_id, "gpt-5.5", 95.0, NOW)

        sent = []
        result = check_and_alert(
            _base_config(), now=NOW,
            send_email=lambda **kw: sent.append(kw))
        assert result["fired"] == []
        assert sent == []


def test_email_failure_retries_next_run(app):
    with app.app_context():
        user_id = User.query.first().id
        _log_cost(user_id, "claude-opus-4.6", 60.0, NOW)

        def broken_send(**kwargs):
            raise RuntimeError("smtp down")

        config = _base_config()
        result = check_and_alert(config, now=NOW, send_email=broken_send)
        assert result["fired"] == []
        assert SpendAlert.query.count() == 0

        # SMTP recovers → alert goes out on the next run.
        sent = []
        result2 = check_and_alert(
            config, now=NOW, send_email=lambda **kw: sent.append(kw))
        assert result2["fired"] == [0.5]
        assert len(sent) == 1

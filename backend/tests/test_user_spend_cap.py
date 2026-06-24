"""Tests for the per-user monthly spend hard cap (issue #85 follow-up).

Covers per-user month-to-date aggregation (all providers), the cheap
flag-based gate with month-rollover auto-reset, the enforce/block/email
path with within-month idempotency, the disabled-by-default behavior, and
the route decorator's 402 response.

Patterned after test_spend_monitor.py: sqlite in-memory, minimal Flask app,
logic tested directly (no celery import).
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
from backend.models import User, APICostLog  # noqa: E402
from backend.utils.spend import (  # noqa: E402
    current_month, enforce_user_spend_cap,
    get_user_month_spend_microdollars, get_user_spend_limit_usd,
    reconcile_user_spend_block, require_spend_headroom, user_is_capped,
)

NOW = datetime(2026, 6, 10, 3, 0, 0)
PREV_MONTH = datetime(2026, 5, 20, 3, 0, 0)


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
    _db.init_app(app)
    with app.app_context():
        _db.create_all()
        _db.session.add(User(username="tester"))
        _db.session.add(User(username="other"))
        _db.session.commit()
        yield app
        _db.session.rollback()
        _db.drop_all()


def _log_cost(user_id, model_id, usd, created_at=NOW):
    row = APICostLog(
        user_id=user_id,
        model_id=model_id,
        request_type="llm",
        cost_microdollars=int(usd * 1_000_000),
    )
    row.created_at = created_at
    _db.session.add(row)
    _db.session.commit()
    return row


def test_user_month_spend_all_providers_scoped(app):
    u = User.query.filter_by(username="tester").first()
    other = User.query.filter_by(username="other").first()
    _log_cost(u.id, "claude-opus-4.6", 10.0)        # anthropic, this month
    _log_cost(u.id, "gpt-5.5", 5.0)                  # openai, this month
    _log_cost(u.id, "claude-opus-4.6", 99.0, PREV_MONTH)  # last month, excluded
    _log_cost(other.id, "gpt-5.5", 42.0)             # other user, excluded
    total = get_user_month_spend_microdollars(u.id, now=NOW)
    assert total == int(15.0 * 1_000_000)            # 10 + 5, both providers


def test_enforce_disabled_when_limit_zero(app):
    u = User.query.filter_by(username="tester").first()
    _log_cost(u.id, "claude-opus-4.6", 999.0)
    send = MagicMock()
    result = enforce_user_spend_cap(u.id, _config(limit=0), now=NOW,
                                    send_email=send)
    assert result["status"] == "disabled"
    send.assert_not_called()
    assert u.spend_blocked_month is None


def test_enforce_under_cap_does_not_block(app):
    u = User.query.filter_by(username="tester").first()
    _log_cost(u.id, "claude-opus-4.6", 30.0)
    send = MagicMock()
    result = enforce_user_spend_cap(u.id, _config(limit=50), now=NOW,
                                    send_email=send)
    assert result["status"] == "ok"
    assert result["blocked"] is False
    send.assert_not_called()
    assert u.spend_blocked_month is None


def test_enforce_blocks_and_emails_once(app):
    u = User.query.filter_by(username="tester").first()
    _log_cost(u.id, "claude-opus-4.6", 40.0)
    _log_cost(u.id, "gpt-5.5", 12.0)   # total $52 >= $50
    send = MagicMock()
    result = enforce_user_spend_cap(u.id, _config(limit=50), now=NOW,
                                    send_email=send)
    assert result["status"] == "blocked"
    assert result["blocked"] is True
    assert u.spend_blocked_month == current_month(NOW)
    send.assert_called_once()
    kwargs = send.call_args.kwargs
    assert kwargs["to_email"] == "alerts@example.com"
    assert kwargs["username"] == "tester"
    assert kwargs["limit_usd"] == 50
    assert round(kwargs["spend_usd"], 2) == 52.0

    # Idempotent within the month: a second run does not re-email.
    send2 = MagicMock()
    again = enforce_user_spend_cap(u.id, _config(limit=50), now=NOW,
                                   send_email=send2)
    assert again["status"] == "already_blocked"
    send2.assert_not_called()


def test_user_is_capped_flag_and_month_rollover(app):
    u = User.query.filter_by(username="tester").first()
    assert user_is_capped(u, now=NOW) is False
    u.spend_blocked_month = current_month(NOW)
    _db.session.commit()
    assert user_is_capped(u, now=NOW) is True
    # Next month the same stored value no longer matches → auto-unblocked.
    next_month = datetime(2026, 7, 1, 0, 0, 0)
    assert user_is_capped(u, now=next_month) is False
    # Accepts a bare id too.
    assert user_is_capped(u.id, now=NOW) is True


def test_create_llm_placeholder_blocked_when_capped(app):
    from backend.models import Node
    from backend.utils.llm_nodes import create_llm_placeholder
    from backend.utils.spend import SpendCapExceeded, current_month
    u = User.query.filter_by(username="tester").first()
    parent = Node(user_id=u.id, human_owner_id=u.id, node_type="user")
    _db.session.add(parent)
    _db.session.commit()
    u.spend_blocked_month = current_month()
    _db.session.commit()
    before = Node.query.count()
    with pytest.raises(SpendCapExceeded):
        create_llm_placeholder(parent.id, "claude-opus-4.6", u.id, enqueue=False)
    # No orphan placeholder node created.
    assert Node.query.count() == before


def test_get_user_spend_limit_override_wins(app):
    u = User.query.filter_by(username="tester").first()
    cfg = _config(limit=50)
    # NULL override → global default.
    assert get_user_spend_limit_usd(u, cfg) == 50
    # Stored override wins…
    u.monthly_spend_limit_usd = 100.0
    assert get_user_spend_limit_usd(u, cfg) == 100.0
    # …including a stored 0 ("uncapped"), which is distinct from NULL.
    u.monthly_spend_limit_usd = 0.0
    assert get_user_spend_limit_usd(u, cfg) == 0.0


def test_enforce_respects_per_user_override(app):
    u = User.query.filter_by(username="tester").first()
    _log_cost(u.id, "claude-opus-4.6", 52.0)   # over the $50 default…
    u.monthly_spend_limit_usd = 100.0          # …but under this user's override
    _db.session.commit()
    send = MagicMock()
    result = enforce_user_spend_cap(u.id, _config(limit=50), now=NOW,
                                    send_email=send)
    assert result["status"] == "ok"
    assert result["blocked"] is False
    assert result["limit_usd"] == 100.0
    send.assert_not_called()
    assert u.spend_blocked_month is None


def test_reconcile_raise_limit_unblocks(app):
    u = User.query.filter_by(username="tester").first()
    _log_cost(u.id, "claude-opus-4.6", 52.0)
    u.spend_blocked_month = current_month(NOW)   # already blocked at $50 default
    _db.session.commit()
    # Admin raises the cap above their spend → unblocked immediately.
    u.monthly_spend_limit_usd = 100.0
    state = reconcile_user_spend_block(u, _config(limit=50), now=NOW)
    assert state["blocked"] is False
    assert u.spend_blocked_month is None
    assert user_is_capped(u, now=NOW) is False


def test_reconcile_lower_limit_blocks(app):
    u = User.query.filter_by(username="tester").first()
    _log_cost(u.id, "claude-opus-4.6", 30.0)     # under the $50 default → unblocked
    assert u.spend_blocked_month is None
    # Admin lowers the cap below their month-to-date spend → blocked.
    u.monthly_spend_limit_usd = 20.0
    state = reconcile_user_spend_block(u, _config(limit=50), now=NOW)
    assert state["blocked"] is True
    assert u.spend_blocked_month == current_month(NOW)
    assert user_is_capped(u, now=NOW) is True


def test_reconcile_zero_limit_is_uncapped(app):
    u = User.query.filter_by(username="tester").first()
    _log_cost(u.id, "claude-opus-4.6", 999.0)
    u.spend_blocked_month = current_month(NOW)
    u.monthly_spend_limit_usd = 0.0              # 0 = uncapped for this user
    state = reconcile_user_spend_block(u, _config(limit=50), now=NOW)
    assert state["blocked"] is False
    assert u.spend_blocked_month is None


def test_require_spend_headroom_decorator(app, monkeypatch):
    from backend.utils import spend as spendmod

    @require_spend_headroom
    def view():
        return "ran"

    fake_user = MagicMock(is_authenticated=True)
    monkeypatch.setattr("flask_login.current_user", fake_user, raising=False)

    monkeypatch.setattr(spendmod, "user_is_capped", lambda u, now=None: True)
    with app.test_request_context():
        body, status = view()
        assert status == 402
        assert body.json["error"] == "monthly_spend_limit_reached"

    monkeypatch.setattr(spendmod, "user_is_capped", lambda u, now=None: False)
    with app.test_request_context():
        assert view() == "ran"

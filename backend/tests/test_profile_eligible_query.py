"""Regression test for User.profile_eligible_query (the user-selection
filter feeding the hourly profile / recent-context update tasks).

Bug: ``~User.twitter_id.like("llm-%")`` was meant to exclude only the
synthetic ``llm-<model>`` placeholder accounts, but in SQL
``NULL NOT LIKE x`` evaluates to NULL (not TRUE), so it silently dropped
every user whose ``twitter_id`` is NULL — i.e. all email / magic-link
signups. Those users never received automatic profile updates.

These tests pin the corrected behavior: NULL-twitter_id Voice-Mode users
are included; ``llm-`` placeholders and free-plan users are excluded.
"""

import os
import sys
from unittest.mock import MagicMock

# ── Environment (mirror the lightweight setup used by other model tests) ─
os.environ["ENCRYPTION_DISABLED"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("TWITTER_API_KEY", "fake")
os.environ.setdefault("TWITTER_API_SECRET", "fake")

# models.py doesn't import celery, but a sibling test may have stubbed it.
sys.modules.setdefault("celery", MagicMock())

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

# If a sibling test left backend.models/extensions mocked, drop the stubs
# so we import the real models.
for _mod in ["backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

from backend.extensions import db  # noqa: E402
from backend.models import User    # noqa: E402


@pytest.fixture
def app():
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    db.init_app(app)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


def _make_user(username, plan, twitter_id, approved=True,
               deactivated_at=None):
    u = User(username=username, plan=plan, twitter_id=twitter_id,
             approved=approved, deactivated_at=deactivated_at)
    db.session.add(u)
    db.session.flush()
    return u


def test_null_twitter_id_voice_user_is_included(app):
    """The core regression: an alpha user with no twitter_id (email
    signup) must be eligible — this is exactly what was being dropped."""
    email_user = _make_user("email_alpha", "alpha", None)
    db.session.commit()

    eligible = {u.id for u in User.profile_eligible_query().all()}
    assert email_user.id in eligible


def test_filter_includes_and_excludes_the_right_cohorts(app):
    email_alpha = _make_user("email_alpha", "alpha", None)      # NULL -> in
    email_pro = _make_user("email_pro", "pro", None)            # NULL -> in
    twitter_alpha = _make_user("tw_alpha", "alpha", "12345")    # real -> in
    llm_bot = _make_user("gpt-5", "alpha", "llm-gpt-5")         # bot  -> out
    free_email = _make_user("free_email", "free", None)         # free -> out
    db.session.commit()

    eligible = {u.id for u in User.profile_eligible_query().all()}

    assert email_alpha.id in eligible
    assert email_pro.id in eligible
    assert twitter_alpha.id in eligible
    assert llm_bot.id not in eligible      # placeholder bots stay excluded
    assert free_email.id not in eligible   # free plan is not Voice-Mode


def test_deactivated_and_unapproved_users_are_excluded(app):
    """Deactivated (approved=False, deactivated_at set) and never-approved
    users must NOT receive background profile generation — otherwise
    deactivating a user wouldn't stop LLM spend on their profile."""
    from datetime import datetime

    active = _make_user("active_alpha", "alpha", None, approved=True)
    deactivated = _make_user(
        "deactivated_alpha", "alpha", None,
        approved=False, deactivated_at=datetime(2026, 6, 1, 0, 0, 0),
    )
    never_approved = _make_user(
        "pending_alpha", "alpha", None, approved=False,
    )
    db.session.commit()

    eligible = {u.id for u in User.profile_eligible_query().all()}

    assert active.id in eligible
    assert deactivated.id not in eligible     # the user-44 hold-off case
    assert never_approved.id not in eligible


def test_global_ai_usage_gates_generation(app):
    """A user who set their global default_ai_usage to 'none' has opted out
    of AI, so automatic profile / recent-context generation must skip them
    (#191). 'chat' and 'train' users stay eligible."""
    chat_user = _make_user("chat_user", "alpha", None)        # default 'chat'
    train_user = _make_user("train_user", "alpha", None)
    train_user.default_ai_usage = "train"
    opted_out = _make_user("opted_out", "alpha", None)
    opted_out.default_ai_usage = "none"
    db.session.commit()

    eligible = {u.id for u in User.profile_eligible_query().all()}

    assert chat_user.id in eligible
    assert train_user.id in eligible          # train is still AI-readable
    assert opted_out.id not in eligible       # global opt-out excludes

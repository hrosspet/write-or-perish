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


def _make_user(username, plan, twitter_id):
    u = User(username=username, plan=plan, twitter_id=twitter_id,
             approved=True)
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

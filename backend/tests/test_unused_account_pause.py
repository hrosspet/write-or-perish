"""Accounts nobody uses (#647; Peter, 2026-11-12: "I'd say a month?"):
what counts as use (utils/activity.in_use_user_ids) and the setting.
The two jobs that pause are tested with their gates, in
test_bookmark_nightly_sync.py and test_external_digest_nightly.py."""
from datetime import datetime, timedelta

from backend.tests.test_admin_access import app, _db  # noqa: F401
from backend.models import ApiToken, User
from backend.utils import activity as act

NOW = datetime(2026, 11, 12, 12, 0, 0)
MONTH = {"UNUSED_ACCOUNT_PAUSE_DAYS": 30.0}


def _user(name, **kw):
    u = User(username=name, approved=True, **kw)
    _db.session.add(u)
    _db.session.commit()
    return u


def _token(user, last_used_at, n):
    _db.session.add(ApiToken(user_id=user.id, name="clipper",
                             token_hash=str(n) * 64, prefix=str(n) * 8,
                             last_used_at=last_used_at))
    _db.session.commit()


def test_use_within_the_month_counts_and_older_use_does_not(app):  # noqa: F811
    recent = _user("recent", last_seen_at=NOW - timedelta(days=29))
    away = _user("away", last_seen_at=NOW - timedelta(days=31))
    # A pre-filled account its owner has not signed into yet.
    never = _user("prefilled", prefilled_handle="prefilled")
    assert act.in_use_user_ids(
        [recent.id, away.id, never.id], MONTH, NOW) == {recent.id}


def test_a_clip_with_the_clipper_token_counts_as_use(app):  # noqa: F811
    """The clipper sends its token only when the owner clips a page, so
    its last use is the owner's; the app's last-seen does not see it."""
    clipper = _user("clipper", last_seen_at=NOW - timedelta(days=60))
    stale = _user("stale-token", last_seen_at=NOW - timedelta(days=60))
    _token(clipper, NOW - timedelta(days=2), 1)
    _token(stale, NOW - timedelta(days=40), 2)
    assert act.in_use_user_ids(
        [clipper.id, stale.id], MONTH, NOW) == {clipper.id}


def test_the_setting_sets_the_window_and_zero_turns_the_pause_off(
        app):  # noqa: F811
    u = _user("u", last_seen_at=NOW - timedelta(days=10))
    never = _user("never")
    assert act.in_use_user_ids(
        [u.id], {"UNUSED_ACCOUNT_PAUSE_DAYS": 7}, NOW) == set()
    # Unset: the default month.
    assert act.in_use_user_ids([u.id, never.id], {}, NOW) == {u.id}
    assert act.in_use_user_ids(
        [u.id, never.id], {"UNUSED_ACCOUNT_PAUSE_DAYS": 0}, NOW) == {
        u.id, never.id}


def test_the_default_setting_is_a_month():
    import os
    import pytest
    from backend.config import Config
    if os.environ.get("UNUSED_ACCOUNT_PAUSE_DAYS"):
        pytest.skip("UNUSED_ACCOUNT_PAUSE_DAYS is set in this environment")
    assert Config.UNUSED_ACCOUNT_PAUSE_DAYS == 30
    assert act.DEFAULT_UNUSED_PAUSE_DAYS == Config.UNUSED_ACCOUNT_PAUSE_DAYS

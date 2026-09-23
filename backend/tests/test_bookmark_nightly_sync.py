"""Tests for the nightly X bookmark refresh (#208).

Covers: revocation marking (dead refresh grant -> account parked, not
retried forever; transient errors re-raise), the revoked-account skip,
and the nightly fan-out (dispatches only connected, non-revoked accounts;
no-op without X_CLIENT_ID). Network + celery glue stubbed.
"""
import os
import sys
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
import requests  # noqa: E402
from flask import Flask  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

from backend.extensions import db as _db  # noqa: E402
from backend.models import (  # noqa: E402
    User, ExternalAccount, ExternalItem, APICostLog, UserNotification,
)


def _make_app():
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["X_CLIENT_ID"] = "client-id"
    _db.init_app(app)
    return app


# Import the REAL sync module against an identity-decorator celery stub
# and our test flask_app (mirrors test_retrieval_loop.py).
_app = _make_app()
_celery_stub = MagicMock()
_celery_stub.celery.task = lambda *a, **k: (lambda fn: fn)
_celery_stub.flask_app = _app


def _import_real_sync_module():
    import importlib
    glue = ("backend.celery_app", "backend.tasks.external_sync")
    saved = {k: sys.modules.get(k) for k in glue}
    sys.modules["backend.celery_app"] = _celery_stub
    sys.modules.pop("backend.tasks.external_sync", None)
    try:
        mod = importlib.import_module("backend.tasks.external_sync")
        if isinstance(mod, MagicMock):
            sys.modules.pop("backend.tasks.external_sync", None)
            mod = importlib.import_module("backend.tasks.external_sync")
        return mod
    finally:
        for _k, _v in saved.items():
            if _v is None:
                sys.modules.pop(_k, None)
            else:
                sys.modules[_k] = _v


_sync_mod = _import_real_sync_module()
assert not isinstance(_sync_mod, MagicMock)


class _FakeSelf:
    def update_state(self, *args, **kwargs):
        pass


@pytest.fixture
def app():
    saved_flask_app = _sync_mod.flask_app
    _sync_mod.flask_app = _app
    with _app.app_context():
        _db.create_all()
        user = User(username="tester")
        _db.session.add(user)
        _db.session.commit()
        yield _app
        _db.session.remove()
        _db.drop_all()
    _sync_mod.flask_app = saved_flask_app


def _mk_account(user_id, revoked=False, expired=True):
    from datetime import datetime, timedelta
    account = ExternalAccount(
        user_id=user_id, provider="twitter", external_user_id="42",
        handle="tester",
        # Expired token forces the refresh path when expired=True.
        token_expires_at=(datetime.utcnow() - timedelta(minutes=1)
                          if expired else
                          datetime.utcnow() + timedelta(hours=2)),
        revoked_at=(datetime.utcnow() if revoked else None),
    )
    account.set_tokens("access-token", "refresh-token")
    _db.session.add(account)
    _db.session.commit()
    return account


def _tz_at_hour(target_hour):
    """An IANA zone whose CURRENT local hour == target_hour, built from the
    fixed-offset Etc/GMT zones (POSIX-inverted signs: Etc/GMT+5 = UTC-5)."""
    from datetime import datetime
    x = (datetime.utcnow().hour - target_hour) % 24
    return f"Etc/GMT+{x}" if x <= 12 else f"Etc/GMT-{24 - x}"


def _http_error(status, error=None, description=None):
    """An HTTPError carrying X's OAuth2 error body. The sync tells a dead
    user grant from bad client credentials by that body, not by the
    status — see _grant_is_dead."""
    resp = MagicMock()
    resp.status_code = status
    body = {}
    if error:
        body["error"] = error
    if description:
        body["error_description"] = description
    resp.json.return_value = body
    err = requests.HTTPError(f"HTTP {status}")
    err.response = resp
    return err


# The two bodies X actually returns, probed live 2026-09-18 against
# api.twitter.com/2/oauth2/token with a bogus refresh token.
DEAD_TOKEN = ("invalid_request", "Value passed for the token was invalid.")
BAD_CLIENT = ("unauthorized_client", "Missing valid authorization header")


def test_dead_refresh_grant_marks_revoked(app, monkeypatch):
    uid = User.query.first().id
    account = _mk_account(uid)

    def dead_refresh(client_id, refresh_token, client_secret=None):
        raise _http_error(400, *DEAD_TOKEN)
    monkeypatch.setattr(_sync_mod, "x_refresh_access_token", dead_refresh)

    result = _sync_mod.sync_twitter_bookmarks(_FakeSelf(), uid)
    assert result["status"] == "revoked"
    _db.session.expire_all()
    assert ExternalAccount.query.get(account.id).revoked_at is not None


def test_revocation_notifies_user_with_reconnect_link(app, monkeypatch):
    """A parked account must not fail silently: the user gets one targeted
    notification (deduped per unread) pointing at the Import page's X card."""
    uid = User.query.first().id
    _mk_account(uid)

    def dead_refresh(client_id, refresh_token, client_secret=None):
        raise _http_error(401, *DEAD_TOKEN)  # reported at 401 as well
    monkeypatch.setattr(_sync_mod, "x_refresh_access_token", dead_refresh)

    _sync_mod.sync_twitter_bookmarks(_FakeSelf(), uid)
    notices = UserNotification.query.filter_by(
        user_id=uid, type="x_disconnected").all()
    assert len(notices) == 1
    assert notices[0].status == "unread"
    assert notices[0].link == "/import#x-bookmarks"
    assert "@tester" in notices[0].body

    # A second failing run (revoked accounts are skipped, but a stale
    # dispatch could still land) must not stack a second notice.
    ExternalAccount.query.filter_by(user_id=uid).one().revoked_at = None
    _db.session.commit()
    _sync_mod.sync_twitter_bookmarks(_FakeSelf(), uid)
    assert UserNotification.query.filter_by(
        user_id=uid, type="x_disconnected").count() == 1


def test_transient_refresh_error_does_not_revoke(app, monkeypatch):
    uid = User.query.first().id
    account = _mk_account(uid)

    def flaky_refresh(client_id, refresh_token, client_secret=None):
        raise _http_error(429)
    monkeypatch.setattr(_sync_mod, "x_refresh_access_token", flaky_refresh)

    with pytest.raises(requests.HTTPError):
        _sync_mod.sync_twitter_bookmarks(_FakeSelf(), uid)
    _db.session.expire_all()
    assert ExternalAccount.query.get(account.id).revoked_at is None


def test_bad_client_credentials_do_not_park_the_user(app, monkeypatch):
    """invalid_client is LOORE's problem, not the user's. Parking accounts
    for it disconnected every X user on a config bug and told them to
    reconnect, which could not help (#313) — it must raise instead, and
    leave the account connected for the next night."""
    uid = User.query.first().id
    account = _mk_account(uid)

    def bad_client(client_id, refresh_token, client_secret=None):
        raise _http_error(401, *BAD_CLIENT)
    monkeypatch.setattr(_sync_mod, "x_refresh_access_token", bad_client)

    with pytest.raises(requests.HTTPError):
        _sync_mod.sync_twitter_bookmarks(_FakeSelf(), uid)
    _db.session.expire_all()
    assert ExternalAccount.query.get(account.id).revoked_at is None
    assert UserNotification.query.filter_by(
        user_id=uid, type="x_disconnected").count() == 0


def test_malformed_refresh_request_does_not_park_the_user(app, monkeypatch):
    """invalid_request is also X's generic "your request was malformed".
    Only the token-is-invalid description means the USER's grant is dead;
    a request we built wrong is ours, and parking everyone for it is the
    #313 shape all over again."""
    uid = User.query.first().id
    account = _mk_account(uid)

    def malformed(client_id, refresh_token, client_secret=None):
        raise _http_error(400, "invalid_request",
                          "Missing required parameter: refresh_token")
    monkeypatch.setattr(_sync_mod, "x_refresh_access_token", malformed)

    with pytest.raises(requests.HTTPError):
        _sync_mod.sync_twitter_bookmarks(_FakeSelf(), uid)
    _db.session.expire_all()
    assert ExternalAccount.query.get(account.id).revoked_at is None


def test_unreadable_refresh_failure_does_not_park_the_user(app, monkeypatch):
    """A 400 with no OAuth body (an HTML error page from an edge, say) is
    not a statement about this user's grant — fail the sync, don't
    disconnect somebody on it."""
    uid = User.query.first().id
    account = _mk_account(uid)

    def html_error(client_id, refresh_token, client_secret=None):
        raise _http_error(400)
    monkeypatch.setattr(_sync_mod, "x_refresh_access_token", html_error)

    with pytest.raises(requests.HTTPError):
        _sync_mod.sync_twitter_bookmarks(_FakeSelf(), uid)
    _db.session.expire_all()
    assert ExternalAccount.query.get(account.id).revoked_at is None
    assert UserNotification.query.filter_by(
        user_id=uid, type="x_disconnected").count() == 0


def test_refresh_passes_the_client_secret(app, monkeypatch):
    """The refresh grant must carry the same client credentials as the
    code exchange — a confidential client that omits them gets 401."""
    uid = User.query.first().id
    _mk_account(uid)
    monkeypatch.setitem(app.config, "X_CLIENT_SECRET", "shh")
    seen = {}

    def ok_refresh(client_id, refresh_token, client_secret=None):
        seen["secret"] = client_secret
        return {"access_token": "fresh", "refresh_token": "rotated",
                "expires_in": 7200}
    monkeypatch.setattr(_sync_mod, "x_refresh_access_token", ok_refresh)

    def empty_pages(token, x_user_id, max_items=800):
        return
        yield  # pragma: no cover — makes this a generator
    monkeypatch.setattr(_sync_mod, "x_fetch_bookmark_pages", empty_pages)

    _sync_mod.sync_twitter_bookmarks(_FakeSelf(), uid)
    assert seen["secret"] == "shh"
    _db.session.expire_all()
    assert ExternalAccount.query.filter_by(
        user_id=uid).one().get_access_token() == "fresh"


def test_unpark_script_repairs_accounts_parked_by_the_bug(app):
    """The #313 repair clears revoked_at and answers the stale "X
    disconnected" notice — but only where a refresh token survived; an
    account without one genuinely has to reconnect."""
    import io
    from datetime import datetime
    from backend.scripts.unpark_x_accounts import _run
    uid = User.query.first().id
    account = _mk_account(uid, revoked=True)
    _db.session.add(UserNotification(
        user_id=uid, type="x_disconnected", title="X disconnected",
        link="/import#x-bookmarks"))
    _db.session.commit()

    assert _run(False, out=io.StringIO())["unparked"] == 1
    _db.session.expire_all()
    assert ExternalAccount.query.get(account.id).revoked_at is not None

    result = _run(True, out=io.StringIO())
    assert result["unparked"] == 1 and result["notices_read"] == 1
    _db.session.expire_all()
    assert ExternalAccount.query.get(account.id).revoked_at is None
    assert UserNotification.query.filter_by(
        user_id=uid, type="x_disconnected").one().status == "read"

    # Nothing left to refresh with: leave it parked.
    account = ExternalAccount.query.get(account.id)
    account.revoked_at = datetime.utcnow()
    account.refresh_token = None
    _db.session.commit()
    result = _run(True, out=io.StringIO())
    assert result["unparked"] == 0 and result["skipped"] == 1
    _db.session.expire_all()
    assert ExternalAccount.query.get(account.id).revoked_at is not None

    # --user-id scopes the repair to named accounts.
    account = ExternalAccount.query.get(account.id)
    account.set_tokens("access-token", "refresh-token")
    _db.session.commit()
    assert _run(True, user_ids=[uid + 999], out=io.StringIO())["unparked"] == 0
    _db.session.expire_all()
    assert ExternalAccount.query.get(account.id).revoked_at is not None
    assert _run(True, user_ids=[uid], out=io.StringIO())["unparked"] == 1


def test_fetch_401_marks_revoked(app, monkeypatch):
    uid = User.query.first().id
    account = _mk_account(uid, expired=False)  # skip the refresh path

    def dead_fetch(token, x_user_id, max_items=800):
        raise _http_error(401)
        yield  # pragma: no cover — makes this a generator
    monkeypatch.setattr(_sync_mod, "x_fetch_bookmark_pages", dead_fetch)

    result = _sync_mod.sync_twitter_bookmarks(_FakeSelf(), uid)
    assert result["status"] == "revoked"
    _db.session.expire_all()
    assert ExternalAccount.query.get(account.id).revoked_at is not None


def test_revoked_account_is_skipped(app):
    uid = User.query.first().id
    _mk_account(uid, revoked=True)
    result = _sync_mod.sync_twitter_bookmarks(_FakeSelf(), uid)
    assert result["status"] == "revoked"


def _tweet_row(user_id, external_id, source="twitter_bookmark",
               public_source=None, checked_at=None):
    row = ExternalItem(user_id=user_id, source=source, external_id=external_id,
                       public_source=public_source,
                       public_source_checked_at=checked_at)
    row.set_content("t")
    _db.session.add(row)
    _db.session.commit()
    return row


def test_public_source_sweep_asks_x_once_per_tweet_and_stamps_every_copy(
        app, monkeypatch):
    """Unknown tweet rows are settled by X's oEmbed answer: one lookup per
    distinct tweet id, the verdict on every user's copy; decided rows and
    pages are never asked, and an archive row from before the column is
    vouched in one statement rather than asked (#295)."""
    uid = User.query.first().id
    other = User(username="other")
    _db.session.add(other)
    _db.session.commit()
    a1 = _tweet_row(uid, "100")
    a2 = _tweet_row(other.id, "100")  # the same tweet, saved by two people
    b = _tweet_row(uid, "200")
    decided = _tweet_row(uid, "300", public_source=True)
    archive = _tweet_row(uid, "400", source="community_archive")  # not an X source
    page = _tweet_row(uid, "abc", source="web_clip", public_source=False)
    asked = []
    verdicts = {"100": (True, 200), "200": (False, 403)}
    monkeypatch.setattr(_sync_mod, "x_tweet_public_status",
                        lambda tid: asked.append(tid) or verdicts[tid])

    result = _sync_mod.run_public_source_sweep(pause=0)

    assert sorted(asked) == ["100", "200"]
    assert (result["lookups"], result["public"], result["refused"],
            result["stopped_on"]) == (2, 1, 1, None)
    assert result["archive_vouched"] == 1
    for row in (a1, a2, b, decided, archive, page):
        _db.session.refresh(row)
    assert (a1.public_source, a2.public_source, b.public_source) == (
        True, True, False)
    assert all(r.public_source_checked_at for r in (a1, a2, b))
    assert decided.public_source_checked_at is None
    assert archive.public_source is True and archive.public_source_checked_at is None
    assert page.public_source is False and page.public_source_checked_at is None


def test_public_source_sweep_stops_on_a_throttle_and_queues_it_behind(
        app, monkeypatch):
    """Never-asked rows go first (newest save first). A 400-class answer
    other than 403/404 is skipped; a 429 (or 5xx, or no answer) ends the
    run with the attempt recorded, so the next night starts with the rows
    asked longest ago and the throttled one comes last."""
    from datetime import datetime, timedelta
    uid = User.query.first().id
    old = _tweet_row(uid, "1", checked_at=datetime.utcnow() - timedelta(days=1))
    fresh = _tweet_row(uid, "2")
    newer = _tweet_row(uid, "3")
    answers = {"3": (None, 400), "2": (None, 429), "1": (True, 200)}
    asked = []
    monkeypatch.setattr(_sync_mod, "x_tweet_public_status",
                        lambda tid: asked.append(tid) or answers[tid])

    result = _sync_mod.run_public_source_sweep(pause=0)

    assert asked == ["3", "2"]  # 1 was asked yesterday: last in line
    assert result["stopped_on"] == 429 and result["unanswered"] == 2
    for row in (old, fresh, newer):
        _db.session.refresh(row)
    assert fresh.public_source is None and fresh.public_source_checked_at
    assert newer.public_source is None and newer.public_source_checked_at
    assert old.public_source is None  # not reached tonight

    answers.update({"3": (True, 200), "2": (True, 200)})
    asked.clear()
    _sync_mod.run_public_source_sweep(pause=0)
    assert asked == ["1", "3", "2"]
    for row in (old, fresh, newer):
        _db.session.refresh(row)
    assert (old.public_source, fresh.public_source, newer.public_source) == (
        True, True, True)


def test_public_source_sweep_respects_the_nightly_cap(app, monkeypatch):
    uid = User.query.first().id
    for i in range(5):
        _tweet_row(uid, str(i))
    asked = []
    monkeypatch.setattr(_sync_mod, "x_tweet_public_status",
                        lambda tid: asked.append(tid) or (True, 200))
    result = _sync_mod.run_public_source_sweep(limit=2, pause=0)
    assert result["lookups"] == 2 and len(asked) == 2
    assert ExternalItem.query.filter_by(public_source=None).count() == 3


def test_public_source_sweep_cap_counts_tweets_not_copies(app, monkeypatch):
    """The nightly budget is lookups: three people saving the newest
    tweet cost one of it, not three, so the older tweet is still
    reached tonight."""
    uid = User.query.first().id
    other, third = User(username="other"), User(username="third")
    _db.session.add_all([other, third])
    _db.session.commit()
    _tweet_row(uid, "older")
    for user_id in (uid, other.id, third.id):
        _tweet_row(user_id, "newest")  # three copies, all saved later
    asked = []
    monkeypatch.setattr(_sync_mod, "x_tweet_public_status",
                        lambda tid: asked.append(tid) or (True, 200))

    result = _sync_mod.run_public_source_sweep(limit=2, pause=0)

    assert asked == ["newest", "older"] and result["lookups"] == 2
    assert ExternalItem.query.filter_by(public_source=None).count() == 0


def test_public_source_sweep_keeps_each_verdict_as_it_lands(app, monkeypatch):
    """Verdicts are committed one by one: a run cut off mid-way (soft
    time limit, a deploy's SIGTERM) keeps what X already answered."""
    later = _tweet_row(uid := User.query.first().id, "1")  # saved first: asked second
    first = _tweet_row(uid, "2")  # saved last: newest, asked first

    def answer(tid):
        if tid == "2":
            return True, 200
        raise RuntimeError("cut off")  # stands in for SoftTimeLimitExceeded
    monkeypatch.setattr(_sync_mod, "x_tweet_public_status", answer)

    with pytest.raises(RuntimeError):
        _sync_mod.run_public_source_sweep(pause=0)
    _db.session.rollback()
    for row in (first, later):
        _db.session.refresh(row)
    assert first.public_source is True and first.public_source_checked_at
    assert later.public_source is None and later.public_source_checked_at is None


def test_public_source_sweep_gate_runs_once_in_its_utc_hour(app, monkeypatch):
    """The beat entry is HOURLY (a daily interval starts over on every
    deploy, which deletes the beat schedule): the task runs the sweep
    only when the UTC clock is in PUBLIC_SOURCE_SWEEP_UTC_HOUR, and not
    again within PUBLIC_SOURCE_SWEEP_MIN_GAP of the last run."""
    from datetime import datetime, timedelta
    uid = User.query.first().id
    row = _tweet_row(uid, "1")
    asked = []
    monkeypatch.setattr(_sync_mod, "x_tweet_public_status",
                        lambda tid: asked.append(tid) or (True, 200))
    monkeypatch.setattr(_sync_mod.time, "sleep", lambda s: None)
    now_hour = datetime.utcnow().hour

    monkeypatch.setattr(_sync_mod, "PUBLIC_SOURCE_SWEEP_UTC_HOUR",
                        (now_hour + 12) % 24)
    assert _sync_mod.verify_public_source_sweep() == {"status": "not_due"}
    assert asked == []

    monkeypatch.setattr(_sync_mod, "PUBLIC_SOURCE_SWEEP_UTC_HOUR", now_hour)
    result = _sync_mod.verify_public_source_sweep()
    assert result["status"] == "ok" and asked == ["1"]

    # A second tick in the same hour (beat restarted mid-hour): no-op.
    _tweet_row(uid, "2")
    assert _sync_mod.verify_public_source_sweep() == {"status": "ran_recently"}
    assert asked == ["1"]

    # Tomorrow: the newest attempt is a night old, so it runs again.
    row.public_source_checked_at = datetime.utcnow() - timedelta(hours=23)
    _db.session.commit()
    assert _sync_mod.verify_public_source_sweep()["status"] == "ok"
    assert asked == ["1", "2"]


def test_nightly_fanout_dispatches_local_3am_connected_only(app, monkeypatch):
    """Only connected, non-revoked accounts whose user's LOCAL clock is in
    the 3am hour get dispatched; recently-synced accounts are skipped."""
    from datetime import datetime, timedelta
    night_tz = _tz_at_hour(_sync_mod.NIGHTLY_SYNC_LOCAL_HOUR)
    day_tz = _tz_at_hour((_sync_mod.NIGHTLY_SYNC_LOCAL_HOUR + 12) % 24)

    night_user = User.query.first()
    night_user.timezone = night_tz
    _mk_account(night_user.id)

    day_user = User(username="daytime", timezone=day_tz)
    revoked_user = User(username="revoked", timezone=night_tz)
    fresh_user = User(username="freshly-synced", timezone=night_tz)
    no_account_user = User(username="never-connected", timezone=night_tz)
    _db.session.add_all([day_user, revoked_user, fresh_user,
                         no_account_user])
    _db.session.commit()
    _mk_account(day_user.id)
    _mk_account(revoked_user.id, revoked=True)
    fresh = _mk_account(fresh_user.id)
    fresh.last_synced_at = datetime.utcnow() - timedelta(hours=2)
    _db.session.commit()

    dispatched = []
    fake_task = MagicMock()
    fake_task.apply_async = lambda args, countdown: dispatched.append(
        (args[0], countdown))
    monkeypatch.setattr(_sync_mod, "sync_twitter_bookmarks", fake_task)

    result = _sync_mod.sync_all_twitter_bookmarks()
    assert result == {"status": "ok", "dispatched": 1}
    assert dispatched == [(night_user.id, 0)]


def test_nightly_fanout_unknown_timezone_falls_back_to_utc(app, monkeypatch):
    """A broken/unset timezone must not crash the gate — it evaluates as
    UTC and simply syncs in UTC's night."""
    from datetime import datetime
    uid = User.query.first().id
    User.query.get(uid).timezone = "Not/AZone"
    _mk_account(uid)
    _db.session.commit()

    dispatched = []
    fake_task = MagicMock()
    fake_task.apply_async = lambda args, countdown: dispatched.append(
        args[0])
    monkeypatch.setattr(_sync_mod, "sync_twitter_bookmarks", fake_task)

    result = _sync_mod.sync_all_twitter_bookmarks()
    expected = (1 if datetime.utcnow().hour
                == _sync_mod.NIGHTLY_SYNC_LOCAL_HOUR else 0)
    assert result == {"status": "ok", "dispatched": expected}


def test_nightly_fanout_noop_without_client_id(app, monkeypatch):
    uid = User.query.first().id
    _mk_account(uid)
    _app.config["X_CLIENT_ID"] = None
    try:
        result = _sync_mod.sync_all_twitter_bookmarks()
    finally:
        _app.config["X_CLIENT_ID"] = "client-id"
    assert result == {"status": "not_configured"}


def test_successful_sync_logs_api_cost(app, monkeypatch):
    """X bills per returned POST, not per page (#271): one bookmark over
    two pages (the second empty) costs one post read, logged to
    APICostLog like any other provider call."""
    uid = User.query.first().id
    _mk_account(uid, expired=False)

    def two_pages(token, x_user_id, max_items=800):
        yield [{"external_id": "n1", "content": "new one",
                "author_handle": "x", "url": None, "posted_at": None}], 1
        yield [], 0  # empty page -> early stop; costs nothing
        raise AssertionError("third page must never be fetched")
    monkeypatch.setattr(_sync_mod, "x_fetch_bookmark_pages", two_pages)

    result = _sync_mod.sync_twitter_bookmarks(_FakeSelf(), uid)
    assert result == {"status": "ok", "created": 1, "skipped": 0,
                      "requests": 2, "posts_read": 1}
    log = APICostLog.query.filter_by(
        user_id=uid, request_type="x_bookmark_sync").one()
    assert log.cost_microdollars == 1 * _sync_mod.X_POST_READ_COST_MICRODOLLARS
    assert log.model_id == "x-api/bookmarks"
    assert log.request_ref == "posts:1/pages:2"


def test_sync_cost_is_per_post_not_per_page(app, monkeypatch):
    """A two-page sync returning N posts logs N * post-read price — the
    old flat per-request constant undercounted by ~the page size."""
    uid = User.query.first().id
    _mk_account(uid, expired=False)

    def _item(i):
        return {"external_id": f"p{i}", "content": f"post {i}",
                "author_handle": "x", "url": None, "posted_at": None}

    def two_full_pages(token, x_user_id, max_items=800):
        yield [_item(i) for i in range(5)], 5
        yield [_item(i) for i in range(5, 8)], 3
    monkeypatch.setattr(_sync_mod, "x_fetch_bookmark_pages", two_full_pages)

    result = _sync_mod.sync_twitter_bookmarks(_FakeSelf(), uid)
    assert result["created"] == 8 and result["posts_read"] == 8
    log = APICostLog.query.filter_by(
        user_id=uid, request_type="x_bookmark_sync").one()
    assert log.cost_microdollars == 8 * _sync_mod.X_POST_READ_COST_MICRODOLLARS
    assert log.request_ref == "posts:8/pages:2"


def test_successful_sync_records_created_count(app, monkeypatch):
    """The import page reports the task's own count after a manual sync
    (diffing item counts around the running task under-reported)."""
    uid = User.query.first().id
    account = _mk_account(uid, expired=False)

    def pages(token, x_user_id, max_items=800):
        yield [{"external_id": f"n{i}", "content": "t", "author_handle": "x",
                "url": None, "posted_at": None} for i in range(3)], 3
        yield [{"external_id": "n0", "content": "t", "author_handle": "x",
                "url": None, "posted_at": None}], 1  # known -> stop
    monkeypatch.setattr(_sync_mod, "x_fetch_bookmark_pages", pages)

    _sync_mod.sync_twitter_bookmarks(_FakeSelf(), uid)
    _db.session.expire_all()
    assert ExternalAccount.query.get(account.id).last_sync_created == 3


def test_posts_read_is_what_x_returned_not_what_normalized(app, monkeypatch):
    """X bills every post it returns, including ones normalization drops
    (no id/text), so the ledger counts the returned figure the fetcher
    reports alongside each page — not len(page)."""
    uid = User.query.first().id
    _mk_account(uid, expired=False)

    def pages(token, x_user_id, max_items=800):
        # 3 returned, 1 normalizable
        yield [{"external_id": "n1", "content": "t", "author_handle": "x",
                "url": None, "posted_at": None}], 3
        yield [], 0
    monkeypatch.setattr(_sync_mod, "x_fetch_bookmark_pages", pages)

    result = _sync_mod.sync_twitter_bookmarks(_FakeSelf(), uid)
    assert result["created"] == 1 and result["posts_read"] == 3
    log = APICostLog.query.filter_by(
        user_id=uid, request_type="x_bookmark_sync").one()
    assert log.cost_microdollars == 3 * _sync_mod.X_POST_READ_COST_MICRODOLLARS
    assert log.request_ref == "posts:3/pages:2"


def test_failed_sync_still_logs_pages_already_billed(app, monkeypatch):
    """A 429 (or 5xx) after some pages arrived re-raises for the next
    scheduled retry, but X already billed those pages: the cost row is
    written before the error propagates. The failing request returned
    nothing and costs nothing."""
    uid = User.query.first().id
    account = _mk_account(uid, expired=False)

    def _item(i):
        return {"external_id": f"p{i}", "content": f"post {i}",
                "author_handle": "x", "url": None, "posted_at": None}

    def pages_then_429(token, x_user_id, max_items=800):
        yield [_item(i) for i in range(10)], 10
        yield [_item(i) for i in range(10, 30)], 20
        raise _http_error(429)
    monkeypatch.setattr(_sync_mod, "x_fetch_bookmark_pages", pages_then_429)

    with pytest.raises(requests.HTTPError):
        _sync_mod.sync_twitter_bookmarks(_FakeSelf(), uid)
    log = APICostLog.query.filter_by(
        user_id=uid, request_type="x_bookmark_sync").one()
    assert log.cost_microdollars == 30 * _sync_mod.X_POST_READ_COST_MICRODOLLARS
    assert log.request_ref == "posts:30/pages:2"
    # The pages that arrived stay imported; the sync is not marked done.
    _db.session.expire_all()
    assert ExternalAccount.query.get(account.id).last_synced_at is None
    assert ExternalAccount.query.get(account.id).revoked_at is None


def test_401_mid_sync_logs_cost_then_revokes(app, monkeypatch):
    uid = User.query.first().id
    account = _mk_account(uid, expired=False)

    def page_then_401(token, x_user_id, max_items=800):
        yield [{"external_id": "n1", "content": "t", "author_handle": "x",
                "url": None, "posted_at": None}], 7
        raise _http_error(401)
    monkeypatch.setattr(_sync_mod, "x_fetch_bookmark_pages", page_then_401)

    result = _sync_mod.sync_twitter_bookmarks(_FakeSelf(), uid)
    assert result["status"] == "revoked"
    log = APICostLog.query.filter_by(
        user_id=uid, request_type="x_bookmark_sync").one()
    assert log.request_ref == "posts:7/pages:1"
    _db.session.expire_all()
    assert ExternalAccount.query.get(account.id).revoked_at is not None


def _fake_x_bookmarks(monkeypatch, ids, fail_on_request=None):
    """Serve *ids* (newest-bookmarked first) from a fake X endpoint that
    honors max_results and paginates by token; return the list of
    max_results asked per request. Drives the REAL fetcher.
    *fail_on_request* (0-based index into the calls) answers that request
    with a 429 instead of a page."""
    from backend.utils import external_content as content

    class _Resp:
        def __init__(self, payload, error=None):
            self._payload = payload
            self._error = error

        def raise_for_status(self):
            if self._error is not None:
                raise self._error

        def json(self):
            return self._payload

    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        size = params["max_results"]
        calls.append(size)
        if len(calls) - 1 == fail_on_request:
            return _Resp({}, error=_http_error(429))
        start = int(params.get("pagination_token") or 0)
        chunk = ids[start:start + size]
        nxt = start + len(chunk)
        payload = {
            "data": [{"id": i, "author_id": "a", "text": f"t{i}"}
                     for i in chunk],
            "includes": {"users": [{"id": "a", "username": "u"}]},
            "meta": {"next_token": str(nxt)} if nxt < len(ids) else {},
        }
        return _Resp(payload)
    monkeypatch.setattr(content.requests, "get", fake_get)
    return calls


@pytest.mark.parametrize("n_new,expected_calls,expected_posts", [
    (0, [10], 10),
    (1, [10, 10], 20),
    (11, [10, 20, 20], 50),
    (31, [10, 20, 40, 40], 110),
])
def test_sync_freezes_page_growth_once_known_bookmarks_appear(
        app, monkeypatch, n_new, expected_calls, expected_posts):
    """The ceiling in external_content.py: N new bookmarks on top of an
    imported set cost at most N + 2·min(N + 10, 100) posts, because the
    sync stops doubling the page once a page reached known bookmarks
    (1 new = 20 posts, not 30; 11 new = 50, not 70). The account has
    finished a sync before: the early stop only applies then (#310)."""
    from datetime import datetime, timedelta
    uid = User.query.first().id
    account = _mk_account(uid, expired=False)
    account.last_synced_at = datetime.utcnow() - timedelta(days=1)
    _db.session.commit()
    known = [f"k{i}" for i in range(200)]
    _sync_mod._upsert_items(uid, "twitter_bookmark", [
        {"external_id": k, "content": "t", "author_handle": "u",
         "url": None, "posted_at": None} for k in known])
    calls = _fake_x_bookmarks(
        monkeypatch, [f"n{i}" for i in range(n_new)] + known)

    result = _sync_mod.sync_twitter_bookmarks(_FakeSelf(), uid)
    assert result["created"] == n_new
    assert calls == expected_calls
    assert result["posts_read"] == expected_posts
    assert ExternalItem.query.filter_by(
        user_id=uid, source="twitter_bookmark").count() == 200 + n_new


def test_interrupted_first_import_resumes_to_the_end(app, monkeypatch):
    """#310: a first import that 429s after two pages keeps those pages
    and is not marked done. The next sync must not stop at the imported
    head (the early stop assumes everything below a known page came in
    earlier, which is false here): it reads on, growing the page size as
    usual, until X has nothing more — and only then sets last_synced_at.
    After that, the ordinary early stop is back: a quiet night costs one
    small page."""
    uid = User.query.first().id
    account = _mk_account(uid, expired=False)
    ids = [f"b{i}" for i in range(250)]

    calls = _fake_x_bookmarks(monkeypatch, ids, fail_on_request=2)
    with pytest.raises(requests.HTTPError):
        _sync_mod.sync_twitter_bookmarks(_FakeSelf(), uid)
    assert calls == [10, 20, 40]  # the third request is the 429
    assert ExternalItem.query.filter_by(
        user_id=uid, source="twitter_bookmark").count() == 30
    _db.session.expire_all()
    assert ExternalAccount.query.get(account.id).last_synced_at is None

    calls = _fake_x_bookmarks(monkeypatch, ids)
    result = _sync_mod.sync_twitter_bookmarks(_FakeSelf(), uid)
    # Re-reads the 30 known ones once, pages still double, reaches the end.
    assert calls == [10, 20, 40, 80, 100]
    assert result["created"] == 220 and result["skipped"] == 30
    assert result["posts_read"] == 250
    assert ExternalItem.query.filter_by(
        user_id=uid, source="twitter_bookmark").count() == 250
    _db.session.expire_all()
    assert ExternalAccount.query.get(account.id).last_synced_at is not None
    rows = APICostLog.query.filter_by(
        user_id=uid, request_type="x_bookmark_sync").order_by(
        APICostLog.id).all()
    # One row per attempt, each counting what X returned in it.
    assert [r.request_ref for r in rows] == [
        "posts:30/pages:2", "posts:250/pages:5"]

    calls = _fake_x_bookmarks(monkeypatch, ids)
    result = _sync_mod.sync_twitter_bookmarks(_FakeSelf(), uid)
    assert calls == [10] and result["created"] == 0


def test_interrupted_catch_up_sync_fills_the_gap(app, monkeypatch):
    """#310 review: an account that HAS synced before (reconnected, or
    unparked after #313) comes back with 300 new bookmarks above the 200
    it has. The catch-up sync 429s after storing 30 of them. last_synced_at
    keeps its old value, so without a marker the next sync would read one
    known page and stop, and the 270 between would never be imported.
    The marker makes it read to the end, then clears with last_synced_at."""
    from datetime import datetime, timedelta
    uid = User.query.first().id
    account = _mk_account(uid, expired=False)
    synced_before = datetime.utcnow() - timedelta(days=60)
    account.last_synced_at = synced_before
    _db.session.commit()
    known = [f"k{i}" for i in range(200)]
    _sync_mod._upsert_items(uid, "twitter_bookmark", [
        {"external_id": k, "content": "t", "author_handle": "u",
         "url": None, "posted_at": None} for k in known])
    ids = [f"n{i}" for i in range(300)] + known

    calls = _fake_x_bookmarks(monkeypatch, ids, fail_on_request=2)
    with pytest.raises(requests.HTTPError):
        _sync_mod.sync_twitter_bookmarks(_FakeSelf(), uid)
    _db.session.expire_all()
    account = ExternalAccount.query.get(account.id)
    assert account.sync_incomplete is True
    assert account.last_synced_at == synced_before
    assert ExternalItem.query.filter_by(
        user_id=uid, source="twitter_bookmark").count() == 230

    calls = _fake_x_bookmarks(monkeypatch, ids)
    result = _sync_mod.sync_twitter_bookmarks(_FakeSelf(), uid)
    assert calls == [10, 20, 40, 80, 100, 100, 100, 100]  # to the end
    assert result["posts_read"] == 500
    assert result["created"] == 270
    assert ExternalItem.query.filter_by(
        user_id=uid, source="twitter_bookmark").count() == 500
    _db.session.expire_all()
    account = ExternalAccount.query.get(account.id)
    assert account.sync_incomplete is None
    assert account.last_synced_at > synced_before

    # Finished: the next quiet night is one small page again.
    calls = _fake_x_bookmarks(monkeypatch, ids)
    _sync_mod.sync_twitter_bookmarks(_FakeSelf(), uid)
    assert calls == [10]


def test_killed_worker_leaves_the_marker_set(app, monkeypatch):
    """A deploy that kills the worker mid-sync raises SystemExit in the
    pool process (#312): no except block runs. The marker was committed
    with the first stored page, so the next sync still reads to the end."""
    from datetime import datetime, timedelta
    uid = User.query.first().id
    account = _mk_account(uid, expired=False)
    account.last_synced_at = datetime.utcnow() - timedelta(days=1)
    _db.session.commit()

    def page_then_killed(token, x_user_id, max_items=800):
        yield [{"external_id": "n1", "content": "t", "author_handle": "x",
                "url": None, "posted_at": None}], 1
        raise SystemExit(0)
    monkeypatch.setattr(_sync_mod, "x_fetch_bookmark_pages", page_then_killed)

    with pytest.raises(SystemExit):
        _sync_mod.sync_twitter_bookmarks(_FakeSelf(), uid)
    _db.session.rollback()
    _db.session.expire_all()
    assert ExternalAccount.query.get(account.id).sync_incomplete is True


def test_quiet_night_leaves_no_marker(app, monkeypatch):
    """A sync that reads one known page and stops has finished: the
    marker it set with that page is cleared with last_synced_at."""
    from datetime import datetime, timedelta
    uid = User.query.first().id
    account = _mk_account(uid, expired=False)
    account.last_synced_at = datetime.utcnow() - timedelta(days=1)
    _db.session.commit()
    known = [f"k{i}" for i in range(50)]
    _sync_mod._upsert_items(uid, "twitter_bookmark", [
        {"external_id": k, "content": "t", "author_handle": "u",
         "url": None, "posted_at": None} for k in known])
    calls = _fake_x_bookmarks(monkeypatch, known)

    _sync_mod.sync_twitter_bookmarks(_FakeSelf(), uid)
    assert calls == [10]
    _db.session.expire_all()
    assert ExternalAccount.query.get(account.id).sync_incomplete is None


def test_first_import_reads_past_bookmarks_saved_another_way(
        app, monkeypatch):
    """Tweets clipped with the extension or imported from JSON share the
    twitter_bookmark source. Before any sync has finished they say
    nothing about what the sync already covered, so they must not end a
    first import either."""
    uid = User.query.first().id
    _mk_account(uid, expired=False)
    ids = [f"b{i}" for i in range(40)]
    _sync_mod._upsert_items(uid, "twitter_bookmark", [
        {"external_id": i, "content": "t", "author_handle": "u",
         "url": None, "posted_at": None} for i in ids[:10]])
    calls = _fake_x_bookmarks(monkeypatch, ids)

    result = _sync_mod.sync_twitter_bookmarks(_FakeSelf(), uid)
    assert calls == [10, 20, 40]
    assert result["created"] == 30 and result["skipped"] == 10
    assert ExternalItem.query.filter_by(
        user_id=uid, source="twitter_bookmark").count() == 40


def test_failed_sync_whose_cost_row_cannot_be_written_raises_the_original(
        app, monkeypatch):
    """When the database is what broke, writing the cost row fails too;
    that must not replace the sync's own error (the one the retry logic
    keys on) — it is logged and the original propagates."""
    uid = User.query.first().id
    _mk_account(uid, expired=False)

    def page_then_429(token, x_user_id, max_items=800):
        yield [{"external_id": "n1", "content": "t", "author_handle": "x",
                "url": None, "posted_at": None}], 1
        raise _http_error(429)
    monkeypatch.setattr(_sync_mod, "x_fetch_bookmark_pages", page_then_429)

    def broken_cost_log(**kwargs):
        raise RuntimeError("database gone")
    monkeypatch.setattr(_sync_mod, "APICostLog", broken_cost_log)

    with pytest.raises(requests.HTTPError):
        _sync_mod.sync_twitter_bookmarks(_FakeSelf(), uid)

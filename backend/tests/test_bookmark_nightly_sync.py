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
    User, ExternalAccount, APICostLog,
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


def _http_error(status):
    resp = MagicMock()
    resp.status_code = status
    err = requests.HTTPError(f"HTTP {status}")
    err.response = resp
    return err


def test_dead_refresh_grant_marks_revoked(app, monkeypatch):
    uid = User.query.first().id
    account = _mk_account(uid)

    def dead_refresh(client_id, refresh_token):
        raise _http_error(400)
    monkeypatch.setattr(_sync_mod, "x_refresh_access_token", dead_refresh)

    result = _sync_mod.sync_twitter_bookmarks(_FakeSelf(), uid)
    assert result["status"] == "revoked"
    _db.session.expire_all()
    assert ExternalAccount.query.get(account.id).revoked_at is not None


def test_transient_refresh_error_does_not_revoke(app, monkeypatch):
    uid = User.query.first().id
    account = _mk_account(uid)

    def flaky_refresh(client_id, refresh_token):
        raise _http_error(429)
    monkeypatch.setattr(_sync_mod, "x_refresh_access_token", flaky_refresh)

    with pytest.raises(requests.HTTPError):
        _sync_mod.sync_twitter_bookmarks(_FakeSelf(), uid)
    _db.session.expire_all()
    assert ExternalAccount.query.get(account.id).revoked_at is None


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


def test_nightly_fanout_dispatches_connected_only(app, monkeypatch):
    uid = User.query.first().id
    _mk_account(uid)  # connected, healthy
    revoked_user = User(username="revoked")
    empty_user = User(username="never-connected")
    _db.session.add_all([revoked_user, empty_user])
    _db.session.commit()
    _mk_account(revoked_user.id, revoked=True)

    dispatched = []
    fake_task = MagicMock()
    fake_task.apply_async = lambda args, countdown: dispatched.append(
        (args[0], countdown))
    monkeypatch.setattr(_sync_mod, "sync_twitter_bookmarks", fake_task)

    result = _sync_mod.sync_all_twitter_bookmarks()
    assert result == {"status": "ok", "dispatched": 1}
    assert dispatched == [(uid, 0)]


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
    """Every consumed page is a paid request; the sync logs the spend to
    APICostLog like any other provider call."""
    uid = User.query.first().id
    _mk_account(uid, expired=False)

    def two_pages(token, x_user_id, max_items=800):
        yield [{"external_id": "n1", "content": "new one",
                "author_handle": "x", "url": None, "posted_at": None}]
        yield []  # stale page -> early stop; still a paid request
        raise AssertionError("third page must never be fetched")
    monkeypatch.setattr(_sync_mod, "x_fetch_bookmark_pages", two_pages)
    monkeypatch.setattr(_sync_mod, "_post_import", lambda *a: None)

    result = _sync_mod.sync_twitter_bookmarks(_FakeSelf(), uid)
    assert result == {"status": "ok", "created": 1, "skipped": 0,
                      "requests": 2}
    log = APICostLog.query.filter_by(
        user_id=uid, request_type="x_bookmark_sync").one()
    assert log.cost_microdollars == 2 * _sync_mod.X_REQUEST_COST_MICRODOLLARS
    assert log.model_id == "x-api/bookmarks"

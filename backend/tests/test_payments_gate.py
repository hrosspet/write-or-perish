"""Payments on (#438): an account that never had a subscription is not let
in. Every /api and /media route of the real app answers it 402
{"error": "no_subscription"}, except the short list it needs: who am I,
its subscription, the terms, deleting itself (and the webhooks and health
checks, which have no user). Accounts set by hand, active and lapsed
accounts, and everyone while payments are off, are not stopped by it.

The gate is a before_request hook of create_app(), so this test builds
the real app (sqlite in memory, the celery glue stubbed, as
test_account_deletion_waitlist.py does) and walks its URL map. A refused
request never reaches its view, so walking every route changes nothing.
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

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login  # noqa: E402
from backend.extensions import db as _db  # noqa: E402
from backend.models import User  # noqa: E402
import backend.models as _real_backend_models  # noqa: E402

# What an account in state "none" may still call (method, rule).
EXPECTED_OPEN = {
    ("GET", "/api/dashboard/"),
    ("PATCH", "/api/dashboard/timezone"),
    ("POST", "/api/account/delete"),
    ("POST", "/api/account/delete/confirm"),
    ("POST", "/api/account/restore"),
    ("POST", "/api/account/restore/decline"),
    ("GET", "/api/account/restore"),
}
OPEN_PREFIXES = ("/api/billing", "/api/terms", "/api/webhooks",
                 "/api/health", "/api/ready", "/api/live")


def _affected(k):
    return (k == "flask_login" or k.startswith("backend.routes")
            or k == "backend.models")


@pytest.fixture
def real_app(monkeypatch):
    saved = {k: sys.modules[k] for k in list(sys.modules) if _affected(k)}
    sys.modules["flask_login"] = _real_flask_login
    sys.modules["backend.models"] = _real_backend_models
    for _k in [k for k in list(sys.modules) if k.startswith("backend.routes")]:
        del sys.modules[_k]
    import backend.config as _config
    monkeypatch.setattr(_config.Config, "SQLALCHEMY_DATABASE_URI",
                        "sqlite:///:memory:")
    from backend import create_app
    app = create_app()
    assert app.config["SQLALCHEMY_DATABASE_URI"] == "sqlite:///:memory:"
    app.config["TESTING"] = True
    app.config["PAYMENTS_ENABLED"] = True
    app.config["DEPLOY_ENV"] = "testing"
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()
    for k in [k for k in list(sys.modules) if _affected(k)]:
        if k not in saved:
            del sys.modules[k]
    for k, mod in saved.items():
        sys.modules[k] = mod


def _user(name, **kw):
    u = User(username=name, approved=True, accepted_terms_version="x", **kw)
    _db.session.add(u)
    _db.session.commit()
    return u


def _client(app, user):
    from flask import g
    g.pop("_login_user", None)
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return c


def _routes(app):
    """(method, rule, a URL for it) for every /api and /media route."""
    adapter = app.url_map.bind("localhost")
    out = []
    for rule in app.url_map.iter_rules():
        if not (rule.rule.startswith("/api") or rule.rule.startswith("/media")):
            continue
        values = {}
        for arg, conv in rule._converters.items():
            values[arg] = 1 if type(conv).__name__ == "IntegerConverter" \
                else "x"
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            out.append((method, rule.rule,
                        adapter.build(rule.endpoint, values, method=method)))
    return out


def _is_open(method, rule):
    path = rule.rstrip("/")
    return ((method, rule) in EXPECTED_OPEN
            or any(path == p or path.startswith(p + "/")
                   for p in OPEN_PREFIXES))


def test_state_none_is_refused_on_every_other_route(real_app):
    from backend.utils.subscriptions import open_without_subscription
    u = _user("newcomer")   # plan "free", no subscription: state "none"
    routes = _routes(real_app)
    assert len(routes) > 150
    refused = 0
    for method, rule, url in routes:
        assert open_without_subscription(method, url) == _is_open(
            method, rule), (method, rule)
        if _is_open(method, rule):
            continue
        r = _client(real_app, u).open(url, method=method, json={})
        assert r.status_code == 402, (method, rule, r.status_code)
        assert r.get_json()["error"] == "no_subscription", (method, rule)
        refused += 1
    assert refused > 150


def test_the_open_routes_are_exactly_these(real_app):
    found = {(m, r) for m, r, _u in _routes(real_app)
             if (m, r) in EXPECTED_OPEN}
    assert found == EXPECTED_OPEN


@pytest.mark.parametrize("kw", [
    {"plan": "alpha"}, {"plan": "pro"}, {"is_admin": True},
    {"subscription_status": "active", "subscription_level": "standard"},
    {"subscription_status": "trialing", "subscription_level": "supporter"},
    {"subscription_status": "ended", "subscription_level": "standard",
     "subscription_ever_paid": True},
])
def test_other_accounts_are_not_stopped(real_app, kw):
    u = _user("someone", **kw)
    c = _client(real_app, u)
    for url in ("/api/updates", "/api/nodes/999999", "/api/log"):
        r = c.get(url)
        body = r.get_json(silent=True) or {}
        assert body.get("error") != "no_subscription", (url, kw)


def test_payments_off_nobody_is_stopped(real_app):
    real_app.config["PAYMENTS_ENABLED"] = False
    u = _user("newcomer")
    r = _client(real_app, u).get("/api/updates")
    assert (r.get_json(silent=True) or {}).get("error") != "no_subscription"


def test_the_subscribe_page_has_what_it_needs(real_app):
    u = _user("newcomer")
    c = _client(real_app, u)
    data = c.get("/api/dashboard/").get_json()
    assert data["user"]["subscription"]["state"] == "none"
    assert "latest_profile" not in data
    assert c.get("/api/billing").status_code == 200

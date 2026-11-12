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


def _stubbed_backend_modules():
    """backend.* modules an earlier test replaced with a MagicMock (e.g. the
    llm_completion task module): the real app needs the real ones."""
    return {k: v for k, v in sys.modules.items()
            if k.startswith("backend.") and isinstance(v, MagicMock)}


@pytest.fixture
def real_app(monkeypatch):
    stubs = _stubbed_backend_modules()
    for k in stubs:
        del sys.modules[k]
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
    sys.modules.update(stubs)


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


# Routes main added in merge train #652, listed by name so a later route
# change shows up here: waiting write tool calls (Peter, 2026-11-12) and
# the re-import's profile-rebuild answer (#625).
NEW_ROUTES = [
    ("GET", "/api/tool-approvals?node=1"),
    ("POST", "/api/tool-approvals/1/approve"),
    ("POST", "/api/tool-approvals/1/reject"),
    ("POST", "/api/import/profile-rebuild"),
]


def test_the_routes_from_main_are_in_the_url_map(real_app):
    rules = {(m, r) for m, r, _u in _routes(real_app)}
    for method, rule in [("GET", "/api/tool-approvals"),
                         ("POST", "/api/tool-approvals/<int:draft_id>/approve"),
                         ("POST", "/api/tool-approvals/<int:draft_id>/reject"),
                         ("POST", "/api/import/profile-rebuild")]:
        assert (method, rule) in rules, (method, rule)


def test_the_routes_from_main_refuse_an_account_not_let_in(real_app):
    u = _user("newcomer")
    c = _client(real_app, u)
    for method, url in NEW_ROUTES:
        r = c.open(url, method=method, json={})
        assert r.status_code == 402, (method, url)
        assert r.get_json()["error"] == "no_subscription", (method, url)


def _waiting_memory_change(owner):
    """owner's thread with another person's reply, and owner's AI reply
    whose change to owner's memory waits (as test_tool_approval_routes)."""
    import json
    from backend.models import Node, UserArtifact
    from backend.utils.tool_approvals import ApprovalHold
    anna = _user("anna-gate")
    bot = User(username="gpt-5", twitter_id="llm-gpt-5")
    _db.session.add(bot)
    _db.session.flush()
    memory = UserArtifact(user_id=owner.id, kind="memory", title="Memory",
                          generated_by="test", ai_usage="chat")
    memory.set_content("- likes tea")
    _db.session.add(memory)
    parent = None
    first = None
    for author, text in ((owner, "a note"), (anna, "anna replies"),
                         (owner, "an answer")):
        node = Node(user_id=author.id, human_owner_id=author.id,
                    parent_id=parent.id if parent else None,
                    node_type="user", privacy_level="public", ai_usage="chat")
        node.set_content(text)
        _db.session.add(node)
        _db.session.flush()
        first = first or node
        parent = node
    reply = Node(user_id=bot.id, human_owner_id=owner.id, parent_id=parent.id,
                 node_type="llm", llm_model="gpt-5",
                 llm_task_status="completed", privacy_level="private",
                 ai_usage="chat")
    reply.set_content("I proposed a change to your memory.")
    _db.session.add(reply)
    _db.session.flush()
    inp = {"kind": "memory", "updated_content": "- likes tea\n- trusts anna",
           "base_version": 1}
    fields = ApprovalHold(["other_user"], reply.id).hold(
        "update_artifact", inp, reply, owner.id,
        previous_artifact_id=memory.id, kind="memory")
    reply.tool_calls_meta = json.dumps([{
        "name": "update_artifact", "input": {"kind": "memory"}, **fields}])
    _db.session.commit()
    return first, fields["pending_id"]


def test_a_lapsed_account_lists_and_approves_a_waiting_change(real_app):
    # A waiting change is the user's own data change, not an AI call: a
    # lapsed account may still list, approve and reject it.
    from backend.models import UserArtifact
    u = _user("lapsed", default_ai_usage="chat", subscription_status="ended",
              subscription_level="standard", subscription_ever_paid=True)
    first, pending_id = _waiting_memory_change(u)
    c = _client(real_app, u)
    r = c.get(f"/api/tool-approvals?node={first.id}")
    assert r.status_code == 200, r.get_json()
    assert [p["id"] for p in r.get_json()["pending"]] == [pending_id]
    c = _client(real_app, u)
    r = c.post(f"/api/tool-approvals/{pending_id}/approve")
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["outcome"] == "approved"
    _db.session.expire_all()
    assert UserArtifact.latest_for(u.id, "memory").get_content() == \
        "- likes tea\n- trusts anna"


def test_a_lapsed_account_rejects_a_waiting_change(real_app):
    u = _user("lapsed", default_ai_usage="chat", subscription_status="ended",
              subscription_level="standard", subscription_ever_paid=True)
    _first, pending_id = _waiting_memory_change(u)
    r = _client(real_app, u).post(f"/api/tool-approvals/{pending_id}/reject")
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["outcome"] == "rejected"


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

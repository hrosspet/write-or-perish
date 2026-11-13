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
    # The subscribe page's "Start from your tweets?" (#697).
    ("PUT", "/api/dashboard/user"),
    ("PATCH", "/api/dashboard/timezone"),
    ("POST", "/api/account/delete"),
    ("POST", "/api/account/delete/confirm"),
    ("POST", "/api/account/restore"),
    ("POST", "/api/account/restore/decline"),
    ("GET", "/api/account/restore"),
    # Its own data as a file (Peter, 2026-11-13: "yes, Ben should be able
    # to export"): test_not_let_in_recording_and_export.
    ("GET", "/api/export/threads"),
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


# Every rule outside /api and /media, by name: pages, sign-in and
# sign-out, public pages, health checks. A new blueprint elsewhere fails
# here and must say whether an account that is not let in may reach it
# (the gate only looks at /api and /media).
EXPECTED_OTHER_RULES = {
    ("GET", "/"), ("GET", "/@<username>"), ("GET", "/@<username>/<slug>"),
    ("GET", "/@<username>/<slug>/og.png"), ("GET", "/@<username>/feed.xml"),
    ("GET", "/@<username>/og.png"), ("GET", "/alpha-thank-you"),
    ("GET", "/auth/login"), ("GET", "/auth/logout"),
    ("GET", "/auth/magic-link/verify"), ("POST", "/auth/magic-link/send"),
    ("GET", "/auth/twitter"), ("GET", "/auth/twitter/authorized"),
    ("GET", "/auth/x/connect"), ("GET", "/health"), ("GET", "/how-to"),
    # A sign-in held because its invite named someone else (#439): it is
    # finished or forgotten here, before any account exists.
    ("POST", "/auth/invite-choice/continue"),
    ("POST", "/auth/invite-choice/cancel"),
    ("GET", "/landing"), ("GET", "/login"), ("GET", "/node/<int:node_id>"),
    ("GET", "/ready"), ("GET", "/sitemap.xml"),
    ("GET", "/static/<path:filename>"), ("GET", "/vision"),
    ("GET", "/why-loore"),
}

# Routes that take a personal API token (utils/api_tokens) as well as the
# session: the clipper's.
EXPECTED_TOKEN_ROUTES = {
    ("POST", "/api/external/clip"),
    ("GET", "/api/external/clip/status"),
}


def _all_rules(app):
    return {(m, r.rule) for r in app.url_map.iter_rules()
            for m in r.methods - {"HEAD", "OPTIONS"}}


def test_the_rules_outside_api_and_media_are_exactly_these(real_app):
    other = {(m, r) for m, r in _all_rules(real_app)
             if not (r.startswith("/api") or r.startswith("/media"))}
    assert other == EXPECTED_OTHER_RULES


def _token_rules(app):
    return {(m, r.rule) for r in app.url_map.iter_rules()
            for m in r.methods - {"HEAD", "OPTIONS"}
            if getattr(app.view_functions[r.endpoint], "accepts_api_token",
                       None)}


def test_the_token_routes_are_exactly_these(real_app):
    assert _token_rules(real_app) == EXPECTED_TOKEN_ROUTES


def _token_for(user):
    from backend.models import ApiToken
    from backend.utils.api_tokens import generate_api_token
    from backend.utils.magic_link import hash_token
    plaintext, prefix = generate_api_token()
    _db.session.add(ApiToken(user_id=user.id, name="clipper", prefix=prefix,
                             token_hash=hash_token(plaintext),
                             scope="external:write"))
    _db.session.commit()
    return plaintext


CLIP = {"url": "https://example.com/a-page", "content": "A page."}


def _no_session_client(app):
    from flask import g
    g.pop("_login_user", None)
    return app.test_client()


def test_a_token_of_an_account_not_let_in_is_refused_on_every_token_route(
        real_app):
    from backend.models import ExternalItem
    u = _user("newcomer", external_content_enabled=True)
    token = _token_for(u)
    headers = {"Authorization": f"Bearer {token}"}
    for method, rule in sorted(EXPECTED_TOKEN_ROUTES):
        r = _no_session_client(real_app).open(
            rule, method=method, json=CLIP, headers=headers)
        assert r.status_code == 402, (method, rule, r.status_code)
        assert r.get_json()["error"] == "no_subscription", (method, rule)
    assert ExternalItem.query.filter_by(user_id=u.id).count() == 0


def test_a_lapsed_accounts_token_gets_what_its_session_gets(real_app):
    u = _user("lapsed", external_content_enabled=True,
              subscription_status="ended", subscription_level="standard",
              subscription_ever_paid=True)
    token = _token_for(u)
    headers = {"Authorization": f"Bearer {token}"}
    by_token = _no_session_client(real_app).get(
        "/api/external/clip/status", headers=headers)
    by_session = _client(real_app, u).get("/api/external/clip/status")
    assert by_token.status_code == by_session.status_code == 200
    r = _no_session_client(real_app).post(
        "/api/external/clip", json=CLIP, headers=headers)
    assert r.status_code == 201, r.get_json()
    r = _client(real_app, u).post("/api/external/clip", json=CLIP)
    assert r.status_code == 200   # the same page again: a re-clip


def test_an_alpha_testers_token_gets_what_its_session_gets(real_app):
    """The alpha free month (#438) is the account's dates, nothing the
    session holds: a tester who never signed in and whose month ended is
    lapsed by token as by session, and makes no billed call."""
    from datetime import datetime, timedelta
    now = datetime.utcnow()
    u = _user("ghost", plan="free", external_content_enabled=True,
              alpha_free_granted_at=now - timedelta(days=50),
              alpha_free_start=now - timedelta(days=40),
              alpha_free_until=now - timedelta(days=10))
    assert u.last_seen_at is None
    token = _token_for(u)
    headers = {"Authorization": f"Bearer {token}"}
    by_token = _no_session_client(real_app).get(
        "/api/external/clip/status", headers=headers)
    by_session = _client(real_app, u).get("/api/external/clip/status")
    assert by_token.status_code == by_session.status_code == 200
    r = _client(real_app, u).get("/api/search/semantic?q=tea")
    assert r.status_code == 402
    assert r.get_json()["error"] == "subscription_required"


# Decided under Peter's "no AI replies and background jobs": a lapsed
# account makes no billed call at all. Every route that starts one
# answers 402 subscription_required, with what stops and what still works.
BILLED_ROUTES = [
    ("POST", "/api/drafts/streaming/init", {}, "transcribe recordings"),
    ("POST", "/api/drafts/streaming/init", {"label": "Voice"},
     "doesn't reply"),
    ("POST", "/api/nodes/upload/init", {}, "transcribe recordings"),
    ("POST", "/api/nodes/streaming/init", {}, "transcribe recordings"),
    ("GET", "/api/search/semantic?q=tea", None, "search by meaning"),
    ("POST", "/api/todo/apply-draft", {"llm_node_id": 1}, "use AI"),
    ("POST", "/api/updates/polls/1/draft", {}, "use AI"),
    ("POST", "/api/external/twitter/sync", {}, "sync bookmarks"),
]


@pytest.mark.parametrize("method,url,body,says", BILLED_ROUTES)
def test_a_lapsed_account_starts_no_billed_call(real_app, method, url, body,
                                                says):
    u = _user("lapsed", subscription_status="ended",
              subscription_level="standard", subscription_ever_paid=True)
    r = _client(real_app, u).open(url, method=method, json=body)
    assert r.status_code == 402, (url, r.status_code, r.get_json())
    data = r.get_json()
    assert data["error"] == "subscription_required"
    assert says in data["message"]
    assert "subscription has ended" in data["message"]


def test_an_audio_upload_of_a_lapsed_account_is_refused(real_app):
    import io
    u = _user("lapsed", subscription_status="ended",
              subscription_level="standard", subscription_ever_paid=True)
    r = _client(real_app, u).post(
        "/api/nodes/", content_type="multipart/form-data",
        data={"audio_file": (io.BytesIO(b"x"), "a.webm")})
    assert r.status_code == 402
    assert "transcribe recordings" in r.get_json()["message"]


@pytest.mark.parametrize("method,url,body,says", BILLED_ROUTES)
def test_an_active_subscription_is_not_refused_there(
        real_app, method, url, body, says):
    u = _user("member", subscription_status="active",
              subscription_level="standard", subscription_ever_paid=True)
    r = _client(real_app, u).open(url, method=method, json=body)
    assert (r.get_json(silent=True) or {}).get("error") != \
        "subscription_required", url


def test_a_lapsed_accounts_held_profile_build_does_not_resume(real_app):
    from backend.tasks.profile_batch import _held_users
    lapsed = _user("lapsed", subscription_status="ended",
                   subscription_level="standard", subscription_ever_paid=True,
                   profile_cap_held=True)
    prefill = _user("prefilled", profile_cap_held=True)   # never let in
    member = _user("member", subscription_status="active",
                   subscription_level="standard", profile_cap_held=True)
    held = {u.id for u in _held_users(User.profile_cap_held, set())}
    assert lapsed.id not in held
    assert {prefill.id, member.id} <= held


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

"""The first cohort (experiment, COHORT_V1): the invitation page, the
invited person's steps, the founder's routes, and the rules they keep.

peter is an admin and the cohort's founder. mara's account is made by
her invitation (the admin whitelist's function, with the X lookup
stubbed); ben and chidi have X accounts already; eve has no X login.
Through the real routes: nobody but the author reads the interview or a
report that was not sent, "Send" is the Circles share and its take-back
closes it again, the circles Loore keeps, the free period, and what the
two drafts read. In-memory SQLite, ENCRYPTION_DISABLED, the reply task
and the model call stubbed: no model is ever called.
"""
import json
import os
import sys
from datetime import date, datetime, timedelta
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
sys.modules.setdefault("ffmpeg", MagicMock())

_mock_llm_task_module = MagicMock()
_mock_task_result = MagicMock()
_mock_task_result.id = "fake-task-id"
_mock_llm_task_module.generate_llm_response.delay.return_value = (
    _mock_task_result
)

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login          # noqa: E402
from backend.extensions import db as _db         # noqa: E402
from backend.models import (                     # noqa: E402
    APICostLog, Circle, CircleMember, Cohort, CohortInvitation, Node,
    NodeCircle, User, UserNotification,
)
import backend.models as _real_backend_models    # noqa: E402
import backend.utils.privacy  # noqa: E402,F401
from backend.utils import cohort as ch  # noqa: E402

# The draft task module, without booting the app through
# backend.celery_app (the test_updates pattern).
_saved_glue = sys.modules.get("backend.celery_app")
sys.modules["backend.celery_app"] = MagicMock()
sys.modules.pop("backend.tasks.cohort_report", None)
import backend.tasks.cohort_report as cohort_tasks  # noqa: E402
if _saved_glue is None:
    sys.modules.pop("backend.celery_app", None)
else:
    sys.modules["backend.celery_app"] = _saved_glue

INTERVIEW_WORDS = "MARA-INTERVIEW-WORDS"
NONE_WORDS = "MARA-NONE-WORDS"
ELSEWHERE_WORDS = "MARA-OTHER-THREAD"
LETTER = "Dear Mara, I read your thread on {user_profile} and on tools."
ABOUT = "Eight people who differ, and one call a week."
X_IDS = {"mara": "900", "ben": "901", "chidi": "902", "dana": "903"}


def _make_app():
    from flask_login import LoginManager

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["SHARE_V1"] = True
    app.config["FRONTEND_BUILD_DIR"] = None
    app.config["PUBLIC_BASE_URL"] = "https://loore.org"
    app.config["DEFAULT_LLM_MODEL"] = "gpt-5"
    app.config["SUPPORTED_MODELS"] = {
        "gpt-5": {"provider": "openai", "api_model": "gpt-5"},
        "claude-x": {"provider": "anthropic", "api_model": "claude-x"},
    }
    _db.init_app(app)
    login_manager = LoginManager(app)

    @app.before_request
    def _fresh_login():
        from flask import g
        g.pop("_login_user", None)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    # The subscription gate, as the app installs it (#438).
    from backend.utils.subscriptions import block_accounts_without_subscription
    app.before_request(block_accounts_without_subscription)

    from backend.routes.circles import circles_bp
    from backend.routes.cohort import (
        cohort_admin_bp, cohort_bp, invitation_bp)
    from backend.routes.dashboard import dashboard_bp
    from backend.routes.nodes import nodes_bp
    from backend.routes.prompts import prompts_bp
    from backend.routes.search import search_bp
    from backend.routes.textmode import textmode_bp
    from backend.routes.updates import updates_bp
    app.register_blueprint(circles_bp, url_prefix="/api/circles")
    app.register_blueprint(invitation_bp, url_prefix="/api/invitation")
    app.register_blueprint(cohort_bp, url_prefix="/api/cohort")
    app.register_blueprint(cohort_admin_bp, url_prefix="/api/admin/cohorts")
    app.register_blueprint(dashboard_bp, url_prefix="/api/dashboard")
    app.register_blueprint(nodes_bp, url_prefix="/api/nodes")
    app.register_blueprint(prompts_bp, url_prefix="/api/prompts")
    app.register_blueprint(search_bp, url_prefix="/api")
    app.register_blueprint(textmode_bp, url_prefix="/api/textmode")
    app.register_blueprint(updates_bp, url_prefix="/api/updates")
    return app


@pytest.fixture
def app():
    _affected = lambda k: (  # noqa: E731
        k == "flask_login"
        or k.startswith("backend.routes")
        or k == "backend.models"
        or k == "backend.tasks.llm_completion"
        or k == "backend.tasks.cohort_report"
    )
    saved = {k: sys.modules[k] for k in list(sys.modules) if _affected(k)}

    sys.modules["flask_login"] = _real_flask_login
    sys.modules["backend.models"] = _real_backend_models
    sys.modules["backend.tasks.llm_completion"] = _mock_llm_task_module
    sys.modules["backend.tasks.cohort_report"] = cohort_tasks
    for _k in [k for k in list(sys.modules) if k.startswith("backend.routes")]:
        del sys.modules[_k]

    app = _make_app()
    with app.app_context():
        _db.create_all()
        _db.session.add_all([
            User(username="peter", default_ai_usage="chat", approved=True,
                 is_admin=True, email="peter@example.com"),
            User(username="ben", twitter_id=X_IDS["ben"],
                 twitter_handle="ben", default_ai_usage="chat",
                 approved=True),
            User(username="chidi", twitter_id=X_IDS["chidi"],
                 twitter_handle="chidi", default_ai_usage="chat",
                 approved=True),
            User(username="eve", default_ai_usage="chat", approved=True,
                 email="eve@example.com"),
        ])
        _db.session.commit()
        app.config["CIRCLES_V1"] = False
        app.config["CIRCLES_USER_IDS"] = set()
        app.config["COHORT_V1"] = True
        # Invitations without a handle, as for trying it locally (W6 has
        # its own test with the setting off).
        app.config["COHORT_ALLOW_OPEN_INVITATIONS"] = True
        app.config["PAYMENTS_ENABLED"] = False
        yield app
        _db.session.remove()
        _db.drop_all()

    for k in [k for k in list(sys.modules) if _affected(k)]:
        del sys.modules[k]
    sys.modules.update(saved)


@pytest.fixture(autouse=True)
def _stubs(monkeypatch):
    """No Redis for the Circles name lookup; the X lookup answers from
    X_IDS; the draft tasks run inline; the model call is recorded."""
    from backend.utils import circles as cz
    from backend.utils import privacy
    from backend.utils import x_identity
    privacy._pointers_seen["at"] = 0.0
    monkeypatch.setattr(cz, "lookup_blocked", lambda uid: False)
    monkeypatch.setattr(cz, "record_lookup_miss", lambda uid, count=1: None)

    def resolve(handle, **kw):
        h = handle.lower()
        if h not in X_IDS:
            raise x_identity.XIdUnresolved("not-in-archive", "Not found.")
        return x_identity.ResolvedX(x_id=X_IDS[h], source="community-archive",
                                    username=handle, display_name=None)
    monkeypatch.setattr(x_identity, "resolve_x_id", resolve)

    calls = []

    def fake_call(model_id, system, text, max_tokens, user_id):
        calls.append({"model_id": model_id, "system": system, "text": text,
                      "max_tokens": max_tokens, "user_id": user_id})
        if "introduction" in system:
            return {"content": "I am Mara and I build boats.",
                    "input_tokens": 10, "output_tokens": 20}
        return {"content": "# My report for Peter\n\n## Where I am now\n\n"
                           "I said things.",
                "input_tokens": 100, "output_tokens": 50}
    monkeypatch.setattr(cohort_tasks, "_call", fake_call)
    from backend.utils import cost
    monkeypatch.setattr(cost, "llm_cost_log_fields",
                        lambda model_id, response, batch=None: {
                            "input_tokens": 1, "output_tokens": 1,
                            "cache_read_tokens": 0, "cache_write_tokens": 0,
                            "cost_microdollars": 5})

    class _Inline:
        def __init__(self, fn):
            self.fn = fn

        def delay(self, invitation_id, stamp=None):
            self.fn(invitation_id, stamp)
    monkeypatch.setattr(cohort_tasks, "draft_cohort_report",
                        _Inline(cohort_tasks.run_report_draft))
    monkeypatch.setattr(cohort_tasks, "draft_cohort_intro",
                        _Inline(cohort_tasks.run_intro_draft))
    return calls


# ── Helpers ─────────────────────────────────────────────────────────────

def _user(name):
    return User.query.filter_by(username=name).first()


def _client(app, name):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(_user(name).id)
        sess["_fresh"] = True
    return client


def _cohort(app, free_until=None):
    r = _client(app, "peter").post("/api/admin/cohorts", json={
        "name": "First Eight", "about": ABOUT, "founder_name": "Peter",
        "booking_url": "https://cal.example/peter",
        "call_url": "https://call.example/eight",
        "free_until": (free_until or date.today() + timedelta(days=42))
        .isoformat()})
    assert r.status_code == 201, r.get_json()
    return r.get_json()["cohort"]["id"]


def _invite(app, cohort_id, handle="mara", letter=LETTER):
    body = {"letter": letter}
    if handle:
        body["x_handle"] = handle
    r = _client(app, "peter").post(
        f"/api/admin/cohorts/{cohort_id}/invitations", json=body)
    assert r.status_code == 201, r.get_json()
    return r.get_json()["link"].rsplit("/", 1)[1]


def _claim(app, who, code):
    return _client(app, who).post("/api/cohort/claim", json={"code": code})


def _inv(code):
    return CohortInvitation.query.filter_by(code=code).first()


def _start(app, who="mara", mode="text"):
    r = _client(app, who).post("/api/cohort/interview/start",
                               json={"mode": mode})
    assert r.status_code in (200, 202), r.get_json()
    return r.get_json()


def _answer(app, who, parent_id, text, ai_usage="chat"):
    r = _client(app, who).post("/api/nodes/", json={
        "content": text, "privacy_level": "private", "ai_usage": ai_usage,
        "parent_id": parent_id})
    assert r.status_code == 201, r.get_json()
    return r.get_json()["id"]


def _interviewed(app, cohort_id=None):
    """mara invited, claimed, and two turns into her interview: the
    opening, her answer, a reply, an answer marked None. Returns
    (code, root id, [node ids])."""
    cohort_id = cohort_id or _cohort(app)
    code = _invite(app, cohort_id)
    assert _claim(app, "mara", code).status_code == 200
    started = _start(app)
    root = started["interview_node_id"]
    opening = _db.session.get(Node, started["opening_node_id"])
    opening.set_content("Hello Mara. What takes most of your attention?")
    opening.llm_task_status = "completed"
    _db.session.commit()
    a1 = _answer(app, "mara", opening.id, INTERVIEW_WORDS)
    reply = Node(user_id=opening.user_id, human_owner_id=_user("mara").id,
                 parent_id=a1, node_type="llm", llm_model="gpt-5",
                 llm_task_status="completed", privacy_level="private",
                 ai_usage="chat")
    reply.set_content("And what would you bring?")
    _db.session.add(reply)
    _db.session.commit()
    a2 = _answer(app, "mara", reply.id, NONE_WORDS, ai_usage="none")
    # Somewhere else entirely: her own other thread.
    _answer(app, "mara", None, ELSEWHERE_WORDS)
    return code, root, [opening.id, a1, reply.id, a2]


def _can_read(app, who, node_id):
    return _client(app, who).get(f"/api/nodes/{node_id}").status_code == 200


def _shared_ids(app, who):
    data = _client(app, who).get("/api/circles/shared-with-me").get_json()
    return [e["id"] for p in data["people"] for e in p["entries"]]


def _report(app):
    r = _client(app, "mara").post("/api/cohort/report/draft", json={})
    assert r.status_code in (200, 202), r.get_json()
    return _client(app, "mara").get("/api/cohort/report").get_json()["report"]


# ── The flag ────────────────────────────────────────────────────────────

def test_with_the_flag_off_every_route_is_404_and_nothing_changes(app):
    cid = _cohort(app)
    code = _invite(app, cid)
    assert _claim(app, "mara", code).status_code == 200
    mara = _user("mara")
    app.config["COHORT_V1"] = False
    anon = app.test_client()
    assert anon.get(f"/api/invitation/{code}").status_code == 404
    assert anon.post(f"/api/invitation/{code}/decline").status_code == 404
    c = _client(app, "mara")
    for path in ("/api/cohort/me", "/api/cohort/page", "/api/cohort/report"):
        assert c.get(path).status_code == 404, path
    assert c.post("/api/cohort/claim", json={"code": code}).status_code == 404
    p = _client(app, "peter")
    assert p.get("/api/admin/cohorts").status_code == 404
    assert p.post("/api/admin/cohorts", json={"name": "x"}).status_code == 404
    # No hook: no Circles, no free access, no prompts, no payload key.
    from backend.utils.privacy import circles_enabled_for
    assert circles_enabled_for(mara.id) is False
    assert circles_enabled_for(_user("peter").id) is False
    mara.plan = "free"
    _db.session.commit()
    assert mara.plan_set_by_hand is False
    keys = [x["prompt_key"] for x in
            c.get("/api/prompts/").get_json()["prompts"]]
    assert not [k for k in keys if k.startswith("cohort_")]
    assert "cohort" not in c.get("/api/dashboard/").get_json()["user"]


# ── The page at the link ────────────────────────────────────────────────

def test_the_invitation_page_and_its_states(app):
    cid = _cohort(app)
    code = _invite(app, cid)
    anon = app.test_client()
    assert anon.get("/api/invitation/nope").status_code == 404
    assert anon.get("/api/invitation/nope").get_json() == {"error": "Not found"}
    r = anon.get(f"/api/invitation/{code}")
    assert r.status_code == 200
    assert r.headers["X-Robots-Tag"] == "noindex, nofollow"
    body = r.get_json()
    assert body["state"] == "open" and body["founder"] == "Peter"
    assert body["cohort"] == {"name": "First Eight", "about": ABOUT}
    assert body["letter"] == LETTER
    inv = _inv(code)
    first = inv.first_opened_at
    assert first is not None
    anon.get(f"/api/invitation/{code}")
    assert _inv(code).first_opened_at == first
    assert ch.invitation_state(inv) == "opened"

    # Once claimed, the letter is no longer served at the link.
    assert _claim(app, "mara", code).status_code == 200
    body = anon.get(f"/api/invitation/{code}").get_json()
    assert body["state"] == "claimed" and "letter" not in body
    me = _client(app, "mara").get("/api/cohort/me").get_json()["invitation"]
    assert me["letter"] == LETTER and me["state"] == "claimed"
    assert me["booking_url"] is None
    # Declining needs the account that accepted it now.
    assert anon.post(f"/api/invitation/{code}/decline").status_code == 403
    assert _client(app, "mara").post(
        f"/api/invitation/{code}/decline").status_code == 200
    assert anon.get(f"/api/invitation/{code}").get_json()["state"] == "declined"


def test_not_for_me_before_any_sign_in(app):
    cid = _cohort(app)
    code = _invite(app, cid, handle="chidi")
    anon = app.test_client()
    r = anon.post(f"/api/invitation/{code}/decline")
    assert r.status_code == 200 and r.get_json() == {"state": "declined"}
    assert ch.invitation_state(_inv(code)) == "declined"
    assert _claim(app, "chidi", code).status_code == 409


# ── Claiming ────────────────────────────────────────────────────────────

def test_the_invitation_makes_an_approved_account_for_the_handle(app):
    cid = _cohort(app)
    _invite(app, cid, handle="@Mara")
    mara = _user("mara")
    assert mara.twitter_id == X_IDS["mara"] and mara.approved is True
    inv = CohortInvitation.query.one()
    assert inv.x_handle == "mara" and inv.x_id == X_IDS["mara"]
    # The same handle twice in one group is refused; an existing account
    # is used as it is.
    r = _client(app, "peter").post(f"/api/admin/cohorts/{cid}/invitations",
                                   json={"x_handle": "mara", "letter": "x"})
    assert r.status_code == 409
    _invite(app, cid, handle="ben")
    assert User.query.filter_by(username="ben").count() == 1
    # A handle nobody has: the whitelist's answer, nothing made.
    r = _client(app, "peter").post(f"/api/admin/cohorts/{cid}/invitations",
                                   json={"x_handle": "nobody", "letter": "x"})
    assert r.status_code == 404 and "x_lookup_cost_usd" in r.get_json()
    assert CohortInvitation.query.count() == 2


def test_claim_with_another_x_account_is_refused(app):
    cid = _cohort(app)
    code = _invite(app, cid)
    r = _claim(app, "ben", code)
    assert r.status_code == 403
    assert r.get_json()["error"] == ("This invitation is for @mara. Sign in "
                                     "with that X account to accept it.")
    r = _claim(app, "eve", code)
    assert r.status_code == 403
    assert "Sign in with X as @mara" in r.get_json()["error"]
    r = _claim(app, "peter", code)
    assert r.status_code == 409   # the founder's own group
    assert _claim(app, "mara", code).status_code == 200
    assert _claim(app, "mara", code).status_code == 200   # idempotent
    assert _claim(app, "ben", code).status_code == 409
    assert _inv(code).user_id == _user("mara").id
    assert _client(app, "eve").post("/api/cohort/claim", json={
        "code": "nope"}).status_code == 404


def test_of_two_claims_at_once_one_binds(app):
    """The second claim loaded the invitation before the first bound it:
    the conditional update binds it once, and the second is told."""
    cid = _cohort(app)
    code = _invite(app, cid, handle=None)
    inv = _inv(code)
    from backend.routes import cohort as routes
    real = routes.ch.claim_refusal

    def first_wins(invitation, user):
        refused = real(invitation, user)
        if refused is None and user.username == "eve":
            CohortInvitation.query.filter_by(id=invitation.id).update(
                {CohortInvitation.user_id: _user("ben").id})
            _db.session.commit()
            # As loaded before ben's claim (a clean value, not an edit).
            from sqlalchemy.orm.attributes import set_committed_value
            set_committed_value(invitation, "user_id", None)
        return refused
    routes.ch.claim_refusal = first_wins
    try:
        r = _claim(app, "eve", code)
    finally:
        routes.ch.claim_refusal = real
    assert r.status_code == 409 and r.get_json()["code"] == "cohort_taken"
    _db.session.expire_all()
    assert _db.session.get(CohortInvitation, inv.id).user_id == _user("ben").id


# ── Nobody but the author reads the interview or an unsent report ───────

def _founder_cannot_read(app, node_ids, secret):
    p = _client(app, "peter")
    for nid in node_ids:
        assert p.get(f"/api/nodes/{nid}").status_code in (403, 404), nid
        assert p.get(f"/api/textmode/from-node/{nid}").status_code in (
            403, 404), nid
        assert p.get(f"/api/nodes/{nid}/audience").status_code in (
            403, 404), nid
        assert p.post(f"/api/nodes/{nid}/llm", json={}).status_code in (
            403, 404), nid
    # Keyword search (an admin's tool) is his own archive only.
    r = p.get(f"/api/search?q={secret}")
    assert r.status_code == 200 and secret not in r.get_data(as_text=True)
    from backend.routes.export_data import build_user_export_content
    out = build_user_export_content(_user("peter"), filter_ai_usage=False)
    assert secret not in (out or "")
    assert not set(_shared_ids(app, "peter")) & set(node_ids)
    # The founder's routes: states and his letter, no node, no text.
    cid = Cohort.query.one().id
    data = p.get(f"/api/admin/cohorts/{cid}/invitations").get_json()
    dump = json.dumps(data)
    assert secret not in dump and "Where I am now" not in dump
    for row in data["invitations"]:
        assert not [k for k in row if "node" in k], row


def test_the_founder_never_reads_the_interview_or_an_unsent_report(app):
    _code, root, nodes = _interviewed(app)
    report = _report(app)
    assert report["task_status"] == "done"
    rid = report["node_id"]
    _founder_cannot_read(app, [root, rid] + nodes, INTERVIEW_WORDS)
    # The author reads all of it.
    for nid in [root, rid] + nodes:
        assert _can_read(app, "mara", nid), nid


def test_send_shares_exactly_the_report_and_take_back_closes_it(app):
    _code, root, nodes = _interviewed(app)
    rid = _report(app)["node_id"]
    mara = _client(app, "mara")
    r = mara.post("/api/cohort/report/send")
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["booking_url"] == "https://cal.example/peter"
    assert r.get_json()["report"]["shared_with_founder"] is True
    report = _db.session.get(Node, rid)
    assert report.privacy_level == "circles"
    from backend.utils.circles import reader_ids
    assert reader_ids(rid) == {_user("peter").id}
    # He reads exactly that node, finds it under Shared with you, and gets
    # the usual note; nothing else of hers.
    assert _can_read(app, "peter", rid)
    assert _shared_ids(app, "peter") == [rid]
    assert UserNotification.query.filter_by(
        user_id=_user("peter").id, node_id=rid).count() == 1
    for nid in [root] + nodes:
        assert not _can_read(app, "peter", nid), nid
    for who in ("ben", "chidi", "eve"):
        assert not _can_read(app, who, rid), who
    me = mara.get("/api/cohort/me").get_json()["invitation"]
    assert me["state"] == "report_sent"
    assert me["booking_url"] == "https://cal.example/peter"
    # No new draft over a sent report, and no "send nothing" either.
    assert mara.post("/api/cohort/report/draft",
                     json={"regenerate": True}).status_code == 409
    assert mara.post("/api/cohort/report/skip").status_code == 409

    # Take-back: the normal edit of the entry's level.
    r = mara.put(f"/api/nodes/{rid}", json={
        "content": report.get_content(), "privacy_level": "private"})
    assert r.status_code == 200, r.get_json()
    assert not _can_read(app, "peter", rid)
    assert _shared_ids(app, "peter") == []
    me = mara.get("/api/cohort/me").get_json()["invitation"]
    assert me["report"]["shared_with_founder"] is False


def test_send_refuses_an_empty_report_and_send_nothing_shares_nothing(app):
    _interviewed(app)
    rid = _report(app)["node_id"]
    mara = _client(app, "mara")
    node = _db.session.get(Node, rid)
    node.set_content("   \n  ")
    _db.session.commit()
    r = mara.post("/api/cohort/report/send")
    assert r.status_code == 400 and r.get_json()["code"] == "cohort_report_empty"
    r = mara.post("/api/cohort/report/skip")
    assert r.status_code == 200
    assert r.get_json()["booking_url"] == "https://cal.example/peter"
    node = _db.session.get(Node, rid)
    assert node.privacy_level == "private"
    assert not _can_read(app, "peter", rid)
    assert _shared_ids(app, "peter") == []
    assert ch.invitation_state(CohortInvitation.query.one()) == "report_skipped"


def test_a_report_written_by_hand_needs_no_model(app, _stubs):
    cid = _cohort(app)
    code = _invite(app, cid)
    _claim(app, "mara", code)
    mara = _user("mara")
    mara.default_ai_usage = "none"
    _db.session.commit()
    c = _client(app, "mara")
    # No AI means no AI: no interview, no draft...
    r = c.post("/api/cohort/interview/start", json={"mode": "text"})
    assert r.status_code == 403 and r.get_json()["code"] == "ai_usage_none"
    # ...but the headings to write it herself, and Send.
    r = c.post("/api/cohort/report/draft", json={"blank": True})
    assert r.status_code == 201
    # Her own writing: the page must not say Loore wrote it.
    assert r.get_json()["report"]["written_by_loore"] is False
    node = _db.session.get(Node, r.get_json()["report"]["node_id"])
    assert node.get_content().startswith("# My report for Peter")
    assert "## My questions for Peter" in node.get_content()
    assert c.post("/api/cohort/report/send").status_code == 200
    assert _stubs == []


# ── The interview ───────────────────────────────────────────────────────

def test_the_interview_starts_private_with_its_prompt_and_an_opening(app):
    cid = _cohort(app)
    code = _invite(app, cid)
    _claim(app, "mara", code)
    started = _start(app)
    assert started["created"] is True
    root = _db.session.get(Node, started["interview_node_id"])
    mara = _user("mara")
    assert root.get_prompt_key() == "cohort_interview"
    assert root.privacy_level == "private" and root.parent_id is None
    assert (root.user_id, root.human_owner_id) == (mara.id, mara.id)
    text = root.get_content()
    # The person's own agentic prompt, then the interview section filled.
    assert "{user_profile}" in text            # her own prompt's placeholder
    assert ABOUT in text and "invited by Peter" in text
    # The founder's letter never acts as a placeholder in her prompt.
    assert "Dear Mara, I read your thread on (user_profile)" in text
    assert "{invitation_letter}" not in text and "{cohort_about}" not in text
    # The way back to the report is named as the web app labels it: the
    # cohort's name, the link at the top of the page.
    assert "{cohort_name}" not in text
    assert 'go back with the "First Eight" link at the top of the page' in text
    opening = _db.session.get(Node, started["opening_node_id"])
    assert opening.parent_id == root.id and opening.node_type == "llm"
    assert opening.human_owner_id == mara.id
    assert started["open"]["text"] == (
        f"/node/{opening.id}?awaitLlm={opening.id}")
    assert started["open"]["voice"] == f"/voice?resume={opening.id}"
    inv = _inv(code)
    assert inv.interview_node_id == root.id
    assert ch.invitation_state(inv) == "interview_started"
    # Idempotent: the same thread, no second opening.
    again = _start(app)
    assert again["created"] is False
    assert again["interview_node_id"] == root.id
    assert Node.query.filter_by(parent_id=root.id).count() == 1
    # The thread is agentic (tools, mode notes) like Voice and Text mode.
    from backend.utils.session_helpers import chain_has_agentic_prompt
    assert chain_has_agentic_prompt([root])
    me = _client(app, "mara").get("/api/cohort/me").get_json()["invitation"]
    assert me["interview"]["node_id"] == root.id
    assert me["interview"]["tip_node_id"] == opening.id


def test_the_prompts_are_listed_for_cohort_people_and_not_editable(app):
    cid = _cohort(app)
    code = _invite(app, cid)
    _claim(app, "mara", code)
    keys = {x["prompt_key"]: x for x in _client(app, "mara").get(
        "/api/prompts/").get_json()["prompts"]}
    for key in ("cohort_interview", "cohort_report", "cohort_intro"):
        assert keys[key]["editable"] is False
        r = _client(app, "mara").put(f"/api/prompts/{key}",
                                     json={"content": "mine"})
        assert r.status_code == 403
    assert keys["textmode"]["editable"] is True
    ben_keys = [x["prompt_key"] for x in _client(app, "ben").get(
        "/api/prompts/").get_json()["prompts"]]
    assert "cohort_report" not in ben_keys


# ── The report task ─────────────────────────────────────────────────────

def test_the_report_reads_only_her_own_interview_and_no_ai_none(app, _stubs):
    _code, root, nodes = _interviewed(app)
    report = _report(app)
    assert report["task_status"] == "done"
    assert report["written_by_loore"] is True
    call = _stubs[-1]
    assert INTERVIEW_WORDS in call["text"]
    assert NONE_WORDS not in call["text"]          # marked None
    assert ELSEWHERE_WORDS not in call["text"]     # another thread
    assert "Interviewer:\nHello Mara" in call["text"]
    assert "addressed to Peter" in call["system"]
    # Room for a model's reasoning and the text (#366).
    assert call["max_tokens"] == ch.draft_output_cap("gpt-5")
    assert call["max_tokens"] >= 16000
    mara = _user("mara")
    assert call["user_id"] == mara.id
    assert call["model_id"] == "gpt-5"   # the thread's model, hers
    node = _db.session.get(Node, report["node_id"])
    assert (node.user_id, node.human_owner_id) == (mara.id, mara.id)
    assert node.privacy_level == "private" and node.node_type == "user"
    assert node.ai_usage == "chat" and node.parent_id is None
    cost = APICostLog.query.filter_by(request_type="cohort_report").one()
    assert cost.user_id == mara.id
    # Re-running replaces nothing; regenerate makes a new draft and keeps
    # the old one.
    c = _client(app, "mara")
    assert c.post("/api/cohort/report/draft", json={}).status_code == 200
    assert len(_stubs) == 1
    r = c.post("/api/cohort/report/draft", json={"regenerate": True})
    assert r.status_code == 202
    new_id = c.get("/api/cohort/report").get_json()["report"]["node_id"]
    assert new_id != node.id and _db.session.get(Node, node.id) is not None
    # Headings to write under herself, over the draft: no longer Loore's.
    r = c.post("/api/cohort/report/draft",
               json={"regenerate": True, "blank": True})
    assert r.status_code == 201
    assert r.get_json()["report"]["written_by_loore"] is False


def test_the_report_task_refuses_a_thread_that_is_not_hers(app, _stubs):
    _code, root, _nodes = _interviewed(app)
    inv = CohortInvitation.query.one()
    # A ben thread written under the interview prompt key: still not hers.
    other = Node(user_id=_user("ben").id, human_owner_id=_user("ben").id,
                 node_type="user", privacy_level="private", ai_usage="chat",
                 prompt_key="cohort_interview")
    other.set_content("ben's prompt")
    _db.session.add(other)
    _db.session.flush()
    child = Node(user_id=_user("ben").id, human_owner_id=_user("ben").id,
                 parent_id=other.id, node_type="user",
                 privacy_level="private", ai_usage="chat")
    child.set_content("BEN-SECRET")
    _db.session.add(child)
    inv.interview_node_id = other.id
    inv.report_task_status = ch.PENDING
    inv.report_task_started_at = datetime.utcnow()
    _db.session.commit()
    cohort_tasks.run_report_draft(inv.id)
    assert CohortInvitation.query.one().report_task_status == ch.NOTHING
    assert _stubs == []


def test_the_report_task_respects_an_account_set_to_none(app, _stubs):
    _interviewed(app)
    inv = CohortInvitation.query.one()
    mara = _user("mara")
    mara.default_ai_usage = "none"
    _db.session.commit()
    r = _client(app, "mara").post("/api/cohort/report/draft", json={})
    assert r.status_code == 403 and r.get_json()["code"] == "ai_usage_none"
    # Switched after the click: the task checks again and calls nothing.
    inv.report_task_status = ch.PENDING
    inv.report_task_started_at = datetime.utcnow()
    _db.session.commit()
    cohort_tasks.run_report_draft(inv.id)
    assert CohortInvitation.query.one().report_task_status == ch.FAILED
    assert _stubs == []


# ── Admitting, the circles Loore keeps, the group page ──────────────────

def _admitted_group(app):
    """mara, ben and chidi invited and claimed; mara and ben admitted."""
    cid = _cohort(app)
    codes = {}
    for who in ("mara", "ben", "chidi"):
        codes[who] = _invite(app, cid, handle=who)
        assert _claim(app, who, codes[who]).status_code == 200
    p = _client(app, "peter")
    for who in ("mara", "ben"):
        iid = _inv(codes[who]).id
        assert p.post(f"/api/admin/cohorts/{cid}/invitations/{iid}/call_done"
                      ).status_code == 200
        r = p.post(f"/api/admin/cohorts/{cid}/invitations/{iid}/admit")
        assert r.status_code == 200 and r.get_json()["invitation"][
            "state"] == "admitted"
    return cid, codes


def _members_of(owner, cohort_id):
    circle = Circle.query.filter_by(user_id=_user(owner).id,
                                    cohort_id=cohort_id).one()
    return circle, sorted(u.username for u in User.query.join(
        CircleMember, CircleMember.user_id == User.id).filter(
        CircleMember.circle_id == circle.id))


def test_admit_makes_circles_both_ways_and_close_removes(app):
    cid, codes = _admitted_group(app)
    assert _members_of("peter", cid)[1] == ["ben", "mara"]
    assert _members_of("mara", cid)[1] == ["ben", "peter"]
    circle, members = _members_of("ben", cid)
    assert members == ["mara", "peter"] and circle.name == "First Eight"
    # A member reads also what the group shared before they joined.
    assert all(m.entries_from is None for m in CircleMember.query)
    assert Circle.query.filter_by(user_id=_user("chidi").id).count() == 0
    # A renamed group renames its circles.
    _client(app, "peter").put(f"/api/admin/cohorts/{cid}",
                              json={"name": "The Eight"})
    assert _members_of("mara", cid)[0].name == "The Eight"

    iid = _inv(codes["ben"]).id
    r = _client(app, "peter").post(
        f"/api/admin/cohorts/{cid}/invitations/{iid}/close")
    assert r.status_code == 200 and r.get_json()["invitation"][
        "state"] == "closed"
    assert _members_of("peter", cid)[1] == ["mara"]
    assert _members_of("mara", cid)[1] == ["peter"]
    assert Circle.query.filter_by(user_id=_user("ben").id,
                                  cohort_id=cid).count() == 0
    from backend.utils.privacy import circles_enabled_for
    assert circles_enabled_for(_user("mara").id)
    assert not circles_enabled_for(_user("ben").id)


def test_cohort_circles_cannot_be_changed_by_hand(app):
    cid, _codes = _admitted_group(app)
    circle, _ = _members_of("mara", cid)
    c = _client(app, "mara")
    listed = c.get("/api/circles").get_json()["circles"]
    assert listed[0]["cohort"] == {"name": "First Eight",
                                   "slug": "first-eight"}
    for r in (c.put(f"/api/circles/{circle.id}", json={"name": "Mine"}),
              c.delete(f"/api/circles/{circle.id}"),
              c.post(f"/api/circles/{circle.id}/members",
                     json={"username": "chidi", "include_existing": True}),
              c.post(f"/api/circles/{circle.id}/members/preview",
                     json={"username": "chidi"}),
              c.delete(f"/api/circles/{circle.id}/members",
                       json={"username": "ben"})):
        assert r.status_code == 409, r.get_json()
        assert r.get_json()["code"] == "cohort_circle"
        assert "Loore keeps this circle for First Eight" in r.get_json()["error"]
    assert _members_of("mara", cid)[1] == ["ben", "peter"]


def test_the_group_page_is_for_members_only(app):
    cid, codes = _admitted_group(app)
    assert _client(app, "chidi").get("/api/cohort/page").status_code == 403
    assert _client(app, "eve").get("/api/cohort/page").status_code == 403
    assert _client(app, "eve").get(
        "/api/cohort/page?cohort=first-eight").status_code == 403
    mara_circle, _ = _members_of("mara", cid)
    # Mara shares an entry with the group; Ben and Peter find it there.
    r = _client(app, "mara").post("/api/nodes/", json={
        "content": "For the group: my week", "privacy_level": "circles",
        "ai_usage": "chat", "circle_ids": [mara_circle.id]})
    assert r.status_code == 201, r.get_json()
    nid = r.get_json()["id"]
    for who in ("ben", "peter", "mara"):
        page = _client(app, who).get("/api/cohort/page").get_json()
        assert [e["node_id"] for e in page["entries"]] == [nid], who
        assert page["entries"][0]["title"] == "For the group: my week"
        assert page["entries"][0]["author_username"] == "mara"
        assert [m["username"] for m in page["members"]] == [
            "peter", "ben", "mara"]
        assert page["members"][0]["is_founder"] is True
    page = _client(app, "ben").get("/api/cohort/page").get_json()
    assert page["circle_id"] == _members_of("ben", cid)[0].id
    assert page["cohort"]["call_url"] == "https://call.example/eight"
    assert not _can_read(app, "chidi", nid)


def test_an_introduction_from_the_report_shared_with_the_group(app, _stubs):
    cid, codes = _admitted_group(app)
    mara = _client(app, "mara")
    # Before admission there is nothing to introduce from: no report yet.
    r = mara.post("/api/cohort/intro/draft")
    assert r.status_code == 409
    r = mara.post("/api/cohort/report/draft", json={"blank": True})
    rid = r.get_json()["report"]["node_id"]
    node = _db.session.get(Node, rid)
    node.set_content("# My report\n\nMY-EDITED-REPORT-TEXT")
    _db.session.commit()
    r = mara.post("/api/cohort/intro/draft")
    assert r.status_code == 202, r.get_json()
    call = _stubs[-1]
    assert call["text"] == "# My report\n\nMY-EDITED-REPORT-TEXT"
    assert "introduction" in call["system"]
    intro_id = mara.get("/api/cohort/intro").get_json()["intro"]["node_id"]
    assert _db.session.get(Node, intro_id).privacy_level == "private"
    assert not _can_read(app, "ben", intro_id)
    # Marked without sharing: refused. Shared in the same call: done.
    r = mara.post("/api/cohort/intro", json={"node_id": intro_id})
    assert r.status_code == 409
    assert r.get_json()["code"] == "cohort_intro_not_shared"
    r = mara.post("/api/cohort/intro", json={"node_id": intro_id,
                                             "share": True})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["intro"]["shared_with_group"] is True
    assert _can_read(app, "ben", intro_id)
    assert _can_read(app, "peter", intro_id)
    assert not _can_read(app, "chidi", intro_id)
    page = _client(app, "ben").get("/api/cohort/page").get_json()
    intros = {m["username"]: m["intro_node_id"] for m in page["members"]}
    assert intros == {"peter": None, "ben": None, "mara": intro_id}
    # Someone else's entry can't be her introduction.
    ben_node = _answer(app, "ben", None, "ben's own")
    r = mara.post("/api/cohort/intro", json={"node_id": ben_node})
    assert r.status_code == 403
    # Not a member: no introduction.
    assert _client(app, "chidi").post("/api/cohort/intro/draft"
                                      ).status_code == 403


def test_a_member_admitted_later_reads_what_the_group_shared_before(app):
    """Mara is admitted on Monday and shares her introduction on Tuesday;
    Chidi is admitted on Thursday and reads it, on the group page too.
    Ordinary circles keep their default: a person their owner adds reads
    new entries only."""
    cid, codes = _admitted_group(app)
    mara_circle, _ = _members_of("mara", cid)
    r = _client(app, "mara").post("/api/nodes/", json={
        "content": "Hello, I am Mara", "privacy_level": "private",
        "ai_usage": "chat"})
    intro_id = r.get_json()["id"]
    r = _client(app, "mara").post("/api/cohort/intro", json={
        "node_id": intro_id, "share": True})
    assert r.status_code == 200, r.get_json()
    assert not _can_read(app, "chidi", intro_id)

    iid = _inv(codes["chidi"]).id
    p = _client(app, "peter")
    assert p.post(f"/api/admin/cohorts/{cid}/invitations/{iid}/call_done"
                  ).status_code == 200
    assert p.post(f"/api/admin/cohorts/{cid}/invitations/{iid}/admit"
                  ).status_code == 200
    assert _can_read(app, "chidi", intro_id)
    page = _client(app, "chidi").get("/api/cohort/page").get_json()
    assert [e["node_id"] for e in page["entries"]] == [intro_id]
    intros = {m["username"]: m["intro_node_id"] for m in page["members"]}
    assert intros["mara"] == intro_id

    # An ordinary circle of Mara's: Ben, added by her, reads new entries
    # only, as before.
    own = _client(app, "mara").post("/api/circles", json={"name": "Friends"})
    assert own.status_code in (200, 201), own.get_json()
    friends = Circle.query.filter_by(user_id=_user("mara").id,
                                     name="Friends").one()
    r = _client(app, "mara").post(f"/api/circles/{friends.id}/members",
                                  json={"username": "ben",
                                        "include_existing": False})
    assert r.status_code in (200, 201), r.get_json()
    member = CircleMember.query.filter_by(circle_id=friends.id).one()
    assert member.entries_from is not None


def test_leaving_a_group_circle_is_refused_with_the_way_out(app):
    cid, _codes = _admitted_group(app)
    mara_circle, _ = _members_of("mara", cid)
    r = _client(app, "mara").post("/api/nodes/", json={
        "content": "group words", "privacy_level": "circles",
        "ai_usage": "chat", "circle_ids": [mara_circle.id]})
    nid = r.get_json()["id"]
    r = _client(app, "ben").post("/api/circles/leave", json={"entry_id": nid})
    assert r.status_code == 409 and r.get_json()["code"] == "cohort_circle"
    assert "tell Peter" in r.get_json()["error"]
    assert _can_read(app, "ben", nid)


# ── The free period ─────────────────────────────────────────────────────

def test_the_subscription_gate_lets_a_live_invitee_in_until_free_until(app):
    app.config["PAYMENTS_ENABLED"] = True
    cid = _cohort(app)
    code = _invite(app, cid)
    mara = _user("mara")
    mara.plan = "free"
    _db.session.commit()
    c = _client(app, "mara")

    def write():
        return c.post("/api/nodes/", json={"content": "hello",
                                           "privacy_level": "private",
                                           "ai_usage": "chat"})
    # Not claimed yet: not let in, but the claim and the page are open.
    assert write().status_code == 402
    assert c.get("/api/cohort/me").status_code == 200
    assert app.test_client().get(f"/api/invitation/{code}").status_code == 200

    def background():
        """The background jobs' view (profile updates, embeddings)."""
        eligible = {u.id for u in User.profile_eligible_query()}
        paid = {u.id for u in User.query.filter(User.paid_features_clause())}
        return mara.id in eligible, mara.id in paid
    assert background() == (False, False)
    assert _claim(app, "mara", code).status_code == 200
    assert write().status_code == 201
    assert background() == (True, True)
    dash = c.get("/api/dashboard/").get_json()["user"]
    assert dash["subscription"]["state"] == "set_by_hand"
    assert dash["subscription"]["cohort_free_until"] == (
        date.today() + timedelta(days=42)).isoformat()
    assert dash["cohort"]["role"] == "invited"
    assert dash["cohort"]["report_step_done"] is False
    # The free period ends.
    cohort = _db.session.get(Cohort, cid)
    cohort.free_until = date.today() - timedelta(days=1)
    _db.session.commit()
    assert write().status_code == 402
    assert background() == (False, False)
    cohort.free_until = date.today()
    _db.session.commit()
    assert write().status_code == 201
    # A closed invitation lets nobody in.
    _client(app, "peter").post(
        f"/api/admin/cohorts/{cid}/invitations/{_inv(code).id}/close")
    assert write().status_code == 402


# ── The founder's routes ────────────────────────────────────────────────

def test_founder_routes_are_his_own_and_states_follow_the_steps(app):
    cid = _cohort(app)
    eve = _user("eve")
    eve.is_admin = True
    _db.session.commit()
    # Another admin does not see this group; a non-admin is refused.
    assert _client(app, "eve").get("/api/admin/cohorts").get_json()[
        "cohorts"] == []
    r = _client(app, "eve").get(f"/api/admin/cohorts/{cid}/invitations")
    assert r.status_code == 404 and r.get_json() == {"error": "Not found"}
    r = _client(app, "ben").get("/api/admin/cohorts")
    assert r.status_code == 403 and r.get_json() == {"error": "Forbidden"}
    p = _client(app, "peter")
    code = _invite(app, cid)
    iid = _inv(code).id
    # The letter changes only before the invitation is accepted.
    assert p.put(f"/api/admin/cohorts/{cid}/invitations/{iid}",
                 json={"letter": "New letter"}).status_code == 200
    assert p.post(f"/api/admin/cohorts/{cid}/invitations/{iid}/admit"
                  ).status_code == 409
    _claim(app, "mara", code)
    assert p.put(f"/api/admin/cohorts/{cid}/invitations/{iid}",
                 json={"letter": "Again"}).status_code == 409
    rows = p.get(f"/api/admin/cohorts/{cid}/invitations").get_json()[
        "invitations"]
    assert rows[0]["state"] == "claimed" and rows[0]["username"] == "mara"
    assert rows[0]["letter"] == "New letter"
    assert rows[0]["link"] == f"/invitation/{code}"
    # Fields: bad values are refused, the question records when it was set.
    assert p.put(f"/api/admin/cohorts/{cid}", json={
        "booking_url": "javascript:alert(1)"}).status_code == 400
    assert p.put(f"/api/admin/cohorts/{cid}", json={
        "free_until": "soon"}).status_code == 400
    r = p.put(f"/api/admin/cohorts/{cid}", json={
        "question": "What did you notice?",
        "next_call_at": "2026-12-03T18:00:00Z"})
    body = r.get_json()["cohort"]
    assert body["question_set_at"] is not None
    assert body["next_call_at"] == "2026-12-03T18:00:00Z"
    assert body["state_counts"] == {"claimed": 1}


def test_invitation_state_order():
    inv = CohortInvitation(code="x", cohort_id=1)
    assert ch.invitation_state(inv) == "invited"
    t = datetime(2026, 11, 1)
    for field, state in (("first_opened_at", "opened"),
                         ("claimed_at", "claimed"),
                         ("interview_started_at", "interview_started"),
                         ("report_drafted_at", "report_drafted"),
                         ("report_skipped_at", "report_skipped"),
                         ("report_sent_at", "report_sent"),
                         ("call_done_at", "call_done"),
                         ("admitted_at", "admitted"),
                         ("closed_at", "closed"),
                         ("declined_at", "declined")):
        t += timedelta(minutes=1)
        setattr(inv, field, t)
        assert ch.invitation_state(inv) == state, field


def test_a_deleted_account_leaves_the_invitation_without_it(app):
    cid = _cohort(app)
    code = _invite(app, cid)
    _claim(app, "mara", code)
    mara_id = _user("mara").id
    from backend.utils.account_deletion import (
        delete_identity, references_to_user)
    assert references_to_user(mara_id) == {"cohort_invitation.user_id": 1}
    counts, _after = delete_identity(mara_id, notify=False,
                                     reserve_handles=False)
    _db.session.commit()
    assert counts["cohort_invitation.user_id"] == 1
    assert counts["cohort_invitation.closed"] == 1
    assert _db.session.get(User, mara_id) is None
    inv = _inv(code)
    assert inv.user_id is None and inv.claimed_at is not None
    assert inv.closed_at is not None
    # The link stays closed: no letter, and nobody accepts it again.
    body = app.test_client().get(f"/api/invitation/{code}").get_json()
    assert body["state"] == "closed" and "letter" not in body
    assert _claim(app, "chidi", code).status_code == 409
    assert NodeCircle.query.count() == 0


# ── Every share passes the audience route's own gate ────────────────────

def test_share_entry_refuses_what_the_audience_route_refuses(app):
    """share_entry (the audience route's step, also Send and the
    introduction's share) checks the route's gate itself: an account
    without Circles shares nothing, and nothing is written."""
    from backend.routes.nodes import share_entry
    eve = _user("eve")
    nid = _answer(app, "eve", None, "eve's own words")
    with app.test_request_context():
        answer, status = share_entry(_db.session.get(Node, nid), eve, {
            "circle_ids": [], "circle_usernames": ["peter"]})
    assert status == 404
    _db.session.expire_all()
    assert _db.session.get(Node, nid).privacy_level == "private"
    assert answer.get_json() == {"error": "Node not found"}
    with app.test_request_context():
        assert share_entry(None, eve, {})[1] == 404


def test_an_introduction_cannot_share_an_ai_reply_from_anothers_thread(app):
    """mara's AI reply under ben's group entry is hers, but it is written
    in ben's thread: as an introduction it is never shared with her
    group, while the thread is open (the Circles reply rule) and after
    ben took the entry back (the audience route's 404, the same answer
    as for a node that does not exist)."""
    cid, _codes = _admitted_group(app)
    ben_circle, _ = _members_of("ben", cid)
    r = _client(app, "ben").post("/api/nodes/", json={
        "content": "BEN-GROUP-ENTRY", "privacy_level": "circles",
        "ai_usage": "chat", "circle_ids": [ben_circle.id]})
    entry = r.get_json()["id"]
    r = _client(app, "mara").post("/api/nodes/", json={
        "content": "mara replies", "privacy_level": "circles",
        "ai_usage": "chat", "parent_id": entry})
    assert r.status_code == 201, r.get_json()
    reply = r.get_json()["id"]
    r = _client(app, "mara").post(f"/api/nodes/{reply}/llm", json={})
    assert r.status_code == 202, r.get_json()
    answer = _db.session.get(Node, r.get_json()["node_id"])
    answer.set_content("An answer written from ben's entry.")
    answer.llm_task_status = "completed"
    _db.session.commit()
    mara = _client(app, "mara")

    r = mara.post("/api/cohort/intro", json={"node_id": answer.id,
                                             "share": True})
    assert r.status_code == 400
    assert r.get_json()["code"] == "circle_reply_audience"

    entry_node = _db.session.get(Node, entry)
    assert _client(app, "ben").put(f"/api/nodes/{entry}", json={
        "content": entry_node.get_content(),
        "privacy_level": "private"}).status_code == 200
    r = mara.post("/api/cohort/intro", json={"node_id": answer.id,
                                             "share": True})
    assert r.status_code == 404
    assert r.get_json() == {"error": "Node not found"}
    _db.session.expire_all()
    answer = _db.session.get(Node, answer.id)
    assert answer.privacy_level == "private"
    assert NodeCircle.query.filter_by(node_id=answer.id).count() == 0
    assert CohortInvitation.query.filter_by(
        user_id=_user("mara").id).one().intro_node_id is None


# ── The review's findings (M1, M2, W2, W3, W5, W6) and its missing tests ─

def _delete_account(name):
    """The purge's Circles rows, then the account itself, as the account
    deletion runs them (#269)."""
    from backend.utils.account_deletion import delete_identity
    from backend.utils.circles import user_rows_filters
    uid = _user(name).id
    for model, cond, _key in user_rows_filters(uid, everything=True):
        model.query.filter(cond).delete(synchronize_session=False)
    _db.session.commit()
    delete_identity(uid, notify=False, reserve_handles=False)
    _db.session.commit()


def test_a_deleted_members_invitation_is_never_accepted_again(app):
    """M1: eve was admitted and deleted her account. Her link stays
    closed: no letter, chidi can't take her place in the group, and she
    has left every group circle."""
    cid = _cohort(app)
    code = _invite(app, cid, handle=None)
    assert _claim(app, "eve", code).status_code == 200
    ben_code = _invite(app, cid, handle="ben")
    assert _claim(app, "ben", ben_code).status_code == 200
    p = _client(app, "peter")
    for c in (code, ben_code):
        assert p.post(f"/api/admin/cohorts/{cid}/invitations/"
                      f"{_inv(c).id}/admit").status_code == 200
    assert _members_of("peter", cid)[1] == ["ben", "eve"]
    _delete_account("eve")
    body = app.test_client().get(f"/api/invitation/{code}").get_json()
    assert body["state"] == "closed" and "letter" not in body
    assert ch.invitation_state(_inv(code)) == "closed"
    assert _claim(app, "chidi", code).status_code == 409
    assert _client(app, "chidi").get("/api/cohort/page").status_code == 403
    assert _members_of("peter", cid)[1] == ["ben"]
    assert _members_of("ben", cid)[1] == ["peter"]


def test_an_invitation_accepted_once_is_claimed_without_its_account(app):
    """M1, the rule itself: claimed_at alone makes it claimed."""
    cid = _cohort(app)
    code = _invite(app, cid, handle=None)
    inv = _inv(code)
    inv.claimed_at = datetime.utcnow()
    _db.session.commit()
    body = app.test_client().get(f"/api/invitation/{code}").get_json()
    assert body["state"] == "claimed" and "letter" not in body
    r = _claim(app, "chidi", code)
    assert r.status_code == 409 and r.get_json()["code"] == "cohort_taken"
    assert _inv(code).user_id is None


def test_a_draft_whose_worker_died_can_be_asked_for_again(app, _stubs):
    """M2: a status left running past DRAFT_STALE_AFTER reads as failed,
    and writing the report by hand or a new draft works again."""
    _interviewed(app)
    inv = CohortInvitation.query.one()
    c = _client(app, "mara")
    # Still within the time: running, nothing new queued.
    inv.report_task_status = ch.PROCESSING
    inv.report_task_started_at = datetime.utcnow() - timedelta(minutes=1)
    _db.session.commit()
    r = c.post("/api/cohort/report/draft", json={"blank": True})
    assert r.status_code == 202
    assert r.get_json()["report"]["task_status"] == "processing"
    # The worker died long ago.
    inv = CohortInvitation.query.one()
    inv.report_task_started_at = (datetime.utcnow() - ch.DRAFT_STALE_AFTER
                                  - timedelta(minutes=1))
    _db.session.commit()
    assert c.get("/api/cohort/report").get_json()["report"][
        "task_status"] == "failed"
    r = c.post("/api/cohort/report/draft", json={"blank": True})
    assert r.status_code == 201, r.get_json()
    assert r.get_json()["report"]["node_id"] is not None
    # The same for a model draft.
    inv = CohortInvitation.query.one()
    inv.report_task_status = ch.PENDING
    inv.report_task_started_at = datetime(2026, 1, 1)
    _db.session.commit()
    r = c.post("/api/cohort/report/draft", json={"regenerate": True})
    assert r.status_code == 202
    assert r.get_json()["report"]["task_status"] == "done"
    assert len(_stubs) == 1


def test_a_task_acts_only_for_the_draft_it_was_queued_for(app, _stubs):
    """M2: a task that comes after its draft was given up on, or after a
    new one was asked for, calls nothing and saves nothing; one whose
    model call fails ends at failed."""
    _interviewed(app)
    inv = CohortInvitation.query.one()
    now = datetime.utcnow()
    inv.report_task_status = ch.PENDING
    inv.report_task_started_at = now
    _db.session.commit()
    older = ch.stamp_of(now - timedelta(seconds=30))
    assert cohort_tasks.run_report_draft(inv.id, older) is None
    assert _stubs == []
    assert CohortInvitation.query.one().report_task_status == ch.PENDING
    # Stale with its own stamp: given up on, marked failed.
    inv = CohortInvitation.query.one()
    old = datetime.utcnow() - ch.DRAFT_STALE_AFTER - timedelta(minutes=1)
    inv.report_task_started_at = old
    _db.session.commit()
    assert cohort_tasks.run_report_draft(inv.id, ch.stamp_of(old)) is None
    assert _stubs == []
    assert CohortInvitation.query.one().report_task_status == ch.FAILED
    # The call itself fails: failed, no node.
    inv = CohortInvitation.query.one()
    fresh = datetime.utcnow()
    inv.report_task_status = ch.PENDING
    inv.report_task_started_at = fresh
    _db.session.commit()

    def boom(*a, **k):
        raise RuntimeError("provider down")
    real = cohort_tasks._call
    cohort_tasks._call = boom
    try:
        assert cohort_tasks.run_report_draft(inv.id,
                                             ch.stamp_of(fresh)) is None
    finally:
        cohort_tasks._call = real
    inv = CohortInvitation.query.one()
    assert inv.report_task_status == ch.FAILED and inv.report_node_id is None


def test_a_double_click_queues_one_draft(app, monkeypatch):
    """W2: another request queued the draft between this one's check and
    its own dispatch: one draft is queued, not two."""
    _interviewed(app)
    from backend.routes import cohort as routes
    queued = []

    class _Record:
        def delay(self, invitation_id, stamp=None):
            queued.append(stamp)
    monkeypatch.setattr(cohort_tasks, "draft_cohort_report", _Record())
    real = routes._billed_call_refusal

    def the_other_request_was_first(user):
        if not queued:
            inv = CohortInvitation.query.one()
            other = datetime.utcnow()
            CohortInvitation.query.filter_by(id=inv.id).update({
                CohortInvitation.report_task_status: ch.PENDING,
                CohortInvitation.report_task_started_at: other})
            _db.session.commit()
            queued.append(ch.stamp_of(other))
        return real(user)
    monkeypatch.setattr(routes, "_billed_call_refusal",
                        the_other_request_was_first)
    r = _client(app, "mara").post("/api/cohort/report/draft", json={})
    assert r.status_code == 202
    assert r.get_json()["report"]["task_status"] == "pending"
    assert len(queued) == 1


def test_a_mistaken_not_for_me_can_be_reopened_by_the_founder(app):
    """W3: "Not for me" stays one click; the founder can reopen an
    invitation declined before anyone accepted it."""
    cid = _cohort(app)
    code = _invite(app, cid, handle="chidi")
    iid = _inv(code).id
    anon = app.test_client()
    assert anon.post(f"/api/invitation/{code}/decline").status_code == 200
    p = _client(app, "peter")
    assert _client(app, "eve").post(
        f"/api/admin/cohorts/{cid}/invitations/{iid}/reopen"
    ).status_code == 403
    r = p.post(f"/api/admin/cohorts/{cid}/invitations/{iid}/reopen")
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["invitation"]["declined_at"] is None
    body = anon.get(f"/api/invitation/{code}").get_json()
    assert body["state"] == "open" and body["letter"] == LETTER
    assert _claim(app, "chidi", code).status_code == 200
    # Not declined, accepted once, or closed: nothing to reopen.
    r = p.post(f"/api/admin/cohorts/{cid}/invitations/{iid}/reopen")
    assert r.status_code == 409
    assert r.get_json()["code"] == "cohort_not_reopenable"
    assert _client(app, "chidi").post(
        f"/api/invitation/{code}/decline").status_code == 200
    r = p.post(f"/api/admin/cohorts/{cid}/invitations/{iid}/reopen")
    assert r.status_code == 409


def test_an_invitee_who_pays_keeps_their_own_subscription(app):
    """W5: the free period counts as a plan set by hand only for an
    account with no subscription of its own."""
    app.config["PAYMENTS_ENABLED"] = True
    cid = _cohort(app)
    code = _invite(app, cid)
    mara = _user("mara")
    mara.plan = "free"
    mara.subscription_level = "standard"
    mara.subscription_status = "active"
    _db.session.commit()
    assert _claim(app, "mara", code).status_code == 200
    mara = _user("mara")
    assert mara.plan_set_by_hand is False
    assert mara.cohort_free_access is False
    sub = _client(app, "mara").get("/api/dashboard/").get_json()[
        "user"]["subscription"]
    assert sub["state"] == "active" and sub["cohort_free_until"] is None
    # Its level's ceiling still applies.
    app.config["SUBSCRIPTION_SPEND_LIMIT_USD_STANDARD"] = 50.0
    from backend.utils.subscriptions import subscription_spend_limit_usd
    assert subscription_spend_limit_usd(mara) == 50.0
    # Without a subscription the free period applies, as before.
    mara.subscription_status = "ended"
    _db.session.commit()
    assert _user("mara").plan_set_by_hand is True


def test_an_invitation_needs_a_handle_unless_the_dev_setting_is_on(app):
    """W6: without a handle whoever opens the link first would accept it."""
    app.config["COHORT_ALLOW_OPEN_INVITATIONS"] = False
    cid = _cohort(app)
    r = _client(app, "peter").post(
        f"/api/admin/cohorts/{cid}/invitations", json={"letter": "Hi"})
    assert r.status_code == 400
    assert r.get_json()["code"] == "cohort_handle_required"
    assert CohortInvitation.query.count() == 0
    _invite(app, cid, handle="mara")
    assert CohortInvitation.query.count() == 1


def test_declining_after_admission_leaves_the_group(app):
    cid, codes = _admitted_group(app)
    ben_circle, _ = _members_of("ben", cid)
    r = _client(app, "ben").post("/api/nodes/", json={
        "content": "BEN-BEFORE", "privacy_level": "circles",
        "ai_usage": "chat", "circle_ids": [ben_circle.id]})
    before = r.get_json()["id"]
    assert _can_read(app, "mara", before)
    assert _client(app, "mara").post(
        f"/api/invitation/{codes['mara']}/decline").status_code == 200
    assert _members_of("peter", cid)[1] == ["ben"]
    assert _members_of("ben", cid)[1] == ["peter"]
    assert Circle.query.filter_by(user_id=_user("mara").id,
                                  cohort_id=cid).count() == 0
    assert not _can_read(app, "mara", before)
    assert _client(app, "mara").get("/api/cohort/page").status_code in (
        403, 404)


def test_a_closed_member_no_longer_opens_group_entries(app):
    cid, codes = _admitted_group(app)
    mara_circle, _ = _members_of("mara", cid)

    def share(text):
        r = _client(app, "mara").post("/api/nodes/", json={
            "content": text, "privacy_level": "circles", "ai_usage": "chat",
            "circle_ids": [_members_of("mara", cid)[0].id]})
        assert r.status_code == 201, r.get_json()
        return r.get_json()["id"]
    before = share("MARA-BEFORE")
    assert _can_read(app, "ben", before)
    iid = _inv(codes["ben"]).id
    assert _client(app, "peter").post(
        f"/api/admin/cohorts/{cid}/invitations/{iid}/close"
    ).status_code == 200
    after = share("MARA-AFTER")
    for nid in (before, after):
        assert not _can_read(app, "ben", nid), nid
        assert _can_read(app, "peter", nid), nid
    assert _client(app, "ben").get("/api/cohort/page").status_code in (
        403, 404)
    # Circles is off for him with the invitation: nothing is shared with
    # him any more.
    assert _client(app, "ben").get(
        "/api/circles/shared-with-me").status_code == 404


def test_no_introduction_from_a_report_set_to_no_ai(app, _stubs):
    cid, codes = _admitted_group(app)
    mara = _client(app, "mara")
    rid = mara.post("/api/cohort/report/draft",
                    json={"blank": True}).get_json()["report"]["node_id"]
    node = _db.session.get(Node, rid)
    node.set_content("# My report\n\nWords.")
    node.ai_usage = "none"
    _db.session.commit()
    r = mara.post("/api/cohort/intro/draft")
    assert r.status_code == 403 and r.get_json()["code"] == "ai_usage_none"
    assert "report is set to No AI" in r.get_json()["error"]
    # Set to None after the click: the task reads the report's setting
    # again and calls nothing.
    inv = _inv(codes["mara"])
    inv.intro_task_status = ch.PENDING
    inv.intro_task_started_at = datetime.utcnow()
    _db.session.commit()
    cohort_tasks.run_intro_draft(inv.id)
    assert _inv(codes["mara"]).intro_task_status == ch.FAILED
    assert _stubs == []


# ── From the browser walk-through (2026-11-27) ──────────────────────────

def test_a_draft_cut_off_at_the_output_cap_is_failed_not_saved(app, _stubs,
                                                               monkeypatch):
    """The introduction once came back as "I teach drawing at a secondary
    school in" (the reasoning used the cap): never saved as done."""
    cid, codes = _admitted_group(app)
    mara = _client(app, "mara")
    rid = mara.post("/api/cohort/report/draft",
                    json={"blank": True}).get_json()["report"]["node_id"]
    _db.session.get(Node, rid).set_content("# My report\n\nI teach drawing.")
    _db.session.commit()
    cut = []

    def truncated(model_id, system, text, max_tokens, user_id):
        cut.append(max_tokens)
        return {"content": "I teach drawing at a secondary school in",
                "truncated": True, "input_tokens": 10, "output_tokens": 999}
    monkeypatch.setattr(cohort_tasks, "_call", truncated)
    nodes_before = Node.query.count()
    assert mara.post("/api/cohort/intro/draft").status_code == 202
    intro = mara.get("/api/cohort/intro").get_json()["intro"]
    assert intro == {"node_id": None, "task_status": "failed",
                     "shared_with_group": False}
    assert Node.query.count() == nodes_before
    assert cut and cut[0] == ch.draft_output_cap("gpt-5")
    # Asking again works, and a whole answer is saved.
    monkeypatch.setattr(cohort_tasks, "_call",
                        lambda *a, **k: {"content": "I teach drawing.",
                                         "input_tokens": 1,
                                         "output_tokens": 1})
    assert mara.post("/api/cohort/intro/draft").status_code == 202
    intro = mara.get("/api/cohort/intro").get_json()["intro"]
    assert intro["task_status"] == "done" and intro["node_id"]


def test_a_report_cut_off_at_the_output_cap_is_failed_not_saved(
        app, _stubs, monkeypatch):
    _interviewed(app)
    monkeypatch.setattr(
        cohort_tasks, "_call",
        lambda *a, **k: {"content": "# My report for Peter\n\n## Where I",
                         "truncated": True, "input_tokens": 1,
                         "output_tokens": 1})
    report = _report(app)
    assert report["task_status"] == "failed" and report["node_id"] is None


def test_an_interview_with_nothing_of_hers_has_nothing_to_draft(app, _stubs):
    cid = _cohort(app)
    code = _invite(app, cid)
    _claim(app, "mara", code)
    started = _start(app)
    opening = _db.session.get(Node, started["opening_node_id"])
    opening.set_content("Hello Mara. What takes most of your attention?")
    opening.llm_task_status = "completed"
    _db.session.commit()
    report = _report(app)
    assert report["task_status"] == "nothing_to_draft"
    assert report["nothing_reason"] == "empty"   # she said nothing
    assert _stubs == []                      # no call for "Nothing said."
    # Her only answer set to No AI: that is the reason, and it is named.
    _answer(app, "mara", opening.id, "Not for the model.", ai_usage="none")
    r = _client(app, "mara").post("/api/cohort/report/draft",
                                  json={"regenerate": True})
    assert r.status_code in (200, 202), r.get_json()
    report = _client(app, "mara").get("/api/cohort/report").get_json()["report"]
    assert report["task_status"] == "nothing_to_draft"
    assert report["nothing_reason"] == "no_ai"
    assert _stubs == []


def test_a_failed_reply_shows_on_me_and_start_asks_for_it_again(app):
    cid = _cohort(app)
    code = _invite(app, cid)
    _claim(app, "mara", code)
    started = _start(app)
    root = started["interview_node_id"]
    opening = _db.session.get(Node, started["opening_node_id"])
    opening.llm_task_status = "failed"
    opening.llm_task_error = "Loore couldn't write this reply. Try again."
    _db.session.commit()
    me = _client(app, "mara").get("/api/cohort/me").get_json()["invitation"]
    interview = me["interview"]
    assert interview["tip_node_id"] == opening.id
    assert interview["tip_status"] == "failed"
    assert interview["tip_is_opening"] is True
    assert interview["tip_error"] == "Loore couldn't write this reply. Try again."
    # The generic failure text is not a reason: /cohort's sentence says it.
    from backend.utils.error_privacy import REPLY_FAILED_TEXT
    opening.llm_task_error = REPLY_FAILED_TEXT
    _db.session.commit()
    assert _client(app, "mara").get("/api/cohort/me").get_json()[
        "invitation"]["interview"]["tip_error"] is None
    opening.llm_task_error = "Loore couldn't write this reply. Try again."
    _db.session.commit()
    # Try again: a new opening under the root, opened where it is written.
    again = _start(app)
    new_id = again["opening_node_id"]
    assert new_id and new_id != opening.id
    new = _db.session.get(Node, new_id)
    assert new.parent_id == root and new.node_type == "llm"
    assert again["open"]["text"] == f"/node/{new_id}?awaitLlm={new_id}"
    interview = _client(app, "mara").get(
        "/api/cohort/me").get_json()["invitation"]["interview"]
    assert interview["tip_node_id"] == new_id
    assert interview["tip_error"] is None
    # A later reply that failed is asked for again under her message.
    new.llm_task_status = "completed"
    _db.session.commit()
    a1 = _answer(app, "mara", new.id, "My answer.")
    reply = Node(user_id=new.user_id, human_owner_id=_user("mara").id,
                 parent_id=a1, node_type="llm", llm_model="gpt-5",
                 llm_task_status="failed", privacy_level="private",
                 ai_usage="chat")
    reply.set_content("[LLM response generation pending...]")
    _db.session.add(reply)
    _db.session.commit()
    interview = _client(app, "mara").get(
        "/api/cohort/me").get_json()["invitation"]["interview"]
    assert interview["tip_is_opening"] is False
    assert interview["tip_status"] == "failed"
    again = _start(app)
    assert _db.session.get(Node, again["opening_node_id"]).parent_id == a1


def test_the_interview_prompt_mentions_a_profile_only_when_there_is_one(app):
    from backend.models import UserProfile
    cid = _cohort(app)
    code = _invite(app, cid)
    _claim(app, "mara", code)
    started = _start(app)
    text = _db.session.get(Node, started["interview_node_id"]).get_content()
    assert ch.PROFILE_NOTE_NO in text and ch.PROFILE_NOTE_YES not in text
    assert "{profile_note}" not in text
    _db.session.add(UserProfile(user_id=_user("ben").id, content="Ben, a sailor.",
                                generated_by="gpt-5", ai_usage="chat"))
    _db.session.commit()
    assert ch.has_profile(_user("ben").id)
    assert not ch.has_profile(_user("mara").id)


def test_a_note_about_a_group_share_names_the_group(app):
    cid, _codes = _admitted_group(app)
    mara_circle, _ = _members_of("mara", cid)
    r = _client(app, "mara").post("/api/nodes/", json={
        "content": "For the group", "privacy_level": "circles",
        "ai_usage": "chat", "circle_ids": [mara_circle.id]})
    assert r.status_code == 201, r.get_json()
    nid = r.get_json()["id"]
    notes = [n for n in _client(app, "ben").get("/api/updates").get_json()[
        "notifications"] if n.get("node_id") == nid]
    assert len(notes) == 1
    assert notes[0]["group"] == "First Eight"
    assert notes[0]["title"] == "@mara shared an entry with First Eight."
    assert ch.group_of_share(nid, _user("mara").id, _user("chidi").id) is None


def test_a_closed_members_group_entries_go_back_to_only_me(app):
    """After Close, what Ben shared with the group is read by nobody but
    him, and says so: Only me, not "circles" with no circle behind it.
    An entry also shared with someone else keeps that audience."""
    cid, codes = _admitted_group(app)
    ben_circle, _ = _members_of("ben", cid)
    ben = _client(app, "ben")
    only_group = ben.post("/api/nodes/", json={
        "content": "For the group only", "privacy_level": "circles",
        "ai_usage": "chat", "circle_ids": [ben_circle.id]}).get_json()["id"]
    also_chidi = ben.post("/api/nodes/", json={
        "content": "For the group and Chidi", "privacy_level": "circles",
        "ai_usage": "chat", "circle_ids": [ben_circle.id],
        "circle_usernames": "chidi"})
    assert also_chidi.status_code == 201, also_chidi.get_json()
    also_chidi = also_chidi.get_json()["id"]
    assert _can_read(app, "mara", only_group)
    iid = _inv(codes["ben"]).id
    r = _client(app, "peter").post(
        f"/api/admin/cohorts/{cid}/invitations/{iid}/close")
    assert r.status_code == 200
    node = _db.session.get(Node, only_group)
    assert node.privacy_level == "private"
    assert not _can_read(app, "mara", only_group)
    assert not _can_read(app, "peter", only_group)
    kept = _db.session.get(Node, also_chidi)
    assert kept.privacy_level == "circles"
    assert not _can_read(app, "mara", also_chidi)

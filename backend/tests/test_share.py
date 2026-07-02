"""Tests for Upload v1 / Share (SHARE_V1 dark flag).

Covers the ShareDraft model, the ### Share propose→confirm flow (detection,
draft creation, apply_share tool, save-proposal route), the Share CRUD +
publish/revoke lifecycle, the public endpoint's published-only guarantee,
the SHARE_V1 gating (routes 404, tool dropped, no drafts created), and the
share_pending input-draft exclusion regression.

Imports the real llm_completion against stub glue — same pattern as
test_artifacts.py.
"""
import os
import sys
import json
import pytest
from unittest.mock import MagicMock
from flask import Flask

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ENCRYPTION_DISABLED", "true")

from backend.extensions import db as _db  # noqa: E402
from backend.models import (  # noqa: E402
    User, Node, Draft, ShareDraft,
)
from backend.utils.tool_meta import parse_share  # noqa: E402
from backend.utils.share import save_share_draft_from_node  # noqa: E402

# ── Import the real llm_completion against stub glue ─────────────────────
_GLUE = ("backend.celery_app", "backend.llm_providers",
         "backend.tasks.llm_completion")
_saved_glue = {k: sys.modules.get(k) for k in _GLUE}
sys.modules["backend.celery_app"] = MagicMock()
sys.modules["backend.llm_providers"] = MagicMock()
sys.modules.pop("backend.tasks.llm_completion", None)

from backend.tasks.llm_completion import (  # noqa: E402
    _execute_tool_calls, _auto_create_drafts, _detect_share_proposal,
    gated_voice_tools,
)
_lc_mod = sys.modules["backend.tasks.llm_completion"]

for _k, _v in _saved_glue.items():
    if _v is None:
        sys.modules.pop(_k, None)
    else:
        sys.modules[_k] = _v


def _make_app(share_v1=True):
    from flask_login import LoginManager

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["SHARE_V1"] = share_v1

    _db.init_app(app)
    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from backend.routes.share import share_bp
    app.register_blueprint(share_bp, url_prefix="/api/share")
    from backend.routes.drafts import drafts_bp
    app.register_blueprint(drafts_bp, url_prefix="/api/drafts")
    return app


@pytest.fixture
def app():
    app = _make_app()
    with app.app_context():
        _db.create_all()
        user = User(username="tester", public_sharing_enabled=True)
        _db.session.add(user)
        _db.session.commit()
        yield app
        _db.session.rollback()
        _db.drop_all()


@pytest.fixture
def client(app):
    client = app.test_client()
    user = User.query.first()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return client


@pytest.fixture
def share_flag_on():
    """Point the llm_completion module's flask_app.config at a real dict so
    the _auto_create_drafts SHARE_V1 gate reads a deterministic value (the
    stub glue leaves flask_app a MagicMock whose .get is truthy)."""
    prev = _lc_mod.flask_app.config
    _lc_mod.flask_app.config = {"SHARE_V1": True}
    yield
    _lc_mod.flask_app.config = prev


@pytest.fixture
def share_flag_off():
    prev = _lc_mod.flask_app.config
    _lc_mod.flask_app.config = {"SHARE_V1": False}
    yield
    _lc_mod.flask_app.config = prev


SHARE_TEXT = (
    "Happy to make that shareable.\n\n"
    "### Share\nLooking for a thinking partner on consciousness — "
    "I bring two years of daily notes.\n\n"
    "### Share type\nneed\n\n"
    "Say the word and I'll save it."
)


def _mk_llm_node(uid, content):
    node = Node(user_id=uid, node_type="llm", llm_model="test-model")
    node.set_content(content)
    _db.session.add(node)
    _db.session.commit()
    return node


def _mk_share(uid, content="a shareable thought", status="draft",
              share_type="insight"):
    share = ShareDraft(user_id=uid, share_type=share_type, status=status)
    share.set_content(content)
    _db.session.add(share)
    _db.session.commit()
    return share


# ── Model + parser ───────────────────────────────────────────────────────

def test_share_draft_content_roundtrip(app):
    with app.app_context():
        uid = User.query.first().id
        share = _mk_share(uid, "the exact text")
        assert share.get_content() == "the exact text"
        assert share.status == "draft"


def test_parse_share_extracts_content_and_type():
    parsed = parse_share(SHARE_TEXT)
    assert parsed["content"].startswith("Looking for a thinking partner")
    assert parsed["share_type"] == "need"


def test_parse_share_type_takes_first_line_only():
    parsed = parse_share("### Share\nbody\n### Share type\noffering\n"
                         "extra remark that is not the type")
    assert parsed["share_type"] == "offering"


def test_save_share_draft_from_node_falls_back_to_other(app):
    with app.app_context():
        uid = User.query.first().id
        node = _mk_llm_node(uid, "### Share\ntext\n### Share type\nbogus")
        share, err = save_share_draft_from_node(node, uid)
        assert err is None
        assert share.share_type == "other"
        assert share.status == "draft"
        assert share.source_node_id == node.id


# ── Detection + auto-draft creation ──────────────────────────────────────

def test_detect_share_proposal():
    assert _detect_share_proposal("### Share\nsomething") is True
    assert _detect_share_proposal("no headings") is False
    # Exact match only — incidental prose headings don't trigger.
    assert _detect_share_proposal("### Shared context\nblah") is False


def test_auto_create_drafts_creates_share_draft(app, share_flag_on):
    with app.app_context():
        uid = User.query.first().id
        node = _mk_llm_node(uid, SHARE_TEXT)
        results = _auto_create_drafts(SHARE_TEXT, node, [node], uid)
        names = {r["name"]: r for r in results}
        assert "propose_share" in names
        assert names["propose_share"]["apply_status"] == "pending_approval"
        draft = Draft.query.filter_by(
            parent_id=node.id, label="share_pending").first()
        assert draft is not None


def test_auto_create_drafts_skips_share_when_flag_off(app, share_flag_off):
    with app.app_context():
        uid = User.query.first().id
        node = _mk_llm_node(uid, SHARE_TEXT)
        results = _auto_create_drafts(SHARE_TEXT, node, [node], uid)
        assert not any(r["name"] == "propose_share" for r in results)
        assert Draft.query.filter_by(label="share_pending").count() == 0


def test_gated_voice_tools_drops_apply_share_when_off():
    names_off = {t["name"] for t in gated_voice_tools({"SHARE_V1": False})}
    names_on = {t["name"] for t in gated_voice_tools({"SHARE_V1": True})}
    assert "apply_share" not in names_off
    assert "apply_share" in names_on


# ── apply_share tool (voice/text confirm) ────────────────────────────────

def test_apply_share_tool_saves_private_draft(app, share_flag_on):
    with app.app_context():
        uid = User.query.first().id
        origin = _mk_llm_node(uid, SHARE_TEXT)
        # Real chain: the proposal turn auto-creates the pending draft AND
        # writes the propose_share entry into tool_calls_meta.
        auto = _auto_create_drafts(SHARE_TEXT, origin, [origin], uid)
        origin.tool_calls_meta = json.dumps(auto)
        _db.session.commit()
        draft = Draft.query.filter_by(
            parent_id=origin.id, label="share_pending").first()
        assert draft is not None

        results = _execute_tool_calls(
            [{"name": "apply_share", "input": {}}], origin, [origin], uid)
        r = results[0]
        assert r["status"] == "success"
        row = ShareDraft.query.get(r["share_id"])
        assert row.get_content().startswith("Looking for a thinking partner")
        assert row.share_type == "need"
        # Saving NEVER publishes — that's the structural consent guarantee.
        assert row.status == "draft"
        assert Draft.query.filter_by(id=draft.id).first() is None
        meta = json.loads(Node.query.get(origin.id).tool_calls_meta)
        entry = next(e for e in meta if e["name"] == "propose_share")
        assert entry["apply_status"] == "completed"


def test_apply_share_without_pending_draft_errors(app):
    with app.app_context():
        uid = User.query.first().id
        origin = _mk_llm_node(uid, SHARE_TEXT)
        results = _execute_tool_calls(
            [{"name": "apply_share", "input": {}}], origin, [origin], uid)
        assert results[0]["status"] == "error"
        assert ShareDraft.query.count() == 0


# ── save-proposal route (button confirm) ─────────────────────────────────

def test_save_proposal_route(app, client):
    with app.app_context():
        uid = User.query.first().id
        origin = _mk_llm_node(uid, SHARE_TEXT)
        draft = Draft(user_id=uid, parent_id=origin.id, label="share_pending")
        draft.set_content("")
        _db.session.add(draft)
        _db.session.commit()
        origin_id = origin.id

    resp = client.post("/api/share/save-proposal",
                       json={"llm_node_id": origin_id})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["share"]["status"] == "draft"
    with app.app_context():
        row = ShareDraft.query.first()
        assert row.get_content().startswith("Looking for a thinking partner")
        assert Draft.query.filter_by(label="share_pending").count() == 0


def test_save_proposal_route_no_pending(app, client):
    with app.app_context():
        uid = User.query.first().id
        origin = _mk_llm_node(uid, SHARE_TEXT)
        origin_id = origin.id
    resp = client.post("/api/share/save-proposal",
                       json={"llm_node_id": origin_id})
    assert resp.status_code == 404


# ── CRUD + lifecycle ─────────────────────────────────────────────────────

def test_create_edit_publish_revoke_delete_lifecycle(app, client):
    r = client.post("/api/share", json={"content": "I can mentor two people",
                                        "share_type": "offering"})
    assert r.status_code == 201
    sid = r.get_json()["id"]

    r = client.patch(f"/api/share/{sid}",
                     json={"content": "I can mentor two people on writing"})
    assert r.status_code == 200

    r = client.post(f"/api/share/{sid}/publish")
    assert r.status_code == 200
    assert r.get_json()["status"] == "published"

    # Published items are locked against edits — revoke first.
    r = client.patch(f"/api/share/{sid}", json={"content": "sneaky edit"})
    assert r.status_code == 409

    r = client.post(f"/api/share/{sid}/revoke")
    assert r.status_code == 200
    assert r.get_json()["status"] == "revoked"

    # Revoked items can be edited and re-published.
    r = client.patch(f"/api/share/{sid}", json={"content": "edited again"})
    assert r.status_code == 200

    r = client.delete(f"/api/share/{sid}")
    assert r.status_code == 200
    with app.app_context():
        assert ShareDraft.query.count() == 0


def test_share_list_returns_own_items(app, client):
    with app.app_context():
        uid = User.query.first().id
        _mk_share(uid, "one")
        _mk_share(uid, "two", status="published")
    r = client.get("/api/share")
    assert r.status_code == 200
    assert len(r.get_json()["shares"]) == 2


def test_cannot_touch_another_users_share(app, client):
    with app.app_context():
        other = User(username="other")
        _db.session.add(other)
        _db.session.commit()
        share = _mk_share(other.id, "not yours")
        sid = share.id
    assert client.patch(f"/api/share/{sid}",
                        json={"content": "x"}).status_code == 404
    assert client.post(f"/api/share/{sid}/publish").status_code == 404
    assert client.delete(f"/api/share/{sid}").status_code == 404


# ── Public endpoint ──────────────────────────────────────────────────────

def test_public_endpoint_serves_only_published(app, client):
    with app.app_context():
        uid = User.query.first().id
        _mk_share(uid, "private draft", status="draft")
        to_publish = _mk_share(uid, "published piece", status="draft")
        _mk_share(uid, "taken back", status="revoked")
        _db.session.commit()
        pid = to_publish.id
    # Publish through the API — the public page serves public NODES, and
    # publishing is what creates one.
    assert client.post(f"/api/share/{pid}/publish").status_code == 200

    # Anonymous client — no session.
    anon = app.test_client()
    r = anon.get("/api/share/public/tester")
    assert r.status_code == 200
    body = r.get_json()
    assert body["username"] == "tester"
    contents = [s["content"] for s in body["shares"]]
    assert contents == ["published piece"]


def test_public_endpoint_unknown_user_404(app):
    anon = app.test_client()
    assert anon.get("/api/share/public/nobody").status_code == 404


# ── SHARE_V1 flag off: everything 404s ───────────────────────────────────

def test_routes_404_when_flag_off():
    app = _make_app(share_v1=False)
    with app.app_context():
        _db.create_all()
        user = User(username="tester", public_sharing_enabled=True)
        _db.session.add(user)
        _db.session.commit()
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
            sess["_fresh"] = True
        assert client.get("/api/share").status_code == 404
        assert client.post("/api/share",
                           json={"content": "x"}).status_code == 404
        anon = app.test_client()
        assert anon.get("/api/share/public/tester").status_code == 404
        _db.session.rollback()
        _db.drop_all()


# ── Input-draft exclusion regression (share_pending) ─────────────────────

def test_composing_reply_under_share_proposal_does_not_clobber_draft(
        app, client):
    """Same regression class as feedback: composing a text reply under the
    share-proposal node must not hijack or delete the share_pending draft,
    or 'yes save that' text confirmation breaks."""
    with app.app_context():
        uid = User.query.first().id
        proposal = _mk_llm_node(uid, SHARE_TEXT)
        pending = Draft(user_id=uid, parent_id=proposal.id,
                        label="share_pending")
        pending.set_content("")
        _db.session.add(pending)
        _db.session.commit()
        proposal_id, pending_id = proposal.id, pending.id

    r = client.post("/api/drafts/",
                    json={"parent_id": proposal_id,
                          "content": "yes, save that"})
    assert r.status_code == 200
    assert r.get_json()["id"] != pending_id
    with app.app_context():
        after = Draft.query.get(pending_id)
        assert after is not None and after.label == "share_pending"

    d = client.delete(f"/api/drafts/?parent_id={proposal_id}")
    assert d.status_code == 200
    with app.app_context():
        assert Draft.query.get(pending_id) is not None

    with app.app_context():
        uid = User.query.first().id
        proposal = Node.query.get(proposal_id)
        results = _execute_tool_calls(
            [{"name": "apply_share", "input": {}}],
            proposal, [proposal], uid)
        assert results[0]["status"] == "success"
        assert ShareDraft.query.count() == 1

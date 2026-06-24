"""Tests for user artifacts + agentic artifact/feedback tools (issue #158).

Covers: model versioning, latest_per_kind, REST routes (list/get/put/
versions), tool executor handlers (update_artifact, read_artifact,
apply_feedback), the feedback propose→confirm flow, tool_calls_meta content
redaction, status-note injection, and session pinning.

Patterned after test_tts_invalidation.py: sqlite in-memory, minimal
Flask app, ENCRYPTION_DISABLED.
"""
import json
import os
import sys
from unittest.mock import MagicMock

# ── Environment ──────────────────────────────────────────────────────────
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
from flask import Flask  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

from backend.extensions import db as _db  # noqa: E402
from backend.models import (  # noqa: E402
    User, Node, NodeContextArtifact, UserArtifact, UserFeedback, UserTodo,
    UserAIPreferences, Draft,
)

# ── Import the real llm_completion against stub glue ─────────────────────
# (same pattern as test_context_artifact_pinning.py: stub celery_app +
# llm_providers, drop any cached/mocked task module, import, restore.)
_GLUE = ("backend.celery_app", "backend.llm_providers",
         "backend.tasks.llm_completion")
_saved_glue = {k: sys.modules.get(k) for k in _GLUE}
sys.modules["backend.celery_app"] = MagicMock()
sys.modules["backend.llm_providers"] = MagicMock()
sys.modules.pop("backend.tasks.llm_completion", None)

from backend.tasks.llm_completion import (  # noqa: E402
    _execute_tool_calls, _scan_proposal_statuses, _mark_status_reported,
    _auto_create_drafts, _detect_feedback_proposal, _redact_tool_input,
    _retrieval_injection_text,
    get_user_artifacts_context, get_user_ai_preferences_content,
)

for _k, _v in _saved_glue.items():
    if _v is None:
        sys.modules.pop(_k, None)
    else:
        sys.modules[_k] = _v


def _make_app():
    from flask_login import LoginManager

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True

    _db.init_app(app)
    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from backend.routes.artifacts import artifacts_bp
    app.register_blueprint(artifacts_bp, url_prefix="/api/artifacts")
    from backend.routes.feedback import feedback_bp
    app.register_blueprint(feedback_bp, url_prefix="/api/feedback")
    from backend.routes.drafts import drafts_bp
    app.register_blueprint(drafts_bp, url_prefix="/api/drafts")
    return app


@pytest.fixture
def app():
    app = _make_app()
    with app.app_context():
        _db.create_all()
        user = User(username="tester")
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


def _mk_artifact(user_id, kind, content, title=None, description=None):
    artifact = UserArtifact(
        user_id=user_id, kind=kind,
        title=title or kind.title(), generated_by="test",
        description=description,
    )
    artifact.set_content(content)
    _db.session.add(artifact)
    _db.session.commit()
    return artifact


def _mk_todo(user_id, content, ai_usage="chat"):
    todo = UserTodo(user_id=user_id, generated_by="test", ai_usage=ai_usage)
    todo.set_content(content)
    _db.session.add(todo)
    _db.session.commit()
    return todo


# ── Model ────────────────────────────────────────────────────────────────

def test_latest_for_returns_newest_version(app):
    with app.app_context():
        uid = User.query.first().id
        _mk_artifact(uid, "memory", "v1")
        _mk_artifact(uid, "memory", "v2")
        latest = UserArtifact.latest_for(uid, "memory")
        assert latest.get_content() == "v2"


def test_latest_per_kind(app):
    with app.app_context():
        uid = User.query.first().id
        _mk_artifact(uid, "memory", "m1")
        _mk_artifact(uid, "memory", "m2")
        _mk_artifact(uid, "reading-list", "books", title="Reading List")
        latest = UserArtifact.latest_per_kind(uid)
        assert set(latest) == {"memory", "reading-list"}
        assert latest["memory"].get_content() == "m2"


# ── Routes ───────────────────────────────────────────────────────────────

def test_list_includes_empty_defaults(app, client):
    resp = client.get("/api/artifacts/")
    assert resp.status_code == 200
    kinds = {a["kind"] for a in resp.get_json()["artifacts"]}
    assert {"memory", "scratchpad"} <= kinds


def test_put_creates_versions_and_get_returns_latest(app, client):
    r1 = client.put("/api/artifacts/memory", json={"content": "first"})
    assert r1.status_code == 200
    r2 = client.put("/api/artifacts/memory", json={"content": "second"})
    assert r2.get_json()["artifact"]["version_number"] == 2

    got = client.get("/api/artifacts/memory").get_json()["artifact"]
    assert got["content"] == "second"

    versions = client.get(
        "/api/artifacts/memory/versions").get_json()["versions"]
    assert len(versions) == 2
    assert versions[0]["version_number"] == 2


def test_put_custom_kind_with_title(app, client):
    resp = client.put("/api/artifacts/reading-list", json={
        "content": "- book", "title": "Reading List"})
    artifact = resp.get_json()["artifact"]
    assert artifact["kind"] == "reading-list"
    assert artifact["title"] == "Reading List"


def test_description_default_store_and_carry_forward(app, client):
    # Built-in default exposes its pre-filled description.
    items = client.get("/api/artifacts/").get_json()["artifacts"]
    memory = next(a for a in items if a["kind"] == "memory")
    assert memory["description"]  # non-empty default

    # Explicit description on a custom kind is stored and returned.
    r = client.put("/api/artifacts/reading-list", json={
        "content": "- book", "title": "Reading List",
        "description": "Books to read"})
    assert r.get_json()["artifact"]["description"] == "Books to read"

    # Omitting description on a later version carries it forward.
    client.put("/api/artifacts/reading-list", json={"content": "- book2"})
    got = client.get("/api/artifacts/reading-list").get_json()["artifact"]
    assert got["description"] == "Books to read"
    assert got["content"] == "- book2"


def test_put_rejects_bad_kind(app, client):
    assert client.put(
        "/api/artifacts/Bad Kind!", json={"content": "x"}
    ).status_code == 400


def test_get_unknown_custom_kind_404(app, client):
    assert client.get("/api/artifacts/nope").status_code == 404


def test_version_ownership_enforced(app, client):
    with app.app_context():
        other = User(username="other")
        _db.session.add(other)
        _db.session.commit()
        foreign = _mk_artifact(other.id, "memory", "secret")
        foreign_id = foreign.id
    assert client.get(
        f"/api/artifacts/versions/{foreign_id}").status_code == 403


# ── Tool executor ────────────────────────────────────────────────────────

def _run_tool(app, name, inp, uid):
    llm_node = MagicMock()
    llm_node.llm_model = "test-model"
    results = _execute_tool_calls(
        [{"name": name, "input": inp}], llm_node, [], uid)
    return results[0]


def test_update_artifact_tool_creates_and_versions(app):
    with app.app_context():
        uid = User.query.first().id
        r1 = _run_tool(app, "update_artifact",
                       {"kind": "memory", "updated_content": "fact A"}, uid)
        assert r1["status"] == "success"
        assert r1["created"] is True

        r2 = _run_tool(app, "update_artifact",
                       {"kind": "memory", "updated_content": "fact A+B"}, uid)
        assert r2["created"] is False
        assert UserArtifact.latest_for(uid, "memory").get_content() == "fact A+B"
        assert UserArtifact.query.filter_by(
            user_id=uid, kind="memory").count() == 2


def test_update_artifact_tool_rejects_bad_kind(app):
    with app.app_context():
        uid = User.query.first().id
        r = _run_tool(app, "update_artifact",
                      {"kind": "NOT OK", "updated_content": "x"}, uid)
        assert r["status"] == "error"


def test_update_artifact_tool_rejects_reserved_kinds(app):
    """update_artifact must refuse non-UserArtifact kinds (todo / profile /
    recent_context) so it can't create a shadow artifact that diverges from
    the real single-row model. ai_preferences is NOT reserved since Slice 5
    folded it into the artifact model (see the positive test below)."""
    with app.app_context():
        uid = User.query.first().id
        for kind in ("todo", "profile", "recent_context"):
            r = _run_tool(app, "update_artifact",
                          {"kind": kind, "updated_content": "x"}, uid)
            assert r["status"] == "error", kind
            # No shadow UserArtifact row was created.
            assert UserArtifact.query.filter_by(
                user_id=uid, kind=kind).count() == 0, kind
        # The todo error points the model at the proposal path.
        r = _run_tool(app, "update_artifact",
                      {"kind": "todo", "updated_content": "x"}, uid)
        assert "proposal" in r["error"].lower()


def test_update_artifact_writes_ai_preferences(app):
    """Slice 5: ai_preferences is a normal writable UserArtifact kind — the AI
    edits it via update_artifact (no dedicated update_ai_preferences tool)."""
    with app.app_context():
        uid = User.query.first().id
        r = _run_tool(app, "update_artifact",
                      {"kind": "ai_preferences",
                       "updated_content": "Be concise. Don't bring up family."},
                      uid)
        assert r["status"] == "success"
        assert r["kind"] == "ai_preferences"
        assert UserArtifact.latest_for(uid, "ai_preferences").get_content() \
            == "Be concise. Don't bring up family."


def test_update_artifact_tool_fills_description(app):
    """The agentic update_artifact tool must set a description (it didn't
    before — AI writes left it null, which blanked the edit form and tripped
    the mandatory-description check). Built-in kind → built-in default;
    explicit value wins; an update with none carries the previous forward."""
    with app.app_context():
        uid = User.query.first().id
        # Built-in kind, no description given → built-in default.
        r = _run_tool(app, "update_artifact",
                      {"kind": "predictions",
                       "updated_content": "AGI by 2030."}, uid)
        assert r["status"] == "success"
        assert UserArtifact.latest_for(uid, "predictions").description == \
            UserArtifact.DEFAULT_DESCRIPTIONS["predictions"]

        # New custom kind with an explicit description.
        _run_tool(app, "update_artifact",
                  {"kind": "reading-list", "updated_content": "Dune",
                   "description": "Books to read"}, uid)
        assert UserArtifact.latest_for(uid, "reading-list").description == \
            "Books to read"
        # Update with no description carries the previous one forward.
        _run_tool(app, "update_artifact",
                  {"kind": "reading-list", "updated_content": "Dune; Piranesi"},
                  uid)
        assert UserArtifact.latest_for(uid, "reading-list").description == \
            "Books to read"


def test_get_default_kind_null_description_serves_default(app, client):
    """A default-kind row with a null description (e.g. an older AI write)
    serves the built-in default, so the edit form prefills it rather than
    coming up blank."""
    with app.app_context():
        uid = User.query.first().id
        a = UserArtifact(user_id=uid, kind="predictions", title="Predictions",
                         generated_by="claude")  # no description
        a.set_content("AGI by 2030.")
        _db.session.add(a)
        _db.session.commit()
    res = client.get("/api/artifacts/predictions")
    assert res.status_code == 200
    assert res.get_json()["artifact"]["description"] == \
        UserArtifact.DEFAULT_DESCRIPTIONS["predictions"]


def test_read_artifact_tool_returns_ref_not_content(app):
    with app.app_context():
        uid = User.query.first().id
        artifact = _mk_artifact(uid, "reading-list", "secret-books")
        r = _run_tool(app, "read_artifact", {"kind": "reading-list"}, uid)
        assert r["status"] == "success"
        assert r["artifact_id"] == artifact.id
        # Content must never sit in tool meta (plaintext column)
        assert "secret-books" not in json.dumps(r)


def test_read_artifact_tool_unknown_kind(app):
    with app.app_context():
        uid = User.query.first().id
        r = _run_tool(app, "read_artifact", {"kind": "ghost"}, uid)
        assert r["status"] == "error"


def test_read_todo_tool_returns_ref_not_content(app):
    with app.app_context():
        uid = User.query.first().id
        todo = _mk_todo(uid, "buy milk\nfinish slice 3")
        r = _run_tool(app, "read_todo", {}, uid)
        assert r["status"] == "success"
        assert r["todo_id"] == todo.id
        # Content must never sit in tool meta (plaintext column)
        assert "buy milk" not in json.dumps(r)


def test_read_todo_tool_no_todo(app):
    with app.app_context():
        uid = User.query.first().id
        r = _run_tool(app, "read_todo", {}, uid)
        assert r["status"] == "error"


def test_read_todo_tool_ai_blocked(app):
    with app.app_context():
        uid = User.query.first().id
        _mk_todo(uid, "private tasks", ai_usage="none")
        r = _run_tool(app, "read_todo", {}, uid)
        assert r["status"] == "error"


def test_scan_statuses_delivers_todo_content(app):
    """Cross-turn (voice/single-shot) read_todo delivery injects the content
    once, re-resolved from the row, then stops on the next scan."""
    with app.app_context():
        uid = User.query.first().id
        todo = _mk_todo(uid, "the actual tasks")
        node = _node_with_meta(uid, [
            {"name": "read_todo", "status": "success", "todo_id": todo.id},
        ])
        notes, to_mark = _scan_proposal_statuses([node])
        joined = "\n".join(notes)
        assert "the actual tasks" in joined
        assert "current todo list" in joined
        assert len(to_mark) == 1

        _mark_status_reported(to_mark)
        _db.session.commit()
        notes2, to_mark2 = _scan_proposal_statuses([node])
        assert notes2 == []


# ── semantic_search retrieval tool (#155 → #196 loop) ────────────────────

def test_semantic_search_tool_returns_refs_and_reresolves(app, monkeypatch):
    """The tool stores only node ids + scores (no snippet content in meta);
    _retrieval_injection_text re-resolves the snippets from the nodes."""
    with app.app_context():
        uid = User.query.first().id
        n1 = Node(user_id=uid, node_type="llm", llm_model="m",
                  ai_usage="chat")
        n1.set_content("I keep thinking about leaving my job for a startup.")
        n2 = Node(user_id=uid, node_type="user", ai_usage="chat")
        n2.set_content("More reflections on a possible career change.")
        _db.session.add_all([n1, n2])
        _db.session.commit()
        n1_id, n2_id = n1.id, n2.id

        import backend.utils.api_keys as _ak
        import backend.utils.embeddings as _emb
        monkeypatch.setattr(_ak, "get_openai_chat_key", lambda cfg: "key")
        captured = {}

        def fake_retrieve(user_id, query, exclude, key, k=4,
                          min_score=0.35, **kw):
            captured.update(query=query, exclude=exclude)
            return [(n1_id, n1.created_at, "RAW_SNIPPET", 0.81),
                    (n2_id, n2.created_at, "RAW_SNIPPET", 0.62)]
        monkeypatch.setattr(_emb, "retrieve_relevant_snippets", fake_retrieve)

        r = _run_tool(app, "semantic_search",
                      {"query": "doubts about my career"}, uid)
        assert r["status"] == "success"
        assert r["query"] == "doubts about my career"
        assert [m["node_id"] for m in r["matches"]] == [n1_id, n2_id]
        assert r["matches"][0]["score"] == 0.81
        # The raw snippet text must NOT be persisted in tool meta.
        assert "RAW_SNIPPET" not in json.dumps(r)
        # Injection re-resolves the actual node content fresh.
        text = _retrieval_injection_text(r)
        assert "leaving my job" in text
        assert "career change" in text


def test_semantic_search_tool_requires_query(app):
    with app.app_context():
        uid = User.query.first().id
        r = _run_tool(app, "semantic_search", {"query": "   "}, uid)
        assert r["status"] == "error"


def test_semantic_search_injection_skips_opted_out_node(app, monkeypatch):
    """A match whose node was opted out of AI access since the search is
    dropped at injection (privacy re-check)."""
    with app.app_context():
        uid = User.query.first().id
        node = Node(user_id=uid, node_type="user", ai_usage="none")
        node.set_content("private musings")
        _db.session.add(node)
        _db.session.commit()
        r = {"name": "semantic_search", "status": "success", "query": "x",
             "matches": [{"node_id": node.id, "score": 0.9}]}
        assert _retrieval_injection_text(r) is None


# ── Feedback propose → confirm flow ──────────────────────────────────────

FEEDBACK_TEXT = (
    "### Feedback\nThe voice mode feels magical.\n### Feedback category\npraise"
)


def _mk_llm_node(uid, content):
    node = Node(user_id=uid, node_type="llm", llm_model="test-model")
    node.set_content(content)
    _db.session.add(node)
    _db.session.commit()
    return node


def test_detect_feedback_proposal():
    assert _detect_feedback_proposal("### Feedback\nlove it") is True
    assert _detect_feedback_proposal("no headings here") is False
    # Exact match only — incidental prose headings don't trigger.
    assert _detect_feedback_proposal("### Feedback I've heard\nblah") is False


def test_auto_create_drafts_creates_feedback_draft(app):
    with app.app_context():
        uid = User.query.first().id
        node = _mk_llm_node(uid, FEEDBACK_TEXT)
        results = _auto_create_drafts(FEEDBACK_TEXT, node, [node], uid)
        names = {r["name"]: r for r in results}
        assert "propose_feedback" in names
        assert names["propose_feedback"]["apply_status"] == "pending_approval"
        draft = Draft.query.filter_by(
            parent_id=node.id, label="feedback_pending").first()
        assert draft is not None


def test_apply_feedback_tool_submits(app):
    with app.app_context():
        uid = User.query.first().id
        origin = _mk_llm_node(uid, FEEDBACK_TEXT)
        draft = Draft(user_id=uid, parent_id=origin.id,
                      label="feedback_pending")
        draft.set_content("")
        _db.session.add(draft)
        _db.session.commit()

        results = _execute_tool_calls(
            [{"name": "apply_feedback", "input": {}}], origin, [origin], uid)
        r = results[0]
        assert r["status"] == "success"
        row = UserFeedback.query.get(r["feedback_id"])
        assert row.get_content() == "The voice mode feels magical."
        assert row.category == "praise"
        assert row.status == "new"
        # Draft consumed; origin meta marked completed.
        assert Draft.query.filter_by(id=draft.id).first() is None


def test_apply_feedback_without_pending_draft_errors(app):
    with app.app_context():
        uid = User.query.first().id
        origin = _mk_llm_node(uid, FEEDBACK_TEXT)
        results = _execute_tool_calls(
            [{"name": "apply_feedback", "input": {}}], origin, [origin], uid)
        assert results[0]["status"] == "error"
        assert UserFeedback.query.count() == 0


def test_feedback_submit_route(app, client):
    with app.app_context():
        uid = User.query.first().id
        origin = _mk_llm_node(uid, FEEDBACK_TEXT)
        draft = Draft(user_id=uid, parent_id=origin.id,
                      label="feedback_pending")
        draft.set_content("")
        _db.session.add(draft)
        _db.session.commit()
        origin_id = origin.id

    resp = client.post("/api/feedback/submit",
                       json={"llm_node_id": origin_id})
    assert resp.status_code == 200
    with app.app_context():
        row = UserFeedback.query.first()
        assert row.get_content() == "The voice mode feels magical."
        assert row.category == "praise"


def test_feedback_submit_route_no_pending(app, client):
    with app.app_context():
        uid = User.query.first().id
        origin = _mk_llm_node(uid, FEEDBACK_TEXT)
        origin_id = origin.id
    resp = client.post("/api/feedback/submit",
                       json={"llm_node_id": origin_id})
    assert resp.status_code == 404


def test_composing_reply_under_proposal_does_not_clobber_draft(app, client):
    """Regression: confirming a proposal via TEXT must work. Composing a reply
    under the proposal node saves/deletes a composing draft sharing the same
    parent_id; that must not hijack or delete the feedback_pending proposal
    draft (which broke 'yes, send it' text confirmation — the button worked
    only because it never composes a reply)."""
    with app.app_context():
        uid = User.query.first().id
        proposal = _mk_llm_node(uid, FEEDBACK_TEXT)
        fb = Draft(user_id=uid, parent_id=proposal.id,
                   label="feedback_pending")
        fb.set_content("")
        _db.session.add(fb)
        _db.session.commit()
        proposal_id, fb_id = proposal.id, fb.id

    # 1) Composing a reply saves an input draft — a NEW row, not the proposal.
    r = client.post("/api/drafts/",
                    json={"parent_id": proposal_id, "content": "yes, send it"})
    assert r.status_code == 200
    assert r.get_json()["id"] != fb_id
    with app.app_context():
        fb_after = Draft.query.get(fb_id)
        assert fb_after is not None and fb_after.label == "feedback_pending"
        assert fb_after.get_content() == ""  # not overwritten by the reply

    # 2) Sending the reply deletes the input draft, not the proposal draft.
    d = client.delete(f"/api/drafts/?parent_id={proposal_id}")
    assert d.status_code == 200
    with app.app_context():
        assert Draft.query.get(fb_id) is not None

    # 3) apply_feedback (the text-confirm path) now finds the draft + submits.
    with app.app_context():
        uid = User.query.first().id
        proposal = Node.query.get(proposal_id)
        results = _execute_tool_calls(
            [{"name": "apply_feedback", "input": {}}],
            proposal, [proposal], uid)
        assert results[0]["status"] == "success"
        assert UserFeedback.query.count() == 1


# ── tool_calls_meta content redaction (privacy) ──────────────────────────

def test_redact_tool_input_strips_content():
    redacted = _redact_tool_input(
        {"kind": "memory", "updated_content": "secret notes",
         "content": "private", "title": "T"})
    assert redacted["updated_content"] == "[redacted]"
    assert redacted["content"] == "[redacted]"
    # Structural fields survive.
    assert redacted["kind"] == "memory"
    assert redacted["title"] == "T"


def test_update_artifact_does_not_persist_content_in_meta(app):
    with app.app_context():
        uid = User.query.first().id
        r = _run_tool(app, "update_artifact",
                      {"kind": "memory", "updated_content": "secret fact"},
                      uid)
        assert r["status"] == "success"
        # The plaintext-bound meta input must not carry the content.
        assert r["input"]["updated_content"] == "[redacted]"
        # But the encrypted row has the real content.
        assert UserArtifact.latest_for(
            uid, "memory").get_content() == "secret fact"


# ── Status notes ─────────────────────────────────────────────────────────

def _node_with_meta(uid, meta):
    node = Node(user_id=uid, node_type="llm")
    node.set_content("resp")
    node.tool_calls_meta = json.dumps(meta)
    _db.session.add(node)
    _db.session.commit()
    return node


def test_scan_statuses_reports_artifact_tools_once(app):
    with app.app_context():
        uid = User.query.first().id
        artifact = _mk_artifact(uid, "reading-list", "the actual books")
        node = _node_with_meta(uid, [
            {"name": "update_artifact", "status": "success",
             "kind": "memory", "created": False},
            {"name": "read_artifact", "status": "success",
             "kind": "reading-list", "artifact_id": artifact.id},
            {"name": "propose_feedback", "status": "success",
             "apply_status": "completed"},
        ])
        notes, to_mark = _scan_proposal_statuses([node])
        joined = "\n".join(notes)
        assert "Artifact 'memory' was updated." in joined
        assert "the actual books" in joined  # read_artifact content delivery
        assert "feedback-proposal" in joined  # applied-successfully note
        assert len(to_mark) == 3

        _mark_status_reported(to_mark)
        _db.session.commit()
        notes2, to_mark2 = _scan_proposal_statuses([node])
        assert notes2 == []
        assert to_mark2 == []


# ── Pinning ──────────────────────────────────────────────────────────────

def test_attach_pins_artifacts_and_node_resolves_them(app):
    from backend.utils.context_artifacts import attach_context_artifacts
    with app.app_context():
        uid = User.query.first().id
        _mk_artifact(uid, "memory", "pinned-memory-v1")
        node = Node(user_id=uid, node_type="system")
        node.set_content("{user_memory}")
        _db.session.add(node)
        _db.session.flush()

        attach_context_artifacts(node.id, uid)
        _db.session.commit()

        pinned = node.get_user_artifacts()
        assert set(pinned) == {"memory"}
        assert pinned["memory"].get_content() == "pinned-memory-v1"

        # New version after pinning must not change the pinned snapshot
        _mk_artifact(uid, "memory", "newer")
        assert node.get_user_artifacts()["memory"].get_content() == \
            "pinned-memory-v1"


def test_attach_pins_multiple_artifact_kinds(app):
    # Regression: attach_context_artifacts pins one user_artifact row per
    # kind. A (node_id, artifact_type) unique constraint 500'd this whenever
    # a user had 2+ kinds; the key must include artifact_id.
    from backend.utils.context_artifacts import attach_context_artifacts
    with app.app_context():
        uid = User.query.first().id
        _mk_artifact(uid, "memory", "m")
        _mk_artifact(uid, "scratchpad", "s")
        _mk_artifact(uid, "reading-list", "books", title="Reading List")
        node = Node(user_id=uid, node_type="system")
        node.set_content("{user_artifacts_index}")
        _db.session.add(node)
        _db.session.flush()

        attach_context_artifacts(node.id, uid)
        _db.session.commit()  # must not raise IntegrityError

        assert set(node.get_user_artifacts()) == {
            "memory", "scratchpad", "reading-list"}


def test_artifacts_context_resolution_pinned_vs_latest(app):
    with app.app_context():
        uid = User.query.first().id
        v1 = _mk_artifact(uid, "memory", "mem-v1")
        _mk_artifact(uid, "scratchpad", "pad-v1")
        _mk_artifact(uid, "reading-list", "books", title="Reading List")

        node = Node(user_id=uid, node_type="system")
        node.set_content("{user_memory}")
        _db.session.add(node)
        _db.session.flush()
        _db.session.add(NodeContextArtifact(
            node_id=node.id, artifact_type="user_artifact",
            artifact_id=v1.id))
        _db.session.commit()

        # Pinned: only memory v1 (the node binding wins)
        memory, scratchpad, index = get_user_artifacts_context(
            uid, pinned_node=node)
        assert memory == "mem-v1"
        assert scratchpad == ""

        # Fallback (no pinned node): latest of everything
        memory, scratchpad, index = get_user_artifacts_context(uid)
        assert memory == "mem-v1"
        assert scratchpad == "pad-v1"
        assert "reading-list" in index


def test_index_includes_description(app):
    with app.app_context():
        uid = User.query.first().id
        _mk_artifact(uid, "reading-list", "books", title="Reading List",
                     description="Books to read")
        _, _, index = get_user_artifacts_context(uid)
        assert "reading-list" in index
        assert "Books to read" in index


# ── Todo in the index (#158 Slice 3) ──────────────────────────────────────

def test_index_includes_todo_with_content(app):
    with app.app_context():
        uid = User.query.first().id
        _mk_todo(uid, "task one\ntask two")
        _, _, index = get_user_artifacts_context(uid)
        assert "todo" in index
        assert "read_todo" in index
        # No raw todo content leaks into the index line.
        assert "task one" not in index
        # A non-empty todo reports a token estimate, not "(empty)".
        assert "tokens)" in index.split("\n")[0]


def test_index_lists_empty_todo(app):
    """A user with no todo row still sees the surface listed as empty."""
    with app.app_context():
        uid = User.query.first().id
        _, _, index = get_user_artifacts_context(uid)
        assert "todo" in index
        assert "(empty)" in index


def test_index_omits_ai_blocked_todo(app):
    """A todo the user opted out of AI access is not listed at all."""
    with app.app_context():
        uid = User.query.first().id
        _mk_todo(uid, "private tasks", ai_usage="none")
        _, _, index = get_user_artifacts_context(uid)
        assert "read_todo" not in index


# ── AI preferences folded into the artifact model (#158 Slice 5) ───────────

def test_index_excludes_ai_preferences(app):
    """ai_preferences is always-inline (its own {user_ai_preferences} tag), so
    it must NOT appear in the artifacts index — even with content."""
    with app.app_context():
        uid = User.query.first().id
        _mk_artifact(uid, "ai_preferences", "be concise",
                     title="AI Interaction Preferences")
        _, _, index = get_user_artifacts_context(uid)
        assert "ai_preferences" not in index


def test_ai_preferences_content_reads_artifact_only(app):
    """get_user_ai_preferences_content reads the folded UserArtifact only —
    the legacy UserAIPreferences fallback was dropped (the table goes in
    #219), so a bare legacy row is no longer surfaced."""
    with app.app_context():
        uid = User.query.first().id
        # A legacy UserAIPreferences row is NOT a fallback anymore.
        legacy = UserAIPreferences(user_id=uid, generated_by="test")
        legacy.set_content("legacy prefs")
        _db.session.add(legacy)
        _db.session.commit()
        assert get_user_ai_preferences_content(uid) is None

        # The UserArtifact is the source of truth.
        _mk_artifact(uid, "ai_preferences", "folded prefs",
                     title="AI Interaction Preferences")
        assert get_user_ai_preferences_content(uid) == "folded prefs"


def test_backfill_ai_preferences_script(app):
    """The standalone backfill (expand-contract, #219) copies each
    UserAIPreferences version into an ai_preferences UserArtifact (preserving
    order + content) AND repoints the legacy node pins to user_artifact so
    the legacy code paths become removable. Non-destructive + idempotent."""
    import importlib.util
    from datetime import datetime, timedelta

    path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "scripts", "backfill_ai_preferences_artifacts.py")
    spec = importlib.util.spec_from_file_location("_bf_aiprefs", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    with app.app_context():
        uid = User.query.first().id
        base = datetime(2026, 1, 1)
        prefs = []
        for i, text in enumerate(["v1 prefs", "v2 prefs"]):
            p = UserAIPreferences(user_id=uid, generated_by="test",
                                  created_at=base + timedelta(days=i))
            p.set_content(text)
            _db.session.add(p)
            prefs.append(p)
        _db.session.flush()
        # An old-style system node pinning the v2 prefs via the legacy type.
        node = Node(user_id=uid, node_type="system")
        node.set_content("{user_ai_preferences}")
        _db.session.add(node)
        _db.session.flush()
        pin = NodeContextArtifact(
            node_id=node.id, artifact_type="ai_preferences",
            artifact_id=prefs[1].id)
        v2_created_at = prefs[1].created_at
        _db.session.add(pin)
        _db.session.commit()

        # Dry run touches nothing (reports rows + the legacy-pin count).
        assert mod.run_backfill(execute=False) == (1, 2, 1)
        assert UserArtifact.query.filter_by(
            user_id=uid, kind="ai_preferences").count() == 0
        assert pin.artifact_type == "ai_preferences"

        # Execute: both versions copied (order preserved) + pin repointed.
        assert mod.run_backfill(execute=True) == (1, 2, 1)
        arts = UserArtifact.query.filter_by(
            user_id=uid, kind="ai_preferences").order_by(
            UserArtifact.created_at.asc()).all()
        assert [a.get_content() for a in arts] == ["v1 prefs", "v2 prefs"]
        # The pin now points at the backfilled v2 artifact (matched by
        # created_at), as a user_artifact.
        v2_art = UserArtifact.query.filter_by(
            user_id=uid, kind="ai_preferences",
            created_at=v2_created_at).first()
        assert pin.artifact_type == "user_artifact"
        assert pin.artifact_id == v2_art.id
        # Non-destructive: the legacy rows are untouched.
        assert UserAIPreferences.query.filter_by(user_id=uid).count() == 2

        # Idempotent: re-running migrates nobody and finds no legacy pins.
        assert mod.run_backfill(execute=True) == (0, 0, 0)
        assert UserArtifact.query.filter_by(
            user_id=uid, kind="ai_preferences").count() == 2

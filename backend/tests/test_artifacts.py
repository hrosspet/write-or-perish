"""Tests for user artifacts + agentic artifact/feedback tools (issue #158).

Covers: model versioning, latest_per_kind, REST routes (list/get/put/
versions), tool executor handlers (update_artifact, read_artifact,
submit_feedback), status-note injection, and session pinning.

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
    UserAIPreferences,
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


def test_submit_feedback_tool(app):
    with app.app_context():
        uid = User.query.first().id
        r = _run_tool(app, "submit_feedback",
                      {"content": "love the voice mode", "category": "praise"},
                      uid)
        assert r["status"] == "success"
        row = UserFeedback.query.get(r["feedback_id"])
        assert row.get_content() == "love the voice mode"
        assert row.category == "praise"
        assert row.status == "new"


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
            {"name": "submit_feedback", "status": "success",
             "feedback_id": 1},
        ])
        notes, to_mark = _scan_proposal_statuses([node])
        joined = "\n".join(notes)
        assert "Artifact 'memory' was updated." in joined
        assert "the actual books" in joined  # read_artifact content delivery
        assert "Feedback was submitted" in joined
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


def test_ai_preferences_content_prefers_artifact(app):
    """get_user_ai_preferences_content reads the folded UserArtifact; it falls
    back to the legacy UserAIPreferences only when no artifact exists."""
    with app.app_context():
        uid = User.query.first().id
        # Legacy fallback when no artifact yet.
        legacy = UserAIPreferences(user_id=uid, generated_by="test")
        legacy.set_content("legacy prefs")
        _db.session.add(legacy)
        _db.session.commit()
        assert get_user_ai_preferences_content(uid) == "legacy prefs"

        # Once a UserArtifact exists, it wins over the legacy row.
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

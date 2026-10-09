"""A proposal is acted on only as its owner's own live proposal.

Accepting an AI reply's proposal (applying todo changes, filing an issue,
sending feedback, saving shares) runs only when the reply is the
requester's own AI reply, not deleted, and carries the propose_* entry of
that kind (utils/proposals). That holds for the card's routes, the voice
and text tools (llm_completion._execute_tool_calls) and the todo merge
task, which checks again before it reads the proposal. Recordings can't
carry a proposal label, and a recording can't start or finish under a
deleted parent.

In each refusal case alice holds a pending draft on the node, as if one had
been parked there, so only the new rule stops the action. Nothing reaches a
model or GitHub: the model, the GitHub call and the task dispatch are fakes,
and Node.get_content is recorded to show the node was never read.

In-memory SQLite, ENCRYPTION_DISABLED, real routes; the completion task
module and the merge task module are imported for real against stub celery
glue, as in test_share.py and test_retrieval_loop.py.
"""
import importlib
import json
import os
import sys
import tempfile
import types
from datetime import datetime
from unittest.mock import MagicMock

# ── Environment ──────────────────────────────────────────────────────────
os.environ["ENCRYPTION_DISABLED"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("TWITTER_API_KEY", "fake")
os.environ.setdefault("TWITTER_API_SECRET", "fake")
os.environ.setdefault(
    "AUDIO_STORAGE_PATH", tempfile.mkdtemp(prefix="wop-proposal-test-"))

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
    APICostLog, Draft, Node, ShareDraft, User, UserFeedback, UserTodo)


def _import_real(module_name, glue):
    """Import *module_name* for real with *glue* ({module: stub}) in
    sys.modules, then put back what was there, the package attributes
    included, so sibling test modules see their own modules."""
    names = list(glue) + [module_name]
    saved = {k: sys.modules.get(k) for k in names}
    pkg_name, _, attr = module_name.rpartition(".")
    pkg = sys.modules.get(pkg_name)
    had_attr = pkg is not None and hasattr(pkg, attr)
    saved_attr = getattr(pkg, attr, None) if had_attr else None
    sys.modules.update(glue)
    sys.modules.pop(module_name, None)
    try:
        return importlib.import_module(module_name)
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        pkg = sys.modules.get(pkg_name)
        if pkg is not None:
            if had_attr:
                setattr(pkg, attr, saved_attr)
            elif hasattr(pkg, attr):
                delattr(pkg, attr)


_identity_celery = MagicMock()
_identity_celery.celery.task = lambda *a, **k: (lambda fn: fn)

# The completion task module (the voice and text apply_* tools).
_lc = _import_real("backend.tasks.llm_completion", {
    "backend.celery_app": MagicMock(),
    "backend.llm_providers": MagicMock(),
})
# The merge task module, with apply_voice_todo a plain function.
_vtm = _import_real("backend.tasks.voice_todo_merge", {
    "backend.celery_app": _identity_celery,
})
assert callable(_vtm.apply_voice_todo) and not isinstance(
    _vtm.apply_voice_todo, MagicMock)

TODO_TEXT = "### New Tasks\n- buy milk"
ISSUE_TEXT = ("### Issue Title\nRecord button does nothing\n"
              "### Description\nTapping record has no effect.\n"
              "### Category\nbug")
FEEDBACK_TEXT = ("### Feedback\nThe voice mode feels magical.\n"
                 "### Feedback category\npraise")
SHARE_TEXT = (":::share need\nLooking for a thinking partner.\n:::")

# kind: (draft label, propose_* entry, proposal text)
KINDS = {
    "todo": ("todo_pending", "propose_todo", TODO_TEXT),
    "issue": ("github_issue_pending", "propose_github_issue", ISSUE_TEXT),
    "feedback": ("feedback_pending", "propose_feedback", FEEDBACK_TEXT),
    "share": ("share_pending", "propose_share", SHARE_TEXT),
}


@pytest.fixture
def app(tmp_path, monkeypatch):
    import backend.celery_app  # noqa: F401  (import-cycle warm-up)
    from flask_login import LoginManager
    from backend.routes.todo import todo_bp
    from backend.routes.github_issues import github_bp
    from backend.routes.feedback import feedback_bp
    from backend.routes.share import share_bp
    from backend.routes.drafts import drafts_bp
    import backend.routes.drafts as drafts_mod

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["SHARE_V1"] = True
    app.config["SUPPORTED_MODELS"] = {}
    app.config["DEFAULT_LLM_MODEL"] = "gpt-5.5"
    _db.init_app(app)
    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    app.register_blueprint(todo_bp, url_prefix="/api/todo")
    app.register_blueprint(github_bp, url_prefix="/api/github")
    app.register_blueprint(feedback_bp, url_prefix="/api/feedback")
    app.register_blueprint(share_bp, url_prefix="/api/share")
    app.register_blueprint(drafts_bp, url_prefix="/api/drafts")
    monkeypatch.setattr(drafts_mod, "AUDIO_STORAGE_ROOT", tmp_path)
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()


class _Provider:
    """Records each model call; answers with an edit adding the task."""
    calls = []

    @classmethod
    def get_completion(cls, model_id, messages, api_keys, **kwargs):
        cls.calls.append(messages)
        content = json.dumps({"edits": [{
            "old_text": "- an old task",
            "new_text": "- an old task\n- buy milk"}],
            "updated_content": ""})
        return {"content": content, "truncated": False,
                "input_tokens": 100, "output_tokens": 10,
                "total_tokens": 110}


class _FakeLock:
    def acquire(self, blocking=True):
        return True

    def release(self):
        pass


class _FakeRedisClient:
    def lock(self, *args, **kwargs):
        return _FakeLock()


@pytest.fixture
def effects(app, monkeypatch):
    """Every effect an accepted proposal can have, faked and recorded: the
    merge dispatch, the merge's model call, the GitHub call, and each read
    of a node's text."""
    import backend.routes.todo as todo_routes
    import backend.routes.github_issues as gh_routes
    import backend.utils.github as gh_utils
    import backend.utils.prompts as prompts
    # Both the route and the tool start a merge through _start_todo_merge,
    # which imports apply_voice_todo from sys.modules at call time.
    vtm_now = importlib.import_module("backend.tasks.voice_todo_merge")
    dispatch = MagicMock()
    dispatch.delay.return_value = MagicMock(id="task-1")
    monkeypatch.setattr(vtm_now, "apply_voice_todo", dispatch)

    issue = MagicMock(return_value={
        "url": "https://github.com/owner/repo/issues/9", "number": 9})
    monkeypatch.setattr(gh_routes, "create_github_issue", issue)
    monkeypatch.setattr(gh_utils, "create_github_issue", issue)

    _Provider.calls = []
    monkeypatch.setattr(_vtm, "LLMProvider", _Provider)
    monkeypatch.setattr(_vtm, "get_api_keys_for_usage", lambda *a, **k: {})
    monkeypatch.setattr(_vtm, "flask_app", app)
    monkeypatch.setattr(_vtm, "redis", types.SimpleNamespace(
        Redis=types.SimpleNamespace(from_url=lambda url: _FakeRedisClient()),
        exceptions=types.SimpleNamespace(LockNotOwnedError=Exception)))
    monkeypatch.setattr(prompts, "get_user_prompt", lambda uid, key: "MERGE")

    reads = []
    real_get_content = Node.get_content

    def get_content(node):
        reads.append(node.id)
        return real_get_content(node)
    monkeypatch.setattr(Node, "get_content", get_content)

    return types.SimpleNamespace(
        dispatch=dispatch.delay, issue=issue, reads=reads,
        todo_routes=todo_routes)


# ── Builders ─────────────────────────────────────────────────────────────

def _user(name, **kw):
    user = User(username=name, plan="alpha", approved=True,
                default_ai_usage="chat", public_sharing_enabled=True, **kw)
    _db.session.add(user)
    _db.session.commit()
    return user


def _llm_user():
    user = User.query.filter_by(username="gpt-5.5").first()
    if user is None:
        user = User(username="gpt-5.5", twitter_id="llm-gpt-5.5")
        _db.session.add(user)
        _db.session.commit()
    return user


def _msg(owner, parent=None, privacy="private", content="a message"):
    node = Node(user_id=owner.id, human_owner_id=owner.id,
                parent_id=parent.id if parent else None, node_type="user",
                privacy_level=privacy, ai_usage="chat")
    node.set_content(content)
    _db.session.add(node)
    _db.session.commit()
    return node


def _reply(owner, parent, kind=None, privacy="private", ai_usage="chat",
           deleted=False, content=None):
    """An AI reply *owner* asked for. *kind* makes it a proposal of that
    kind: the propose_* entry the completion task writes."""
    meta = None
    if kind is not None:
        meta = json.dumps([{"name": KINDS[kind][1], "status": "success",
                            "apply_status": "pending_approval"}])
    node = Node(user_id=_llm_user().id, human_owner_id=owner.id,
                parent_id=parent.id, node_type="llm", llm_model="gpt-5.5",
                privacy_level=privacy, ai_usage=ai_usage,
                tool_calls_meta=meta,
                deleted_at=datetime.utcnow() if deleted else None)
    node.set_content(content or KINDS[kind or "todo"][2])
    _db.session.add(node)
    _db.session.commit()
    return node


def _pending(user, node, kind):
    draft = Draft(user_id=user.id, parent_id=node.id, label=KINDS[kind][0])
    draft.set_content("")
    _db.session.add(draft)
    _db.session.commit()
    return draft


def _todo(user, content="- an old task", ai_usage="chat"):
    todo = UserTodo(user_id=user.id, generated_by="user", ai_usage=ai_usage)
    todo.set_content(content)
    _db.session.add(todo)
    _db.session.commit()
    return todo


def _client(app, user):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return client


def _meta(node_id):
    return json.loads(Node.query.get(node_id).tool_calls_meta or "[]")


def _case(case, alice, kind):
    """The node alice tries to accept, with her pending draft on it:
    - "other": bob's public proposal of that kind;
    - "deleted": alice's own proposal, deleted;
    - "not_proposal": alice's own AI reply with no propose_* entry."""
    if case == "other":
        bob = _user("bob")
        root = _msg(bob, privacy="public")
        node = _reply(bob, root, kind, privacy="public")
    elif case == "deleted":
        node = _reply(alice, _msg(alice), kind, deleted=True)
    else:
        node = _reply(alice, _msg(alice), None, content=KINDS[kind][2])
    _pending(alice, node, kind)
    return node


ROUTES = {
    "todo": ("/api/todo/apply-draft", "llm_node_id"),
    "issue": ("/api/github/create-issue", "llm_node_id"),
    "feedback": ("/api/feedback/submit", "llm_node_id"),
    "share": ("/api/share/save-proposal", "node_id"),
}


def _nothing_happened(effects, alice, node, kind, todo_before):
    assert effects.dispatch.call_count == 0
    assert _Provider.calls == []
    effects.issue.assert_not_called()
    assert UserFeedback.query.count() == 0
    assert ShareDraft.query.count() == 0
    # The node's text was never read, and its apply state never written.
    assert node.id not in effects.reads
    assert all("apply_status" not in e or e["apply_status"]
               == "pending_approval" for e in _meta(node.id))
    # alice's todo list is unchanged.
    newest = UserTodo.query.filter_by(user_id=alice.id).order_by(
        UserTodo.id.desc()).first()
    assert newest.id == todo_before.id
    assert newest.get_content() == "- an old task"


# ── The proposal routes ──────────────────────────────────────────────────

@pytest.mark.parametrize("kind", list(ROUTES))
@pytest.mark.parametrize("case", ["other", "deleted", "not_proposal"])
def test_route_refuses_a_node_that_is_not_the_users_live_proposal(
        app, effects, case, kind):
    alice = _user("alice")
    todo = _todo(alice)
    node = _case(case, alice, kind)
    path, field = ROUTES[kind]

    resp = _client(app, alice).post(path, json={field: node.id})

    assert resp.status_code == 404
    _nothing_happened(effects, alice, node, kind, todo)
    # The parked draft is left alone (the clean-up script removes it).
    assert Draft.query.filter_by(user_id=alice.id,
                                 label=KINDS[kind][0]).count() == 1


@pytest.mark.parametrize("kind", list(ROUTES))
def test_route_accepts_the_users_own_proposal(app, effects, kind):
    alice = _user("alice")
    _todo(alice)
    node = _reply(alice, _msg(alice), kind)
    _pending(alice, node, kind)
    path, field = ROUTES[kind]

    resp = _client(app, alice).post(path, json={field: node.id})

    assert resp.status_code in (200, 202), resp.get_json()
    if kind == "todo":
        effects.dispatch.assert_called_once_with(
            node.id, "gpt-5.5", alice.id, node.id)
    elif kind == "issue":
        effects.issue.assert_called_once()
        assert effects.issue.call_args.kwargs["username"] == "alice"
    elif kind == "feedback":
        row = UserFeedback.query.one()
        assert row.user_id == alice.id
    else:
        row = ShareDraft.query.one()
        assert row.user_id == alice.id and row.status == "draft"


def test_route_refuses_a_proposal_marked_none(app, effects):
    from backend.routes.todo import TODO_PROPOSAL_REFUSED_MESSAGE
    alice = _user("alice")
    todo = _todo(alice)
    node = _reply(alice, _msg(alice), "todo", ai_usage="none")
    _pending(alice, node, "todo")

    resp = _client(app, alice).post("/api/todo/apply-draft",
                                    json={"llm_node_id": node.id})

    assert resp.status_code == 403
    assert resp.get_json()["error"] == TODO_PROPOSAL_REFUSED_MESSAGE
    _nothing_happened(effects, alice, node, "todo", todo)


def test_share_route_still_saves_the_users_own_written_blocks(app, effects):
    """The share route's other origin: share blocks alice wrote in her own
    entry, with no proposal."""
    alice = _user("alice")
    node = _msg(alice, content=SHARE_TEXT)

    resp = _client(app, alice).post("/api/share/save-proposal",
                                    json={"node_id": node.id})

    assert resp.status_code == 200
    assert ShareDraft.query.one().user_id == alice.id


# ── The voice and text tools ─────────────────────────────────────────────

TOOLS = {
    "todo": "apply_todo_changes",
    "issue": "apply_github_issue",
    "feedback": "apply_feedback",
    "share": "apply_share",
}


@pytest.fixture
def started_merges(monkeypatch):
    import backend.routes.todo as todo_routes
    started = []
    monkeypatch.setattr(todo_routes, "_start_todo_merge",
                        lambda *a, **k: started.append(a) or "task-1")
    return started


def _confirm_turn(alice, under):
    """alice's "yes, do it" under *under*, and the reply placeholder the
    tool runs in."""
    msg = _msg(alice, under, content="yes, do it")
    placeholder = _reply(alice, msg, None, content="[pending]")
    return [under, msg], placeholder


@pytest.mark.parametrize("kind", list(TOOLS))
@pytest.mark.parametrize("case", ["other", "deleted", "not_proposal"])
def test_tool_refuses_a_node_that_is_not_the_users_live_proposal(
        app, effects, started_merges, case, kind):
    alice = _user("alice")
    todo = _todo(alice)
    node = _case(case, alice, kind)
    chain, placeholder = _confirm_turn(alice, node)

    result = _lc._execute_tool_calls(
        [{"name": TOOLS[kind], "input": {}}], placeholder, chain,
        alice.id)[0]

    assert result["status"] == "error"
    assert started_merges == []
    _nothing_happened(effects, alice, node, kind, todo)


@pytest.mark.parametrize("kind", list(TOOLS))
def test_tool_accepts_the_users_own_proposal(
        app, effects, started_merges, kind):
    alice = _user("alice")
    _todo(alice)
    node = _reply(alice, _msg(alice), kind)
    _pending(alice, node, kind)
    chain, placeholder = _confirm_turn(alice, node)

    result = _lc._execute_tool_calls(
        [{"name": TOOLS[kind], "input": {}}], placeholder, chain,
        alice.id)[0]

    assert result["status"] == "success", result
    if kind == "todo":
        assert started_merges[0][1].id == node.id


# ── The merge task ───────────────────────────────────────────────────────

def _run_task(node, user, confirm_node_id=None):
    _vtm.apply_voice_todo(MagicMock(), node.id, "gpt-5.5", user.id,
                          confirm_node_id)
    _db.session.rollback()   # only what was committed counts


@pytest.mark.parametrize("case", ["other", "deleted", "not_proposal"])
def test_task_refuses_a_node_that_is_not_the_users_live_proposal(
        app, effects, case):
    """Called directly, as a parked draft or a retry could start it."""
    alice = _user("alice")
    todo = _todo(alice)
    node = _case(case, alice, "todo")
    meta_before = Node.query.get(node.id).tool_calls_meta

    _run_task(node, alice)

    assert _Provider.calls == []
    assert APICostLog.query.count() == 0
    assert node.id not in effects.reads
    assert Node.query.get(node.id).tool_calls_meta == meta_before
    newest = UserTodo.query.filter_by(user_id=alice.id).order_by(
        UserTodo.id.desc()).first()
    assert newest.id == todo.id


def test_task_refuses_a_proposal_marked_none_before_reading_it(app, effects):
    from backend.routes.todo import TODO_PROPOSAL_REFUSED_MESSAGE
    alice = _user("alice")
    todo = _todo(alice)
    node = _reply(alice, _msg(alice), "todo", ai_usage="none")

    _run_task(node, alice)

    assert _Provider.calls == []
    assert APICostLog.query.count() == 0
    assert node.id not in effects.reads
    entry = next(e for e in _meta(node.id) if e["name"] == "propose_todo")
    assert entry["apply_status"] == "failed"
    assert entry["apply_error"] == TODO_PROPOSAL_REFUSED_MESSAGE
    assert UserTodo.query.filter_by(user_id=alice.id).one().id == todo.id


def test_task_refusal_shows_on_the_users_confirming_reply(app, effects):
    alice = _user("alice")
    _todo(alice)
    node = _case("other", alice, "todo")
    _, placeholder = _confirm_turn(alice, node)
    placeholder.tool_calls_meta = json.dumps([
        {"name": "apply_todo_changes", "status": "success",
         "apply_status": "started"}])
    _db.session.commit()

    _run_task(node, alice, confirm_node_id=placeholder.id)

    entry = _meta(placeholder.id)[0]
    assert entry["apply_status"] == "failed"
    assert entry["apply_error"] == _vtm.PROPOSAL_NOT_FOUND_MESSAGE
    assert _Provider.calls == []


def test_task_merges_the_users_own_proposal(app, effects):
    alice = _user("alice")
    _todo(alice)
    node = _reply(alice, _msg(alice), "todo")

    _run_task(node, alice)

    assert len(_Provider.calls) == 1
    newest = UserTodo.query.filter_by(user_id=alice.id).order_by(
        UserTodo.id.desc()).first()
    assert newest.get_content() == "- an old task\n- buy milk"
    entry = next(e for e in _meta(node.id) if e["name"] == "propose_todo")
    assert entry["apply_status"] == "completed"
    assert entry["todo_id"] == newest.id


# ── Recording drafts ─────────────────────────────────────────────────────

@pytest.fixture
def finalize_task(monkeypatch):
    fake = types.ModuleType("backend.tasks.streaming_transcription")
    fake.finalize_draft_streaming = MagicMock()
    fake.finalize_draft_streaming.delay.return_value.id = "task-1"
    monkeypatch.setitem(
        sys.modules, "backend.tasks.streaming_transcription", fake)
    return fake.finalize_draft_streaming.delay


def _session(user, session_id="session-a"):
    draft = Draft(user_id=user.id, session_id=session_id,
                  streaming_status="recording", label="Voice")
    draft.set_content("")
    _db.session.add(draft)
    _db.session.commit()
    return draft


PROPOSAL_LABELS = [label for label, _, _ in KINDS.values()]


@pytest.mark.parametrize("label", PROPOSAL_LABELS + ["Anything"])
def test_init_refuses_a_label_recordings_dont_carry(app, label):
    alice = _user("alice")
    node = _reply(alice, _msg(alice), "todo")

    resp = _client(app, alice).post("/api/drafts/streaming/init", json={
        "parent_id": node.id, "ai_usage": "chat", "label": label})

    assert resp.status_code == 400
    assert Draft.query.count() == 0


def test_init_refuses_a_deleted_parent(app):
    alice = _user("alice")
    node = _reply(alice, _msg(alice), None, deleted=True, content="hi")

    resp = _client(app, alice).post("/api/drafts/streaming/init", json={
        "parent_id": node.id, "ai_usage": "chat", "label": "Voice"})

    assert resp.status_code == 410
    assert Draft.query.count() == 0


@pytest.mark.parametrize("label", [None, "Voice"])
@pytest.mark.parametrize("threaded", [False, True])
def test_init_still_starts_the_clients_recordings(app, label, threaded):
    """The web and iPhone recorders: voice mode ("Voice") and dictation (no
    label), in a new thread or under the user's own entry."""
    alice = _user("alice")
    body = {"ai_usage": "chat", "privacy_level": "private"}
    if label:
        body["label"] = label
    if threaded:
        body["parent_id"] = _msg(alice).id

    resp = _client(app, alice).post("/api/drafts/streaming/init", json=body)

    assert resp.status_code == 201, resp.get_json()
    draft = Draft.query.one()
    assert draft.label == label and draft.session_id


@pytest.mark.parametrize("label", PROPOSAL_LABELS + ["Anything"])
def test_finalize_refuses_a_label_recordings_dont_carry(
        app, finalize_task, label):
    alice = _user("alice")
    draft = _session(alice)

    resp = _client(app, alice).post(
        "/api/drafts/streaming/session-a/finalize",
        json={"total_chunks": 1, "label": label, "model": "gpt-5.5"})

    assert resp.status_code == 400
    finalize_task.assert_not_called()
    assert Draft.query.get(draft.id).streaming_status == "recording"


def test_finalize_refuses_a_deleted_parent(app, finalize_task):
    alice = _user("alice")
    draft = _session(alice)
    node = _reply(alice, _msg(alice), None, deleted=True, content="hi")

    resp = _client(app, alice).post(
        "/api/drafts/streaming/session-a/finalize",
        json={"total_chunks": 1, "label": "Voice", "model": "gpt-5.5",
              "parent_id": node.id})

    assert resp.status_code == 410
    finalize_task.assert_not_called()
    assert Draft.query.get(draft.id).streaming_status == "recording"


@pytest.mark.parametrize("label", [None, "Voice"])
def test_finalize_still_finishes_the_clients_recordings(
        app, finalize_task, label):
    alice = _user("alice")
    _session(alice)
    parent = _msg(alice)
    body = {"total_chunks": 1, "parent_id": parent.id}
    if label:
        body.update(label=label, model="gpt-5.5")

    resp = _client(app, alice).post(
        "/api/drafts/streaming/session-a/finalize", json=body)

    assert resp.status_code == 202, resp.get_json()
    finalize_task.assert_called_once()

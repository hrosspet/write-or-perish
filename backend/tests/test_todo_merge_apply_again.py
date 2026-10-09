"""A failed todo merge leaves its proposal applicable (#434).

Starting a merge removes the proposal's pending draft (the delete is the
claim, so two requests can't both start one). A failed merge puts the draft
back and marks the proposal ``retryable``, so the card's "Apply again", the
apply-draft route and the voice apply_todo_changes tool find it again. A
successful merge leaves no draft. Not when a newer proposal is pending.

Same harness as test_todo_merge_ai_usage: in-memory SQLite,
ENCRYPTION_DISABLED, celery mocked so the modules import; the model call,
the prompt and the task dispatch are faked.
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
from backend.models import Draft, Node, User, UserTodo  # noqa: E402


@pytest.fixture
def app():
    import backend.celery_app  # noqa: F401
    from flask_login import LoginManager
    from backend.routes.todo import todo_bp

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["SUPPORTED_MODELS"] = {}
    _db.init_app(app)
    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    app.register_blueprint(todo_bp, url_prefix="/api/todo")
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()


GOOD_REPLY = json.dumps({"edits": [{
    "old_text": "- an old task",
    "new_text": "- an old task\n- buy milk"}],
    "updated_content": ""})


class _Provider:
    """Answers each merge call with the next queued reply."""
    replies = []

    @classmethod
    def get_completion(cls, model_id, messages, api_keys, **kwargs):
        content, truncated = cls.replies.pop(0)
        return {"content": content, "truncated": truncated,
                "input_tokens": 100, "output_tokens": 10,
                "total_tokens": 110}


@pytest.fixture
def merge(app, monkeypatch):
    import backend.tasks.voice_todo_merge as vtm
    import backend.utils.prompts as prompts
    _Provider.replies = []
    monkeypatch.setattr(vtm, "LLMProvider", _Provider)
    monkeypatch.setattr(vtm, "get_api_keys_for_usage", lambda *a, **k: {})
    monkeypatch.setattr(prompts, "get_user_prompt", lambda uid, key: "MERGE")
    return vtm


@pytest.fixture
def dispatched(merge, monkeypatch):
    task = MagicMock()
    task.delay.return_value = MagicMock(id="task-1")
    monkeypatch.setattr(merge, "apply_voice_todo", task)
    return task.delay


def _user(name="alice"):
    user = User(username=name, plan="alpha", twitter_id=None, approved=True,
                default_ai_usage="chat")
    _db.session.add(user)
    _db.session.commit()
    return user


def _todo(user_id, content):
    todo = UserTodo(user_id=user_id, generated_by="user", ai_usage="chat")
    todo.set_content(content)
    _db.session.add(todo)
    _db.session.commit()
    return todo


def _proposal(user_id):
    node = Node(user_id=user_id, node_type="llm", llm_model="gpt-5.5",
                ai_usage="chat", tool_calls_meta=json.dumps([{
                    "name": "propose_todo", "status": "success",
                    "apply_status": "pending_approval"}]))
    node.set_content("### New Tasks\n- buy milk")
    _db.session.add(node)
    _db.session.commit()
    return node


def _pending_draft(user_id, proposal):
    draft = Draft(user_id=user_id, parent_id=proposal.id,
                  label="todo_pending")
    draft.set_content("")
    _db.session.add(draft)
    _db.session.commit()
    return draft


def _client(app, user):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return client


def _entry(node_id, name):
    meta = json.loads(Node.query.get(node_id).tool_calls_meta)
    return next(e for e in meta if e["name"] == name)


def _pending_drafts(user_id):
    return Draft.query.filter_by(user_id=user_id,
                                 label="todo_pending").all()


def _run(merge, proposal, user, replies):
    """The merge task's body for the proposal, as the worker runs it."""
    _Provider.replies = list(replies)
    merge._run_merge(proposal, proposal.get_content(), user.id, "gpt-5.5",
                     proposal.id)
    _db.session.rollback()   # only what was committed counts


def test_a_failed_merge_can_be_applied_again(app, merge, dispatched):
    user = _user()
    _todo(user.id, "- an old task")
    proposal = _proposal(user.id)
    _pending_draft(user.id, proposal)
    client = _client(app, user)

    first = client.post("/api/todo/apply-draft",
                        json={"llm_node_id": proposal.id})
    assert first.status_code == 202
    assert _pending_drafts(user.id) == []

    # The model's reply is cut off twice (the merge retries once).
    _run(merge, proposal, user, [("- a\n- a", True), ("- a\n- a", True)])

    entry = _entry(proposal.id, "propose_todo")
    assert entry["apply_status"] == "failed"
    assert entry["apply_error"] == merge.TRUNCATED_MESSAGE
    assert entry["retryable"] is True
    # The proposal is pending again: its draft is back.
    assert [d.parent_id for d in _pending_drafts(user.id)] == [proposal.id]

    again = client.post("/api/todo/apply-draft",
                        json={"llm_node_id": proposal.id})
    assert again.status_code == 202
    assert dispatched.call_count == 2
    # Started again, without the old error.
    entry = _entry(proposal.id, "propose_todo")
    assert entry["apply_status"] == "started"
    assert "apply_error" not in entry and "retryable" not in entry
    confirm = _entry(proposal.id, "apply_todo_changes")
    assert confirm["apply_status"] == "started"
    assert "apply_error" not in confirm

    _run(merge, proposal, user, [(GOOD_REPLY, False)])

    entry = _entry(proposal.id, "propose_todo")
    assert entry["apply_status"] == "completed"
    assert "apply_error" not in entry
    newest = UserTodo.query.filter_by(user_id=user.id).order_by(
        UserTodo.id.desc()).first()
    assert newest.get_content() == "- an old task\n- buy milk"
    # A merge that succeeded leaves nothing pending.
    assert _pending_drafts(user.id) == []


def test_a_second_apply_while_the_merge_runs_starts_nothing(
        app, merge, dispatched):
    user = _user()
    _todo(user.id, "- an old task")
    proposal = _proposal(user.id)
    _pending_draft(user.id, proposal)
    client = _client(app, user)

    assert client.post("/api/todo/apply-draft",
                       json={"llm_node_id": proposal.id}).status_code == 202
    res = client.post("/api/todo/apply-draft",
                      json={"llm_node_id": proposal.id})

    # A second tab: the cards show the merge as started and follow it.
    assert res.status_code == 409
    assert res.get_json()["code"] == "todo_merge_started"
    assert dispatched.call_count == 1


def test_two_requests_that_found_the_same_draft_start_one_merge(
        app, merge, dispatched):
    """A double click: both requests found the draft before either removed
    it. Only the one whose delete removed it starts a merge."""
    from backend.routes.todo import _start_todo_merge
    user = _user()
    proposal = _proposal(user.id)
    draft = _pending_draft(user.id, proposal)
    draft_id = draft.id
    stale = Draft(id=draft_id, user_id=user.id, parent_id=proposal.id,
                  label="todo_pending")

    assert _start_todo_merge(draft, proposal, user.id) == "task-1"
    assert _start_todo_merge(stale, proposal, user.id) is None
    assert dispatched.call_count == 1


def test_a_failed_merge_does_not_come_back_over_a_newer_proposal(
        app, merge, dispatched):
    user = _user()
    _todo(user.id, "- an old task")
    proposal = _proposal(user.id)
    _pending_draft(user.id, proposal)
    client = _client(app, user)
    assert client.post("/api/todo/apply-draft",
                       json={"llm_node_id": proposal.id}).status_code == 202
    # The conversation went on and the model proposed again meanwhile.
    newer = _proposal(user.id)
    _pending_draft(user.id, newer)

    _run(merge, proposal, user, [("- a\n- a", True), ("- a\n- a", True)])

    entry = _entry(proposal.id, "propose_todo")
    assert entry["apply_status"] == "failed"
    assert entry["retryable"] is False
    # Only the newer proposal is pending.
    assert [d.parent_id for d in _pending_drafts(user.id)] == [newer.id]
    res = client.post("/api/todo/apply-draft",
                      json={"llm_node_id": proposal.id})
    assert res.status_code == 404


def test_a_merge_whose_model_call_raised_can_be_applied_again(
        app, merge, dispatched, monkeypatch):
    """The model call raising (a provider outage) goes the same way."""
    user = _user()
    _todo(user.id, "- an old task")
    proposal = _proposal(user.id)
    _pending_draft(user.id, proposal)
    client = _client(app, user)
    assert client.post("/api/todo/apply-draft",
                       json={"llm_node_id": proposal.id}).status_code == 202

    def boom(cls, *a, **k):
        raise RuntimeError("provider unavailable")
    monkeypatch.setattr(_Provider, "get_completion", classmethod(boom))
    merge._run_merge(proposal, proposal.get_content(), user.id,
                     "gpt-5.5", proposal.id)
    _db.session.rollback()

    entry = _entry(proposal.id, "propose_todo")
    assert entry["apply_status"] == "failed"
    assert entry["apply_error"] == "provider unavailable"
    assert entry["retryable"] is True
    assert [d.parent_id for d in _pending_drafts(user.id)] == [proposal.id]

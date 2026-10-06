"""A todo merge sends the newest todo list and the proposal it applies to a
model (orient_apply_todo), so it does not run where AI may not read them
(#396 review: content marked 'none' is never sent to a model).

The merge task checks when it runs, under the per-user lock, and the
apply-draft route checks before starting one; both answer with a message
the web (ProposalInline) and the iPhone app (ProposalCard) show: the
task's apply_error, the route's "error". A chat todo list merges as before.
The apply_todo_changes tool is covered in test_artifacts.

Same harness as test_empty_truncated_bg_output: in-memory SQLite,
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
from backend.models import (  # noqa: E402
    APICostLog, Draft, Node, User, UserTodo)


@pytest.fixture
def app():
    # Warm celery_app first so it resolves the exports <-> profile_batch
    # import cycle in the safe order (see test_profile_regen_resume).
    import backend.celery_app  # noqa: F401
    from flask_login import LoginManager
    from backend.routes.todo import todo_bp

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    # Unknown model -> cost 0 without a KeyError.
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


class _Provider:
    """Records each model call; answers with a merged list."""
    calls = []

    @classmethod
    def get_completion(cls, model_id, messages, api_keys, **kwargs):
        cls.calls.append(messages)
        # The merge's edits reply (#234): add the task after the old one.
        content = json.dumps({"edits": [{
            "old_text": "- an old task",
            "new_text": "- an old task\n- buy milk"}],
            "updated_content": ""})
        return {"content": content, "truncated": False,
                "input_tokens": 100, "output_tokens": 10,
                "total_tokens": 110}


@pytest.fixture
def merge(app, monkeypatch):
    """The merge task module with the model call and the prompt faked."""
    import backend.tasks.voice_todo_merge as vtm
    import backend.utils.prompts as prompts
    _Provider.calls = []
    prompt_reads = []
    monkeypatch.setattr(vtm, "LLMProvider", _Provider)
    monkeypatch.setattr(vtm, "get_api_keys_for_usage", lambda *a, **k: {})
    monkeypatch.setattr(
        prompts, "get_user_prompt",
        lambda uid, key: prompt_reads.append(key) or "MERGE")
    vtm.prompt_reads = prompt_reads
    return vtm


def _user(name="alice"):
    user = User(username=name, plan="alpha", twitter_id=None, approved=True,
                default_ai_usage="chat")
    _db.session.add(user)
    _db.session.commit()
    return user


def _todo(user_id, content, ai_usage):
    todo = UserTodo(user_id=user_id, generated_by="user", ai_usage=ai_usage)
    todo.set_content(content)
    _db.session.add(todo)
    _db.session.commit()
    return todo


def _proposal(user_id, ai_usage="chat", meta=None):
    node = Node(user_id=user_id, node_type="llm", llm_model="gpt-5.5",
                ai_usage=ai_usage,
                tool_calls_meta=json.dumps(meta or [{"name": "propose_todo"}]))
    node.set_content("### New Tasks\n- buy milk")
    _db.session.add(node)
    _db.session.commit()
    return node


def _confirm_node(user_id):
    node = Node(user_id=user_id, node_type="llm", llm_model="gpt-5.5",
                ai_usage="chat", tool_calls_meta=json.dumps([
                    {"name": "apply_todo_changes", "status": "success",
                     "apply_status": "started"}]))
    node.set_content("Done.")
    _db.session.add(node)
    _db.session.commit()
    return node


def _entry(node_id, name):
    meta = json.loads(Node.query.get(node_id).tool_calls_meta)
    return next(e for e in meta if e["name"] == name)


# ── the merge task ───────────────────────────────────────────────────────

def test_merge_over_a_none_todo_list_makes_no_model_call(merge):
    from backend.routes.todo import TODO_MERGE_REFUSED_MESSAGE
    user = _user()
    kept = _todo(user.id, "- SECRET TASK", "none")
    proposal = _proposal(user.id)
    confirm = _confirm_node(user.id)

    merge._run_merge(proposal, proposal.get_content(), user.id, "gpt-5.5",
                     confirm.id)
    _db.session.rollback()   # only what was committed counts

    assert _Provider.calls == []
    assert merge.prompt_reads == []
    for node_id, name in ((proposal.id, "propose_todo"),
                          (confirm.id, "apply_todo_changes")):
        entry = _entry(node_id, name)
        assert entry["apply_status"] == "failed"
        assert entry["apply_error"] == TODO_MERGE_REFUSED_MESSAGE
    # The list is unchanged, and nothing was billed.
    assert UserTodo.query.filter_by(user_id=user.id).one().id == kept.id
    assert APICostLog.query.count() == 0


def test_merge_of_a_proposal_marked_none_makes_no_model_call(merge):
    from backend.routes.todo import TODO_PROPOSAL_REFUSED_MESSAGE
    user = _user()
    _todo(user.id, "- an old task", "chat")
    proposal = _proposal(user.id, ai_usage="none")

    merge._run_merge(proposal, proposal.get_content(), user.id, "gpt-5.5",
                     None)
    _db.session.rollback()

    assert _Provider.calls == []
    entry = _entry(proposal.id, "propose_todo")
    assert entry["apply_status"] == "failed"
    assert entry["apply_error"] == TODO_PROPOSAL_REFUSED_MESSAGE


def test_merge_over_a_chat_todo_list_runs_as_before(merge):
    user = _user()
    _todo(user.id, "- an old task", "chat")
    proposal = _proposal(user.id)

    merge._run_merge(proposal, proposal.get_content(), user.id, "gpt-5.5",
                     None)
    _db.session.rollback()

    assert len(_Provider.calls) == 1
    sent = _Provider.calls[0][-1]["content"][0]["text"]
    assert "- an old task" in sent
    newest = UserTodo.query.filter_by(user_id=user.id).order_by(
        UserTodo.id.desc()).first()
    assert newest.get_content() == "- an old task\n- buy milk"
    entry = _entry(proposal.id, "propose_todo")
    assert entry["apply_status"] == "completed"


# ── the apply-draft route ────────────────────────────────────────────────

def _client(app, user):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return client


def _pending_draft(user_id, proposal):
    draft = Draft(user_id=user_id, parent_id=proposal.id,
                  label="todo_pending")
    draft.set_content("")
    _db.session.add(draft)
    _db.session.commit()
    return draft


@pytest.fixture
def dispatched(merge, monkeypatch):
    task = MagicMock()
    task.delay.return_value = MagicMock(id="task-1")
    monkeypatch.setattr(merge, "apply_voice_todo", task)
    return task.delay


def test_apply_draft_refused_for_a_none_todo_list(app, dispatched):
    from backend.routes.todo import TODO_MERGE_REFUSED_MESSAGE
    user = _user()
    _todo(user.id, "- SECRET TASK", "none")
    proposal = _proposal(user.id)
    draft = _pending_draft(user.id, proposal)

    res = _client(app, user).post("/api/todo/apply-draft",
                                  json={"llm_node_id": proposal.id})

    # The shape both clients show: {"error": ...}.
    assert res.status_code == 403
    assert res.get_json() == {"error": TODO_MERGE_REFUSED_MESSAGE,
                              "code": "ai_usage_none"}
    dispatched.assert_not_called()
    # Nothing started: the proposal stays pending.
    assert Draft.query.get(draft.id) is not None
    assert "apply_status" not in _entry(proposal.id, "propose_todo")


def test_apply_draft_starts_the_merge_for_a_chat_todo_list(app, dispatched):
    user = _user()
    _todo(user.id, "- an old task", "chat")
    proposal = _proposal(user.id)
    _pending_draft(user.id, proposal)

    res = _client(app, user).post("/api/todo/apply-draft",
                                  json={"llm_node_id": proposal.id})

    assert res.status_code == 202
    assert res.get_json()["status"] == "started"
    dispatched.assert_called_once_with(proposal.id, "gpt-5.5", user.id,
                                       proposal.id)

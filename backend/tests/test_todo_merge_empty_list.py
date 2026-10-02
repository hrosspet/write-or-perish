"""#410: a todo merge onto an empty list saved a new task as done and
dropped its link.

A user with no todo list asked Text mode to add one task. The merge sent
"Here is the current full todo list:" followed by nothing, and the model
applied the prompt's rule for completed items "NOT on the todo list" to the
new task. The merge now says the list is empty and which items become
`- [ ]` and `- [x]`; with a list, the message is unchanged. The merge also
records which todo version it produced (`todo_id` in the proposal's
tool_calls_meta), which it never did because the id was read before the
row was flushed.

Same harness as test_todo_merge_ai_usage: in-memory SQLite,
ENCRYPTION_DISABLED, celery mocked so the modules import. No model is
called: LLMProvider.get_completion is replaced on the class, the API keys
are fake, and the real keys are removed from the environment.
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
from backend.models import Node, User, UserPrompt, UserTodo  # noqa: E402

FAKE_KEYS = {"openai": "fake-openai-key", "anthropic": "fake-anthropic-key"}

# The proposal from the issue, with the task text replaced.
PROPOSAL = (
    "Your todo list is currently empty, so I'll propose adding just the "
    "one task under Today.\n\n"
    "### New Tasks\n"
    "- Renew the passport (steps: [node 123](/node/123))\n\n"
    "### Note\n"
    "Starting fresh — this is the only item on the list."
)
MERGED = "## Today\n- [ ] Renew the passport (steps: [node 123](/node/123))"


@pytest.fixture
def app():
    # Warm celery_app first so it resolves the exports <-> profile_batch
    # import cycle in the safe order (see test_profile_regen_resume).
    import backend.celery_app  # noqa: F401

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    # Unknown model -> cost 0 without a KeyError.
    app.config["SUPPORTED_MODELS"] = {}
    _db.init_app(app)
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()


class _FakeProvider:
    """Stands in for LLMProvider.get_completion: records each call and
    answers with `answer`."""
    calls = []
    answer = MERGED

    @classmethod
    def get_completion(cls, model_id, messages, api_keys, **kwargs):
        cls.calls.append({"model_id": model_id, "messages": messages,
                          "api_keys": api_keys})
        return {"content": cls.answer, "truncated": False,
                "input_tokens": 100, "output_tokens": 10,
                "total_tokens": 110}


@pytest.fixture
def merge(app, monkeypatch):
    """The merge task module, with no way to reach a real model."""
    import backend.llm_providers as lp
    import backend.tasks.voice_todo_merge as vtm
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY_CHAT",
                "OPENAI_API_KEY", "OPENAI_API_KEY_CHAT"):
        monkeypatch.delenv(key, raising=False)
    _FakeProvider.calls = []
    _FakeProvider.answer = MERGED
    # On the class itself, so every reference to LLMProvider gets the fake
    # (both names, in case another test re-imported backend.llm_providers).
    for provider in {lp.LLMProvider, vtm.LLMProvider}:
        monkeypatch.setattr(provider, "get_completion",
                            _FakeProvider.get_completion)
    monkeypatch.setattr(vtm, "get_api_keys_for_usage",
                        lambda *a, **k: dict(FAKE_KEYS))
    return vtm


def _user(name="newcomer"):
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


def _proposal(user_id, content=PROPOSAL):
    node = Node(user_id=user_id, node_type="llm", llm_model="claude-opus-4.6",
                ai_usage="chat",
                tool_calls_meta=json.dumps([{"name": "propose_todo"}]))
    node.set_content(content)
    _db.session.add(node)
    _db.session.commit()
    return node


def _propose_todo_entry(node_id):
    meta = json.loads(Node.query.get(node_id).tool_calls_meta)
    return next(e for e in meta if e["name"] == "propose_todo")


def _newest_todo(user_id):
    return UserTodo.query.filter_by(user_id=user_id).order_by(
        UserTodo.id.desc()).first()


def _run(merge, user, proposal):
    merge._run_merge(proposal, proposal.get_content(), user.id,
                     "claude-opus-4.6", None)
    _db.session.rollback()   # only what was committed counts


# ── empty list ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("existing", [None, "", "  \n\n "])
def test_empty_list_merge_says_the_list_is_empty(merge, existing):
    from backend.utils.prompts import load_default_prompt
    user = _user()
    if existing is not None:
        _todo(user.id, existing)
    proposal = _proposal(user.id)

    _run(merge, user, proposal)

    assert len(_FakeProvider.calls) == 1
    call = _FakeProvider.calls[0]
    assert call["api_keys"] == FAKE_KEYS
    system, assistant, user_msg = call["messages"]
    # No custom prompt: the file default, with the #410 rules.
    assert system["content"][0]["text"] == load_default_prompt(
        "orient_apply_todo")
    assert assistant["content"][0]["text"] == PROPOSAL
    assert user_msg["role"] == "user"
    assert user_msg["content"][0]["text"] == (
        "The todo list is empty. Create it from the proposal: new tasks as "
        "`- [ ]`, only items listed under Completed as `- [x]`."
        "\n\nNow apply the changes described above.")


def test_empty_list_merge_saves_the_model_output_and_its_todo_id(merge):
    user = _user()
    proposal = _proposal(user.id)

    _run(merge, user, proposal)

    saved = _newest_todo(user.id)
    assert saved is not None
    assert saved.get_content() == MERGED
    assert saved.generated_by == "voice_session"
    entry = _propose_todo_entry(proposal.id)
    assert entry["apply_status"] == "completed"
    assert entry["todo_id"] == saved.id


def test_custom_merge_prompt_also_gets_the_empty_list_message(merge):
    """A user's own merge prompt is used as it is (not changed by #410);
    the empty-list message reaches it in the user message."""
    user = _user()
    row = UserPrompt(user_id=user.id, prompt_key="orient_apply_todo",
                     title="Apply to Todo", generated_by="user")
    row.set_content("MY OWN MERGE RULES")
    _db.session.add(row)
    _db.session.commit()
    proposal = _proposal(user.id)

    _run(merge, user, proposal)

    system, _, user_msg = _FakeProvider.calls[0]["messages"]
    assert system["content"][0]["text"] == "MY OWN MERGE RULES"
    assert user_msg["content"][0]["text"].startswith(
        "The todo list is empty.")


# ── list with items: message unchanged ───────────────────────────────────

def test_non_empty_list_message_is_unchanged(merge):
    user = _user()
    _todo(user.id, "## Today\n- [ ] an old task")
    proposal = _proposal(user.id, "### New Tasks\n- buy milk")
    _FakeProvider.answer = "## Today\n- [ ] an old task\n- [ ] buy milk"

    _run(merge, user, proposal)

    messages = _FakeProvider.calls[0]["messages"]
    assert messages[1:] == [
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "### New Tasks\n- buy milk"}],
        },
        {
            "role": "user",
            "content": [{"type": "text", "text": (
                "Here is the current full todo list:\n\n"
                "## Today\n- [ ] an old task"
                "\n\nNow apply the changes described above.")}],
        },
    ]
    saved = _newest_todo(user.id)
    assert saved.get_content() == "## Today\n- [ ] an old task\n- [ ] buy milk"
    assert _propose_todo_entry(proposal.id)["todo_id"] == saved.id


# ── the merge prompt ─────────────────────────────────────────────────────

def test_merge_prompt_keeps_its_rules_and_has_the_410_rules():
    from backend.utils.prompts import load_default_prompt
    prompt = load_default_prompt("orient_apply_todo")
    # The rules from before #410 are all still there.
    for rule in (
        "- Items you identified as completed that were on the todo list: "
        "change `- [ ]` to `- [x]`",
        "- Items you identified as completed that were NOT on the todo "
        "list: add them as `- [x]` in an appropriate section",
        "- New tasks you identified: add them as `- [ ]` in an appropriate "
        "section",
        "- Keep ALL existing items not mentioned in your update — do not "
        "remove anything",
        "- Preserve the original structure, sections, and formatting",
        "- Return ONLY the complete updated todo list — no commentary",
    ):
        assert rule in prompt
    # #410: checkbox state, wording and links, the named section.
    assert "A new task is always `- [ ]`" in prompt
    assert "word for word" in prompt and "including links" in prompt
    assert '"under Today"' in prompt

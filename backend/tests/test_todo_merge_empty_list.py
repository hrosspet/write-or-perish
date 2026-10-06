"""#410: a todo merge onto an empty list saved a new task as done and
dropped its link.

A user with no todo list asked Text mode to add one task. The merge sent
"Here is the current full todo list:" followed by nothing, and the model
applied the prompt's rule for completed items "NOT on the todo list" to the
new task. The merge now says the list is empty and which items become
`- [ ]` and `- [x]`. A list with headings but no tasks yet (the Todo page's
Create template) is sent as before, followed by the same rule. With a
task on the list, the message is unchanged. The merge also
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


def full_write(text):
    """The merge reply that writes the whole list (#234: allowed only on a
    list without tasks)."""
    return json.dumps({"edits": [], "updated_content": text})


# The model's reply for an empty list: the whole new list.
MERGED_REPLY = full_write(MERGED)
# The Todo page's Create template (TodoPage.js handleCreate), saved as is.
TEMPLATE = "## Today\n\n- [ ] \n\n## Upcoming\n\n- [ ] \n\n## Completed recently\n"
RULE = ("Add the items under New Tasks as `- [ ]` and the items under "
        "Completed as `- [x]`; add nothing else.")


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
    answer = MERGED_REPLY

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
    _FakeProvider.answer = MERGED_REPLY
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
        "The todo list is empty. Add the items under New Tasks as `- [ ]` "
        "and the items under Completed as `- [x]`; add nothing else."
        "\n\nNow apply the changes described above.")


# ── headings but no tasks yet ────────────────────────────────────────────

@pytest.mark.parametrize("existing", [
    TEMPLATE,
    "## Today\n\n## Upcoming\n",
    "## Today\n- [ ]\n- [x] \n* [ ]\t\n\n## Notes\nnothing here yet\n",
], ids=["create_template", "headings_only", "empty_checkboxes_and_prose"])
def test_list_without_tasks_is_sent_with_the_rule(merge, existing):
    """The Create template saved unchanged: its `## Completed recently`
    heading is a place for the old misreading. The list is sent as it is,
    so its sections stay, and the rule follows it."""
    user = _user()
    _todo(user.id, existing)
    proposal = _proposal(user.id)
    # A full write that keeps the list's headings and prose.
    _FakeProvider.answer = full_write(existing.replace(
        "## Today", MERGED, 1))

    _run(merge, user, proposal)

    assert len(_FakeProvider.calls) == 1
    _, assistant, user_msg = _FakeProvider.calls[0]["messages"]
    assert assistant["content"][0]["text"] == PROPOSAL
    assert user_msg["content"][0]["text"] == (
        "Here is the current full todo list:\n\n" + existing
        + "\n\n" + RULE + "\n\nNow apply the changes described above.")
    assert _propose_todo_entry(proposal.id)["todo_id"] == (
        _newest_todo(user.id).id)


@pytest.mark.parametrize("text, expected", [
    (None, False), ("", False), ("  \n", False), (TEMPLATE, False),
    ("## Today\n- [ ]\n- [x]   \n1. \n- \n", False),
    ("## Today\n- [ ] call mom", True),
    ("## Done\n- [x] call mom", True),
    ("## Done\n  * [X] call mom", True),
    ("- call mom", True),
    ("1. call mom", True),
    ("## Today\n- [ ] \n- [ ] call mom\n", True),
])
def test_has_tasks(text, expected):
    import backend.tasks.voice_todo_merge as vtm
    assert vtm.has_tasks(text) is expected


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

@pytest.mark.parametrize("old_task", ["- [ ] an old task", "- [x] an old task",
                                      "- an old task"])
def test_non_empty_list_message_is_unchanged(merge, old_task):
    user = _user()
    _todo(user.id, "## Today\n" + old_task)
    proposal = _proposal(user.id, "### New Tasks\n- buy milk")
    # A list with tasks gets edits (#234).
    _FakeProvider.answer = json.dumps({"edits": [
        {"old_text": old_task, "new_text": old_task + "\n- [ ] buy milk"}],
        "updated_content": ""})

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
                "## Today\n" + old_task
                + "\n\nNow apply the changes described above.")}],
        },
    ]
    saved = _newest_todo(user.id)
    assert saved.get_content() == (
        "## Today\n" + old_task + "\n- [ ] buy milk")
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
        # #234: the reply is edits, not the list.
        "- Return ONLY the JSON object — no commentary",
    ):
        assert rule in prompt
    # #410: checkbox state, wording and links, the named section.
    assert "A new task is always `- [ ]`" in prompt
    assert "word for word" in prompt and "including links" in prompt
    assert '"under Today"' in prompt
    # The section rule is for new items only: existing items stay where
    # they are ("Preserve the original structure").
    assert ("When your update names a section for a new item (e.g. "
            '"under Today"), put the item in that `## section`') in prompt



# ── cut-off output (#432) ────────────────────────────────────────────────

def _merge_with(merge, monkeypatch, user, proposal, truncated,
                content=MERGED_REPLY, confirm=None):
    def get_completion(*a, **k):
        return {"content": content, "truncated": truncated,
                "input_tokens": 100, "output_tokens": 10,
                "total_tokens": 110}
    import backend.llm_providers as lp
    for provider in {lp.LLMProvider, merge.LLMProvider}:
        monkeypatch.setattr(provider, "get_completion", get_completion)
    merge._run_merge(proposal, proposal.get_content(), user.id,
                     "claude-opus-4.6", confirm.id if confirm else None)
    _db.session.rollback()


def _confirm_node(user_id):
    node = Node(user_id=user_id, node_type="llm", llm_model="claude-opus-4.6",
                ai_usage="chat",
                tool_calls_meta=json.dumps([{"name": "apply_todo_changes"}]))
    node.set_content("ok")
    _db.session.add(node)
    _db.session.commit()
    return node


def _todo_cost_rows(user_id):
    from backend.models import APICostLog
    return APICostLog.query.filter_by(
        user_id=user_id, request_type="todo_merge").all()


def test_truncated_merge_fails_and_saves_nothing(merge, monkeypatch):
    user = _user()
    old = _todo(user.id, "- [ ] old task")
    proposal = _proposal(user.id)
    confirm = _confirm_node(user.id)

    _merge_with(merge, monkeypatch, user, proposal, True,
                content="- [ ] a\n- [ ] a\n- [ ] a", confirm=confirm)

    assert _newest_todo(user.id).id == old.id
    assert UserTodo.query.filter_by(user_id=user.id).count() == 1
    entry = _propose_todo_entry(proposal.id)
    assert entry["apply_status"] == "failed"
    assert entry["apply_error"] == merge.TRUNCATED_MESSAGE
    assert "todo_id" not in entry and "apply_truncated" not in entry
    cmeta = json.loads(Node.query.get(confirm.id).tool_calls_meta)[0]
    assert cmeta["apply_status"] == "failed"
    assert cmeta["apply_error"] == merge.TRUNCATED_MESSAGE
    rows = _todo_cost_rows(user.id)
    assert len(rows) == 1
    assert rows[0].request_ref == "refused:truncated"


def test_untruncated_merge_still_saves_and_logs_plain_cost(merge, monkeypatch):
    user = _user()
    proposal = _proposal(user.id)

    _merge_with(merge, monkeypatch, user, proposal, False)

    assert _newest_todo(user.id).get_content() == MERGED
    assert _propose_todo_entry(proposal.id)["apply_status"] == "completed"
    rows = _todo_cost_rows(user.id)
    assert len(rows) == 1 and rows[0].request_ref is None


def test_empty_merge_still_fails_as_before(merge, monkeypatch):
    user = _user()
    proposal = _proposal(user.id)

    _merge_with(merge, monkeypatch, user, proposal, False, content="  ")

    assert _newest_todo(user.id) is None
    entry = _propose_todo_entry(proposal.id)
    assert entry["apply_error"] == "Empty merge result"
    assert _todo_cost_rows(user.id)[0].request_ref is None

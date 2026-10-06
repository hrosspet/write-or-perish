"""#234: the todo merge as edits.

The merge model used to rewrite the whole todo list, and copies went wrong
(an open task ticked that the proposal never named, a digit changed in a
link, an item reworded, a repetition loop). It now replies with edits in
the artifact tool's {old_text, new_text} format, applied by the same
helper as update_artifact, with one retry after a refused reply: an anchor
not found or not unique, a full rewrite of a list that has tasks, or a
line of the previous list changed or dropped (the kept-lines check).

Same harness as test_todo_merge_empty_list: in-memory SQLite,
ENCRYPTION_DISABLED, celery mocked so the modules import. No model is
called: the provider is a fake, the API keys are fake, and the real keys
are removed from the environment.
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
    APICostLog, Node, User, UserPrompt, UserTodo)
from backend.utils import todo_merge_edits as tme  # noqa: E402
from backend.utils.text_edits import apply_text_edits  # noqa: E402

FAKE_KEYS = {"openai": "fake-openai-key", "anthropic": "fake-anthropic-key"}

LIST = (
    "## Today\n"
    "- [ ] call the bank\n"
    "- [ ] fix [issue 4512](https://github.com/x/y/issues/4512)\n"
    "\n"
    "## Later\n"
    "- [ ] gym\n"
    "- [x] old done thing\n"
)


def reply(*edits, full=""):
    """A model reply in the merge's JSON format."""
    return json.dumps({
        "edits": [{"old_text": o, "new_text": n} for o, n in edits],
        "updated_content": full})


def resolve(content, current=LIST):
    run = tme.MergeRun()
    merged, failure, error = tme.resolve_merge_reply(content, current, run)
    return merged, failure, error, run


class Scripted:
    """A provider that answers with the given replies in order (a dict is
    returned as the whole response) and records every call."""

    def __init__(self, *answers, usage=None):
        self.answers = list(answers)
        self.usage = usage or {"input_tokens": 1000, "output_tokens": 50}
        self.calls = []

    def get_completion(self, model_id, messages, api_keys, **kwargs):
        self.calls.append({"model_id": model_id, "messages": messages,
                           "api_keys": api_keys, "kwargs": kwargs})
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, dict):
            return dict(self.usage, total_tokens=1050, **answer)
        return dict(self.usage, content=answer, truncated=False,
                    total_tokens=1050)


# ── applying edits ───────────────────────────────────────────────────────

def test_tick_an_item():
    merged, failure, _, run = resolve(reply(
        ("- [ ] call the bank", "- [x] call the bank")))
    assert failure is None
    assert merged == LIST.replace("- [ ] call the bank",
                                  "- [x] call the bank")
    assert run.edits_applied == 1 and not run.full_write


def test_add_to_an_existing_section():
    merged, failure, _, _ = resolve(reply(
        ("- [ ] gym", "- [ ] gym\n- [ ] book the dentist")))
    assert failure is None
    assert "## Later\n- [ ] gym\n- [ ] book the dentist\n- [x] old" in merged


def test_add_a_new_section():
    merged, failure, _, _ = resolve(reply(
        ("- [x] old done thing",
         "- [x] old done thing\n\n## Errands\n- [ ] buy stamps")))
    assert failure is None
    assert merged.endswith(
        "- [x] old done thing\n\n## Errands\n- [ ] buy stamps\n")


def test_add_at_the_end_of_a_list_without_a_final_newline():
    current = "## Today\n- [ ] a\n- [ ] b"
    merged, failure, _, _ = resolve(
        reply(("- [ ] b", "- [ ] b\n- [ ] c")), current)
    assert failure is None
    assert merged == "## Today\n- [ ] a\n- [ ] b\n- [ ] c"


def test_several_edits_apply_in_order():
    merged, failure, _, run = resolve(reply(
        ("- [ ] call the bank", "- [x] call the bank"),
        ("- [x] call the bank", "- [x] call the bank\n- [ ] new one")))
    assert failure is None
    assert "- [x] call the bank\n- [ ] new one\n- [ ] fix" in merged
    assert run.edits_applied == 2


def test_anchor_not_found():
    merged, failure, error, run = resolve(reply(
        ("- [ ] call the bank", "- [x] call the bank"),
        ("- [ ] swim", "- [x] swim")))
    assert merged is None and failure == tme.FAILURE_ANCHOR
    assert error.startswith("edits[1].old_text was not found")
    # The artifact tool's hint about its full-text mode is not offered.
    assert "updated_content" not in error
    assert run.anchor_errors == 1


def test_anchor_matched_twice():
    current = "## A\n- [ ] gym\n## B\n- [ ] gym\n"
    merged, failure, error, run = resolve(
        reply(("- [ ] gym", "- [x] gym")), current)
    assert merged is None and failure == tme.FAILURE_ANCHOR
    assert "matches 2 places" in error
    assert run.anchor_errors == 1


def test_edits_are_all_or_nothing():
    text, error = apply_text_edits(LIST, [
        {"old_text": "- [ ] call the bank", "new_text": "- [x] call the bank"},
        {"old_text": "nowhere", "new_text": "x"}])
    assert text is None and "edits[1]" in error
    # The helper never mutates its input; the caller keeps the old list.
    merged, failure, _, _ = resolve(reply(
        ("- [ ] call the bank", "- [x] call the bank"),
        ("nowhere", "x")))
    assert merged is None and failure == tme.FAILURE_ANCHOR


def test_empty_anchor_and_missing_new_text_are_refused():
    _, error = apply_text_edits("a", [{"old_text": "", "new_text": "b"}])
    assert "old_text is empty" in error
    _, error = apply_text_edits("a", [{"old_text": "a"}])
    assert "new_text is missing" in error
    _, error = apply_text_edits("a", ["a"])
    assert "is not an object" in error


def test_artifact_hint_is_kept_for_the_artifact_tool():
    _, error = apply_text_edits(
        "a", [{"old_text": "b", "new_text": "c"}],
        not_found_hint=", or send updated_content with the full new text "
                       "instead")
    assert error == (
        "edits[0].old_text was not found in the current version. Match "
        "the current text exactly (whitespace included), or send "
        "updated_content with the full new text instead.")


def test_no_edits_keeps_the_list():
    merged, failure, _, run = resolve(reply())
    assert failure is None and merged == LIST
    assert run.edits_applied == 0


# ── the reply format ─────────────────────────────────────────────────────

@pytest.mark.parametrize("content", [
    "## Today\n- [ ] call the bank",          # the old full-list reply
    "[]",
    '{"edits": "nope", "updated_content": ""}',
    '{"edits": [], "updated_content": 5}',
    '{"edits": [{"old_text": 5, "new_text": "x"}], "updated_content": ""}',
])
def test_a_reply_that_is_not_the_json_object_is_refused(content):
    merged, failure, error, run = resolve(content)
    assert merged is None and failure == tme.FAILURE_FORMAT
    assert error == tme.FORMAT_ERROR and run.format_errors == 1


def test_a_fenced_reply_is_accepted():
    content = "```json\n" + reply(
        ("- [ ] gym", "- [x] gym")) + "\n```"
    merged, failure, _, _ = resolve(content)
    assert failure is None and "- [x] gym" in merged


def test_schema_is_strict_compatible():
    """OpenAI strict structured output: every object lists all its
    properties as required and allows no others."""
    def objects(schema):
        if schema.get("type") == "object":
            yield schema
            for prop in schema["properties"].values():
                yield from objects(prop)
        if schema.get("type") == "array":
            yield from objects(schema["items"])

    found = list(objects(tme.TODO_EDITS_SCHEMA))
    assert len(found) == 2
    for obj in found:
        assert obj["additionalProperties"] is False
        assert sorted(obj["required"]) == sorted(obj["properties"])
    # The edit shape is update_artifact's.
    item = tme.TODO_EDITS_SCHEMA["properties"]["edits"]["items"]
    assert set(item["properties"]) == {"old_text", "new_text"}


# ── full rewrite ─────────────────────────────────────────────────────────

def test_full_rewrite_is_refused_on_a_list_with_tasks():
    merged, failure, error, run = resolve(reply(full=LIST + "- [ ] x\n"))
    assert merged is None and failure == tme.FAILURE_REWRITE
    assert error == tme.REWRITE_REFUSED_ERROR
    assert run.rewrite_refusals == 1


@pytest.mark.parametrize("current", [
    "", "   \n",
    "## Today\n\n- [ ] \n\n## Upcoming\n\n- [ ] \n\n## Completed recently\n",
    "## Today\n\n## Upcoming\n",
], ids=["empty", "blank", "create_template", "headings_only"])
def test_full_write_is_allowed_on_a_list_without_tasks(current):
    full = "## Today\n- [ ] renew the passport\n\n## Upcoming\n"
    if "Completed recently" in current:
        full += "\n## Completed recently\n"
    merged, failure, _, run = resolve(reply(full=full), current)
    assert failure is None and merged == full
    assert run.full_write and run.edits_applied == 0


def test_full_write_on_a_template_keeps_its_headings():
    """The kept-lines check also covers a full write: the user's headings
    stay, the empty `- [ ] ` placeholders may go."""
    template = ("## Today\n\n- [ ] \n\n## Upcoming\n\n- [ ] \n\n"
                "## Completed recently\n")
    merged, failure, error, _ = resolve(
        reply(full="## Today\n- [ ] renew the passport\n"), template)
    assert merged is None and failure == tme.FAILURE_KEPT_LINES
    assert "## Upcoming" in error and "## Completed recently" in error


# ── the kept-lines check ─────────────────────────────────────────────────

def test_kept_lines_pass_on_legitimate_edits():
    merged = (LIST.replace("- [ ] call the bank", "- [X] call the bank")
              .replace("- [ ] gym", "- [ ] gym\n- [ ] new task\n  - sub"))
    merged += "\n## New section\n- [x] done today\n"
    assert tme.lines_not_kept(LIST, merged) == []


def test_kept_lines_catch_an_altered_anchor_line():
    """The model copied the anchor into new_text with one digit changed."""
    merged, failure, error, run = resolve(reply((
        "- [ ] fix [issue 4512](https://github.com/x/y/issues/4512)",
        "- [ ] fix [issue 4512](https://github.com/x/y/issues/4521)\n"
        "- [ ] new task")))
    assert merged is None and failure == tme.FAILURE_KEPT_LINES
    assert run.kept_lines_failures == 1
    assert ("line 3: - [ ] fix [issue 4512](https://github.com/x/y/"
            "issues/4512)") in error
    assert error.startswith("1 line(s) of the current list")


def test_kept_lines_catch_a_removed_line():
    merged, failure, error, _ = resolve(reply(
        ("- [ ] gym\n", "")))
    assert merged is None and failure == tme.FAILURE_KEPT_LINES
    assert "line 6: - [ ] gym" in error


def test_kept_lines_catch_a_reworded_or_unticked_item():
    reworded = LIST.replace("call the bank", "call the bank today")
    assert tme.lines_not_kept(LIST, reworded) == [
        (2, "- [ ] call the bank")]
    unticked = LIST.replace("- [x] old done thing", "- [ ] old done thing")
    assert tme.lines_not_kept(LIST, unticked) == [
        (7, "- [x] old done thing")]


def test_kept_lines_count_duplicates_and_ignore_blanks_and_placeholders():
    previous = "## A\n- [ ] gym\n\n- [ ] \n## B\n- [ ] gym\n"
    # One of the two gym lines ticked, the other kept; blank lines and the
    # empty placeholder dropped; trailing spaces don't count.
    assert tme.lines_not_kept(
        previous, "## A  \n- [x] gym\n## B\n- [ ] gym") == []
    # One of the two dropped.
    assert tme.lines_not_kept(previous, "## A\n- [ ] gym\n## B\n") == [
        (6, "- [ ] gym")]


def test_kept_lines_error_names_at_most_ten_lines():
    previous = "\n".join(f"- [ ] task {i}" for i in range(15))
    error = tme.kept_lines_error(tme.lines_not_kept(previous, ""))
    assert error.startswith("15 line(s)")
    assert "line 10: - [ ] task 9" in error
    assert "task 10" not in error and "(and 5 more)" in error


# ── the calls: retry, truncation ─────────────────────────────────────────

MESSAGES = [{"role": "system", "content": [{"type": "text", "text": "P"}]},
            {"role": "assistant",
             "content": [{"type": "text", "text": "### New Tasks\n- x"}]},
            {"role": "user", "content": [{"type": "text", "text": "L"}]}]


def test_one_call_asks_for_structured_output():
    good = reply(("- [ ] gym", "- [x] gym"))
    provider = Scripted(good)
    run = tme.run_todo_merge(provider, "claude-opus-5.5", MESSAGES,
                             FAKE_KEYS, LIST)
    assert run.failure is None and "- [x] gym" in run.merged
    assert len(provider.calls) == 1
    call = provider.calls[0]
    assert call["model_id"] == "claude-opus-5.5"
    assert call["messages"] == MESSAGES
    assert call["kwargs"] == {"output_schema": tme.TODO_EDITS_SCHEMA,
                              "output_schema_name": "todo_edits"}
    assert run.stats() == {
        "calls": 1, "retries": 0, "edits_applied": 1, "full_write": False,
        "format_errors": 0, "anchor_errors": 0, "rewrite_refusals": 0,
        "kept_lines_failures": 0, "failure": None}


def test_retry_then_success():
    bad = reply(("- [ ] swim", "- [x] swim"))
    good = reply(("- [ ] gym", "- [x] gym"))
    provider = Scripted(bad, good)
    run = tme.run_todo_merge(provider, "gpt-6-sol", MESSAGES, FAKE_KEYS,
                             LIST)
    assert run.failure is None and run.merged.count("- [x] gym") == 1
    assert (run.retries, run.anchor_errors, len(run.responses)) == (1, 1, 2)
    # The retry is the same conversation plus the refused reply and why.
    retry = provider.calls[1]["messages"]
    assert retry[:3] == MESSAGES
    assert retry[3] == {"role": "assistant",
                        "content": [{"type": "text", "text": bad}]}
    assert retry[4]["role"] == "user"
    sent = retry[4]["content"][0]["text"]
    assert sent.startswith("Your reply could not be applied, so nothing "
                           "was changed: edits[0].old_text was not found")
    assert run.replies == [bad, good]
    assert run.error is None
    assert [r["kind"] for r in run.refusals] == [tme.FAILURE_ANCHOR]


@pytest.mark.parametrize("first, second, failure", [
    (reply(("- [ ] swim", "- [x] swim")), reply(("- [ ] run", "- [x] run")),
     tme.FAILURE_ANCHOR),
    (reply(full=LIST), reply(("- [ ] gym\n", "")), tme.FAILURE_KEPT_LINES),
    ("not json", reply(full=LIST), tme.FAILURE_REWRITE),
])
def test_retry_then_failure_saves_nothing(first, second, failure):
    provider = Scripted(first, second)
    run = tme.run_todo_merge(provider, "gpt-6-sol", MESSAGES, FAKE_KEYS,
                             LIST)
    assert run.merged is None and run.failure == failure
    assert len(provider.calls) == tme.MAX_MERGE_ATTEMPTS == 2
    assert run.retries == 1


@pytest.mark.parametrize("answers", [
    [{"content": reply(("- [ ] gym", "- [x] gym")), "truncated": True}],
    [reply(("- [ ] swim", "x")),
     {"content": '{"edits": [{"old_text": "- [ ] gym", "new',
      "truncated": True}],
], ids=["first_call", "retry"])
def test_truncated_reply_fails_without_applying(answers):
    """Even a cut-off reply that parses is not applied (#432)."""
    provider = Scripted(*answers)
    run = tme.run_todo_merge(provider, "gpt-6-sol", MESSAGES, FAKE_KEYS,
                             LIST)
    assert run.merged is None and run.failure == tme.FAILURE_TRUNCATED
    assert len(provider.calls) == len(answers)


def test_empty_reply_fails_at_once():
    provider = Scripted("  ")
    run = tme.run_todo_merge(provider, "gpt-6-sol", MESSAGES, FAKE_KEYS,
                             LIST)
    assert run.failure == tme.FAILURE_EMPTY and len(provider.calls) == 1


def test_a_provider_error_keeps_the_earlier_responses():
    provider = Scripted(reply(("- [ ] swim", "x")), RuntimeError("503"))
    run = tme.MergeRun()
    with pytest.raises(RuntimeError):
        tme.run_todo_merge(provider, "gpt-6-sol", MESSAGES, FAKE_KEYS,
                           LIST, run)
    assert len(run.responses) == 1 and run.anchor_errors == 1


# ── the task, end to end ─────────────────────────────────────────────────

PROPOSAL = (
    "Here's the update.\n\n"
    "### Completed\n- call the bank\n\n"
    "### New Tasks\n- book the dentist (under Later)\n\n"
    "### Priority Order\n1. book the dentist\n2. gym\n")


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


@pytest.fixture
def task(app, monkeypatch):
    """The merge task module with a scripted provider: set
    ``task.provider`` before running. No way to reach a real model."""
    import backend.llm_providers as lp
    import backend.tasks.voice_todo_merge as vtm
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY_CHAT",
                "OPENAI_API_KEY", "OPENAI_API_KEY_CHAT"):
        monkeypatch.delenv(key, raising=False)
    holder = {}

    def get_completion(model_id, messages, api_keys, **kwargs):
        return holder["provider"].get_completion(
            model_id, messages, api_keys, **kwargs)
    for provider in {lp.LLMProvider, vtm.LLMProvider}:
        monkeypatch.setattr(provider, "get_completion",
                            staticmethod(get_completion))
    monkeypatch.setattr(vtm, "get_api_keys_for_usage",
                        lambda *a, **k: dict(FAKE_KEYS))

    def run(*answers, todo=LIST, confirm=False, saved_prompt=None):
        holder["provider"] = Scripted(*answers)
        user = User(username="alice", plan="alpha", twitter_id=None,
                    approved=True, default_ai_usage="chat")
        _db.session.add(user)
        _db.session.commit()
        if saved_prompt is not None:
            # A merge prompt the user saved on the Prompts page.
            row = UserPrompt(user_id=user.id, prompt_key="orient_apply_todo",
                             title="Apply to Todo", generated_by="user")
            row.set_content(saved_prompt)
            _db.session.add(row)
        if todo is not None:
            row = UserTodo(user_id=user.id, generated_by="user",
                           ai_usage="chat")
            row.set_content(todo)
            _db.session.add(row)
        node = Node(user_id=user.id, node_type="llm",
                    llm_model="claude-opus-5.5", ai_usage="chat",
                    tool_calls_meta=json.dumps([{"name": "propose_todo"}]))
        node.set_content(PROPOSAL)
        _db.session.add(node)
        confirm_node = None
        if confirm:
            confirm_node = Node(
                user_id=user.id, node_type="llm",
                llm_model="claude-opus-5.5", ai_usage="chat",
                tool_calls_meta=json.dumps([{"name": "apply_todo_changes"}]))
            confirm_node.set_content("ok")
            _db.session.add(confirm_node)
        _db.session.commit()
        vtm._run_merge(node, node.get_content(), user.id, "claude-opus-5.5",
                       confirm_node.id if confirm_node else None)
        _db.session.rollback()   # only what was committed counts
        return user, node, confirm_node, holder["provider"]

    monkeypatch.setattr(vtm, "run", run, raising=False)
    return vtm


def _entry(node, name="propose_todo"):
    meta = json.loads(Node.query.get(node.id).tool_calls_meta)
    return next(e for e in meta if e["name"] == name)


def _versions(user):
    return UserTodo.query.filter_by(user_id=user.id).order_by(
        UserTodo.id).all()


def _cost_rows(user):
    return APICostLog.query.filter_by(
        user_id=user.id, request_type="todo_merge").order_by(
            APICostLog.id).all()


GOOD = reply(("- [ ] call the bank", "- [x] call the bank"),
             ("- [ ] gym", "- [ ] gym\n- [ ] book the dentist (under Later)"))
MERGED = (LIST.replace("- [ ] call the bank", "- [x] call the bank")
          .replace("- [ ] gym", "- [ ] gym\n- [ ] book the dentist (under "
                                "Later)"))


def test_task_applies_the_edits_to_the_newest_list(task):
    user, node, _, provider = task.run(GOOD)

    versions = _versions(user)
    assert len(versions) == 2
    saved = versions[-1]
    assert saved.get_content() == MERGED
    assert saved.generated_by == "voice_session"
    assert saved.tokens_used == 50
    entry = _entry(node)
    assert entry["apply_status"] == "completed"
    assert entry["todo_id"] == saved.id
    # The conversation's model, structured output, the task's messages.
    call = provider.calls[0]
    assert call["model_id"] == "claude-opus-5.5"
    assert call["kwargs"]["output_schema"] == tme.TODO_EDITS_SCHEMA
    from backend.utils.prompts import load_default_prompt
    assert call["messages"] == task.build_merge_messages(
        load_default_prompt("orient_apply_todo"), PROPOSAL, LIST)
    assert call["messages"][0]["content"][0]["text"].endswith(
        tme.REPLY_FORMAT)
    rows = _cost_rows(user)
    assert len(rows) == 1 and rows[0].request_ref is None
    assert rows[0].model_id == "claude-opus-5.5"


# The merge prompt as it was before #234, saved by the user: it asks for
# the whole list.
SAVED_FULL_REWRITE_PROMPT = (
    "Now apply the changes you just described to the user's full todo "
    "list.\n\nRules:\n"
    "- Keep ALL existing items not mentioned in your update — do not "
    "remove anything\n"
    "- My own rule: put errands under ## Errands\n"
    "- Return ONLY the complete updated todo list — no commentary\n")


def test_saved_full_rewrite_prompt_gets_the_reply_format_and_merges(task):
    """A merge prompt the user saved before #234 is sent unchanged, and
    the reply format follows it, so the model is told to send edits."""
    user, node, _, provider = task.run(GOOD,
                                       saved_prompt=SAVED_FULL_REWRITE_PROMPT)

    assert len(provider.calls) == 1
    system = provider.calls[0]["messages"][0]["content"][0]["text"]
    assert system == (SAVED_FULL_REWRITE_PROMPT.rstrip() + "\n\n"
                      + tme.REPLY_FORMAT)
    assert system.startswith(SAVED_FULL_REWRITE_PROMPT.rstrip())
    assert ("replaces any instruction above to return the whole list"
            in system)
    assert '{"edits": [{"old_text": "...", "new_text": "..."}]' in system
    assert _versions(user)[-1].get_content() == MERGED
    entry = _entry(node)
    assert entry["apply_status"] == "completed"
    assert len(_cost_rows(user)) == 1


def test_task_retries_once_and_logs_both_calls(task):
    user, node, _, provider = task.run(
        reply(full=LIST + "- [ ] book the dentist\n"), GOOD)

    assert _versions(user)[-1].get_content() == MERGED
    assert _entry(node)["apply_status"] == "completed"
    assert len(provider.calls) == 2
    assert len(_cost_rows(user)) == 2


def test_task_fails_after_two_refusals_and_saves_nothing(task):
    bad = reply(("- [ ] fix [issue 4512](https://github.com/x/y/issues/4512)",
                 "- [ ] fix [issue 4512](https://github.com/x/y/issues/4521)"))
    user, node, confirm, provider = task.run(bad, bad, confirm=True)

    assert len(_versions(user)) == 1
    entry = _entry(node)
    assert entry["apply_status"] == "failed"
    assert entry["apply_error"] == task.EDITS_FAILED_MESSAGE
    assert "todo_id" not in entry
    cmeta = _entry(confirm, "apply_todo_changes")
    assert cmeta["apply_status"] == "failed"
    assert cmeta["apply_error"] == task.EDITS_FAILED_MESSAGE
    rows = _cost_rows(user)
    assert len(rows) == 2 and all(r.request_ref is None for r in rows)


def test_task_truncated_reply_fails_and_saves_nothing(task):
    user, node, _, _ = task.run({"content": GOOD, "truncated": True})

    assert len(_versions(user)) == 1
    entry = _entry(node)
    assert entry["apply_status"] == "failed"
    assert entry["apply_error"] == task.TRUNCATED_MESSAGE
    rows = _cost_rows(user)
    assert len(rows) == 1 and rows[0].request_ref == "refused:truncated"


def test_task_provider_error_on_the_retry_logs_the_first_call(task):
    user, node, _, _ = task.run(reply(("- [ ] swim", "x")),
                                RuntimeError("upstream 503"))

    assert len(_versions(user)) == 1
    entry = _entry(node)
    assert entry["apply_status"] == "failed"
    assert entry["apply_error"] == "upstream 503"
    assert len(_cost_rows(user)) == 1


def test_task_empty_list_takes_a_full_write(task):
    full = "## Later\n- [ ] book the dentist (under Later)\n"
    user, node, _, provider = task.run(reply(full=full), todo=None)

    assert _versions(user)[-1].get_content() == full
    assert _entry(node)["apply_status"] == "completed"
    sent = provider.calls[0]["messages"][-1]["content"][0]["text"]
    assert sent.startswith("The todo list is empty.")


def test_task_logs_counts_not_list_text(task, monkeypatch):
    logged = []
    for level in ("info", "warning", "error"):
        monkeypatch.setattr(
            task.logger, level,
            lambda msg, *a, _l=level, **k: logged.append((_l, msg % a
                                                          if a else msg)))
    bad = reply(("- [ ] call the bank", "- [x] call the bank today"))
    task.run(bad, GOOD)

    text = "\n".join(m for _, m in logged)
    assert "'kept_lines_failures': 1" in text
    assert "'retries': 1" in text
    assert any(level == "warning" and "kept-lines check refused 1 of 2"
               in m for level, m in logged)
    assert "call the bank" not in text and "dentist" not in text


# ── the merge prompt ─────────────────────────────────────────────────────

def test_merge_prompt_asks_for_edits_and_keeps_the_417_rules():
    from backend.utils.prompts import load_default_prompt
    prompt = load_default_prompt("orient_apply_todo")
    for rule in (
        # #417 / #410
        "A new task is always `- [ ]`",
        "Add each new item word for word as your update writes it, "
        "including links",
        'When your update names a section for a new item (e.g. "under '
        'Today"), put the item in that `## section`, and create the '
        "section if it doesn't exist yet",
        "Keep ALL existing items not mentioned in your update",
        # Priority Order and the other free sections stay out (#234).
        "Only the New Tasks and Completed sections of your update change "
        "the list",
        "Priority Order",
    ):
        assert rule in prompt, rule
    assert "Return ONLY the complete updated todo list" not in prompt
    # The reply format is the parser's contract: in code, not in the
    # user-editable prompt.
    assert "old_text" not in prompt and "JSON" not in prompt
    for rule in (
        "replaces any instruction above to return the whole list",
        "Do not write the list out again",
        '{"edits": [{"old_text": "...", "new_text": "..."}], '
        '"updated_content": ""}',
        "occurs in it exactly once",
        "Leave updated_content empty. Only when the todo list is empty or "
        "has no tasks yet",
        "Return ONLY the JSON object",
    ):
        assert rule in tme.REPLY_FORMAT, rule


# ── the provider passes the schema name to OpenAI ────────────────────────

def test_openai_call_names_the_schema(app, monkeypatch):
    import backend.llm_providers as providers
    sent = {}

    class Response:
        output = []
        status = "completed"
        incomplete_details = None
        id = "resp_1"

        class usage:
            input_tokens = 10
            output_tokens = 5
            total_tokens = 15
            input_tokens_details = None

    def final_response(client, kwargs, listener=None):
        sent.update(kwargs)
        return Response()

    monkeypatch.setattr(providers, "_openai_final_response", final_response)
    providers.LLMProvider._call_openai(
        "gpt-6-sol", [{"role": "user", "content": "x"}], "fake-key",
        output_schema=tme.TODO_EDITS_SCHEMA,
        output_schema_name=tme.SCHEMA_NAME)
    assert sent["text"]["format"] == {
        "type": "json_schema", "name": "todo_edits",
        "schema": tme.TODO_EDITS_SCHEMA, "strict": True}
    sent.clear()
    providers.LLMProvider._call_openai(
        "gpt-6-sol", [{"role": "user", "content": "x"}], "fake-key",
        output_schema={"type": "object"})
    assert sent["text"]["format"]["name"] == "feed_reply"

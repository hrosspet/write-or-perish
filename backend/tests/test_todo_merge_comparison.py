"""backend/scripts/compare_todo_merge_models.py (#234): the offline
comparison of todo-merge models that Peter runs on prod on his own data.

It must refuse without an explicit (admin) user whose username the
operator types, never write to the DB (no todo versions, nodes, drafts or
api_cost_log rows), never initialise Sentry, rebuild a past merge's inputs
exactly as the merge task built them, and compare outputs item by item.
The model is always a fake here: local dev has a real key.

Same harness as test_todo_merge_ai_usage: in-memory SQLite,
ENCRYPTION_DISABLED, celery mocked so the modules import.
"""
import json
import os
import stat
import sys
from collections import namedtuple
from datetime import datetime, timedelta
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
from sqlalchemy import func, select  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

from backend.extensions import db as _db  # noqa: E402
from backend.models import (  # noqa: E402
    APICostLog, Node, NodeVersion, User, UserPrompt, UserTodo)
import backend.scripts.compare_todo_merge_models as cmp  # noqa: E402


T0 = datetime(2026, 9, 1, 10, 0, 0)
V1 = "## Today\n- [ ] write tests\n- [ ] call mom"
P1 = "### New Tasks\n- buy milk"
M1 = "## Today\n- [ ] write tests\n- [ ] call mom\n- [ ] buy milk"
P2 = "### Completed\n- write tests"
M2 = "## Today\n- [x] write tests\n- [ ] call mom\n- [ ] buy milk"
V4 = M2 + "\n- [ ] gym"
P3 = "### New Tasks\n- read a book"
M3 = V4 + "\n- [ ] read a book"
TEXTS = [V1, P1, M1, P2, M2, V4, P3, M3]


@pytest.fixture
def app():
    # Warm celery_app first so it resolves the exports <-> profile_batch
    # import cycle in the safe order (see test_profile_regen_resume).
    import backend.celery_app  # noqa: F401
    from backend.config import Config
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["SUPPORTED_MODELS"] = Config.SUPPORTED_MODELS
    _db.init_app(app)
    with app.app_context():
        assert str(_db.engine.url).startswith("sqlite")
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()


@pytest.fixture(autouse=True)
def no_real_model(monkeypatch):
    """Fail loudly if anything reaches the real provider."""
    from backend.llm_providers import LLMProvider

    def _refuse(*a, **k):
        raise AssertionError("a test reached the real LLM provider")
    monkeypatch.setattr(LLMProvider, "get_completion",
                        staticmethod(_refuse))


@pytest.fixture(autouse=True)
def no_real_keys(monkeypatch):
    """No test can pick up a real API key: in a worktree, importing backend
    loads the main checkout's .env, which has them."""
    import backend.config as config
    for name in list(vars(config.Config)):
        if "_API_KEY" in name and not name.startswith("TWITTER"):
            monkeypatch.setattr(config.Config, name, None)
            monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def typed_username(monkeypatch):
    """Answers the username confirmation the way Peter would for his
    account; the tests of the confirmation replace it. Returns the prompts
    shown."""
    asked = []

    def _input(prompt=""):
        asked.append(prompt)
        return "peter"
    monkeypatch.setattr("builtins.input", _input)
    return asked


@pytest.fixture(autouse=True)
def pin_live_prompt(monkeypatch):
    """The pinned hash guards runs on prod (a changed default prompt can't
    rebuild older merges). The tests rebuild with whatever the file says
    today, so they don't fail when the prompt is edited (#410 / PR #417)."""
    import hashlib
    from backend.utils.prompts import load_default_prompt
    live = load_default_prompt(cmp.PROMPT_KEY)
    monkeypatch.setattr(cmp, "PROMPT_FILE_SHA256",
                        hashlib.sha256(live.encode()).hexdigest())


def test_a_changed_default_prompt_refuses_to_run(monkeypatch):
    monkeypatch.setattr(cmp, "PROMPT_FILE_SHA256", "0" * 64)
    with pytest.raises(SystemExit, match="orient_apply_todo.txt changed"):
        cmp.file_default_prompt()


class FakeProvider:
    """Answers each merge from a table keyed by the proposal text (the
    assistant message), per model; records every call."""

    def __init__(self, answers=None, usage=None):
        self.answers = answers or {}
        self.usage = usage or {"input_tokens": 5000, "output_tokens": 4000}
        self.calls = []

    def get_completion(self, model_id, messages, api_keys, **kwargs):
        self.calls.append((model_id, messages, kwargs))
        proposal = messages[1]["content"][0]["text"]
        text = self.answers.get((model_id, proposal), "")
        return dict(self.usage, content=text, truncated=False,
                    total_tokens=sum(self.usage.values()))


@pytest.fixture
def decrypted(monkeypatch):
    """Records (model, id, owner) of every row whose content is read."""
    seen = []
    for model, owner_attr in ((UserTodo, "user_id"),
                              (Node, "human_owner_id"),
                              (UserPrompt, "user_id")):
        original = model.get_content

        def spy(self, _original=original, _attr=owner_attr):
            seen.append((type(self).__name__, self.id,
                         getattr(self, _attr)))
            return _original(self)
        monkeypatch.setattr(model, "get_content", spy)
    return seen


def _add(row):
    _db.session.add(row)
    _db.session.commit()
    return row


def _user(name, admin=False, ai="chat"):
    return _add(User(username=name, plan="alpha", twitter_id=None,
                     approved=True, default_ai_usage=ai, is_admin=admin))


def _version(user, text, at, generated_by="user", tokens=0, ai="chat"):
    row = UserTodo(user_id=user.id, generated_by=generated_by,
                   tokens_used=tokens, ai_usage=ai, created_at=at)
    row.set_content(text)
    return _add(row)


def _proposal(user, text, at, model="claude-opus-4.6", status="completed",
              ai="chat", name="propose_todo"):
    llm = (User.query.filter_by(username=model).first()
           or _user(model))
    node = Node(user_id=llm.id, human_owner_id=user.id, node_type="llm",
                llm_model=model, ai_usage=ai, created_at=at,
                tool_calls_meta=json.dumps([
                    {"name": name, "status": "success",
                     "apply_status": status}]))
    node.set_content(text)
    return _add(node)


def _merge(user, proposal_text, proposal_at, output, model="claude-opus-4.6",
           delay=timedelta(minutes=1), cost_row=True, **proposal_kw):
    """A past merge as the task leaves it: the applied proposal, the
    merge output version and, in the same commit, its cost row."""
    proposal = _proposal(user, proposal_text, proposal_at, model=model,
                         **proposal_kw)
    merged_at = proposal_at + delay
    version = _version(user, output, merged_at,
                       generated_by="voice_session", tokens=321)
    if cost_row:
        _add(APICostLog(user_id=user.id, model_id=model,
                        request_type="todo_merge", input_tokens=4000,
                        output_tokens=321, cost_microdollars=150000,
                        created_at=merged_at - timedelta(milliseconds=3)))
    return proposal, version


@pytest.fixture
def history(app):
    """Peter's three rebuildable merges, one that is not (a version saved
    between proposal and merge), and Eve's merges (another account)."""
    peter = _user("peter", admin=True)
    _version(peter, V1, T0)
    p1, m1 = _merge(peter, P1, T0 + timedelta(hours=1), M1)
    p2, m2 = _merge(peter, P2, T0 + timedelta(hours=2), M2)
    v4 = _version(peter, V4, T0 + timedelta(hours=3))
    p3, m3 = _merge(peter, P3, T0 + timedelta(hours=3, minutes=10), M3)
    # Proposal, then a manual save, then the merge: which list the merge
    # read is unknown, so it is skipped.
    p4 = _proposal(peter, "### New Tasks\n- AMBIGUOUS", T0 + timedelta(hours=4))
    _version(peter, M3 + "\n- [ ] typed by hand",
             T0 + timedelta(hours=4, minutes=1))
    m4 = _version(peter, "AMBIGUOUS OUTPUT", T0 + timedelta(hours=4, minutes=2),
                  generated_by="voice_session", tokens=321)
    _add(APICostLog(user_id=peter.id, model_id="claude-opus-4.6",
                    request_type="todo_merge", input_tokens=4000,
                    output_tokens=321, cost_microdollars=150000,
                    created_at=m4.created_at))
    eve = _user("eve")
    _version(eve, "## Eve\n- [ ] EVE SECRET", T0)
    _merge(eve, "### New Tasks\n- EVE PROPOSAL", T0 + timedelta(hours=1),
           "## Eve\n- [ ] EVE SECRET\n- [ ] EVE PROPOSAL")
    return {"peter": peter, "eve": eve, "v4": v4, "p": [p1, p2, p3, p4],
            "m": [m1, m2, m3, m4]}


def _counts():
    return {t.name: _db.session.execute(
        select(func.count()).select_from(t)).scalar()
        for t in _db.metadata.sorted_tables}


def _jsonl(path):
    with open(path) as fh:
        return [json.loads(line) for line in fh]


def _luna_answers():
    """GPT-6 Luna reproduces every stored merge exactly."""
    return {("gpt-6-luna", P1): M1, ("gpt-6-luna", P2): M2,
            ("gpt-6-luna", P3): M3}


def _edits(*pairs, full=""):
    """A reply of the merge by edits (#234)."""
    return json.dumps({
        "edits": [{"old_text": o, "new_text": n} for o, n in pairs],
        "updated_content": full})


# The stored merges as edits of their previous lists (V1 -> M1, M1 -> M2,
# V4 -> M3): what --current-prompt sends back.
E1 = _edits(("- [ ] call mom", "- [ ] call mom\n- [ ] buy milk"))
E2 = _edits(("- [ ] write tests", "- [x] write tests"))
E3 = _edits(("- [ ] gym", "- [ ] gym\n- [ ] read a book"))


def _edit_answers(*models):
    """These models reproduce every stored merge exactly, by edits."""
    return {(model, proposal): reply for model in models
            for proposal, reply in ((P1, E1), (P2, E2), (P3, E3))}


# ── refusals ─────────────────────────────────────────────────────────────

def test_cli_requires_an_explicit_user():
    with pytest.raises(SystemExit):
        cmp.parse_args([])
    assert cmp.parse_args(["--user", "peter"]).user == "peter"


@pytest.mark.parametrize("ident", [None, "", "   ", "nobody", "999"])
def test_refuses_without_a_real_explicit_user(history, decrypted, tmp_path,
                                              ident):
    provider = FakeProvider()
    out = tmp_path / "out.jsonl"
    with pytest.raises(SystemExit):
        cmp.run(ident, provider=provider, out_path=str(out))
    assert provider.calls == [] and decrypted == []
    assert not out.exists()


def test_refuses_another_users_account(history, decrypted, tmp_path):
    """Eve is not an admin: her todo lists are never read for this."""
    provider = FakeProvider()
    with pytest.raises(SystemExit, match="admin"):
        cmp.run("eve", provider=provider,
                out_path=str(tmp_path / "out.jsonl"))
    with pytest.raises(SystemExit, match="admin"):
        cmp.run(str(history["eve"].id), provider=provider,
                out_path=str(tmp_path / "out2.jsonl"))
    assert provider.calls == [] and decrypted == []


def test_refuses_an_account_set_to_ai_usage_none(app, decrypted, tmp_path):
    _user("peter", admin=True, ai="none")
    provider = FakeProvider()
    with pytest.raises(SystemExit, match="none"):
        cmp.run("peter", provider=provider,
                out_path=str(tmp_path / "out.jsonl"))
    assert provider.calls == [] and decrypted == []


def test_asks_for_the_username_before_decrypting(history, decrypted,
                                                 typed_username, tmp_path,
                                                 capsys):
    provider = FakeProvider(_luna_answers())
    cmp.run(str(history["peter"].id), provider=provider,
            out_path=str(tmp_path / "out.jsonl"))
    assert typed_username == ["Type the username to continue: "]
    assert "'peter'" in capsys.readouterr().out
    assert decrypted and len(provider.calls) == 3


@pytest.mark.parametrize("answer", ["", "Peter", "eve", "no", EOFError])
def test_a_wrong_or_missing_username_stops_before_decrypting(
        history, decrypted, monkeypatch, tmp_path, answer):
    """Another admin's account passes the admin check; typing its username
    is what makes the operator see whose data it is."""
    def _input(prompt=""):
        if answer is EOFError:
            raise EOFError
        return answer
    monkeypatch.setattr("builtins.input", _input)
    provider = FakeProvider(_luna_answers())
    out = tmp_path / "out.jsonl"
    with pytest.raises(SystemExit, match="Not confirmed"):
        cmp.run("peter", provider=provider, out_path=str(out))
    assert provider.calls == [] and decrypted == []
    assert not out.exists()


def test_dry_run_does_not_ask_and_names_the_account(history, monkeypatch,
                                                    capsys):
    def _input(prompt=""):
        raise AssertionError("a dry run asked for confirmation")
    monkeypatch.setattr("builtins.input", _input)
    cmp.run("peter", dry_run=True, provider=FakeProvider())
    first_line = capsys.readouterr().out.splitlines()[0]
    assert first_line.startswith("User 'peter' (id ")


def test_main_turns_sentry_off_before_create_app(app, monkeypatch):
    """create_app() initialises Sentry when SENTRY_DSN is set (prod's
    .env.production sets it). Sentry reports an unhandled exception or
    Ctrl-C with every stack frame's local variables, which hold the
    decrypted texts. main() removes the variable before create_app().
    (The real create_app() can't be built here after other test modules
    have replaced flask_login with a mock, so a stand-in records what it
    would have read.)"""
    import sentry_sdk
    import backend
    monkeypatch.setenv("SENTRY_DSN", "https://key@sentry.invalid/1")
    inits = []
    monkeypatch.setattr(sentry_sdk, "init",
                        lambda *a, **k: inits.append(k.get("dsn")))
    seen = []

    def create_app():
        seen.append(os.environ.get("SENTRY_DSN"))
        return app
    monkeypatch.setattr(backend, "create_app", create_app)
    runs = []
    monkeypatch.setattr(cmp, "run", lambda *a, **k: runs.append(a))
    cmp.main(["--user", "peter", "--dry-run"])
    assert seen == [None]
    assert inits == []
    assert "SENTRY_DSN" not in os.environ
    assert runs == [("peter",)]


def test_reads_only_the_passed_users_rows(history, decrypted, tmp_path):
    provider = FakeProvider(_luna_answers())
    cmp.run("peter", provider=provider, out_path=str(tmp_path / "o.jsonl"))
    assert decrypted, "the comparison decrypted nothing"
    assert {owner for _, _, owner in decrypted} == {history["peter"].id}
    sent = json.dumps([messages for _, messages, _ in provider.calls])
    assert "EVE" not in sent


# ── never writes ─────────────────────────────────────────────────────────

def test_never_writes_to_the_database(history, tmp_path):
    before = _counts()
    provider = FakeProvider(_luna_answers())
    cmp.run("peter", models=["gpt-6-luna", "gpt-6-sol"],
            rerun_original=True, provider=provider,
            out_path=str(tmp_path / "out.jsonl"))
    assert len(provider.calls) == 3 * 3
    assert _counts() == before
    assert not (_db.session.new or _db.session.dirty or _db.session.deleted)


def test_no_cost_rows_but_cost_from_the_app_calculator(history, tmp_path):
    from backend.utils.cost import llm_cost_log_fields
    cost_rows = APICostLog.query.count()
    provider = FakeProvider(_luna_answers())
    out = tmp_path / "out.jsonl"
    cmp.run("peter", provider=provider, out_path=str(out))
    assert APICostLog.query.count() == cost_rows
    merges = [r for r in _jsonl(out) if r["type"] == "merge"]
    response = dict(provider.usage, content=M3)
    expected = llm_cost_log_fields("gpt-6-luna", response)
    run = merges[0]["runs"][0]
    assert run["cost_usd"] == expected["cost_microdollars"] / 1e6 == 0.0025
    assert (run["input_tokens"], run["output_tokens"]) == (5000, 4000)


def test_the_session_guard_refuses_any_flush(app):
    with cmp.refuse_writes():
        _db.session.add(APICostLog(user_id=1, model_id="gpt-6-luna",
                                   request_type="todo_merge",
                                   cost_microdollars=1))
        with pytest.raises(cmp.ReadOnlyViolation):
            _db.session.flush()
    assert APICostLog.query.count() == 0
    # The guard is gone afterwards: the app's own writes work again.
    _user("after")


def test_a_provider_that_tries_to_write_stops_the_run(history, tmp_path):
    before = _counts()

    class Writer(FakeProvider):
        def get_completion(self, model_id, messages, api_keys, **kwargs):
            _db.session.add(APICostLog(user_id=1, model_id=model_id,
                                       request_type="todo_merge",
                                       cost_microdollars=1))
            _db.session.flush()

    with pytest.raises(cmp.ReadOnlyViolation):
        cmp.run("peter", provider=Writer(),
                out_path=str(tmp_path / "out.jsonl"))
    assert _counts() == before


def test_postgres_guard_is_a_no_op_elsewhere(app):
    assert cmp.make_postgres_read_only(_db.engine) is False


# ── rebuilding the inputs ────────────────────────────────────────────────

def test_rebuilds_each_merges_inputs_as_the_task_built_them(history,
                                                            tmp_path):
    provider = FakeProvider(_luna_answers())
    out = tmp_path / "out.jsonl"
    cmp.run("peter", provider=provider, out_path=str(out))

    prompt = cmp.file_default_prompt()
    # Newest first; the previous version of the third merge is the list
    # Peter saved by hand before that proposal (V4, with "gym").
    expected = [cmp.build_merge_messages(prompt, P3, V4),
                cmp.build_merge_messages(prompt, P2, M1),
                cmp.build_merge_messages(prompt, P1, V1)]
    assert [m for _, m, _ in provider.calls] == expected
    # Called exactly like the task: no max_tokens or other options.
    assert all(model == "gpt-6-luna" and kwargs == {}
               for model, _, kwargs in provider.calls)

    records = _jsonl(out)
    merges = [r for r in records if r["type"] == "merge"]
    assert [m["todo_id"] for m in merges] == [
        history["m"][2].id, history["m"][1].id, history["m"][0].id]
    assert [m["proposal_node_id"] for m in merges] == [
        history["p"][2].id, history["p"][1].id, history["p"][0].id]
    assert merges[0]["previous_todo_id"] == history["v4"].id
    assert merges[0]["stored"]["model"] == "claude-opus-4.6"
    assert merges[0]["stored"]["text"] == M3
    assert merges[0]["inputs"] == {"proposal_text": P3,
                                   "previous_todo_text": V4}
    skipped = [r for r in records if r["type"] == "skipped"]
    assert [(s["todo_id"], s["reason"]) for s in skipped] == [
        (history["m"][3].id, "version_between")]
    assert "AMBIGUOUS" not in json.dumps(
        [m for _, m, _ in provider.calls])


def test_messages_match_the_merge_task(app, monkeypatch):
    """build_merge_messages calls the task's own builder; this fails if
    _run_merge stops sending what that builder returns."""
    import backend.tasks.voice_todo_merge as vtm
    import backend.utils.prompts as prompts
    peter = _user("peter", admin=True)
    _version(peter, V1, T0)
    proposal = _proposal(peter, P1, T0 + timedelta(hours=1),
                         status="started")
    sent = []

    class Recorder:
        @staticmethod
        def get_completion(model_id, messages, api_keys, **kwargs):
            sent.append(messages)
            return {"content": E1, "truncated": False, "input_tokens": 1,
                    "output_tokens": 1, "total_tokens": 2}

    monkeypatch.setattr(vtm, "LLMProvider", Recorder)
    monkeypatch.setattr(vtm, "get_api_keys_for_usage", lambda *a, **k: {})
    monkeypatch.setattr(prompts, "get_user_prompt",
                        lambda uid, key: "THE MERGE PROMPT")
    vtm._run_merge(proposal, proposal.get_content(), peter.id,
                   "claude-opus-4.6", None)
    assert sent == [cmp.build_merge_messages("THE MERGE PROMPT", P1, V1)]


# The Todo page's Create template (TodoPage.js handleCreate), saved as is.
TEMPLATE = "## Today\n\n- [ ] \n\n## Upcoming\n\n- [ ] \n\n## Completed recently\n"


@pytest.mark.parametrize("previous", [None, "   ", TEMPLATE],
                         ids=["no_list", "blank_list", "create_template"])
def test_messages_match_the_merge_task_for_lists_without_tasks(
        app, monkeypatch, previous):
    """The same check for the lists that #410 / PR #417 sends another
    message for: the script must rebuild them with that message too."""
    import backend.tasks.voice_todo_merge as vtm
    import backend.utils.prompts as prompts
    peter = _user("peter", admin=True)
    if previous is not None:
        _version(peter, previous, T0)
    proposal = _proposal(peter, P1, T0 + timedelta(hours=1),
                         status="started")
    sent = []

    class Recorder:
        @staticmethod
        def get_completion(model_id, messages, api_keys, **kwargs):
            sent.append(messages)
            # No tasks to anchor on: a full write that keeps the headings.
            content = _edits(full=(previous or "") + "\n- [ ] buy milk")
            return {"content": content, "truncated": False,
                    "input_tokens": 1, "output_tokens": 1,
                    "total_tokens": 2}

    monkeypatch.setattr(vtm, "LLMProvider", Recorder)
    monkeypatch.setattr(vtm, "get_api_keys_for_usage", lambda *a, **k: {})
    monkeypatch.setattr(prompts, "get_user_prompt",
                        lambda uid, key: "THE MERGE PROMPT")
    vtm._run_merge(proposal, proposal.get_content(), peter.id,
                   "claude-opus-4.6", None)
    assert sent == [cmp.build_merge_messages("THE MERGE PROMPT", P1,
                                             previous or "")]


def test_the_script_uses_the_tasks_builder(monkeypatch):
    import backend.tasks.voice_todo_merge as vtm
    monkeypatch.setattr(vtm, "build_merge_messages",
                        lambda *args: ["FROM THE TASK", args])
    assert cmp.build_merge_messages("p", "u", "t") == [
        "FROM THE TASK", ("p", "u", "t")]


def test_custom_merge_prompt_is_rebuilt_from_its_row(history, tmp_path):
    peter = history["peter"]
    row = UserPrompt(user_id=peter.id, prompt_key="orient_apply_todo",
                     title="Apply to Todo", generated_by="user",
                     created_at=T0 - timedelta(days=1))
    row.set_content("MY OWN MERGE RULES")
    _add(row)
    provider = FakeProvider(_luna_answers())
    out = tmp_path / "out.jsonl"
    cmp.run("peter", provider=provider, out_path=str(out))
    from backend.utils.todo_merge_edits import REPLY_FORMAT
    assert {m[0]["content"][0]["text"] for _, m, _ in provider.calls} == {
        "MY OWN MERGE RULES\n\n" + REPLY_FORMAT}
    merges = [r for r in _jsonl(out) if r["type"] == "merge"]
    assert {m["prompt"] for m in merges} == {f"user_prompt:{row.id}"}


def test_limit_and_since(history, tmp_path):
    provider = FakeProvider(_luna_answers())
    cmp.run("peter", limit=1, provider=provider,
            out_path=str(tmp_path / "a.jsonl"))
    assert [m[1]["content"][0]["text"] for _, m, _ in provider.calls] == [P3]
    provider = FakeProvider(_luna_answers())
    cmp.run("peter", since=T0 + timedelta(hours=1, minutes=30),
            provider=provider, out_path=str(tmp_path / "b.jsonl"))
    assert [m[1]["content"][0]["text"] for _, m, _ in provider.calls] == [
        P3, P2]


def test_dry_run_decrypts_nothing_and_calls_nothing(history, decrypted,
                                                    tmp_path, capsys):
    provider = FakeProvider()
    out = tmp_path / "out.jsonl"
    result = cmp.run("peter", dry_run=True, rerun_original=True,
                     provider=provider, out_path=str(out))
    assert result == {"dry_run": True, "chosen": 3, "rebuildable": 3}
    assert provider.calls == [] and decrypted == [] and not out.exists()
    printed = capsys.readouterr().out
    assert "gpt-6-luna" in printed and "Estimated cost" in printed


def test_ai_usage_none_content_is_never_sent(app, tmp_path):
    peter = _user("peter", admin=True)
    _version(peter, V1, T0)
    _merge(peter, "### New Tasks\n- NONE PROPOSAL", T0 + timedelta(hours=1),
           M1, ai="none")
    _version(peter, "## Hidden\n- [ ] NONE LIST", T0 + timedelta(hours=2),
             ai="none")
    _merge(peter, P3, T0 + timedelta(hours=3), M3)
    provider = FakeProvider()
    out = tmp_path / "out.jsonl"
    cmp.run("peter", provider=provider, out_path=str(out))
    assert provider.calls == []
    reasons = [r["reason"] for r in _jsonl(out) if r["type"] == "skipped"]
    assert reasons == ["previous_ai_none", "proposal_ai_none"]


def test_edited_or_deleted_proposals_are_skipped(app, tmp_path):
    """Edited after the merge: the stored text is not what the merge read."""
    peter = _user("peter", admin=True)
    _version(peter, V1, T0)
    edited, _ = _merge(peter, P1, T0 + timedelta(hours=1), M1)
    # Edited once before the merge (allowed) and once after it.
    _add(NodeVersion(node_id=edited.id, content="first text",
                     timestamp=T0 + timedelta(hours=1, seconds=30)))
    _add(NodeVersion(node_id=edited.id, content="older text",
                     timestamp=T0 + timedelta(days=1)))
    deleted, _ = _merge(peter, P2, T0 + timedelta(hours=2), M2)
    deleted.deleted_at = T0 + timedelta(days=1)
    _db.session.commit()
    provider = FakeProvider()
    out = tmp_path / "out.jsonl"
    cmp.run("peter", provider=provider, out_path=str(out))
    assert provider.calls == []
    reasons = [r["reason"] for r in _jsonl(out) if r["type"] == "skipped"]
    assert reasons == ["proposal_deleted", "proposal_edited"]


def test_a_proposal_edited_in_its_card_before_the_apply_is_rebuilt(
        app, tmp_path):
    """Ticking or adding an item in the proposal card before the apply
    saves a NodeVersion; the card allows no edit after it. The stored
    text is then what the merge read, so the merge is used."""
    peter = _user("peter", admin=True)
    _version(peter, V1, T0)
    adjusted = "### New Tasks\n- buy milk\n- added in the card"
    proposal, merged = _merge(peter, adjusted, T0 + timedelta(hours=1), M1)
    _add(NodeVersion(node_id=proposal.id, content=P1,
                     timestamp=T0 + timedelta(hours=1, seconds=30)))
    assert merged.created_at > T0 + timedelta(hours=1, seconds=30)
    provider = FakeProvider({("gpt-6-luna", adjusted): M1})
    out = tmp_path / "out.jsonl"
    cmp.run("peter", provider=provider, out_path=str(out))
    assert [m[1]["content"][0]["text"] for _, m, _ in provider.calls] == [
        adjusted]
    records = _jsonl(out)
    assert [r["type"] for r in records if r["type"] != "run"] == [
        "merge", "summary"]
    assert records[1]["proposal_node_id"] == proposal.id


def test_proposal_edit_time_decides_the_skip():
    versions = [V(1, _h(0), "user", 0, "chat"),
                V(2, _h(1, 1), "voice_session", 5, "chat"),
                V(3, _h(2, 1), "voice_session", 5, "chat")]
    proposals = [_p(10, _h(1)), _p(20, _h(2))]
    costs = [C(v.id, v.created_at, "claude-opus-4.6", 1, 5, 1)
             for v in versions[1:]]
    last_edit = {10: _h(1, 0) + timedelta(seconds=20),   # before its merge
                 20: _h(2, 1)}                           # at its merge
    linked = cmp.link_merges(versions, proposals, costs, last_edit, [])
    assert {r["todo"].id: r["skip"] for r in linked} == {
        3: "proposal_edited", 2: None}


# ── linking (metadata only) ──────────────────────────────────────────────

V = namedtuple("V", "id created_at generated_by tokens_used ai_usage")
C = namedtuple("C", "id created_at model_id input_tokens output_tokens "
                    "cost_microdollars")


def _p(pid, at, model="claude-opus-4.6"):
    return {"id": pid, "created_at": at, "llm_model": model,
            "ai_usage": "chat", "deleted_at": None, "truncated": False}


def _link(versions, proposals, costs=None):
    if costs is None:
        costs = [C(v.id, v.created_at, "claude-opus-4.6", 1, v.tokens_used, 1)
                 for v in versions if v.generated_by == "voice_session"]
    return {r["todo"].id: r["skip"] for r in cmp.link_merges(
        versions, proposals, costs, {}, [])}


def _h(hours, minutes=0):
    return T0 + timedelta(hours=hours, minutes=minutes)


def test_a_lost_proposal_skips_the_older_merges_not_mispairs_them():
    """Merge 2's proposal is gone (purged): rank pairing would pair merge
    2 with merge 1's proposal; the between-check catches it."""
    versions = [V(1, _h(0), "user", 0, "chat"),
                V(2, _h(1, 1), "voice_session", 5, "chat"),
                V(3, _h(2, 1), "voice_session", 5, "chat"),
                V(4, _h(3, 1), "voice_session", 5, "chat")]
    proposals = [_p(10, _h(1)), _p(30, _h(3))]
    assert _link(versions, proposals) == {
        4: None, 3: "not_between", 2: "unpaired"}


def test_a_proposal_written_during_the_previous_merge_is_skipped():
    versions = [V(1, _h(0), "user", 0, "chat"),
                V(2, _h(1, 5), "voice_session", 5, "chat"),
                V(3, _h(2), "voice_session", 5, "chat")]
    # The second proposal came while the first merge was still running.
    proposals = [_p(10, _h(1)), _p(20, _h(1, 2))]
    assert _link(versions, proposals) == {3: "not_between", 2: None}


def test_cost_row_must_match_and_name_the_proposals_model():
    versions = [V(1, _h(0), "user", 0, "chat"),
                V(2, _h(1, 1), "voice_session", 5, "chat"),
                V(3, _h(2, 1), "voice_session", 7, "chat")]
    proposals = [_p(10, _h(1)), _p(20, _h(2))]
    costs = [C(1, _h(1, 1) + timedelta(seconds=5), "claude-opus-4.6", 1, 5,
               1),                                      # too late
             C(2, _h(2, 1), "gpt-6-sol", 1, 7, 1)]      # another model
    assert _link(versions, proposals, costs) == {
        3: "model_mismatch", 2: "no_cost_row"}


def test_merges_before_the_prompt_change_are_skipped():
    old = cmp.FLOOR - timedelta(days=1)
    versions = [V(1, old - timedelta(hours=1), "user", 0, "chat"),
                V(2, old, "voice_session", 5, "chat"),
                V(3, _h(1, 1), "voice_session", 5, "chat")]
    proposals = [_p(10, old - timedelta(minutes=1)), _p(20, _h(1))]
    assert _link(versions, proposals) == {3: None, 2: "before_floor"}


def test_proposal_meta_parsing():
    assert cmp._proposal_state(json.dumps([
        {"name": "propose_todo", "apply_status": "completed",
         "apply_truncated": True}])) == (True, True)
    assert cmp._proposal_state(json.dumps([
        {"name": "update_todo", "apply_status": "completed"}])) == (
            True, False)
    for status in ("pending_approval", "started", "failed", "superseded"):
        assert cmp._proposal_state(json.dumps([
            {"name": "propose_todo", "apply_status": status}]))[0] is False
    assert cmp._proposal_state("not json") == (False, False)


# ── accuracy ─────────────────────────────────────────────────────────────

REF = """## Today
- [ ] a task
- [x] done thing
- [ ] call the bank about the card

## Later
- [ ] gym
- plain bullet
"""
CAND = """## Today
- [x] a task
- [ ] call the bank about the cards
- [ ] new item

## Later
- [x] done thing
- [ ] gym
"""


def test_parse_todo_items():
    assert cmp.parse_todo(REF) == [
        ("Today", "a task", False), ("Today", "done thing", True),
        ("Today", "call the bank about the card", False),
        ("Later", "gym", False), ("Later", "plain bullet", None)]
    assert cmp.parse_todo("") == []


def test_compare_identical_lists():
    result = cmp.compare_todos(REF, REF + "\n\n")
    assert result["exact_match"] and result["identical_text"]
    assert result["same_items_any_order"]
    assert [result[k] for k in ("added", "removed", "reworded", "moved",
                                "checkbox_changed")] == [0, 0, 0, 0, 0]


def test_compare_counts_each_kind_of_difference():
    result = cmp.compare_todos(REF, CAND)
    assert not result["exact_match"] and not result["identical_text"]
    assert (result["added"], result["removed"], result["reworded"],
            result["moved"], result["checkbox_changed"]) == (1, 1, 1, 1, 1)
    details = result["details"]
    assert details["added"] == [["Today", "new item", False]]
    assert details["removed"] == [["Later", "plain bullet", None]]
    assert details["reworded"] == [["call the bank about the card",
                                    "call the bank about the cards"]]
    assert details["moved"] == [["done thing", "Today", "Later"]]
    assert details["checkbox_changed"] == [["a task", False, True]]


def test_compare_order_only_difference():
    swapped = "## Today\n- [ ] b\n- [ ] a"
    result = cmp.compare_todos("## Today\n- [ ] a\n- [ ] b", swapped)
    assert not result["exact_match"] and result["same_items_any_order"]
    assert result["added"] == result["removed"] == 0


def test_missing_input_items():
    assert cmp.missing_input_items("- [ ] a\n- [ ] b\n- [ ] b",
                                   "## Done\n- [x] a\n- [ ] b") == ["b"]


def test_results_scored_against_stored_and_original_rerun(history, tmp_path):
    answers = _luna_answers()
    # GPT-6 Sol drops "call mom" and ticks "buy milk" on the newest merge.
    answers[("gpt-6-sol", P3)] = M3.replace("- [ ] call mom\n", "").replace(
        "- [ ] buy milk", "- [x] buy milk")
    for proposal, output in ((P1, M1), (P2, M2), (P3, M3)):
        answers[("claude-opus-4.6", proposal)] = output
        answers.setdefault(("gpt-6-sol", proposal), output)
    provider = FakeProvider(answers)
    out = tmp_path / "out.jsonl"
    summary = cmp.run("peter", models=["gpt-6-luna", "gpt-6-sol"],
                      rerun_original=True, provider=provider,
                      out_path=str(out))
    assert summary["gpt-6-luna"]["exact_match"] == 3
    assert summary["gpt-6-luna"]["exact_match_vs_original_rerun"] == 3
    assert summary["gpt-6-sol"]["exact_match"] == 2
    assert summary["gpt-6-sol"]["with_missing_input"] == 1
    assert summary["claude-opus-4.6 (original re-run)"]["compared"] == 3
    newest = [r for r in _jsonl(out) if r["type"] == "merge"][0]
    sol = next(r for r in newest["runs"] if r["model"] == "gpt-6-sol")
    assert (sol["vs_stored"]["removed"], sol["vs_stored"]["checkbox_changed"]
            ) == (1, 1)
    assert sol["missing_input_detail"] == ["call mom"]
    assert [r["role"] for r in newest["runs"]] == [
        "candidate", "candidate", "original_rerun"]


def test_a_failing_model_is_recorded_and_the_run_goes_on(history, tmp_path):
    class Flaky(FakeProvider):
        def get_completion(self, model_id, messages, api_keys, **kwargs):
            if model_id == "gpt-6-sol":
                raise RuntimeError("upstream 500 with SECRET-ish body")
            return super().get_completion(model_id, messages, api_keys)

    out = tmp_path / "out.jsonl"
    summary = cmp.run("peter", models=["gpt-6-sol", "gpt-6-luna"],
                      provider=Flaky(_luna_answers()), out_path=str(out))
    assert summary["gpt-6-sol"]["errors"] == 3
    assert summary["gpt-6-luna"]["exact_match"] == 3


# ── output ───────────────────────────────────────────────────────────────

def test_jsonl_is_private_and_the_terminal_shows_no_todo_text(
        history, tmp_path, capsys):
    out = tmp_path / "out.jsonl"
    cmp.run("peter", provider=FakeProvider(_luna_answers()),
            out_path=str(out))
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
    printed = capsys.readouterr().out
    assert "exact match 3/3" in printed
    for text in TEXTS + ["AMBIGUOUS", "EVE"]:
        for line in text.splitlines():
            item = line.lstrip("#-[] x").strip()
            if item and item not in ("Today",):
                assert item not in printed, item
    body = out.read_text()
    assert "buy milk" in body and "read a book" in body


def test_never_overwrites_an_existing_file(history, tmp_path):
    out = tmp_path / "out.jsonl"
    out.write_text("keep me")
    with pytest.raises(FileExistsError):
        cmp.run("peter", provider=FakeProvider(_luna_answers()),
                out_path=str(out))
    assert out.read_text() == "keep me"


# ── --current-prompt ─────────────────────────────────────────────────────

NEW_PROMPT = "TODAY'S MERGE RULES"


@pytest.fixture
def new_prompt_file(monkeypatch):
    """The prompt file has changed since the past merges: the pinned hash
    no longer matches, as after #417 deploys."""
    import backend.utils.prompts as prompts
    monkeypatch.setattr(prompts, "load_default_prompt",
                        lambda key: NEW_PROMPT)
    monkeypatch.setattr(cmp, "PROMPT_FILE_SHA256", "0" * 64)


def test_current_prompt_flag_parses():
    assert cmp.parse_args(["--user", "x"]).current_prompt is False
    assert cmp.parse_args(
        ["--user", "x", "--current-prompt"]).current_prompt is True


def test_hash_check_is_skipped_only_with_current_prompt(
        history, new_prompt_file, tmp_path):
    provider = FakeProvider(_edit_answers("gpt-6-luna"))
    with pytest.raises(SystemExit, match="orient_apply_todo.txt changed"):
        cmp.run("peter", provider=provider,
                out_path=str(tmp_path / "a.jsonl"))
    with pytest.raises(SystemExit, match="orient_apply_todo.txt changed"):
        cmp.run("peter", provider=provider, dry_run=True)
    assert provider.calls == []
    cmp.run("peter", provider=provider, current_prompt=True,
            out_path=str(tmp_path / "b.jsonl"))
    assert len(provider.calls) == 3


def test_current_prompt_dry_run_works_on_a_changed_file(
        history, new_prompt_file, capsys):
    result = cmp.run("peter", provider=FakeProvider(), dry_run=True,
                     current_prompt=True)
    assert result["dry_run"] is True
    assert "current file" in capsys.readouterr().out


def test_every_call_uses_the_current_prompt_and_builder(
        history, new_prompt_file, tmp_path):
    provider = FakeProvider(_edit_answers("gpt-6-luna", "claude-opus-4.6"))
    out = tmp_path / "out.jsonl"
    cmp.run("peter", models=["gpt-6-luna"], rerun_original=True,
            provider=provider, current_prompt=True, out_path=str(out))
    # Same inputs as a normal run, only the prompt is today's; candidate
    # and original re-run alike.
    expected = [cmp.build_merge_messages(NEW_PROMPT, P3, V4),
                cmp.build_merge_messages(NEW_PROMPT, P2, M1),
                cmp.build_merge_messages(NEW_PROMPT, P1, V1)]
    assert [m for model, m, _ in provider.calls
            if model == "gpt-6-luna"] == expected
    assert [m for model, m, _ in provider.calls
            if model == "claude-opus-4.6"] == expected
    assert len(provider.calls) == 6
    merges = [r for r in _jsonl(out) if r["type"] == "merge"]
    # The reference and the inputs are the past merge's own.
    assert merges[0]["stored"]["text"] == M3
    assert merges[0]["inputs"] == {"proposal_text": P3,
                                   "previous_todo_text": V4}
    assert [r["role"] for r in merges[0]["runs"]] == [
        "candidate", "original_rerun"]
    # Both run the merge by edits, with the task's structured output.
    assert {r["mode"] for m in merges for r in m["runs"]} == {"edits"}
    assert all(r["ok"] and r["vs_stored"]["exact_match"]
               for m in merges for r in m["runs"])
    from backend.utils.todo_merge_edits import TODO_EDITS_SCHEMA
    assert all(kwargs == {"output_schema": TODO_EDITS_SCHEMA,
                          "output_schema_name": "todo_edits"}
               for _, _, kwargs in provider.calls)


def test_current_prompt_is_recorded_in_the_run_and_each_merge(
        history, new_prompt_file, tmp_path, capsys):
    import hashlib
    sha = hashlib.sha256(NEW_PROMPT.encode()).hexdigest()
    out = tmp_path / "out.jsonl"
    cmp.run("peter", provider=FakeProvider(_edit_answers("gpt-6-luna")),
            current_prompt=True, out_path=str(out))
    records = _jsonl(out)
    run_rec = records[0]
    assert run_rec["type"] == "run"
    assert run_rec["current_prompt"] is True
    assert run_rec["prompt_file_sha256"] == sha
    merges = [r for r in records if r["type"] == "merge"]
    assert merges and all(m["current_prompt"] is True
                          and m["prompt"] == "current_file"
                          and m["prompt_sha256"] == sha
                          and m["stored_prompt"] == "file_default"
                          for m in merges)
    shown = capsys.readouterr().out
    assert (f"prompt: current file {sha[:8]}, not the one these merges "
            "used") in shown


def test_a_normal_run_records_that_it_did_not_use_the_current_prompt(
        history, tmp_path):
    out = tmp_path / "out.jsonl"
    cmp.run("peter", provider=FakeProvider(_luna_answers()),
            out_path=str(out))
    records = _jsonl(out)
    assert records[0]["current_prompt"] is False
    assert records[0]["prompt_file_sha256"] == cmp.PROMPT_FILE_SHA256
    assert all(m["current_prompt"] is False and m["prompt"] == "file_default"
               for m in records if m["type"] == "merge")


def test_current_prompt_replaces_a_custom_prompt_and_does_not_read_it(
        history, new_prompt_file, decrypted, tmp_path):
    row = UserPrompt(user_id=history["peter"].id,
                     prompt_key="orient_apply_todo", title="Apply to Todo",
                     generated_by="user", created_at=T0 - timedelta(days=1))
    row.set_content("MY OWN MERGE RULES")
    _add(row)
    provider = FakeProvider(_edit_answers("gpt-6-luna"))
    out = tmp_path / "out.jsonl"
    cmp.run("peter", provider=provider, current_prompt=True,
            out_path=str(out))
    from backend.utils.todo_merge_edits import REPLY_FORMAT
    assert {m[0]["content"][0]["text"] for _, m, _ in provider.calls} == {
        NEW_PROMPT + "\n\n" + REPLY_FORMAT}
    assert not [s for s in decrypted if s[0] == "UserPrompt"]
    merges = [r for r in _jsonl(out) if r["type"] == "merge"]
    assert {m["prompt"] for m in merges} == {"current_file"}
    assert {m["stored_prompt"] for m in merges} == {f"user_prompt:{row.id}"}


def test_current_prompt_keeps_the_safety_properties(
        history, new_prompt_file, tmp_path):
    before = _counts()
    out = tmp_path / "out.jsonl"
    provider = FakeProvider(_edit_answers("gpt-6-luna"))
    cmp.run("peter", provider=provider, current_prompt=True,
            out_path=str(out))
    assert len(provider.calls) == 3
    assert _counts() == before
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
    with pytest.raises(SystemExit):
        cmp.run("eve", provider=FakeProvider(), current_prompt=True,
                out_path=str(tmp_path / "eve.jsonl"))


# ── --current-prompt runs the merge by edits (#234) ──────────────────────

class ScriptedProvider(FakeProvider):
    """Answers each (model, proposal) from a list, one reply per call, so
    a merge's retry gets the next one."""

    def get_completion(self, model_id, messages, api_keys, **kwargs):
        self.calls.append((model_id, messages, kwargs))
        proposal = messages[1]["content"][0]["text"]
        answer = self.answers[(model_id, proposal)].pop(0)
        if isinstance(answer, Exception):
            raise answer
        return dict(self.usage, content=answer, truncated=False,
                    total_tokens=sum(self.usage.values()))


def test_peters_evaluation_command_is_in_the_docstring():
    assert ("python backend/scripts/compare_todo_merge_models.py --user "
            "hrosspet --current-prompt --models claude-opus-5.5") in cmp.__doc__


def test_current_prompt_records_what_each_merge_by_edits_did(
        history, new_prompt_file, tmp_path, capsys):
    from backend.utils.cost import llm_cost_log_fields
    luna = "gpt-6-luna"
    altered = _edits(("- [ ] write tests", "- [x] write test"))
    provider = ScriptedProvider({
        # Newest merge: an anchor that isn't in the list, then the fix.
        (luna, P3): [_edits(("- [ ] swim", "- [x] swim")), E3],
        # Second: the ticked line altered twice; nothing saved.
        (luna, P2): [altered, altered],
        (luna, P1): [E1],
    })
    out = tmp_path / "out.jsonl"
    summary = cmp.run("peter", provider=provider, current_prompt=True,
                      out_path=str(out))

    assert len(provider.calls) == 5
    merges = [r for r in _jsonl(out) if r["type"] == "merge"]
    newest, second, oldest = (m["runs"][0] for m in merges)
    assert newest["ok"] and newest["vs_stored"]["exact_match"]
    assert newest["text"] == M3
    assert newest["edits"] == {
        "calls": 2, "retries": 1, "edits_applied": 1, "full_write": False,
        "format_errors": 0, "anchor_errors": 1, "rewrite_refusals": 0,
        "kept_lines_failures": 0, "sub_items_moved_failures": 0,
        "failure": None}
    # Tokens and cost of both calls.
    one_call = llm_cost_log_fields(luna, dict(provider.usage, content=E3))
    assert newest["input_tokens"] == 2 * 5000
    assert newest["output_tokens"] == 2 * 4000
    assert newest["cost_usd"] == pytest.approx(
        2 * one_call["cost_microdollars"] / 1e6)
    assert newest["latency_s"] >= 0
    assert len(newest["replies"]) == 2
    assert not second["ok"]
    assert second["error_type"] == "MergeFailed:kept_lines"
    assert second["edits"]["kept_lines_failures"] == 2
    assert second["edits"]["failure"] == "kept_lines"
    assert [r["kind"] for r in second["refusals"]] == [
        "kept_lines", "kept_lines"]
    assert "line 2: - [ ] write tests" in second["refusals"][1]["reason"]
    assert [r["kind"] for r in newest["refusals"]] == ["anchor"]
    assert oldest["ok"] and oldest["edits"]["retries"] == 0

    edits = summary[luna]["edits"]
    assert edits == {
        "merges": 3, "failed": 1, "failed_by_reason": {"kept_lines": 1},
        "with_retry": 2, "calls": 5, "edits_applied": 2, "full_writes": 0,
        "anchor_errors": 1, "with_anchor_error": 1,
        "kept_lines_refusals": 2, "with_kept_lines_refusal": 1,
        "sub_items_moved_refusals": 0, "with_sub_items_moved_refusal": 0,
        "rewrite_refusals": 0, "format_errors": 0}
    printed = capsys.readouterr().out
    assert "kept-lines check refused 2 replies in 1 merges" in printed
    assert "nesting check refused 0 replies in 0 merges" in printed
    assert "anchor errors 1 in 1 merges" in printed
    assert "edits: failed 1/3 (33%) (kept_lines 1)" in printed
    assert "ERROR MergeFailed:kept_lines" in printed
    # Counts only on the terminal, never the list.
    for text in ("write tests", "call mom", "read a book", "swim"):
        assert text not in printed


def test_current_prompt_uses_the_tasks_apply_function(
        history, new_prompt_file, tmp_path, monkeypatch):
    from backend.utils import todo_merge_edits
    seen = []

    def fake_run(provider, model_id, messages, api_keys, current_todo,
                 run=None):
        seen.append((model_id, current_todo))
        run.merged = current_todo + "\n- [ ] from the task"
        return run
    monkeypatch.setattr(todo_merge_edits, "run_todo_merge", fake_run)
    out = tmp_path / "out.jsonl"
    cmp.run("peter", provider=FakeProvider(), current_prompt=True,
            out_path=str(out))
    assert seen == [("gpt-6-luna", V4), ("gpt-6-luna", M1),
                    ("gpt-6-luna", V1)]
    merges = [r for r in _jsonl(out) if r["type"] == "merge"]
    assert merges[0]["runs"][0]["text"] == V4 + "\n- [ ] from the task"


def test_current_prompt_records_a_provider_error_with_the_calls_before_it(
        history, new_prompt_file, tmp_path):
    luna = "gpt-6-luna"
    provider = ScriptedProvider({
        (luna, P3): [_edits(("- [ ] swim", "x")),
                     RuntimeError("upstream 500")],
        (luna, P2): [E2], (luna, P1): [E1]})
    out = tmp_path / "out.jsonl"
    summary = cmp.run("peter", provider=provider, current_prompt=True,
                      out_path=str(out))
    newest = [r for r in _jsonl(out) if r["type"] == "merge"][0]["runs"][0]
    assert not newest["ok"] and newest["error_type"] == "RuntimeError"
    assert newest["edits"]["calls"] == 1
    assert newest["input_tokens"] == 5000 and newest["cost_usd"] > 0
    assert summary[luna]["errors"] == 1


def test_current_prompt_dry_run_estimates_the_smaller_edits_output(
        history, capsys):
    from backend.utils.cost import calculate_llm_cost_microdollars
    cmp.run("peter", models=["claude-opus-5.5"], dry_run=True,
            rerun_original=True, current_prompt=True,
            provider=FakeProvider())
    printed = capsys.readouterr().out
    # The fixture's past merges sent 4000 input tokens each.
    once = calculate_llm_cost_microdollars(
        "claude-opus-5.5", 4000 + cmp.EDITS_EXTRA_INPUT_TOKENS,
        cmp.EDITS_OUTPUT_TOKENS)
    assert once == 48400   # $4/M in, $20/M out: 4600 in, 1500 out
    retry = calculate_llm_cost_microdollars(
        "claude-opus-5.5", 4000 + cmp.EDITS_EXTRA_INPUT_TOKENS
        + cmp.EDITS_OUTPUT_TOKENS, cmp.EDITS_OUTPUT_TOKENS)
    assert "merges by edits" in printed
    assert (f"claude-opus-5.5        ${3 * once / 1e6:.4f} total, "
            f"${once / 1e6:.4f} per merge; up to "
            f"${3 * (once + retry) / 1e6:.4f} if every merge retried"
            ) in printed
    assert "original re-run" in printed

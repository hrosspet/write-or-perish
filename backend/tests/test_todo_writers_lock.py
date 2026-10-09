"""The todo writers wait for each other, and a tick during a merge stays
(#477).

Every write of a user's todo (a tick, the row "+" and quick-add as PATCH,
the editor's Save as PUT, a revert, a todo merge's save) takes the user's
todo lock (utils/todo_lock.py) before it reads the newest version, and
the commit releases it. The merge's model call is outside the lock: at
save time the merge reads the newest list again and, when it changed while
the model worked, applies its edits to it (todo_merge_edits.rebase_merge).
When they no longer fit, the merge saves nothing and the proposal can be
applied again.

The interleavings are made deterministic: the other writer runs from
inside the fake model call (the merge's window), or from inside the lock
call (the writer that held the lock commits just before this one gets
it). It runs in its own app context, so it has its own database session,
as another worker or request would.

The SQLite tests can't show two transactions waiting for each other; the
last tests do that on PostgreSQL, and run only when LOORE_TEST_POSTGRES_URL
names a throwaway database (its tables are created and dropped).
"""
import json
import os
import sys
import threading
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

POSTGRES_URL = os.environ.get("LOORE_TEST_POSTGRES_URL")


def _make_app(database_url):
    from flask_login import LoginManager
    from backend.routes.todo import todo_bp

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["SUPPORTED_MODELS"] = {}
    _db.init_app(app)
    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return _db.session.get(User, int(user_id))

    app.register_blueprint(todo_bp, url_prefix="/api/todo")
    return app


@pytest.fixture
def app():
    import backend.celery_app  # noqa: F401
    app = _make_app("sqlite:///:memory:")
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()


class _Provider:
    """Answers each merge call with the next queued reply, after running
    ``meanwhile``: what the user does while the model works."""
    replies = []
    meanwhile = None

    @classmethod
    def get_completion(cls, model_id, messages, api_keys, **kwargs):
        if cls.meanwhile is not None:
            action, cls.meanwhile = cls.meanwhile, None
            action()
        return {"content": cls.replies.pop(0), "truncated": False,
                "input_tokens": 100, "output_tokens": 10,
                "total_tokens": 110}


def _patched_merge(monkeypatch):
    """The merge task module, with the model call, keys and prompt faked."""
    import backend.tasks.voice_todo_merge as vtm
    import backend.utils.prompts as prompts
    _Provider.replies = []
    _Provider.meanwhile = None
    monkeypatch.setattr(vtm, "LLMProvider", _Provider)
    monkeypatch.setattr(vtm, "get_api_keys_for_usage", lambda *a, **k: {})
    monkeypatch.setattr(prompts, "get_user_prompt", lambda uid, key: "MERGE")
    return vtm


@pytest.fixture
def merge(app, monkeypatch):
    return _patched_merge(monkeypatch)


def _user(name="alice"):
    user = User(username=name, plan="alpha", twitter_id=None, approved=True,
                default_ai_usage="chat")
    _db.session.add(user)
    _db.session.commit()
    return user


def _todo(user_id, content, generated_by="user"):
    todo = UserTodo(user_id=user_id, generated_by=generated_by,
                    ai_usage="chat")
    todo.set_content(content)
    _db.session.add(todo)
    _db.session.commit()
    return todo


def _proposal(user_id):
    """An AI reply proposing todo changes, as the app stores one: the
    model's account is its author, the user its human owner."""
    model_user = User.query.filter_by(username="gpt-5.5").first()
    if model_user is None:
        model_user = User(username="gpt-5.5", twitter_id="llm-gpt-5.5")
        _db.session.add(model_user)
        _db.session.flush()
    node = Node(user_id=model_user.id, human_owner_id=user_id,
                node_type="llm", llm_model="gpt-5.5", ai_usage="chat",
                tool_calls_meta=json.dumps([{
                    "name": "propose_todo", "status": "success",
                    "apply_status": "started"}]))
    node.set_content("### New Tasks\n- buy milk")
    _db.session.add(node)
    _db.session.commit()
    return node


def _client(app, user):
    """A signed-in client; *user* is a User or a user id."""
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(getattr(user, "id", user))
        sess["_fresh"] = True
    return client


def _elsewhere(app, action):
    """Runs *action* as another request or worker would: in its own app
    context, so with its own database session."""
    def run():
        with app.app_context():
            return action()
    return run


def _contents(user_id):
    return [t.get_content() for t in UserTodo.query.filter_by(
        user_id=user_id).order_by(UserTodo.id).all()]


def _entry(node_id, name="propose_todo"):
    meta = json.loads(_db.session.get(Node, node_id).tool_calls_meta)
    return next(e for e in meta if e["name"] == name)


def _reply(*edits):
    return json.dumps({
        "edits": [{"old_text": old, "new_text": new} for old, new in edits],
        "updated_content": ""})


def _run(merge, proposal, user, reply, meanwhile=None):
    """The merge task's body, as the worker runs it; *meanwhile* runs
    while the model works. *reply* is the model's reply, or a list of
    them (a refused reply gets one retry)."""
    _Provider.replies = [reply] if isinstance(reply, str) else list(reply)
    _Provider.meanwhile = meanwhile
    merge._run_merge(proposal, proposal.get_content(), user.id, "gpt-5.5",
                     proposal.id)
    _db.session.rollback()   # only what was committed counts
    _db.session.expire_all()


LIST = "## Today\n- [ ] call mom\n- [ ] an old task"
# The merge adds "buy milk" after "an old task".
ADD_MILK = ("- [ ] an old task", "- [ ] an old task\n- [ ] buy milk")


def _tick(app, user, content):
    """A tick on the web page: the edited list and the revision it was
    made on."""
    def tick():
        client = _client(app, user)
        loaded = client.get("/api/todo/").get_json()["todo"]
        res = client.patch("/api/todo/", json={
            "content": content, "base_revision": loaded["revision"]})
        assert res.status_code == 200
    return _elsewhere(app, tick)


# ── A tick or a Save while the merge's model works ──────────────────────

def test_a_tick_during_a_merge_stays(app, merge):
    """The scenario of #477: the merge read the list, the user ticked an
    item while the model worked, then the merge saved."""
    user = _user()
    _todo(user.id, LIST)
    proposal = _proposal(user.id)

    _run(merge, proposal, user, _reply(ADD_MILK), meanwhile=_tick(
        app, user, "## Today\n- [x] call mom\n- [ ] an old task"))

    assert _contents(user.id) == [
        "## Today\n- [x] call mom\n- [ ] an old task",
        "## Today\n- [x] call mom\n- [ ] an old task\n- [ ] buy milk"]
    assert _entry(proposal.id)["apply_status"] == "completed"


def test_a_tick_on_the_line_the_merge_adds_after_stays(app, merge):
    """The merge's edit names the line the user just ticked: it applies
    where that line is, and the line stays ticked."""
    user = _user()
    _todo(user.id, LIST)
    proposal = _proposal(user.id)

    _run(merge, proposal, user, _reply(ADD_MILK), meanwhile=_tick(
        app, user, "## Today\n- [ ] call mom\n- [x] an old task"))

    assert _contents(user.id)[-1] == (
        "## Today\n- [ ] call mom\n- [x] an old task\n- [ ] buy milk")
    assert _entry(proposal.id)["apply_status"] == "completed"


def test_an_item_ticked_by_both_the_merge_and_the_user(app, merge):
    user = _user()
    _todo(user.id, LIST)
    proposal = _proposal(user.id)

    _run(merge, proposal, user,
         _reply(("- [ ] call mom", "- [x] call mom"), ADD_MILK),
         meanwhile=_tick(
             app, user, "## Today\n- [x] call mom\n- [ ] an old task"))

    assert _contents(user.id)[-1] == (
        "## Today\n- [x] call mom\n- [ ] an old task\n- [ ] buy milk")


def test_an_untick_during_a_merge_wins_over_the_merges_tick(app, merge):
    """The user's edit wins: the merge would tick the item, the user
    unticked it meanwhile."""
    user = _user()
    _todo(user.id, "## Today\n- [x] call mom\n- [ ] an old task")
    proposal = _proposal(user.id)

    _run(merge, proposal, user,
         _reply(("- [x] call mom\n- [ ] an old task",
                 "- [x] call mom\n- [x] an old task")),
         meanwhile=_tick(
             app, user, "## Today\n- [ ] call mom\n- [ ] an old task"))

    assert _contents(user.id)[-1] == (
        "## Today\n- [ ] call mom\n- [x] an old task")


def test_an_editor_save_during_a_merge_stays(app, merge):
    user = _user()
    _todo(user.id, LIST)
    proposal = _proposal(user.id)
    mine = "## Today\n- [ ] call mom at six\n- [ ] an old task\n- [ ] bake"

    def save():
        client = _client(app, user)
        opened = client.get("/api/todo/").get_json()["todo"]
        assert client.put("/api/todo/", json={
            "content": mine,
            "base_revision": opened["revision"]}).status_code == 200

    _run(merge, proposal, user, _reply(ADD_MILK),
         meanwhile=_elsewhere(app, save))

    assert _contents(user.id) == [
        LIST, mine,
        "## Today\n- [ ] call mom at six\n- [ ] an old task\n- [ ] buy milk"
        "\n- [ ] bake"]


def test_a_merge_whose_line_the_user_removed_saves_nothing(app, merge):
    """The editor removed the line the merge's edit names: the edit no
    longer fits, so nothing is saved and the proposal can be applied again
    (on the newest list)."""
    user = _user()
    _todo(user.id, LIST)
    proposal = _proposal(user.id)
    mine = "## Today\n- [ ] call mom"

    def save():
        assert _client(app, user).put(
            "/api/todo/", json={"content": mine}).status_code == 200

    _run(merge, proposal, user, _reply(ADD_MILK),
         meanwhile=_elsewhere(app, save))

    assert _contents(user.id) == [LIST, mine]
    entry = _entry(proposal.id)
    assert entry["apply_status"] == "failed"
    assert entry["apply_error"] == merge.LIST_CHANGED_MESSAGE
    assert entry["retryable"] is True
    assert [d.parent_id for d in Draft.query.filter_by(
        user_id=user.id, label="todo_pending")] == [proposal.id]


def test_a_full_write_merge_saves_nothing_when_the_list_changed(
        app, merge):
    """A merge onto a list with no tasks writes the whole list; a task the
    user added meanwhile would be dropped by it."""
    user = _user()
    _todo(user.id, "## Today\n\n- [ ] \n")
    proposal = _proposal(user.id)
    full = json.dumps({"edits": [],
                       "updated_content": "## Today\n- [ ] buy milk"})

    _run(merge, proposal, user, full, meanwhile=_tick(
        app, user, "## Today\n\n- [ ] call mom\n"))

    assert _contents(user.id) == ["## Today\n\n- [ ] call mom\n"]
    assert _entry(proposal.id)["apply_status"] == "failed"
    assert _entry(proposal.id)["retryable"] is True


def test_a_merge_with_no_change_meanwhile_saves_its_result(app, merge):
    user = _user()
    _todo(user.id, LIST)
    proposal = _proposal(user.id)

    _run(merge, proposal, user, _reply(ADD_MILK))

    assert _contents(user.id) == [
        LIST, "## Today\n- [ ] call mom\n- [ ] an old task\n- [ ] buy milk"]


# ── rebase_merge on its own ─────────────────────────────────────────────

def _accepted(previous, reply):
    """A MergeRun that accepted *reply* for *previous*."""
    from backend.utils.todo_merge_edits import MergeRun, resolve_merge_reply
    run = MergeRun()
    run.merged, failure, _ = resolve_merge_reply(reply, previous, run)
    assert failure is None
    return run


def test_rebase_keeps_the_merge_when_the_text_is_unchanged():
    from backend.utils.todo_merge_edits import rebase_merge
    run = _accepted(LIST, _reply(ADD_MILK))
    assert rebase_merge(run, LIST, LIST) == run.merged


def test_rebase_refuses_an_edit_whose_line_was_reworded():
    from backend.utils.todo_merge_edits import rebase_merge
    run = _accepted(LIST, _reply(ADD_MILK))
    newest = "## Today\n- [ ] call mom\n- [ ] an older task"
    assert rebase_merge(run, LIST, newest) is None


def test_rebase_refuses_an_edit_whose_line_is_now_there_twice():
    """The user ticked the line the edit names and added a second ticked
    copy of it: where the edit goes isn't certain, so it isn't guessed."""
    from backend.utils.todo_merge_edits import rebase_merge
    run = _accepted(LIST, _reply(ADD_MILK))
    newest = "## Today\n- [ ] call mom\n- [x] an old task\n- [x] an old task"
    assert rebase_merge(run, LIST, newest) is None


# From the review of #492: an edit's old_text is usually one line with no
# newline, so as plain text it also matches the start of a longer line.
# Only whole lines count.
TICK_BOB = (("- [ ] Email Bob", "- [x] Email Bob"),)


def test_rebase_refuses_a_line_reworded_at_its_end():
    """(a) The editor reworded the line the merge ticks: nothing saved,
    not the reworded line ticked."""
    from backend.utils.todo_merge_edits import rebase_merge
    run = _accepted("- [ ] Email Bob", _reply(*TICK_BOB))
    assert rebase_merge(run, "- [ ] Email Bob",
                        "- [ ] Email Bob and Alice") is None


def test_rebase_does_not_tick_a_new_task_that_starts_like_the_line():
    """(b) The user ticked the line and quick-added a task that starts with
    its text: the new task stays open."""
    from backend.utils.todo_merge_edits import rebase_merge
    run = _accepted("- [ ] Email Bob", _reply(*TICK_BOB))
    newest = "- [x] Email Bob\n- [ ] Email Bob's landlord"
    assert rebase_merge(run, "- [ ] Email Bob", newest) == newest


def test_rebase_does_not_undo_an_untick_on_a_longer_line():
    """(c) `- [ ] Call` is not the start of `Call mom`: the user's untick
    of Call mom stays."""
    from backend.utils.todo_merge_edits import rebase_merge
    previous = "- [ ] Call\n- [x] Call mom"
    run = _accepted(previous, _reply(("- [ ] Call", "- [x] Call")))
    newest = "- [x] Call\n- [ ] Call mom"
    assert rebase_merge(run, previous, newest) == newest


def test_rebase_refuses_when_a_look_alike_line_was_added():
    """The user ticked the line and added a new task with the same text:
    which one the edit meant isn't certain."""
    from backend.utils.todo_merge_edits import rebase_merge
    run = _accepted("- [ ] Email Bob", _reply(*TICK_BOB))
    newest = "- [x] Email Bob\n- [ ] Email Bob"
    assert rebase_merge(run, "- [ ] Email Bob", newest) is None


RECURRING = "## Today\n- [ ] Gym\n- [ ] call mom\n## Done\n- [x] Gym"


def test_rebase_applies_to_the_same_copy_of_a_recurring_task():
    from backend.utils.todo_merge_edits import rebase_merge
    run = _accepted(RECURRING, _reply(("- [ ] Gym", "- [x] Gym")))
    newest = RECURRING.replace("- [ ] call mom", "- [x] call mom")
    assert rebase_merge(run, RECURRING, newest) == (
        "## Today\n- [x] Gym\n- [x] call mom\n## Done\n- [x] Gym")


def test_rebase_refuses_when_the_user_changed_a_copy_of_a_recurring_task():
    """The user unticked the done copy and ticked the open one: the edit's
    text now matches the other copy, so it isn't applied."""
    from backend.utils.todo_merge_edits import rebase_merge
    run = _accepted(RECURRING, _reply(("- [ ] Gym", "- [x] Gym")))
    newest = "## Today\n- [x] Gym\n- [ ] call mom\n## Done\n- [ ] Gym"
    assert rebase_merge(run, RECURRING, newest) is None


def test_a_quick_add_during_a_merge_is_not_saved_as_done(app, merge):
    """(b) through the merge task: the tick and the quick-add are made
    while the model works."""
    user = _user()
    _todo(user.id, "## Today\n- [ ] Email Bob")
    proposal = _proposal(user.id)

    _run(merge, proposal, user, _reply(*TICK_BOB), meanwhile=_tick(
        app, user, "## Today\n- [x] Email Bob\n- [ ] Email Bob's landlord"))

    assert _contents(user.id)[-1] == (
        "## Today\n- [x] Email Bob\n- [ ] Email Bob's landlord")
    assert _entry(proposal.id)["apply_status"] == "completed"


def test_rebase_keeps_the_users_boxes_in_a_multi_line_edit():
    from backend.utils.todo_merge_edits import rebase_merge
    previous = "## Today\n- [ ] a\n  - [ ] a1\n- [ ] b"
    run = _accepted(previous, _reply(
        ("- [ ] a\n  - [ ] a1", "- [ ] a\n  - [ ] a1\n  - [ ] a2")))
    newest = "## Today\n- [ ] a\n  - [x] a1\n- [x] b"
    assert rebase_merge(run, previous, newest) == (
        "## Today\n- [ ] a\n  - [x] a1\n  - [ ] a2\n- [x] b")


# ── A write that commits while this one waits for the lock ──────────────

def _holder_commits_first(monkeypatch, module, other):
    """Replaces *module*'s lock_user_todo: the first call runs *other*,
    the writer that held the lock and commits before this one gets it.
    Returns the list of lock calls."""
    calls = []

    def lock(user_id):
        calls.append(user_id)
        if len(calls) == 1:
            other()
    monkeypatch.setattr(module, "lock_user_todo", lock)
    return calls


def test_a_tick_reads_the_list_after_it_gets_the_lock(app, monkeypatch):
    """#477's second case: a Save committed while the tick request was
    under way. The tick is checked against the Save's version, not
    written into the version before it."""
    import backend.routes.todo as todo_routes
    user = _user()
    _todo(user.id, LIST)
    client = _client(app, user)
    loaded = client.get("/api/todo/").get_json()["todo"]
    mine = LIST + "\n- [ ] bake"

    def save():
        _todo(user.id, mine)
    calls = _holder_commits_first(monkeypatch, todo_routes,
                                  _elsewhere(app, save))

    res = client.patch("/api/todo/", json={
        "content": "## Today\n- [x] call mom\n- [ ] an old task",
        "base_revision": loaded["revision"]})

    assert calls == [user.id]
    assert res.status_code == 409
    assert res.get_json()["todo"]["content"] == mine
    # The older version wasn't changed in place.
    assert _contents(user.id) == [LIST, mine]


def test_every_todo_writer_takes_the_lock(app, merge, monkeypatch):
    import backend.routes.todo as todo_routes
    user = _user()
    first = _todo(user.id, LIST)
    proposal = _proposal(user.id)
    taken = []
    monkeypatch.setattr(todo_routes, "lock_user_todo", taken.append)
    monkeypatch.setattr(merge, "lock_user_todo", taken.append)
    client = _client(app, user)

    client.patch("/api/todo/", json={"content": LIST + "\n- [ ] a"})
    client.put("/api/todo/", json={"content": LIST + "\n- [ ] b"})
    client.post(f"/api/todo/revert/{first.id}")
    _run(merge, proposal, user, _reply(ADD_MILK))

    assert taken == [user.id] * 4


def test_a_busy_lock_answers_503_and_writes_nothing(app, monkeypatch):
    import backend.routes.todo as todo_routes
    from backend.utils.todo_lock import TodoBusy
    user = _user()
    _todo(user.id, LIST)

    def busy(user_id):
        raise TodoBusy()
    monkeypatch.setattr(todo_routes, "lock_user_todo", busy)
    client = _client(app, user)

    res = client.patch("/api/todo/", json={"content": LIST + "\n- [ ] a"})
    assert res.status_code == 503
    assert res.get_json()["code"] == "todo_busy"
    assert client.put("/api/todo/", json={
        "content": "x"}).status_code == 503
    assert _contents(user.id) == [LIST]


def test_a_merge_that_cant_get_the_lock_saves_nothing(
        app, merge, monkeypatch):
    from backend.utils.todo_lock import TodoBusy
    user = _user()
    _todo(user.id, LIST)
    proposal = _proposal(user.id)

    def busy(user_id):
        raise TodoBusy()
    monkeypatch.setattr(merge, "lock_user_todo", busy)

    _run(merge, proposal, user, _reply(ADD_MILK))

    assert _contents(user.id) == [LIST]
    entry = _entry(proposal.id)
    assert entry["apply_status"] == "failed"
    assert entry["apply_error"] == merge.LOCK_BUSY_MESSAGE
    assert entry["retryable"] is True


# ── An older proposal doesn't come back over a newer applied one ────────

def test_a_failed_merge_does_not_come_back_after_a_newer_one_was_applied(
        app, merge):
    """From the review of #472: P1 is applied, P2 is proposed and applied
    while P1's merge runs, then P1's merge fails. Applying P1 again could
    add P2's tasks a second time, so P1 doesn't come back."""
    user = _user()
    _todo(user.id, LIST)
    p1 = _proposal(user.id)
    _proposal(user.id)   # P2, started: its merge waits for P1's

    _run(merge, p1, user, ["not json", "not json"])

    entry = _entry(p1.id)
    assert entry["apply_status"] == "failed"
    assert entry["retryable"] is False
    assert Draft.query.filter_by(user_id=user.id,
                                 label="todo_pending").count() == 0


def test_a_second_apply_of_a_real_proposal_reports_the_running_merge(app):
    """A proposal is an AI reply, owned by the user through human_owner_id;
    a second apply while its merge runs is told so (409), not 404."""
    user = _user()
    proposal = _proposal(user.id)

    res = _client(app, user).post("/api/todo/apply-draft",
                                  json={"llm_node_id": proposal.id})

    assert res.status_code == 409
    assert res.get_json()["code"] == "todo_merge_started"


# ── PostgreSQL: two transactions really wait for each other ─────────────

pg = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="set LOORE_TEST_POSTGRES_URL to a throwaway PostgreSQL database")


@pytest.fixture
def pg_app():
    import backend.celery_app  # noqa: F401
    app = _make_app(POSTGRES_URL)
    with app.app_context():
        _db.create_all()
    yield app
    with app.app_context():
        _db.session.remove()
        _db.drop_all()


def _in_thread(target):
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


@pg
def test_pg_a_second_writer_waits_until_the_first_commits(pg_app):
    from sqlalchemy import text
    from backend.utils.todo_lock import lock_user_todo
    with pg_app.app_context():
        alice, bob = _user("alice"), _user("bob")
        alice_id, bob_id = alice.id, bob.id
    first_holds, second_has_it, release = (
        threading.Event(), threading.Event(), threading.Event())
    timeouts = []

    def first():
        with pg_app.app_context():
            lock_user_todo(alice_id)
            # The wait's bound is put back once the lock is taken.
            timeouts.append(_db.session.execute(
                text("SELECT current_setting('lock_timeout')")).scalar())
            first_holds.set()
            release.wait(10)
            _db.session.commit()

    def second():
        with pg_app.app_context():
            lock_user_todo(alice_id)
            second_has_it.set()
            _db.session.commit()

    one = _in_thread(first)
    assert first_holds.wait(10)
    two = _in_thread(second)
    assert not second_has_it.wait(0.5)
    # Another user's todo isn't held up.
    with pg_app.app_context():
        lock_user_todo(bob_id)
        _db.session.commit()
    release.set()
    assert second_has_it.wait(10)
    one.join(10)
    two.join(10)
    assert timeouts == ["0"]


@pg
def test_pg_a_writer_gives_up_after_the_wait(pg_app, monkeypatch):
    import backend.utils.todo_lock as todo_lock
    monkeypatch.setattr(todo_lock, "TODO_LOCK_WAIT_SECONDS", 0.2)
    with pg_app.app_context():
        user_id = _user().id
    holds, release = threading.Event(), threading.Event()

    def holder():
        with pg_app.app_context():
            todo_lock.lock_user_todo(user_id)
            holds.set()
            release.wait(10)
            _db.session.commit()

    thread = _in_thread(holder)
    assert holds.wait(10)
    try:
        with pg_app.app_context():
            with pytest.raises(todo_lock.TodoBusy):
                todo_lock.lock_user_todo(user_id)
    finally:
        release.set()
        thread.join(10)


@pg
def test_pg_a_tick_during_a_merge_stays(pg_app, monkeypatch):
    """test_a_tick_during_a_merge_stays with the tick on its own database
    connection, as on the server."""
    merge = _patched_merge(monkeypatch)
    with pg_app.app_context():
        user = _user()
        _todo(user.id, LIST)
        proposal = _proposal(user.id)

        _run(merge, proposal, user, _reply(ADD_MILK), meanwhile=_tick(
            pg_app, user.id,
            "## Today\n- [x] call mom\n- [ ] an old task"))

        assert _contents(user.id) == [
            "## Today\n- [x] call mom\n- [ ] an old task",
            "## Today\n- [x] call mom\n- [ ] an old task\n- [ ] buy milk"]


@pg
def test_pg_a_stale_save_waits_for_a_tick_then_sees_it(pg_app, monkeypatch):
    """The tick request holds the lock between its read and its write; an
    editor Save opened before the tick waits, then is refused with the
    ticked list instead of dropping the tick."""
    import backend.routes.todo as todo_routes
    with pg_app.app_context():
        user = _user().id
        _todo(user, LIST)
    tick_read, release, save_done = (
        threading.Event(), threading.Event(), threading.Event())
    real_newest = todo_routes.newest_todo
    results = {}

    def paused_newest(user_id):
        todo = real_newest(user_id)
        if threading.current_thread().name == "tick":
            tick_read.set()
            release.wait(10)
        return todo
    monkeypatch.setattr(todo_routes, "newest_todo", paused_newest)

    client = _client(pg_app, user)
    opened = client.get("/api/todo/").get_json()["todo"]

    def tick():
        results["tick"] = _client(pg_app, user).patch("/api/todo/", json={
            "content": "## Today\n- [x] call mom\n- [ ] an old task",
            "base_revision": opened["revision"]})

    def save():
        results["save"] = _client(pg_app, user).put("/api/todo/", json={
            "content": LIST + "\n- [ ] bake",
            "base_revision": opened["revision"]})
        save_done.set()

    one = threading.Thread(target=tick, name="tick", daemon=True)
    one.start()
    assert tick_read.wait(10)
    two = _in_thread(save)
    assert not save_done.wait(0.5)
    release.set()
    one.join(10)
    two.join(10)

    assert results["tick"].status_code == 200
    assert results["save"].status_code == 409
    assert results["save"].get_json()["todo"]["content"] == (
        "## Today\n- [x] call mom\n- [ ] an old task")
    with pg_app.app_context():
        assert _contents(user) == [
            "## Today\n- [x] call mom\n- [ ] an old task"]

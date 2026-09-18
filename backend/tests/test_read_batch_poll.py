"""The {ca_tweets} batch poll (2026-09-18), after a prod Read failed on
2026-09-17 when a deploy restarted the worker while a poll was inside the
prompt render.

While the reply's batch is submitted, a poll is one provider call made
before any context is built, and only the provider's verdict on the item
or the poll cap fails the node: any other error (the restart, KMS, the
network, the DB) re-queues the poll with the entry still submitted, so a
batch is never paid for twice. Runs the real task body through the
test_retrieval_loop harness with the provider's batch calls scripted.
"""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest  # noqa: F401

from backend.tests.test_retrieval_loop import (  # noqa: F401 (fixture)
    app, _llm_task_mod, generate_llm_response, _mk_user,
)
from backend.tests.test_read_context import (
    _prompt_node, _stub_archive, _feed_json, CORPUS, REFS,
)
from backend.extensions import db as _db
from backend.models import Node, ExternalItem, FeedPick, APICostLog
from backend.utils import llm_batch
from backend.utils.llm_batch import BatchItemFailed, BatchItemCancelled
from backend.utils.cost import llm_cost_log_fields as _real_cost_fields

POLL = _llm_task_mod.CA_BATCH_POLL_SECONDS
MAX_POLLS = _llm_task_mod.CA_BATCH_MAX_POLLS


class _Task:
    """The bound Celery task: retry() records its arguments and raises
    what celery raises (Retry, or the cap when told to)."""
    def __init__(self, cap=False, retries=3):
        self.retries = []
        self.cap = cap
        self.request = SimpleNamespace(id=None, retries=retries)

    def update_state(self, *args, **kwargs):
        pass

    def retry(self, **kwargs):
        self.retries.append(kwargs)
        if self.cap:
            raise _llm_task_mod.MaxRetriesExceededError("Can't retry")
        raise _llm_task_mod.Retry("re-queued")


def _reload(node_id):
    _db.session.expire_all()
    return Node.query.get(node_id)


def _entry(node):
    return next(m for m in json.loads(node.tool_calls_meta)
                if m.get("name") == "_batch")


def _batch_resp():
    """What openai_batch_collect_one returns once the batch completed."""
    return {
        "content": _feed_json([
            {"n": 2, "qt": "Meets your question about pacing.",
             "relevance": 40, "recommend": True},
        ]),
        "total_tokens": 150, "input_tokens": 100, "output_tokens": 50,
        "cached_tokens": 0, "cache_write_subset_tokens": 0,
        "tool_calls": [], "truncated": False,
        "batch": True, "batch_id": "batch_1",
    }


def _read_thread(*, submitted=True, key_type="chat", entry_status="submitted"):
    """read prompt -> reply placeholder, with a live batch entry
    (submitted, or cancelling: a withdrawal whose outcome is pending)."""
    alice = _mk_user("alice", approved=True, plan="alpha", is_admin=True)
    llm_user = _mk_user("gpt-5", twitter_id="llm-gpt-5")
    read = _prompt_node(alice, "read")
    llm_node = Node(user_id=llm_user.id, human_owner_id=alice.id,
                    parent_id=read.id, node_type="llm", llm_model="gpt-5",
                    llm_task_status="processing" if submitted else "pending",
                    privacy_level="private", ai_usage="chat")
    llm_node.set_content("[LLM response generation pending...]")
    _db.session.add(llm_node)
    _db.session.flush()
    if submitted:
        entry = {
            "name": "_batch", "batch_id": "batch_1",
            "custom_id": f"node-{llm_node.id}", "model": "gpt-5",
            "provider": "openai", "submitted_at": "2026-09-17T09:07:00",
            "status": entry_status,
        }
        if key_type:
            entry["key_type"] = key_type
        if entry_status == "cancelling":
            entry["cancel_requested_at"] = "2026-09-17T10:00:00"
            entry["cancel_reason"] = "spend_cap"
        llm_node.tool_calls_meta = json.dumps([entry])
    _db.session.commit()
    return alice, read, llm_node


def _script(monkeypatch, tmp_path, collect, render=None, cancel=None):
    """Script the provider's batch calls and count the context builders.
    *collect* is called per poll and returns (status, resp) or raises;
    *render* replaces the archive render when given; *cancel* replaces
    the provider's cancel (recorded either way)."""
    _stub_archive(monkeypatch, tmp_path)
    # The harness stubs llm_providers, which makes this constant a mock.
    monkeypatch.setattr(_llm_task_mod, "DEFAULT_MAX_OUTPUT_TOKENS", 10000)
    calls = {"collect": [], "submit": [], "cancel": [], "chain": 0,
             "render": 0}

    def _cancel(key, batch_id):
        calls["cancel"].append((key, batch_id))
        if cancel is not None:
            return cancel(key, batch_id)
        return "cancelling"
    monkeypatch.setattr(llm_batch, "openai_batch_cancel_one", _cancel)

    def _collect(key, batch_id, custom_id):
        calls["collect"].append((key, batch_id, custom_id))
        return collect()
    monkeypatch.setattr(llm_batch, "openai_batch_collect_one", _collect)

    def _submit(key, custom_id, api_model, messages, max_tokens,
                output_schema=None):
        calls["submit"].append(custom_id)
        return "batch_9"
    monkeypatch.setattr(llm_batch, "openai_batch_submit_one", _submit)

    real_chain = _llm_task_mod._load_node_chain

    def _chain(parent):
        calls["chain"] += 1
        return real_chain(parent)
    monkeypatch.setattr(_llm_task_mod, "_load_node_chain", _chain)

    from backend.utils import community_archive as ca

    def _render(*args, **kwargs):
        calls["render"] += 1
        if render is not None:
            return render()
        return CORPUS, {"tweets": 2}, dict(REFS)
    monkeypatch.setattr(ca, "render_recent_tweets", _render)
    return calls


def _run(task, alice, read, llm_node):
    return generate_llm_response(task, read.id, llm_node.id, "gpt-5",
                                 alice.id)


# ── polling ──────────────────────────────────────────────────────────────

def test_pending_poll_builds_no_context(app, monkeypatch, tmp_path):  # noqa: F811
    calls = _script(monkeypatch, tmp_path,
                    collect=lambda: ("in_progress", None))
    alice, read, llm_node = _read_thread()
    task = _Task()

    with pytest.raises(_llm_task_mod.Retry):
        _run(task, alice, read, llm_node)

    assert calls["chain"] == 0
    assert calls["render"] == 0
    # The stored key type picks the key; the batch key override applies.
    assert calls["collect"] == [("sk-test", "batch_1", f"node-{llm_node.id}")]
    assert calls["submit"] == []
    assert calls["cancel"] == []
    assert task.retries == [{"countdown": POLL, "max_retries": MAX_POLLS}]
    node = _reload(llm_node.id)
    assert node.llm_task_status == "processing"
    assert node.llm_task_error is None
    entry = _entry(node)
    assert entry["status"] == "submitted"
    assert entry["last_polled_at"]
    assert "ended_at" not in entry


def test_entry_without_key_type_loads_the_chain_for_the_key(app, monkeypatch, tmp_path):  # noqa: F811
    """A batch submitted before the key type was stored: the chain is
    loaded to pick the key, the archive is still not rendered."""
    calls = _script(monkeypatch, tmp_path,
                    collect=lambda: ("in_progress", None))
    alice, read, llm_node = _read_thread(key_type=None)

    with pytest.raises(_llm_task_mod.Retry):
        _run(_Task(), alice, read, llm_node)

    assert calls["chain"] == 1
    assert calls["render"] == 0
    assert calls["collect"][0][0] == "sk-test"
    # Resolved once: the poll writes it back with the heartbeat.
    assert _entry(_reload(llm_node.id))["key_type"] == "chat"


def test_submit_records_the_key_type(app, monkeypatch, tmp_path):  # noqa: F811
    calls = _script(monkeypatch, tmp_path,
                    collect=lambda: ("in_progress", None))
    alice, read, llm_node = _read_thread(submitted=False)
    task = _Task()

    with pytest.raises(_llm_task_mod.Retry):
        _run(task, alice, read, llm_node)

    assert calls["submit"] == [f"node-{llm_node.id}"]
    assert calls["collect"] == []
    assert calls["render"] == 1
    assert task.retries == [{"countdown": POLL, "max_retries": MAX_POLLS}]
    node = _reload(llm_node.id)
    assert node.llm_task_status == "processing"
    entry = _entry(node)
    assert entry["status"] == "submitted"
    assert entry["batch_id"] == "batch_9"
    assert entry["provider"] == "openai"
    assert entry["key_type"] == "chat"
    assert entry["submitted_at"]

    # The first poll then needs neither the chain nor the archive.
    calls["chain"] = calls["render"] = 0
    with pytest.raises(_llm_task_mod.Retry):
        _run(_Task(), alice, read, llm_node)
    assert calls["collect"] == [("sk-test", "batch_9", f"node-{llm_node.id}")]
    assert calls["chain"] == 0
    assert calls["render"] == 0


# ── what fails the node, what does not ───────────────────────────────────

def test_poll_error_keeps_the_node_processing_and_polls_again(app, monkeypatch, tmp_path):  # noqa: F811
    def unreachable():
        raise ConnectionError("provider unreachable")
    calls = _script(monkeypatch, tmp_path, collect=unreachable)
    alice, read, llm_node = _read_thread()
    task = _Task()

    with pytest.raises(_llm_task_mod.Retry):
        _run(task, alice, read, llm_node)

    assert task.retries == [{"countdown": POLL, "max_retries": MAX_POLLS}]
    assert calls["submit"] == []
    node = _reload(llm_node.id)
    assert node.llm_task_status == "processing"
    assert node.llm_task_error is None
    assert _entry(node)["status"] == "submitted"


def test_provider_verdict_fails_the_node(app, monkeypatch, tmp_path):  # noqa: F811
    def expired():
        raise BatchItemFailed("OpenAI batch batch_1 expired")
    _script(monkeypatch, tmp_path, collect=expired)
    alice, read, llm_node = _read_thread()
    task = _Task()

    with pytest.raises(BatchItemFailed):
        _run(task, alice, read, llm_node)

    assert task.retries == []
    node = _reload(llm_node.id)
    assert node.llm_task_status == "failed"
    assert "expired" in node.llm_task_error


def test_poll_cap_fails_the_node(app, monkeypatch, tmp_path):  # noqa: F811
    """The batch still running when retry() reports the cap."""
    _script(monkeypatch, tmp_path, collect=lambda: ("in_progress", None))
    alice, read, llm_node = _read_thread()

    with pytest.raises(_llm_task_mod.MaxRetriesExceededError):
        _run(_Task(cap=True), alice, read, llm_node)

    node = _reload(llm_node.id)
    assert node.llm_task_status == "failed"
    assert f"{MAX_POLLS} polls" in node.llm_task_error


def test_poll_error_at_the_cap_fails_the_node_with_the_last_error(app, monkeypatch, tmp_path):  # noqa: F811
    def unreachable():
        raise ConnectionError("provider unreachable")
    _script(monkeypatch, tmp_path, collect=unreachable)
    alice, read, llm_node = _read_thread()

    with pytest.raises(_llm_task_mod.MaxRetriesExceededError):
        _run(_Task(cap=True), alice, read, llm_node)

    node = _reload(llm_node.id)
    assert node.llm_task_status == "failed"
    assert "provider unreachable" in node.llm_task_error
    assert f"{MAX_POLLS} polls" in node.llm_task_error


def test_error_without_a_batch_still_fails_the_node(app, monkeypatch, tmp_path):  # noqa: F811
    """No batch submitted yet: an error is a failure, as before."""
    def gone():
        raise RuntimeError("snapshot directory missing")
    calls = _script(monkeypatch, tmp_path,
                    collect=lambda: ("in_progress", None), render=gone)
    alice, read, llm_node = _read_thread(submitted=False)
    task = _Task()

    with pytest.raises(RuntimeError):
        _run(task, alice, read, llm_node)

    assert task.retries == []
    assert calls["submit"] == []
    node = _reload(llm_node.id)
    assert node.llm_task_status == "failed"
    assert "snapshot directory missing" in node.llm_task_error


def test_truncated_reply_fails_at_the_first_collect(app, monkeypatch, tmp_path):  # noqa: F811
    """A reply cut off at max_tokens is a verdict on the stored result:
    no re-collect can change it, so the node fails at once instead of
    re-rendering the archive every poll until the cap."""
    from backend.utils.ca_feed import FeedReplyError
    cut = _batch_resp()
    cut["content"] = cut["content"][:20]
    cut["truncated"] = True
    calls = _script(monkeypatch, tmp_path, collect=lambda: ("completed", cut))
    alice, read, llm_node = _read_thread()
    task = _Task()

    with pytest.raises(FeedReplyError):
        _run(task, alice, read, llm_node)

    assert task.retries == []
    assert calls["render"] == 1
    assert calls["submit"] == []
    node = _reload(llm_node.id)
    assert node.llm_task_status == "failed"
    assert "cut off" in node.llm_task_error


def test_unparseable_reply_fails_at_the_first_collect(app, monkeypatch, tmp_path):  # noqa: F811
    from backend.utils.ca_feed import FeedReplyError
    bad = _batch_resp()
    bad["content"] = "not the promised object"
    calls = _script(monkeypatch, tmp_path, collect=lambda: ("completed", bad))
    alice, read, llm_node = _read_thread()
    task = _Task()

    with pytest.raises(FeedReplyError):
        _run(task, alice, read, llm_node)

    assert task.retries == []
    assert calls["render"] == 1
    node = _reload(llm_node.id)
    assert node.llm_task_status == "failed"
    assert "not JSON" in node.llm_task_error


# ── the collecting run ───────────────────────────────────────────────────

def test_interrupted_collect_polls_again_and_never_resubmits(app, monkeypatch, tmp_path):  # noqa: F811
    """The 2026-09-17 failure: the provider reports the batch ended, the
    render dies (the deploy's SIGTERM surfaced as a DuckDB exception).
    The entry stays submitted, the next poll fetches the same result
    and finishes the reply; no second batch is ever submitted."""
    renders = iter([RuntimeError("Query interrupted"), None])

    def render():
        exc = next(renders)
        if exc is not None:
            raise exc
        return CORPUS, {"tweets": 2}, dict(REFS)
    calls = _script(monkeypatch, tmp_path,
                    collect=lambda: ("completed", _batch_resp()),
                    render=render)
    alice, read, llm_node = _read_thread()
    task = _Task()

    with pytest.raises(_llm_task_mod.Retry):
        _run(task, alice, read, llm_node)

    assert task.retries == [{"countdown": POLL, "max_retries": MAX_POLLS}]
    assert calls["render"] == 1
    node = _reload(llm_node.id)
    assert node.llm_task_status == "processing"
    assert node.llm_task_error is None
    entry = _entry(node)
    assert entry["status"] == "submitted"
    assert entry["ended_at"]
    assert calls["submit"] == []

    result = _run(_Task(), alice, read, llm_node)

    assert result["status"] == "completed"
    assert calls["submit"] == []
    assert len(calls["collect"]) == 2
    node = _reload(llm_node.id)
    assert node.llm_task_status == "completed"
    entry = _entry(node)
    assert entry["status"] == "ended"
    assert entry["collected_at"]
    item = ExternalItem.query.filter_by(
        user_id=alice.id, source="community_archive", external_id="222").one()
    assert node.get_content() == (
        "One thing today.\n\n"
        "Meets your question about pacing.\n\n"
        f"{{quote_ext:{item.id}}}")
    assert FeedPick.query.filter_by(node_id=node.id).count() == 1
    assert item.surfaced_count == 1


def test_collect_that_dies_inside_the_finalize_counts_once(app, monkeypatch, tmp_path):  # noqa: F811
    """A finalize that fails past its first commit (here the cost log)
    is run again by the next poll: one cost row, one surfacing bump,
    one set of picks."""
    calls_to_cost = []

    def cost_once(*args, **kwargs):
        calls_to_cost.append(args)
        if len(calls_to_cost) == 1:
            raise RuntimeError("KMS unavailable")
        return _real_cost_fields(*args, **kwargs)
    monkeypatch.setattr(_llm_task_mod, "llm_cost_log_fields", cost_once)
    calls = _script(monkeypatch, tmp_path,
                    collect=lambda: ("completed", _batch_resp()))
    alice, read, llm_node = _read_thread()

    with pytest.raises(_llm_task_mod.Retry):
        _run(_Task(), alice, read, llm_node)

    node = _reload(llm_node.id)
    assert node.llm_task_status == "processing"
    assert _entry(node)["status"] == "submitted"
    assert APICostLog.query.count() == 0
    item = ExternalItem.query.filter_by(
        user_id=alice.id, source="community_archive", external_id="222").one()
    assert item.surfaced_count == 0

    _run(_Task(), alice, read, llm_node)

    node = _reload(llm_node.id)
    assert node.llm_task_status == "completed"
    assert _entry(node)["status"] == "ended"
    assert calls["submit"] == []
    assert APICostLog.query.count() == 1
    assert FeedPick.query.filter_by(node_id=node.id).count() == 1
    item = ExternalItem.query.filter_by(
        user_id=alice.id, source="community_archive", external_id="222").one()
    assert item.surfaced_count == 1


def test_capped_user_still_collects_an_ended_batch(app, monkeypatch, tmp_path):  # noqa: F811
    """The batch ended before a poll saw the cap: the provider processed
    and billed it, so the reply is collected and its cost row written.
    A new read for the capped user is refused."""
    monkeypatch.setattr("backend.utils.spend.user_is_capped",
                        lambda user_id: True)
    calls = _script(monkeypatch, tmp_path,
                    collect=lambda: ("completed", _batch_resp()))
    alice, read, llm_node = _read_thread()

    _run(_Task(), alice, read, llm_node)

    node = _reload(llm_node.id)
    assert node.llm_task_status == "completed"
    assert APICostLog.query.count() == 1

    # A first run for the capped user does not submit.
    llm_user = Node.query.get(llm_node.id).user_id
    new = Node(user_id=llm_user, human_owner_id=alice.id, parent_id=read.id,
               node_type="llm", llm_model="gpt-5", llm_task_status="pending",
               privacy_level="private", ai_usage="chat")
    new.set_content("[LLM response generation pending...]")
    _db.session.add(new)
    _db.session.commit()
    _run(_Task(), alice, read, new)
    assert _reload(new.id).llm_task_status == "failed"
    assert calls["submit"] == []


def test_read_under_its_own_agentic_prompt_keeps_the_batch_record(app, monkeypatch, tmp_path):  # noqa: F811
    """{ca_tweets} inside a Text-mode prompt: the reply carries tool meta
    (_mode), and the batch entry must survive that write and close."""
    calls = _script(monkeypatch, tmp_path,
                    collect=lambda: ("completed", _batch_resp()))
    alice = _mk_user("alice", approved=True, plan="alpha", is_admin=True)
    llm_user = _mk_user("gpt-5", twitter_id="llm-gpt-5")
    prompt = _prompt_node(alice, "textmode",
                          body="You are the text-mode persona.\n\n"
                               "{ca_tweets?days=1}")
    llm_node = Node(user_id=llm_user.id, human_owner_id=alice.id,
                    parent_id=prompt.id, node_type="llm", llm_model="gpt-5",
                    llm_task_status="processing", privacy_level="private",
                    ai_usage="chat")
    llm_node.set_content("[LLM response generation pending...]")
    _db.session.add(llm_node)
    _db.session.flush()
    llm_node.tool_calls_meta = json.dumps([{
        "name": "_batch", "batch_id": "batch_1",
        "custom_id": f"node-{llm_node.id}", "model": "gpt-5",
        "provider": "openai", "key_type": "chat",
        "submitted_at": "2026-09-17T09:07:00", "status": "submitted"}])
    _db.session.commit()

    generate_llm_response(_Task(), prompt.id, llm_node.id, "gpt-5",
                          alice.id, source_mode="textmode")

    assert calls["submit"] == []
    node = _reload(llm_node.id)
    assert node.llm_task_status == "completed"
    names = [m["name"] for m in json.loads(node.tool_calls_meta)]
    assert "_mode" in names
    assert names.count("_batch") == 1
    entry = _entry(node)
    assert entry["status"] == "ended"
    assert entry["collected_at"]


# ── the backstop's lookup ────────────────────────────────────────────────

def test_find_stuck_feed_batches_wants_a_stale_submitted_entry(app):  # noqa: F811
    alice = _mk_user("alice", approved=True, plan="alpha", is_admin=True)
    now = datetime(2026, 9, 18, 12, 0, 0)
    stale = (now - timedelta(seconds=_llm_task_mod.CA_BATCH_STALE_SECONDS + 5)
             ).isoformat(timespec="seconds")
    fresh = (now - timedelta(seconds=30)).isoformat(timespec="seconds")

    def node(status, entries):
        n = Node(user_id=alice.id, human_owner_id=alice.id, node_type="llm",
                 llm_model="gpt-5", llm_task_status=status,
                 privacy_level="private", ai_usage="chat",
                 tool_calls_meta=json.dumps(entries))
        n.set_content("x")
        _db.session.add(n)
        _db.session.flush()
        return n

    def batch(status, polled):
        return {"name": "_batch", "batch_id": f"b-{status}-{polled}",
                "status": status, "submitted_at": stale,
                "last_polled_at": polled}
    orphan = node("processing", [batch("submitted", stale)])
    withdrawing = node("processing", [batch("cancelling", stale)])
    node("processing", [batch("submitted", fresh)])
    node("completed", [batch("ended", stale)])
    node("failed", [batch("submitted", stale)])
    node("processing", [batch("cancelled", stale)])
    resumed = node("processing", [batch("cancelled", stale),
                                  batch("submitted", stale)])
    _db.session.commit()

    found = _llm_task_mod.find_stuck_feed_batches(now=now)

    assert sorted(n.id for n, _ in found) == sorted(
        [orphan.id, withdrawing.id, resumed.id])
    assert {e["status"] for _, e in found} == {"submitted", "cancelling"}


# ── withdrawal: the spend cap reached while the batch is queued ──────────
# A batch request is billed when the provider processes it, not when it
# is submitted. A user who hits the cap while theirs is queued gets it
# withdrawn, and the provider's answer decides: ran anyway (billed, so
# collected and logged) or never ran (not billed, node cancelled).

def _capped(monkeypatch, value=True):
    monkeypatch.setattr("backend.utils.spend.user_is_capped",
                        lambda user_id: value)


def test_capped_user_pending_batch_is_withdrawn_at_the_provider(app, monkeypatch, tmp_path):  # noqa: F811
    _capped(monkeypatch)
    calls = _script(monkeypatch, tmp_path,
                    collect=lambda: ("in_progress", None))
    alice, read, llm_node = _read_thread()
    task = _Task()

    with pytest.raises(_llm_task_mod.Retry):
        _run(task, alice, read, llm_node)

    assert calls["cancel"] == [("sk-test", "batch_1")]
    assert task.retries == [{"countdown": POLL, "max_retries": MAX_POLLS}]
    node = _reload(llm_node.id)
    assert node.llm_task_status == "processing"
    entry = _entry(node)
    assert entry["status"] == "cancelling"
    assert entry["cancel_requested_at"]
    assert entry["cancel_reason"] == "spend_cap"
    assert "cancel_error" not in entry

    # Asked once: while the provider is still cancelling, later polls
    # wait for the outcome and build nothing.
    with pytest.raises(_llm_task_mod.Retry):
        _run(_Task(), alice, read, llm_node)
    assert len(calls["cancel"]) == 1
    assert calls["chain"] == 0
    assert calls["render"] == 0
    assert _entry(_reload(llm_node.id))["status"] == "cancelling"


def test_withdrawn_batch_that_never_ran_is_cancelled_unbilled(app, monkeypatch, tmp_path):  # noqa: F811
    _capped(monkeypatch)

    def never_ran():
        raise BatchItemCancelled(
            "OpenAI batch batch_1 cancelled before item node-1 ran")
    calls = _script(monkeypatch, tmp_path, collect=never_ran)
    alice, read, llm_node = _read_thread(entry_status="cancelling")
    task = _Task()

    result = _run(task, alice, read, llm_node)

    assert result["status"] == "cancelled"
    assert result["reason"] == "spend_cap"
    assert task.retries == []
    assert calls["submit"] == []
    assert calls["chain"] == 0
    assert calls["render"] == 0
    node = _reload(llm_node.id)
    assert node.llm_task_status == "cancelled"
    assert node.llm_task_progress == 100
    assert "nothing was billed" in node.get_content()
    assert node.llm_task_error == node.get_content()
    entry = _entry(node)
    assert entry["status"] == "cancelled"
    assert entry["cancel_outcome"] == "not_processed"
    assert entry["cancelled_at"]
    assert APICostLog.query.count() == 0
    assert FeedPick.query.count() == 0
    assert ExternalItem.query.count() == 0


def test_withdrawn_batch_that_ran_anyway_is_collected_and_billed(app, monkeypatch, tmp_path):  # noqa: F811
    _capped(monkeypatch)
    calls = _script(monkeypatch, tmp_path,
                    collect=lambda: ("cancelled", _batch_resp()))
    alice, read, llm_node = _read_thread(entry_status="cancelling")

    result = _run(_Task(), alice, read, llm_node)

    assert result["status"] == "completed"
    assert calls["submit"] == []
    assert calls["cancel"] == []
    node = _reload(llm_node.id)
    assert node.llm_task_status == "completed"
    assert APICostLog.query.count() == 1
    item = ExternalItem.query.filter_by(
        user_id=alice.id, source="community_archive", external_id="222").one()
    assert node.get_content().endswith(f"{{quote_ext:{item.id}}}")
    entry = _entry(node)
    assert entry["status"] == "ended"
    assert entry["cancel_outcome"] == "processed"
    assert entry["cancel_requested_at"]
    assert entry["collected_at"]


def test_cancel_refusal_is_recorded_and_the_outcome_still_read(app, monkeypatch, tmp_path):  # noqa: F811
    """The provider refuses the cancel (say the batch had just ended):
    recorded on the entry, and the next poll collects and bills as
    usual."""
    _capped(monkeypatch)
    polls = iter([("in_progress", None), ("completed", _batch_resp())])

    def refuse(key, batch_id):
        raise RuntimeError("batch already ended")
    calls = _script(monkeypatch, tmp_path, collect=lambda: next(polls),
                    cancel=refuse)
    alice, read, llm_node = _read_thread()

    with pytest.raises(_llm_task_mod.Retry):
        _run(_Task(), alice, read, llm_node)

    entry = _entry(_reload(llm_node.id))
    assert entry["status"] == "cancelling"
    assert "already ended" in entry["cancel_error"]
    assert calls["cancel"] == [("sk-test", "batch_1")]

    _run(_Task(), alice, read, llm_node)

    node = _reload(llm_node.id)
    assert node.llm_task_status == "completed"
    assert APICostLog.query.count() == 1
    assert _entry(node)["status"] == "ended"


def test_cancel_nobody_asked_for_fails_the_node(app, monkeypatch, tmp_path):  # noqa: F811
    """A batch cancelled from outside (the provider console) while the
    entry is still submitted: the provider's verdict, the node fails."""
    def gone():
        raise BatchItemCancelled(
            "OpenAI batch batch_1 cancelled before item node-1 ran")
    calls = _script(monkeypatch, tmp_path, collect=gone)
    alice, read, llm_node = _read_thread()
    task = _Task()

    with pytest.raises(BatchItemCancelled):
        _run(task, alice, read, llm_node)

    assert task.retries == []
    assert calls["cancel"] == []
    node = _reload(llm_node.id)
    assert node.llm_task_status == "failed"
    assert "cancelled" in node.llm_task_error
    assert _entry(node)["status"] == "submitted"

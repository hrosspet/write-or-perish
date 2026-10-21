"""Glean again after a rating reads the prompt cache (#511).

A verdict on a pick marks its tweet read, and a read tweet was left out
of the next render of the day. The list is numbered, so a Glean again
after a rating, on the same export, sent a different list from the
dropped tweet on: the header's counts and every later number changed,
and the request missed the prompt cache from the list on (the list is
most of the request).

Now a live Glean again on the same export keeps the earlier gleaning's
list as it was, and names the tweets read since in the closing note,
after the cached prefix; a pick of one is dropped on collect. The test
renders a real (tiny) archive snapshot with the same code a glean uses
and compares the two requests as the provider receives them. The model
is scripted: no real model call.
"""
import copy
import json
import sys
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from backend.tests.test_retrieval_loop import (  # noqa: F401 (fixture)
    app, _llm_task_mod, generate_llm_response, _FakeSelf, _mk_user, _resp,
    _fresh,
)
from backend.tests.test_read_context import _prompt_node, _user_node
from backend.extensions import db as _db
from backend.models import ExternalItem, FeedPick, FeedRender, Node
from backend.utils.ca_feed import (
    CA_READ_AGAIN_TURN, CA_READ_AGAIN_TURN_KEPT, READ_FURTHER_MARKER,
    list_to_keep, read_numbers,
    read_reply_ids, read_since_note, seen_tweet_ids,
)


@pytest.fixture
def snapshot(tmp_path):
    """A day of six tweets by three accounts, in render order: ann #1,
    #2; bob #3, #4; cat #5, #6."""
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    tweets = tmp_path / "tweets.parquet"
    profiles = tmp_path / "profiles.parquet"
    con.execute(
        "copy (select * from (values "
        "('t1', 'a1', timestamptz '2026-10-20 06:00:00+00', 'mornings are for deep work'), "
        "('t2', 'a1', timestamptz '2026-10-20 08:00:00+00', 'a second thought on mornings'), "
        "('t3', 'a2', timestamptz '2026-10-20 05:00:00+00', 'slow mornings make the work better'), "
        "('t4', 'a2', timestamptz '2026-10-20 07:00:00+00', 'pacing is a skill'), "
        "('t5', 'a3', timestamptz '2026-10-20 06:30:00+00', 'on pacing a day'), "
        "('t6', 'a3', timestamptz '2026-10-20 09:00:00+00', 'the newest tweet')"
        ") v(tweet_id, account_id, created_at, full_text)) "
        f"to '{tweets}' (format parquet)")
    con.execute(
        "copy (select * from (values ('a1', 'ann'), ('a2', 'bob'), "
        f"('a3', 'cat')) v(account_id, username)) to '{profiles}' "
        "(format parquet)")
    (tmp_path / "export_id").write_text("2026-10-20T07-00-00Z")
    return tmp_path


class _RecordingProvider:
    """The provider stand-in: records each request as the provider gets
    it (the message dicts with their content blocks, deep-copied, and
    the keyword arguments) and returns the queued replies."""
    responses = []
    requests = []

    @classmethod
    def reset(cls, responses):
        cls.responses = list(responses)
        cls.requests = []

    @classmethod
    def get_completion(cls, model_id, messages, api_keys, tools=None,
                       prompt_cache_key=None, **kwargs):
        cls.requests.append({
            "model_id": model_id,
            "messages": copy.deepcopy(messages),
            "tools": copy.deepcopy(tools),
            "prompt_cache_key": prompt_cache_key,
            "output_schema": copy.deepcopy(kwargs.get("output_schema")),
        })
        nxt = cls.responses.pop(0)
        # Like the real provider: whether a cache comparison went out (#348).
        nxt["cache_comparison_sent"] = bool(
            kwargs.get("cache_comparison_response_id"))
        return nxt


def _real_archive(monkeypatch, snapshot):
    """The task renders *snapshot* with the real render; the background
    refresh a read queues is not run."""
    imports = sys.modules.get("backend.tasks.imports")
    if imports is None or isinstance(imports, MagicMock):
        imports = MagicMock()
        monkeypatch.setitem(sys.modules, "backend.tasks.imports", imports)
        imports.snapshot_dir_for = lambda config: snapshot
    else:
        monkeypatch.setattr(imports, "snapshot_dir_for",
                            lambda config: snapshot)
    monkeypatch.setattr(_llm_task_mod, "request_snapshot_refresh",
                        lambda *a, **k: False)


def _placeholder(llm_user, alice, parent_id, meta=None):
    n = Node(user_id=llm_user.id, human_owner_id=alice.id,
             parent_id=parent_id, node_type="llm", llm_model="gpt-5",
             llm_task_status="pending", privacy_level="private",
             ai_usage="chat",
             tool_calls_meta=json.dumps(meta) if meta else None)
    n.set_content("[LLM response generation pending...]")
    _db.session.add(n)
    _db.session.commit()
    return n


def _glean(monkeypatch, alice, parent, llm_node, picks):
    """One glean as the task runs it (live: no batch marker), the model
    answering with *picks*. Returns the request the provider got."""
    _RecordingProvider.reset([_resp(json.dumps(
        {"verdict": "One thing today.", "picks": picks}))])
    monkeypatch.setattr(_llm_task_mod, "LLMProvider", _RecordingProvider)
    generate_llm_response(_FakeSelf(), parent.id, llm_node.id, "gpt-5",
                          alice.id, source_mode=None)
    assert len(_RecordingProvider.requests) == 1
    return _RecordingProvider.requests[0]


# The sentence of the read-further request that a kept list makes false.
READ_OUT_OF_LIST = "Tweets I have read are out of that list;"


def _gleaned_and_rated(monkeypatch, snapshot, rate=True):
    """A reflection, the Glean prompt under it, the first gleaning (it
    picks bob's #3) and the user's verdict on that pick, which marks the
    tweet read (routes/external.apply_item_feedback, as the page does).
    The user had read cat's newest tweet (t6) before: no gleaning ever
    lists it."""
    from backend.routes.external import apply_item_feedback
    _real_archive(monkeypatch, snapshot)
    alice = _mk_user("alice", approved=True, plan="alpha", is_admin=True)
    llm_user = _mk_user("gpt-5", twitter_id="llm-gpt-5")
    bookmark = ExternalItem(user_id=alice.id, source="twitter_bookmark",
                            external_id="t6",
                            read_at=datetime(2026, 10, 20, 10, 0))
    bookmark.set_content("the newest tweet")
    _db.session.add(bookmark)
    note = _user_node(alice, None, "how do I pace the mornings?")
    read = _prompt_node(alice, "read_thread", parent_id=note.id)
    _db.session.commit()
    gleaning = _placeholder(llm_user, alice, read.id, meta=[{"name": "_live"}])
    first = _glean(monkeypatch, alice, read, gleaning, [
        {"n": 3, "qt": "This is about your mornings.", "relevance": 50,
         "recommend": True}])
    gleaning = _fresh(gleaning.id)
    assert gleaning.llm_task_status == "completed"
    pick = ExternalItem.query.filter_by(user_id=alice.id,
                                        external_id="t3").one()
    if rate:
        apply_item_feedback(pick, "good", node_id=gleaning.id)
        _db.session.commit()
        assert seen_tweet_ids(alice.id) == {"t3", "t6"}
    return alice, llm_user, read, gleaning, pick, first


def _list_message(request):
    """The read prompt's message: the one carrying the day's list."""
    found = [m for m in request["messages"]
             if any("[#1] " in b["text"] for b in m["content"])]
    assert len(found) == 1
    return found[0]


def test_glean_again_after_a_rating_sends_the_first_request_byte_for_byte(app, monkeypatch, snapshot):  # noqa: F811
    from backend.utils import community_archive as ca
    alice, llm_user, read, gleaning, pick, first = _gleaned_and_rated(
        monkeypatch, snapshot)
    first_list = "".join(b["text"] for b in _list_message(first)["content"])
    assert "[#3] slow mornings make the work better" in first_list
    # The setup reproduces the issue: the first gleaning sent the whole
    # day, and rendered afresh without the read tweet the list differs
    # from its header on.
    whole, _, _ = ca.render_recent_tweets(
        snapshot, days=1, exclude_usernames=["alice"],
        exclude_tweet_ids={"t6"})
    assert first_list.endswith(whole.rstrip())
    assert "1 tweets the reader had already read are left out" in whole
    fresh, _, _ = ca.render_recent_tweets(
        snapshot, days=1, exclude_usernames=["alice"],
        exclude_tweet_ids={"t3", "t6"})
    assert fresh.split("\n")[0] != whole.split("\n")[0]
    assert "[#3] pacing is a skill" in fresh

    # Glean again under the gleaning, as the Glean button sends it.
    again = _placeholder(llm_user, alice, gleaning.id,
                         meta=[{"name": READ_FURTHER_MARKER}])
    second = _glean(monkeypatch, alice, gleaning, again, [
        {"n": 3, "qt": "Still about your mornings.", "relevance": 50,
         "recommend": True},
        {"n": 5, "qt": "On pacing.", "relevance": 30, "recommend": False}])

    # The first request is a prefix of the second, byte for byte: every
    # message, block by block, and the parts sent with them.
    n = len(first["messages"])
    assert second["messages"][:n] == first["messages"]
    assert (json.dumps(second["messages"][:n], ensure_ascii=False)
            == json.dumps(first["messages"], ensure_ascii=False))
    assert second["prompt_cache_key"] == first["prompt_cache_key"]
    assert second["output_schema"] == first["output_schema"]
    assert second["tools"] == first["tools"] is None
    # After the prefix: the gleaning, then one closing user turn with the
    # marks, the tweets read since (by their number in the list) and the
    # request, in that order.
    rest = second["messages"][n:]
    assert [m["role"] for m in rest] == ["assistant", "user"]
    closing = rest[-1]["content"][-1]["text"]
    marks = closing.index('@bob\'s tweet "slow mornings make the work '
                          'better" — read ')
    since = closing.index(read_since_note([3]))
    assert marks < since < closing.index(CA_READ_AGAIN_TURN_KEPT)
    # The request no longer says the read tweets are out of the list.
    assert READ_OUT_OF_LIST not in closing
    assert CA_READ_AGAIN_TURN not in closing

    # The pinned render is the first one's list; the read tweet's pick is
    # dropped and counted, the other one is saved.
    again = _fresh(again.id)
    assert again.llm_task_status == "completed"
    rows = {r.node_id: r for r in FeedRender.query.all()}
    assert rows[again.id].tweet_ids == rows[gleaning.id].tweet_ids \
        == "t1,t2,t3,t4,t5"
    assert rows[again.id].excluded_count == rows[gleaning.id].excluded_count == 1
    assert rows[again.id].dropped_picks == 1
    picks = FeedPick.query.filter_by(node_id=again.id).all()
    assert [(p.rank, p.item.external_id) for p in picks] == [(1, "t5")]
    assert "{quote_ext:%d}" % pick.id not in again.get_content()


def test_a_third_glean_keeps_the_list_and_names_every_tweet_read_since(app, monkeypatch, snapshot):  # noqa: F811
    from backend.routes.external import apply_item_feedback
    alice, llm_user, read, gleaning, pick, first = _gleaned_and_rated(
        monkeypatch, snapshot)
    again = _placeholder(llm_user, alice, gleaning.id,
                         meta=[{"name": READ_FURTHER_MARKER}])
    second = _glean(monkeypatch, alice, gleaning, again, [
        {"n": 5, "qt": "On pacing.", "relevance": 30, "recommend": False}])
    again = _fresh(again.id)
    pick5 = ExternalItem.query.filter_by(user_id=alice.id,
                                         external_id="t5").one()
    apply_item_feedback(pick5, "bad", node_id=again.id)
    _db.session.commit()

    third_node = _placeholder(llm_user, alice, again.id,
                              meta=[{"name": READ_FURTHER_MARKER}])
    third = _glean(monkeypatch, alice, again, third_node, [])
    # Everything before the second request's closing note is the third's
    # prefix too.
    n = len(second["messages"]) - 1
    assert second["messages"][n]["role"] == "user"
    assert third["messages"][:n] == second["messages"][:n]
    assert _list_message(third) == _list_message(first)
    closing = third["messages"][-1]["content"][-1]["text"]
    assert closing.index(read_since_note([3, 5])) < closing.index(
        CA_READ_AGAIN_TURN_KEPT)
    assert READ_OUT_OF_LIST not in closing


def test_glean_again_with_nothing_read_since_sends_the_usual_request(app, monkeypatch, snapshot):  # noqa: F811
    """No rating in between: the list is kept as well, nothing is named
    and the read-further request is the usual one, word for word."""
    alice, llm_user, read, gleaning, pick, first = _gleaned_and_rated(
        monkeypatch, snapshot, rate=False)
    again = _placeholder(llm_user, alice, gleaning.id,
                         meta=[{"name": READ_FURTHER_MARKER}])
    second = _glean(monkeypatch, alice, gleaning, again, [])
    n = len(first["messages"])
    assert second["messages"][:n] == first["messages"]
    closing = second["messages"][-1]["content"][-1]["text"]
    assert closing.endswith(CA_READ_AGAIN_TURN)
    assert "not candidates" not in closing


def test_the_usual_read_further_request_is_unchanged():
    """Only a kept list's closing turn reads differently (#511)."""
    assert CA_READ_AGAIN_TURN == (
        "Read further: go through today's tweets again against everything "
        "above — your earlier picks, my marks on them, and whatever I have "
        "written since. Your earlier picks are quoted above by author and "
        "text; they carry no numbers, as the numbers belong to today's list "
        "alone. Tweets I have read are out of that list; an unread earlier "
        "pick is yours to repeat if it still stands and is in today's list, "
        "by its number there. Beyond that, find what else is worth my time. "
        "Answer in the same shape, a verdict and the picks.")
    assert READ_OUT_OF_LIST not in CA_READ_AGAIN_TURN_KEPT
    assert ("Tweets I had read before that list was made are out of it, and "
            "the ones named above are in it but are not candidates; an "
            "unread earlier pick") in CA_READ_AGAIN_TURN_KEPT


def test_glean_again_on_a_new_export_renders_the_day_afresh(app, monkeypatch, snapshot):  # noqa: F811
    """The archive refreshed between the two gleanings: a new list, minus
    every tweet the reader has read, and no note about read tweets."""
    alice, llm_user, read, gleaning, pick, first = _gleaned_and_rated(
        monkeypatch, snapshot)
    (snapshot / "export_id").write_text("2026-10-21T07-00-00Z")
    again = _placeholder(llm_user, alice, gleaning.id,
                         meta=[{"name": READ_FURTHER_MARKER}])
    second = _glean(monkeypatch, alice, gleaning, again, [])
    listed = "".join(b["text"] for b in _list_message(second)["content"])
    assert "slow mornings make the work better" not in listed
    assert "[#3] pacing is a skill" in listed
    assert "2 tweets the reader had already read are left out" in listed
    closing = second["messages"][-1]["content"][-1]["text"]
    assert "not candidates" not in closing
    assert CA_READ_AGAIN_TURN in closing
    assert CA_READ_AGAIN_TURN_KEPT not in closing
    row = FeedRender.query.filter_by(node_id=again.id).one()
    assert row.tweet_ids == "t1,t2,t4,t5"
    assert row.excluded_count == 2


def test_the_admins_batch_read_again_leaves_the_read_tweets_out(app, monkeypatch, snapshot):  # noqa: F811
    """A read again sent through the Batch API (the admin's experiments)
    renders as before: its collect comes long after any cache entry."""
    alice, llm_user, read, gleaning, pick, first = _gleaned_and_rated(
        monkeypatch, snapshot)

    class _Submitted(Exception):
        pass

    def _submit(*args, **kwargs):
        raise _Submitted()
    monkeypatch.setattr(_llm_task_mod, "_ca_batch_submit", _submit)
    again = _placeholder(llm_user, alice, gleaning.id, meta=[
        {"name": READ_FURTHER_MARKER}, {"name": "_read_batch"}])
    try:
        generate_llm_response(_FakeSelf(), gleaning.id, again.id, "gpt-5",
                              alice.id, source_mode=None)
    except _Submitted:
        pass
    row = FeedRender.query.filter_by(node_id=again.id).one()
    assert row.tweet_ids == "t1,t2,t4,t5"


def test_list_to_keep_takes_the_newest_matching_render_of_the_readers_own(app):  # noqa: F811
    alice = _mk_user("alice", approved=True, plan="alpha")
    bob = _mk_user("bob", approved=True, plan="alpha")
    llm_user = _mk_user("gpt-5", twitter_id="llm-gpt-5")
    read = _prompt_node(alice, "read_thread")
    _db.session.commit()

    def _reply(parent, owner, export_id, ids, days=1, scope="all"):
        n = _placeholder(llm_user, owner, parent.id)
        n.llm_task_status = "completed"
        row = FeedRender(node_id=n.id, export_id=export_id, days=days,
                         scope=scope)
        row.set_tweet_ids(ids)
        _db.session.add(row)
        _db.session.commit()
        return n

    r1 = _reply(read, alice, "exp-1", ["a", "b"])
    r2 = _reply(r1, alice, "exp-1", ["a", "b", "c"])
    r3 = _reply(r2, alice, "exp-1", ["x"], days=2)       # another window
    r4 = _reply(r3, alice, "exp-1", ["y"], scope="follows")
    r5 = _reply(r4, bob, "exp-1", ["z"])                 # not the reader's
    chain = [read, r1, r2, r3, r4, r5]
    ids = read_reply_ids(chain)

    def keep(export_id, days=1, scope="all", chain=chain):
        return list_to_keep(chain, read, ids, alice.id, export_id, days,
                            scope)
    assert keep("exp-1") == {"a", "b", "c"}
    assert keep("exp-1", days=2) == {"x"}
    assert keep("exp-1", scope="follows") == {"y"}
    assert keep("exp-2") == frozenset()
    assert keep(None) == frozenset()
    # A deleted reply's render is not kept; nor one above the prompt.
    r2.deleted_at = datetime(2026, 10, 20, 9, 0)
    _db.session.commit()
    assert keep("exp-1") == {"a", "b"}
    assert keep("exp-1", chain=[r1, read]) == frozenset()


def test_read_numbers_and_the_note():
    refs = {1: {"tweet_id": "t1"}, 2: {"tweet_id": "t2"},
            3: {"tweet_id": "t3"}}
    assert read_numbers(refs, {"t3", "t1", "t9"}) == [1, 3]
    assert read_numbers(refs, set()) == []
    assert read_since_note([]) is None
    assert read_since_note([1, 3]) == (
        "[The user has read these tweets of today's list since it was "
        "made: #1, #3. Like every tweet the user has read, they are not "
        "candidates: do not pick them.]")

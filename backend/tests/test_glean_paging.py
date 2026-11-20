"""Glean reads the newest tweets that fit, and pages through the day
(#717; Peter, 2026-11-20).

A day of the Community Archive grew past what one prompt may hold, and
since #648 a prompt over THREAD_PROMPT_TOKEN_LIMIT is refused, so Gleans
were refused. Now a Glean keeps the newest of its window's tweets that
fit the prompt, and the next Glean in the same thread reads the next
older ones; when the window is used up, the Glean says so with no model
call. Another thread, or a new window, starts again at the newest.

The tests render a real (small) archive snapshot with the code a Glean
uses. Its tweets are long, so that two of them fill a page under the
test's limit; tokens are counted at one per four characters (the
counter that stands in for tiktoken). The model is scripted: no real
model call.
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
from backend.llm_providers import message_text
from backend.models import APICostLog, ExternalItem, FeedRender, Node
from backend.utils.ca_feed import (
    CA_READ_AGAIN_TURN, CA_READ_OLDER_TURN, GLEAN_BUDGET_MARGIN,
    GLEAN_DAY_DONE_CODE, GLEAN_DAY_DONE_MESSAGE, READ_FURTHER_MARKER,
    glean_prompt_budget, gleaned_tweet_ids, read_reply_ids,
    read_window_fields,
)
from backend.utils.thread_size import REFUSED_MARKER

# Each tweet is this long: about 2,000 tokens at four characters a token.
TWEET_CHARS = 8_000
# The test's thread limit: the budget (less GLEAN_BUDGET_MARGIN) holds the
# rest of the prompt (about 1,500 tokens) and two tweets, not three.
LIMIT = 6_400


def _tweet(word):
    return (word + " ") * (TWEET_CHARS // (len(word) + 1))


# (tweet id, account, posted, text) — t1 oldest .. t6 newest; the reader's
# own tweet and a retweet are newer still and never listed.
TWEETS = [
    ("t1", "a1", "2026-10-20 03:00:00", _tweet("one")),
    ("t2", "a2", "2026-10-20 04:00:00", _tweet("two")),
    ("t3", "a3", "2026-10-20 05:00:00", _tweet("three")),
    ("t4", "a1", "2026-10-20 06:00:00", _tweet("four")),
    ("t5", "a2", "2026-10-20 07:00:00", _tweet("five")),
    ("t6", "a3", "2026-10-20 08:00:00", _tweet("six")),
    ("t7", "a0", "2026-10-20 08:30:00", "my own tweet"),
    ("t8", "a1", "2026-10-20 08:45:00", "RT @bob: a retweet"),
]


@pytest.fixture
def snapshot(tmp_path):
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    tweets = tmp_path / "tweets.parquet"
    profiles = tmp_path / "profiles.parquet"
    rows = ", ".join(
        f"('{i}', '{a}', timestamptz '{t}+00', '{text}')"
        for i, a, t, text in TWEETS)
    con.execute(
        f"copy (select * from (values {rows}) "
        "v(tweet_id, account_id, created_at, full_text)) "
        f"to '{tweets}' (format parquet)")
    con.execute(
        "copy (select * from (values ('a0', 'alice'), ('a1', 'ann'), "
        "('a2', 'bob'), ('a3', 'cat')) v(account_id, username)) "
        f"to '{profiles}' (format parquet)")
    (tmp_path / "export_id").write_text("2026-10-20T09-00-00Z")
    return tmp_path


@pytest.fixture
def paged(app, monkeypatch, snapshot):  # noqa: F811
    """The thread limit set low, the real render on *snapshot*, and the
    counter at four characters a token."""
    from backend.utils import tokens
    saved = app.config.get("THREAD_PROMPT_TOKEN_LIMIT")
    app.config["THREAD_PROMPT_TOKEN_LIMIT"] = LIMIT
    monkeypatch.setattr(tokens, "local_token_counter",
                        lambda model_id: _count)
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
    yield app
    app.config["THREAD_PROMPT_TOKEN_LIMIT"] = saved


def _count(text):
    return len(text or "") // 4


class _RecordingProvider:
    """Records each request as the provider gets it and answers with the
    queued replies."""
    responses = []
    requests = []

    @classmethod
    def reset(cls, responses):
        cls.responses = list(responses)
        cls.requests = []

    @classmethod
    def get_completion(cls, model_id, messages, api_keys, tools=None,
                       prompt_cache_key=None, **kwargs):
        cls.requests.append({"messages": copy.deepcopy(messages),
                             "prompt_cache_key": prompt_cache_key})
        return cls.responses.pop(0)


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


def _glean(monkeypatch, alice, parent, llm_node, picks=()):
    """One glean as the task runs it (live). Returns (result, the request
    the provider got, or None when none went out)."""
    _RecordingProvider.reset([_resp(json.dumps(
        {"verdict": "One thing today.", "picks": list(picks)}))])
    monkeypatch.setattr(_llm_task_mod, "LLMProvider", _RecordingProvider)
    result = generate_llm_response(_FakeSelf(), parent.id, llm_node.id,
                                   "gpt-5", alice.id, source_mode=None)
    sent = _RecordingProvider.requests
    assert len(sent) <= 1
    return result, (sent[0] if sent else None)


def _users():
    alice = _mk_user("alice", approved=True, plan="alpha", is_admin=True)
    llm_user = _mk_user("gpt-5", twitter_id="llm-gpt-5")
    return alice, llm_user


def _thread(alice, text="how do I pace the mornings?"):
    note = _user_node(alice, None, text)
    read = _prompt_node(alice, "read_thread", parent_id=note.id)
    _db.session.commit()
    return read


def _listed(request):
    """The tweet ids the request's list holds, by their words."""
    text = "".join(message_text(m) for m in request["messages"])
    words = {"one": "t1", "two": "t2", "three": "t3", "four": "t4",
             "five": "t5", "six": "t6"}
    return sorted(i for w, i in words.items() if f"] {w} {w} " in text)


def _tokens(request):
    return sum(_count(message_text(m)) + 4 for m in request["messages"])


def _page(read_id):
    row = FeedRender.query.filter_by(node_id=read_id).one()
    return sorted(row.tweet_ids.split(",")), row


def _again(monkeypatch, alice, llm_user, under, meta=None):
    node = _placeholder(llm_user, alice, under.id,
                        meta=meta or [{"name": READ_FURTHER_MARKER}])
    result, request = _glean(monkeypatch, alice, under, node)
    return _fresh(node.id), result, request


# ── How to check (#717) ──────────────────────────────────────────────────

def test_a_day_over_the_budget_reads_the_newest_tweets_that_fit(paged, monkeypatch):
    """Not refused: the newest tweets that fit, the prompt under the
    budget, the reader's own tweet and the retweet never listed."""
    alice, llm_user = _users()
    read = _thread(alice)
    gleaning = _placeholder(llm_user, alice, read.id, meta=[{"name": "_live"}])

    result, request = _glean(monkeypatch, alice, read, gleaning)

    assert result.get("status") != "refused"
    assert _fresh(gleaning.id).llm_task_status == "completed"
    assert _listed(request) == ["t5", "t6"]
    assert _tokens(request) <= LIMIT * (1 - GLEAN_BUDGET_MARGIN)
    text = "".join(message_text(m) for m in request["messages"])
    assert "my own tweet" not in text and "RT @bob" not in text
    # The header says what part of the window this is.
    assert ("6 tweets by 3 accounts, retweets omitted. This list holds the "
            "newest 2 of them, by 2 accounts, posted from 2026-10-20 07:00 "
            "to 2026-10-20 08:00 UTC; the older ones are left for a later "
            "read.") in text
    ids, row = _page(gleaning.id)
    assert ids == ["t5", "t6"]
    assert row.page_start == datetime(2026, 10, 20, 7, 0)
    assert row.page_end == datetime(2026, 10, 20, 8, 0)
    assert row.window_end == datetime(2026, 10, 20, 8, 45)
    assert APICostLog.query.filter_by(request_type="thread_limit").count() == 0


def test_the_next_glean_reads_the_next_older_tweets_then_says_the_day_is_done(paged, monkeypatch):
    alice, llm_user = _users()
    read = _thread(alice)
    first = _placeholder(llm_user, alice, read.id, meta=[{"name": "_live"}])
    _, request1 = _glean(monkeypatch, alice, read, first)

    second, _, request2 = _again(monkeypatch, alice, llm_user, _fresh(first.id))
    third, _, request3 = _again(monkeypatch, alice, llm_user, second)

    assert _listed(request1) == ["t5", "t6"]
    assert _listed(request2) == ["t3", "t4"]
    assert _listed(request3) == ["t1", "t2"]
    assert _page(second.id)[0] == ["t3", "t4"]
    # A read again in the same window asks for the older tweets.
    closing = message_text(request2["messages"][-1])
    assert closing.endswith(CA_READ_OLDER_TURN)
    assert CA_READ_AGAIN_TURN not in closing
    listed = "".join(message_text(m) for m in request2["messages"])
    assert ("2 tweets of this window were in the earlier reads of this "
            "conversation and are left out.") in listed
    for request in (request2, request3):
        assert _tokens(request) <= LIMIT * (1 - GLEAN_BUDGET_MARGIN)

    # The window is used up: said plainly, no model call, nothing billed.
    fourth, result, request4 = _again(monkeypatch, alice, llm_user, third)
    assert request4 is None
    assert result["status"] == "refused"
    assert result["reason"] == GLEAN_DAY_DONE_CODE
    assert fourth.llm_task_status == "failed"
    assert fourth.llm_task_error == GLEAN_DAY_DONE_MESSAGE
    meta = json.loads(fourth.tool_calls_meta)
    assert {"name": REFUSED_MARKER, "code": GLEAN_DAY_DONE_CODE} in meta
    assert FeedRender.query.filter_by(node_id=fourth.id).count() == 0
    assert APICostLog.query.filter_by(
        request_ref=f"node:{fourth.id}").count() == 0


def test_a_day_that_fit_whole_is_done_after_one_glean(paged, monkeypatch):
    """The second Glean on a day the first one read whole says so, with
    the code llm-status gives the owner (REFUSED_MARKER, as for the
    thread's length) and the sentence the Voice screens say."""
    from backend.utils.tool_meta import get_tool_meta_entry
    from backend.utils.voice_refusals import SPOKEN_REFUSALS
    alice, llm_user = _users()
    paged.config["THREAD_PROMPT_TOKEN_LIMIT"] = 250_000
    read = _thread(alice)
    first = _placeholder(llm_user, alice, read.id, meta=[{"name": "_live"}])
    _glean(monkeypatch, alice, read, first)

    done, result, request = _again(monkeypatch, alice, llm_user,
                                   _fresh(first.id))

    assert request is None and result["reason"] == GLEAN_DAY_DONE_CODE
    assert get_tool_meta_entry(done, REFUSED_MARKER)["code"] \
        == GLEAN_DAY_DONE_CODE
    assert SPOKEN_REFUSALS[GLEAN_DAY_DONE_CODE] == GLEAN_DAY_DONE_MESSAGE


def test_a_glean_in_another_thread_starts_again_at_the_newest(paged, monkeypatch):
    alice, llm_user = _users()
    read = _thread(alice)
    first = _placeholder(llm_user, alice, read.id, meta=[{"name": "_live"}])
    _glean(monkeypatch, alice, read, first)
    _again(monkeypatch, alice, llm_user, _fresh(first.id))

    other = _thread(alice, "what should I read about pacing?")
    elsewhere = _placeholder(llm_user, alice, other.id,
                             meta=[{"name": "_live"}])
    _, request = _glean(monkeypatch, alice, other, elsewhere)

    assert _listed(request) == ["t5", "t6"]
    assert "earlier reads of this conversation" not in "".join(
        message_text(m) for m in request["messages"])


def test_a_new_window_starts_again_at_its_newest(paged, monkeypatch, snapshot):
    alice, llm_user = _users()
    read = _thread(alice)
    first = _placeholder(llm_user, alice, read.id, meta=[{"name": "_live"}])
    _glean(monkeypatch, alice, read, first)
    (snapshot / "export_id").write_text("2026-10-21T09-00-00Z")

    _, _, request = _again(monkeypatch, alice, llm_user, _fresh(first.id))

    assert _listed(request) == ["t5", "t6"]
    assert message_text(request["messages"][-1]).endswith(CA_READ_AGAIN_TURN)


def test_a_small_day_that_fits_whole_is_sent_as_before(paged, monkeypatch, snapshot):
    """Control: under a budget the whole day fits, the list is the
    render, byte for byte, and its record names no part."""
    from backend.utils import community_archive as ca
    paged.config["THREAD_PROMPT_TOKEN_LIMIT"] = 250_000
    alice, llm_user = _users()
    read = _thread(alice)
    gleaning = _placeholder(llm_user, alice, read.id, meta=[{"name": "_live"}])

    _, request = _glean(monkeypatch, alice, read, gleaning)

    whole, stats, _ = ca.render_recent_tweets(
        snapshot, days=1, exclude_usernames=["alice"])
    text = "".join(message_text(m) for m in request["messages"])
    assert whole.rstrip() in text
    assert "This list holds" not in text
    assert _listed(request) == ["t1", "t2", "t3", "t4", "t5", "t6"]
    ids, row = _page(gleaning.id)
    assert len(ids) == 6 and row.page_start is None and row.page_end is None
    fields = read_window_fields(row)
    assert fields["window_start"] == "2026-10-19T08:45:00Z"
    assert fields["window_end"] == fields["newest_at"] == "2026-10-20T08:45:00Z"


def test_the_filters_apply_before_the_list_is_counted(paged, monkeypatch):
    """A tweet the reader has read never takes a place in the list: the
    newest two that remain are listed."""
    alice, llm_user = _users()
    seen = ExternalItem(user_id=alice.id, source="twitter_bookmark",
                        external_id="t6", read_at=datetime(2026, 10, 20, 9))
    seen.set_content("six")
    _db.session.add(seen)
    read = _thread(alice)
    gleaning = _placeholder(llm_user, alice, read.id, meta=[{"name": "_live"}])

    _, request = _glean(monkeypatch, alice, read, gleaning)

    assert _listed(request) == ["t4", "t5"]
    text = "".join(message_text(m) for m in request["messages"])
    assert "1 tweets the reader had already read are left out." in text


def test_the_part_before_the_list_is_the_same_on_the_next_glean(paged, monkeypatch):
    """Prompt cache (#511 then, #717 now): the thread, the instructions,
    the profile and the intentions come before the list, so the next
    Glean's request starts with the same bytes up to the list's count."""
    alice, llm_user = _users()
    read = _thread(alice)
    first = _placeholder(llm_user, alice, read.id, meta=[{"name": "_live"}])
    _, request1 = _glean(monkeypatch, alice, read, first)
    _, _, request2 = _again(monkeypatch, alice, llm_user, _fresh(first.id))

    def _list_index(request):
        return next(i for i, m in enumerate(request["messages"])
                    if "# Community Archive — tweets from" in message_text(m))
    i = _list_index(request1)
    assert _list_index(request2) == i
    assert request2["messages"][:i] == request1["messages"][:i]
    one, two = (message_text(r["messages"][i]) for r in (request1, request2))
    head = one[:one.index("): ") + 3]
    assert head.endswith("(last 1 day(s) of export 2026-10-20T09-00-00Z): ")
    assert two.startswith(head)
    assert request1["prompt_cache_key"] == request2["prompt_cache_key"]


def test_the_admins_batch_read_again_reads_on_too(paged, monkeypatch):
    """The batch experiments (READ_BATCH_MARKER) follow the same rule."""
    alice, llm_user = _users()
    read = _thread(alice)
    first = _placeholder(llm_user, alice, read.id, meta=[{"name": "_live"}])
    _glean(monkeypatch, alice, read, first)

    class _Submitted(Exception):
        pass

    def _submit(*args, **kwargs):
        raise _Submitted()
    monkeypatch.setattr(_llm_task_mod, "_ca_batch_submit", _submit)
    again = _placeholder(llm_user, alice, first.id, meta=[
        {"name": READ_FURTHER_MARKER}, {"name": "_read_batch"}])
    with pytest.raises(_Submitted):
        generate_llm_response(_FakeSelf(), first.id, again.id, "gpt-5",
                              alice.id, source_mode=None)

    assert _page(again.id)[0] == ["t3", "t4"]


def test_without_a_local_count_the_list_is_fitted_by_the_estimate(paged, monkeypatch):
    """A model with no local count (Anthropic's, or an o200k model whose
    encoding did not load in this process): the rest of the prompt is
    estimated as the thread limit estimates it, and the tweets at the
    tweets prior, its upper end. The list holds fewer tweets than a count
    would let in, still the newest, and the estimate is the size the
    limit's check uses, so the Glean is not refused."""
    from backend.utils import tokens
    from backend.utils.thread_size import THREAD_TOO_LONG_CODE
    monkeypatch.setattr(tokens, "local_token_counter", lambda model_id: None)
    paged.config["THREAD_PROMPT_TOKEN_LIMIT"] = 20_000
    alice, llm_user = _users()
    read = _thread(alice)
    gleaning = _placeholder(llm_user, alice, read.id, meta=[{"name": "_live"}])

    result, request = _glean(monkeypatch, alice, read, gleaning)

    assert request is not None
    assert result.get("reason") != THREAD_TOO_LONG_CODE
    listed = _listed(request)
    newest = ["t1", "t2", "t3", "t4", "t5", "t6"][-len(listed):]
    assert 1 <= len(listed) < 6 and listed == newest
    # At the tweets prior (0.6 tokens a character on o200k) a tweet of
    # 8,000 characters costs 4,800 tokens: three fit 19,600, not four.
    assert len(listed) == 3
    assert _page(gleaning.id)[0] == newest


def test_a_thread_with_no_room_for_one_tweet_is_refused_as_too_long(paged, monkeypatch):
    """The rest of the prompt alone fills the budget: the thread itself
    is too long, and the refusal says so, as before."""
    paged.config["THREAD_PROMPT_TOKEN_LIMIT"] = 1_000
    alice, llm_user = _users()
    read = _thread(alice)
    gleaning = _placeholder(llm_user, alice, read.id, meta=[{"name": "_live"}])

    result, request = _glean(monkeypatch, alice, read, gleaning)

    assert request is None
    assert result["reason"] == "thread_too_long"
    assert FeedRender.query.filter_by(node_id=gleaning.id).count() == 0


# ── The parts ────────────────────────────────────────────────────────────

def test_newest_that_fit_keeps_a_render_that_fits_and_stops_at_the_first_that_does_not():
    from backend.utils import community_archive as ca
    entries = [
        {"username": "ann", "tweet_id": "11", "text": "a" * 400,
         "posted_at": datetime(2026, 10, 20, 1)},
        {"username": "ann", "tweet_id": "14", "text": "b" * 40,
         "posted_at": datetime(2026, 10, 20, 4)},
        {"username": "bob", "tweet_id": "12", "text": "c" * 4000,
         "posted_at": datetime(2026, 10, 20, 2)},
        {"username": "bob", "tweet_id": "13", "text": "d" * 40,
         "posted_at": datetime(2026, 10, 20, 3)},
    ]
    meta = {"export_id": "e", "days": 1, "window_start": "2026-10-19 05:00",
            "window_end": "2026-10-20 05:00", "excluded": 0, "skipped": 0,
            "scope": "all"}
    text, stats, refs = ca._format_render(entries, meta)
    assert ca.newest_that_fit(text, stats, refs, 10_000, _count) == (
        text, stats, refs)
    # Room for the two newest (14, 13) and the next one, the long 12, does
    # not fit: the list stops there, though 11 alone would.
    room = _count(text) - 1000
    part, part_stats, part_refs = ca.newest_that_fit(
        text, stats, refs, room, _count)
    assert [r["tweet_id"] for r in part_refs.values()] == ["14", "13"]
    assert sorted(part_refs) == [1, 2]
    assert _count(part) <= room
    assert part_stats["tweets"] == 2 and part_stats["window_tweets"] == 4
    assert part_stats["page_start_at"] == datetime(2026, 10, 20, 3)
    assert part_stats["page_end_at"] == datetime(2026, 10, 20, 4)
    assert "This list holds the newest 2 of them" in part
    assert ca.newest_that_fit(text, stats, refs, 10, _count) is None
    assert ca.newest_that_fit(text, stats, refs, 0, _count) is None


def test_the_thread_s_earlier_reads_are_its_own_completed_ones_of_the_window(app):  # noqa: F811
    alice = _mk_user("alice", approved=True, plan="alpha")
    bob = _mk_user("bob", approved=True, plan="alpha")
    llm_user = _mk_user("gpt-5", twitter_id="llm-gpt-5")
    read = _prompt_node(alice, "read_thread")
    _db.session.commit()

    def _reply(parent, owner, export_id, ids, days=1, scope="all",
               status="completed"):
        n = _placeholder(llm_user, owner, parent.id)
        n.llm_task_status = status
        row = FeedRender(node_id=n.id, export_id=export_id, days=days,
                         scope=scope)
        row.set_tweet_ids(ids)
        _db.session.add(row)
        _db.session.commit()
        return n

    r1 = _reply(read, alice, "exp-1", ["a", "b"])
    r2 = _reply(r1, alice, "exp-1", ["c"])
    r3 = _reply(r2, alice, "exp-1", ["x"], days=2)
    r4 = _reply(r3, alice, "exp-1", ["y"], scope="follows")
    r5 = _reply(r4, bob, "exp-1", ["z"])
    r6 = _reply(r5, alice, "exp-1", ["f"], status="failed")
    chain = [read, r1, r2, r3, r4, r5, r6]
    ids = read_reply_ids(chain)

    def gleaned(export_id, days=1, scope="all"):
        return gleaned_tweet_ids(chain, ids, alice.id, export_id, days, scope)
    assert gleaned("exp-1") == {"a", "b", "c"}
    assert gleaned("exp-1", days=2) == {"x"}
    assert gleaned("exp-1", scope="follows") == {"y"}
    assert gleaned("exp-2") == frozenset()
    assert gleaned(None) == frozenset()
    r2.deleted_at = datetime(2026, 10, 20, 9, 0)
    _db.session.commit()
    assert gleaned("exp-1") == {"a", "b"}


def test_the_budget_is_the_thread_limit_less_the_margin(app):  # noqa: F811
    """250k less 2 %: 245,000 tokens on GPT-5.6 Luna and GPT-6 Luna, under
    their 272k price tier; a model whose window is smaller sets it."""
    from backend.config import Config
    from backend.llm_providers import model_input_cap
    models = app.config["SUPPORTED_MODELS"]
    saved = app.config.get("THREAD_PROMPT_TOKEN_LIMIT")
    app.config["THREAD_PROMPT_TOKEN_LIMIT"] = 250_000
    added = [m for m in ("gpt-5.6-luna", "gpt-6-luna") if m not in models]
    for model in added:
        models[model] = Config.SUPPORTED_MODELS[model]
    models["small-x"] = {"provider": "openai", "context_window": 100_000}
    added.append("small-x")
    try:
        assert glean_prompt_budget("gpt-5.6-luna", app.config) == 245_000
        assert glean_prompt_budget("gpt-6-luna", app.config) == 245_000
        assert glean_prompt_budget("small-x", app.config) == int(
            model_input_cap(models["small-x"], price_tier=False)
            * (1 - GLEAN_BUDGET_MARGIN))
        app.config["THREAD_PROMPT_TOKEN_LIMIT"] = 0
        assert glean_prompt_budget("gpt-5.6-luna", app.config) == int(
            922_000 * (1 - GLEAN_BUDGET_MARGIN))
    finally:
        app.config["THREAD_PROMPT_TOKEN_LIMIT"] = saved
        for model in added:
            models.pop(model, None)

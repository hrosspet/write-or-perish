"""The completion task measures the prompt it assembled before its first
call (#648; Peter, 2026-11-12).

- Under the notice size, nothing about the call changes: the same bytes
  go out with the limit on as with it off (the prompt cache, #187).
- At or over THREAD_PROMPT_TOKEN_LIMIT the reply fails with the limit's
  message before any generation call; the measurement is recorded as a
  $0 "thread_limit" row under the node it was asked under.
- An estimate that is not certain (no measured reply, another tokenizer,
  a Read turn's tweets) is counted before it refuses or cuts anything,
  so a prompt under the limit always goes out as it would without it.
- The base's part is measured again in characters: an edit there, or a
  quoted entry deleted, counts both ways.
- Text a quote pulls in counts (a thread holding one large document),
  and then the refusal says the entry is too large, not the thread long.
- A reply that started under the limit finishes its tool rounds.
- Every conversation call records its prompt's characters.

Runs the real task body through the test_retrieval_loop harness
(identity @celery.task, scripted provider, test flask_app); nothing
reaches a real model. A turn the tests measure is run once with the
limit off and its cost row given the token count a provider would have
reported for its characters ("real" ratios below), so the row's
characters match the prompt the next turn re-measures.
"""
import json
from datetime import datetime

import pytest

from backend.tests.test_retrieval_loop import (  # noqa: F401 (fixture)
    app, _llm_task_mod, generate_llm_response, _FakeSelf,
    _ScriptedProvider, _build_chain, _mk_artifact, _mk_user, _resp, _fresh,
)
from backend.extensions import db as _db
from backend.models import APICostLog, Node, User
from backend.utils.thread_size import (
    CONTENT_TOO_LARGE_MESSAGE, REFUSED_MARKER, THREAD_LIMIT_REQUEST_TYPE,
    THREAD_TOO_LONG_MESSAGE, prompt_chars, thread_length,
)

LIMIT = 10_000
# "Real" tokens per character of the test tokenizers: what a provider
# would count. o200k reads 1 token per 4 characters here, the Claude
# tokenizer 2 per 4.
REAL = {"gpt-5": 0.25, "claude-x": 0.5}


@pytest.fixture
def limited(app):  # noqa: F811
    app.config["THREAD_PROMPT_TOKEN_LIMIT"] = LIMIT
    app.config["THREAD_PROMPT_NOTICE_FRACTION"] = 0.9
    app.config["SUPPORTED_MODELS"]["claude-x"] = {
        "provider": "anthropic", "api_model": "claude-x",
        "tokenizer_family": "claude_new"}
    yield app
    app.config.pop("THREAD_PROMPT_TOKEN_LIMIT", None)
    app.config.pop("THREAD_PROMPT_NOTICE_FRACTION", None)
    app.config["SUPPORTED_MODELS"].pop("claude-x", None)


@pytest.fixture
def counting(monkeypatch):
    """The provider's free count: the prompt's characters at the "real"
    ratio of the model. Records each count."""
    counts = []

    def count_tokens(model_id, messages, api_keys):
        counts.append(model_id)
        return int(prompt_chars(messages) * REAL[model_id])
    monkeypatch.setattr(_ScriptedProvider, "count_tokens",
                        staticmethod(count_tokens), raising=False)
    return counts


@pytest.fixture
def fixed_clock(monkeypatch):
    """Every time stamp the prompt carries, fixed: two runs of one turn
    then send the same bytes."""
    monkeypatch.setattr(_llm_task_mod, "local_stamp",
                        lambda *_a, **_k: "[2026-11-12 10:00]")


def _llm_user(model):
    u = User.query.filter_by(username=model).first()
    return u or _mk_user(model, twitter_id=f"llm-{model}")


def _placeholder(parent, owner, model="gpt-5"):
    n = Node(user_id=_llm_user(model).id, human_owner_id=owner.id,
             parent_id=parent.id, node_type="llm", llm_model=model,
             llm_task_status="pending", privacy_level="private",
             ai_usage="chat")
    n.set_content("[LLM response generation pending...]")
    _db.session.add(n)
    _db.session.commit()
    return n


def _entry(owner, parent, text):
    n = Node(user_id=owner.id, human_owner_id=owner.id,
             parent_id=parent.id if parent else None, node_type="user",
             privacy_level="private", ai_usage="chat",
             token_count=len(text) // 4)
    n.set_content(text)
    _db.session.add(n)
    _db.session.commit()
    return n


def _run(parent, pending, owner, model="gpt-5"):
    return generate_llm_response(
        _FakeSelf(), parent.id, pending.id, model, owner.id,
        source_mode="textmode")


def _measured_turn(app, owner, parent, model="gpt-5", answer="An answer."):  # noqa: F811
    """A turn run with the limit off whose cost row says what a provider
    would have counted for the prompt's characters. Returns the reply."""
    pending = _placeholder(parent, owner, model)
    saved = app.config.get("THREAD_PROMPT_TOKEN_LIMIT")
    app.config["THREAD_PROMPT_TOKEN_LIMIT"] = 0
    _ScriptedProvider.reset([_resp(answer)])
    _run(parent, pending, owner, model)
    app.config["THREAD_PROMPT_TOKEN_LIMIT"] = saved
    _db.session.expire_all()
    row = APICostLog.query.filter_by(
        request_ref=f"node:{pending.id}").one()
    row.input_tokens = int(row.prompt_chars * REAL[model])
    reply = Node.query.get(pending.id)
    reply.token_count = len(answer) // 4
    _db.session.commit()
    return reply, row


def _long_thread(app, doc_chars, model="gpt-5", parts=1):  # noqa: F811
    """system -> *doc_chars* of writing in *parts* entries -> a measured
    reply -> the next entry (returned; a reply is asked under it)."""
    alice, system, user_node, pending = _build_chain("textmode")
    _db.session.delete(pending)
    part = doc_chars // parts
    user_node.set_content("x" * part)
    user_node.token_count = part // 4
    _db.session.commit()
    last = user_node
    for _ in range(parts - 1):
        last = _entry(alice, last, "x" * part)
    reply, row = _measured_turn(app, alice, last, model)
    entry = _entry(alice, reply, "And one more thing.")
    return alice, entry, row


def test_under_the_notice_size_the_bytes_sent_are_unchanged(  # noqa: F811
        app, fixed_clock):
    """The same turn, sent once with the limit off and once with the
    default limit on: identical messages and tools."""
    alice, entry, _row = _long_thread(app, 4_000)
    first = _placeholder(entry, alice)
    app.config["THREAD_PROMPT_TOKEN_LIMIT"] = 0
    _ScriptedProvider.reset([_resp("One.")])
    _run(entry, first, alice)
    off = _ScriptedProvider.calls[0]

    second = _placeholder(entry, alice)
    app.config["THREAD_PROMPT_TOKEN_LIMIT"] = 250_000
    _ScriptedProvider.reset([_resp("Two.")])
    _run(entry, second, alice)
    on = _ScriptedProvider.calls[0]
    app.config.pop("THREAD_PROMPT_TOKEN_LIMIT", None)

    assert json.dumps(on["raw"], sort_keys=True) == json.dumps(
        off["raw"], sort_keys=True)
    assert on["tools"] == off["tools"]


def test_the_measure_matches_the_next_turns_real_count(limited, counting):
    """With the thread's own measured ratio the estimate is certain and
    close: within 1 % of what the provider counts for the next turn."""
    alice, entry, _row = _long_thread(limited, 30_000)
    pending = _placeholder(entry, alice)
    _ScriptedProvider.reset([_resp("Fine.")])
    _run(entry, pending, alice)
    sent = _ScriptedProvider.calls[0]
    real = prompt_chars(sent["raw"], sent["tools"]) * REAL["gpt-5"]
    from backend.utils.thread_size import thread_size
    size, _state = thread_size(entry, alice.id, "gpt-5")
    assert size.certain
    assert abs(size.tokens - real) / real < 0.01
    assert counting == []  # certain: never counted


def test_between_the_notice_and_the_limit_the_reply_goes_out(limited):
    alice, entry, row = _long_thread(limited, 28_500)
    assert 0.9 * LIMIT <= row.input_tokens < LIMIT
    pending = _placeholder(entry, alice)
    _ScriptedProvider.reset([_resp("Still here.")])

    _run(entry, pending, alice)

    assert len(_ScriptedProvider.calls) == 1
    assert _fresh(pending.id).get_content() == "Still here."


def test_at_the_limit_the_reply_is_refused_before_any_call(limited):
    """A long thread over the limit: refused with the thread-length text,
    no generation call, a certain $0 row the page reads."""
    # The writing is spread over ten entries: the thread's length, not
    # one entry, takes it over.
    alice, entry, row = _long_thread(limited, 39_500, parts=10)
    pending = _placeholder(entry, alice)
    _ScriptedProvider.reset([_resp("must not be sent")])

    result = _run(entry, pending, alice)

    assert result["status"] == "refused"
    assert result["reason"] == "thread_too_long"
    assert _ScriptedProvider.calls == []
    node = _fresh(pending.id)
    assert node.llm_task_status == "failed"
    assert node.llm_task_error == THREAD_TOO_LONG_MESSAGE
    meta = json.loads(node.tool_calls_meta)
    assert {"name": REFUSED_MARKER, "code": "thread_too_long"} in meta
    refusal = APICostLog.query.filter_by(
        request_type=THREAD_LIMIT_REQUEST_TYPE).one()
    assert refusal.request_ref == f"node:{entry.id}"
    assert refusal.cost_microdollars == 0
    assert refusal.input_tokens >= LIMIT
    assert refusal.prompt_chars  # certain: a base for later estimates
    assert thread_length(entry, alice.id)[1] == "limit"


# ── Review item 1: a base measured on another tokenizer ──────────────────

def test_a_base_from_another_tokenizer_is_converted_not_reused(
        limited, counting):
    """Measured on gpt-5 at 7,250 tokens; the next reply runs on a Claude
    model whose tokenizer reads the same text as about twice the tokens.
    The base is no longer taken as it is: the reply is counted and
    refused, with no generation call."""
    alice, entry, row = _long_thread(limited, 28_000)
    assert row.input_tokens < LIMIT
    pending = _placeholder(entry, alice, "claude-x")
    _ScriptedProvider.reset([_resp("must not be sent")])

    result = _run(entry, pending, alice, "claude-x")

    assert result["reason"] == "thread_too_long"
    assert _ScriptedProvider.calls == []
    assert counting == ["claude-x"]


def test_a_base_from_another_tokenizer_does_not_refuse_under_the_limit(
        limited, counting):
    """Measured on the Claude model near the limit; the next reply on
    gpt-5 is about half that: it goes out."""
    alice, entry, row = _long_thread(limited, 19_500, model="claude-x")
    assert row.input_tokens >= 0.9 * LIMIT
    page = thread_length(entry, alice.id, "gpt-5")[1]
    assert page != "limit"
    pending = _placeholder(entry, alice, "gpt-5")
    _ScriptedProvider.reset([_resp("Here.")])

    _run(entry, pending, alice, "gpt-5")

    assert len(_ScriptedProvider.calls) == 1


# ── Review item 2: Glean again counts the day's tweets ──────────────────

def test_a_read_turn_measures_its_whole_prompt(limited):
    """The tweets count at the tweets prior (2.4 per 4 characters on
    o200k), the rest at the ratio; with tweets in it the size is not
    certain, so a count decides before any refusal."""
    from backend.utils.thread_size import assembled_prompt_size
    alice, entry, _row = _long_thread(limited, 4_000)
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "x" * 4_000 + "t" * 20_000}]}]
    whole = assembled_prompt_size(
        [entry], alice.id, "gpt-5", messages, {entry.id: 0}, 1,
        whole=True, dense_chars=20_000)
    assert whole.tokens >= 20_000 * 2.4 / 4
    assert not whole.certain


def test_glean_again_counts_the_days_tweets(limited, monkeypatch, tmp_path):
    """A Glean thread with one chat turn measured, then Glean again on a
    day with many tweets: the day sits in the read prompt (the base's
    part, as a stub then), so base plus what was written since missed it.
    The whole prompt is measured and the read is refused, no call."""
    from backend.tests.test_glean_again_new_day import (
        _gleaned_yesterday, _placeholder as _glean_placeholder, _corpus,
    )
    from backend.tests.test_read_context import _CapturingProvider
    from backend.utils import community_archive as ca
    from backend.utils.ca_feed import READ_FURTHER_MARKER
    limited.config["THREAD_PROMPT_TOKEN_LIMIT"] = 0
    state, alice, llm_user, read, gleaning, pick = _gleaned_yesterday(
        monkeypatch, tmp_path)
    limited.config["THREAD_PROMPT_TOKEN_LIMIT"] = LIMIT
    # A chat turn about the picks, measured.
    chat = _entry(alice, gleaning, "Tell me more about the first one.")
    chat_reply = _glean_placeholder(llm_user, alice, chat.id)
    chat_reply.set_content("It is about mornings.")
    chat_reply.llm_task_status = "completed"
    chat_reply.token_count = 5
    _db.session.add(APICostLog(
        user_id=alice.id, model_id="gpt-5", request_type="conversation",
        input_tokens=1_200, output_tokens=10, prompt_chars=4_800,
        cost_microdollars=1, request_ref=f"node:{chat_reply.id}"))
    _db.session.commit()
    # Today: a big day of tweets.
    big = {n: {"username": f"u{n}", "tweet_id": str(1000 + n),
               "text": "a fairly long tweet about something " * 4,
               "posted_at": datetime(2026, 10, 13, 8, 0)}
           for n in range(1, 400)}

    def _render(*args, **kwargs):
        stats = {"export_id": "exp-big", "tweets": len(big), "accounts": 1,
                 "excluded": 0, "scope": "all",
                 "window_start_at": datetime(2026, 10, 13, 7),
                 "window_end_at": datetime(2026, 10, 14, 7)}
        return _corpus(big, "exp-big"), stats, dict(big)
    monkeypatch.setattr(ca, "render_recent_tweets", _render)

    again = _glean_placeholder(llm_user, alice, chat_reply.id,
                               meta=[{"name": READ_FURTHER_MARKER}])
    _CapturingProvider.kwargs = []
    _CapturingProvider.reset([_resp("must not be sent")])
    monkeypatch.setattr(_llm_task_mod, "LLMProvider", _CapturingProvider)
    result = generate_llm_response(
        _FakeSelf(), chat_reply.id, again.id, "gpt-5", alice.id,
        source_mode=None, ca_live=True)

    assert result["reason"] == "thread_too_long"
    assert _CapturingProvider.calls == []


# ── Review item 3: estimates never refuse or cut under the limit ────────

def test_a_first_reply_over_the_estimate_but_under_the_count_goes_out(
        limited, counting):
    """A new account (no measured call): the prior says over the limit,
    the count says under. The reply goes out, no refusal recorded."""
    alice, system, user_node, pending = _build_chain("textmode")
    user_node.set_content("x" * 28_000)
    user_node.token_count = 28_000 // 4
    _db.session.commit()
    _ScriptedProvider.reset([_resp("Read it.")])

    _run(user_node, pending, alice)

    assert counting == ["gpt-5"]
    assert len(_ScriptedProvider.calls) == 1
    assert APICostLog.query.filter_by(
        request_type=THREAD_LIMIT_REQUEST_TYPE).count() == 0


def test_a_refusal_the_task_could_not_count_is_never_a_base(limited):
    """No count available (the scripted provider has none): refused on
    the estimate, but the row carries no characters, so the next request
    is measured again instead of taking the guess as a base."""
    alice, system, user_node, pending = _build_chain("textmode")
    user_node.set_content("x" * 60_000)
    user_node.token_count = 60_000 // 4
    _db.session.commit()
    _ScriptedProvider.reset([_resp("must not be sent")])

    result = _run(user_node, pending, alice)

    assert result["reason"] == "thread_too_long"
    row = APICostLog.query.filter_by(
        request_type=THREAD_LIMIT_REQUEST_TYPE).one()
    assert row.prompt_chars is None
    from backend.utils.thread_size import chain_for, latest_measurement
    chain = chain_for(user_node)
    assert latest_measurement(chain.nodes, alice.id, chain.reads) is None
    assert thread_length(user_node, alice.id)[1] != "limit"


def test_an_uncapped_archive_that_fits_is_not_cut(limited, counting,
                                                  monkeypatch, fixed_clock):
    """The prior says the archive's prompt is over the limit; the count
    says it fits. It goes out whole: the bytes an unlimited run sends."""
    alice, system, user_node, pending = _build_chain("textmode")
    alice.plan = "pro"
    user_node.set_content("Read my archive: {user_export}")
    _db.session.commit()
    budgets = []

    def fake_export(user, max_tokens=None, **_kwargs):
        budgets.append(max_tokens)
        return "e" * (4 * (max_tokens if max_tokens else 7_000))
    monkeypatch.setattr(_llm_task_mod, "build_user_export_content",
                        fake_export)
    _ScriptedProvider.reset([_resp("A letter.")])
    _run(user_node, pending, alice)
    on = _ScriptedProvider.calls[0]

    second = _placeholder(user_node, alice)
    limited.config["THREAD_PROMPT_TOKEN_LIMIT"] = 0
    _ScriptedProvider.reset([_resp("A letter.")])
    _run(user_node, second, alice)
    off = _ScriptedProvider.calls[0]
    limited.config["THREAD_PROMPT_TOKEN_LIMIT"] = LIMIT

    assert budgets == [None, None]
    assert json.dumps(on["raw"], sort_keys=True) == json.dumps(
        off["raw"], sort_keys=True)


def test_an_uncapped_archive_over_the_limit_shrinks_to_fit(
        limited, counting, monkeypatch):
    """{user_export} without a limit loads as much of the archive as fits:
    under the thread limit, as under the model's window."""
    alice, system, user_node, pending = _build_chain("textmode")
    alice.plan = "pro"
    user_node.set_content("Read my archive: {user_export}")
    _db.session.commit()
    budgets = []

    def fake_export(user, max_tokens=None, **_kwargs):
        budgets.append(max_tokens)
        return "e" * (4 * (max_tokens if max_tokens else 40_000))
    monkeypatch.setattr(_llm_task_mod, "build_user_export_content",
                        fake_export)
    _ScriptedProvider.reset([_resp("A letter.")])

    _run(user_node, pending, alice)

    assert budgets[0] is None and budgets[-1] < 40_000
    assert len(_ScriptedProvider.calls) == 1
    sent = _ScriptedProvider.calls[0]
    assert prompt_chars(sent["raw"]) * REAL["gpt-5"] < LIMIT


# ── Review items 4 and 5: what the thread holds; edits ──────────────────

def test_a_quoted_large_document_counts_and_says_what_to_do(limited,
                                                            counting):
    """A thread with one large document in it, quoted: no stored size
    shows it. The task counts it over the limit and refuses with the text
    for an entry too large (a new thread with it would not help)."""
    alice, system, user_node, pending = _build_chain("textmode")
    doc = _entry(alice, None, "A long chapter. " * 3_500)   # 56k chars
    user_node.set_content(f"What do you think of this? {{quote:{doc.id}}}")
    user_node.token_count = 10
    _db.session.commit()
    assert thread_length(user_node, alice.id)[1] is None  # cannot see it
    _ScriptedProvider.reset([_resp("must not be sent")])

    result = _run(user_node, pending, alice)

    assert result["reason"] == "thread_too_long"
    assert _ScriptedProvider.calls == []
    assert _fresh(pending.id).llm_task_error == CONTENT_TOO_LARGE_MESSAGE
    assert thread_length(user_node, alice.id)[1] == "limit"


def test_the_quoted_document_deleted_after_a_refusal_lets_the_reply_go(
        limited, counting):
    """The user deletes the quoted document after the refusal: the
    refusal row no longer stands, and the reply goes out."""
    alice, system, user_node, pending = _build_chain("textmode")
    doc = _entry(alice, None, "A long chapter. " * 3_500)
    user_node.set_content(f"What do you think of this? {{quote:{doc.id}}}")
    user_node.token_count = 10
    _db.session.commit()
    _ScriptedProvider.reset([_resp("must not be sent")])
    _run(user_node, pending, alice)
    assert thread_length(user_node, alice.id)[1] == "limit"

    doc = Node.query.get(doc.id)
    doc.deleted_at = datetime.utcnow()
    _db.session.commit()

    assert thread_length(user_node, alice.id)[1] != "limit"
    again = _placeholder(user_node, alice)
    _ScriptedProvider.reset([_resp("Now I can.")])
    _run(user_node, again, alice)
    assert len(_ScriptedProvider.calls) == 1


def test_an_earlier_entry_edited_to_hold_much_more_is_measured(limited,
                                                               counting):
    """The user edits an entry in the measured part to paste a large text:
    the stored base does not show it; re-measured in characters, the
    reply is refused before any call."""
    alice, entry, row = _long_thread(limited, 8_000)
    first = Node.query.filter_by(node_type="user").filter(
        Node.parent_id.isnot(None)).order_by(Node.id).first()
    first.set_content("y" * 200_000)
    _db.session.commit()
    pending = _placeholder(entry, alice)
    _ScriptedProvider.reset([_resp("must not be sent")])

    result = _run(entry, pending, alice)

    assert result["reason"] == "thread_too_long"
    assert _ScriptedProvider.calls == []


def test_an_earlier_entry_shortened_lets_the_reply_go(limited, counting):
    """The other way: the measured part shrank since, so a thread whose
    base was near the limit is under it now."""
    alice, entry, row = _long_thread(limited, 39_500)
    first = Node.query.filter_by(node_type="user").filter(
        Node.parent_id.isnot(None)).order_by(Node.id).first()
    first.set_content("short now")
    _db.session.commit()
    pending = _placeholder(entry, alice)
    _ScriptedProvider.reset([_resp("Here.")])

    _run(entry, pending, alice)

    assert len(_ScriptedProvider.calls) == 1


# ── Tool rounds, cost rows ──────────────────────────────────────────────

def test_tool_rounds_of_a_reply_that_started_under_the_limit_finish(limited):
    """The first call is just under the limit; the round's injection takes
    the continuation over it. The reply still finishes (Peter's decision
    covers the start of a reply)."""
    alice, entry, row = _long_thread(limited, 29_500)
    _mk_artifact(alice.id, "reading-list", "book " * 2_000,
                 title="Reading List")
    _db.session.commit()
    pending = _placeholder(entry, alice)
    _ScriptedProvider.reset([
        _resp("Let me look.", tool_calls=[{
            "id": "t1", "name": "read_artifact",
            "input": {"kind": "reading-list"},
        }]),
        _resp("Here it is."),
    ])

    _run(entry, pending, alice)

    assert len(_ScriptedProvider.calls) == 2
    interim = _fresh(pending.id)
    final = Node.query.get(interim.continuation_node_id)
    assert final.get_content() == "Here it is."


def test_every_call_records_its_prompt_characters(app):  # noqa: F811
    alice, system, user_node, pending = _build_chain("textmode")
    _ScriptedProvider.reset([_resp("An answer.")])

    _run(user_node, pending, alice)

    row = APICostLog.query.filter_by(request_type="conversation").one()
    sent = _ScriptedProvider.calls[0]
    assert row.prompt_chars == prompt_chars(sent["raw"], sent["tools"])
    assert row.prompt_chars > 0

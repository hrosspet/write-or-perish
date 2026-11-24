"""A Glean on Haiku 5.5 stays under Haiku's 100,000-token price step
(#733; Peter, 2026-11-21: "pls fix as a followup").

glean_prompt_budget takes the smaller of the thread limit and the
model's price step, the ``long_context_threshold`` of its entry in
SUPPORTED_MODELS (the price table), less GLEAN_BUDGET_MARGIN: 98,000
tokens on Haiku 5.5. Haiku has no local token count, so its list of
tweets is sized by the estimate (the claude_new tweets prior); the tests
show with numbers that the estimate keeps a Haiku Glean under the step,
how many tweets the Glean then holds and how many Gleans a 46,000-tweet
day takes at least. Every other model's step is above the thread limit
or absent, and the list it sends is byte for byte what it was. A Haiku
Glean with no room under its step, in a thread the thread limit still
leaves room in, is refused with a sentence that names the model and the
way on (another model, or a new thread), not the thread-length one.
"""
import copy
import json
import math
import sys
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from backend.tests.test_retrieval_loop import (  # noqa: F401 (fixture)
    app, _llm_task_mod, generate_llm_response, _FakeSelf, _mk_user,
    _fresh, _resp,
)
from backend.tests.test_glean_paging import (
    _RecordingProvider, _count, _placeholder, _thread,
)
from backend.config import Config
from backend.llm_providers import message_text, model_input_cap
from backend.models import APICostLog, FeedRender, User
from backend.utils import community_archive as ca
from backend.utils.ca_feed import (
    GLEAN_BUDGET_MARGIN, GLEAN_DAY_DONE_CODE, GLEAN_TOO_LONG_FOR_MODEL_CODE,
    GLEAN_TOO_LONG_FOR_MODEL_MESSAGE, glean_prompt_budget,
    glean_too_long_for_model,
)
from backend.utils.thread_size import (
    CHARS_PER_UNIT, REFUSED_MARKER, THREAD_TOO_LONG_CODE, Size, prompt_limit,
)
from backend.utils.tokens import TOKENS_PER_UNIT_PRIOR, tokenizer_family
from backend.utils.voice_refusals import SPOKEN_REFUSALS

HAIKU = "claude-haiku-5.5"
SONNET = "claude-sonnet-5.5"
LUNA = "gpt-6-luna"
LIMIT = 250_000
STEP = 100_000

# What the estimate counts a character of a Haiku Glean's list at:
# the claude_new tweets prior, 3.1 tokens per stored unit of four
# characters.
HAIKU_PER_CHAR = TOKENS_PER_UNIT_PRIOR["tweets"]["claude_new"] / CHARS_PER_UNIT

# Measured on the Community Archive day of the 2026-11-03 export (17,638
# tweets by 1,428 accounts, rendered as a Glean renders them, counted
# with tiktoken's o200k_base): 617,291 tokens in 2,269,364 characters.
O200K_PER_CHAR = 617_291 / 2_269_364
# The most Claude's newer tokenizer (Haiku 5.5's) has been seen to make of
# a text against o200k_base: docs/design/chunk-planner.md measured Loore
# threads at 0.85-1.13 o200k tokens a unit and 1.51-2.18 Claude-new
# tokens a unit; pairing the extreme ends (2.18 / 0.85) gives 2.56, above
# every same-text ratio on record (about 1.9 there; Anthropic states
# about 1.3x its previous tokenizer, and 1.21x was measured on tweets).
CLAUDE_PER_O200K_MAX = 2.18 / 0.85


@pytest.fixture
def priced(app):  # noqa: F811
    """The real price table and the 250k thread limit on the test app."""
    models = app.config["SUPPORTED_MODELS"]
    saved_models = dict(models)
    saved_limit = app.config.get("THREAD_PROMPT_TOKEN_LIMIT")
    for model_id, cfg in Config.SUPPORTED_MODELS.items():
        models.setdefault(model_id, copy.deepcopy(cfg))
    app.config["THREAD_PROMPT_TOKEN_LIMIT"] = LIMIT
    yield app
    models.clear()
    models.update(saved_models)
    app.config["THREAD_PROMPT_TOKEN_LIMIT"] = saved_limit


def _budget_before(model_id, config):
    """glean_prompt_budget as #717 built it: the model's input ceiling
    without its price step."""
    limits = [prompt_limit(config)] if prompt_limit(config) else []
    cfg = (config.get("SUPPORTED_MODELS") or {}).get(model_id)
    if cfg:
        limits.append(model_input_cap(cfg, price_tier=False))
    return int(min(limits) * (1 - GLEAN_BUDGET_MARGIN)) if limits else None


def _local_count(model_id):
    """The local count as the server has it: tiktoken's o200k_base on an
    o200k model (here one token per four characters, as in the other
    Glean tests), none on an Anthropic model."""
    return _count if tokenizer_family(model_id) == "o200k" else None


def _estimate(text):
    return int(math.ceil(len(text or "") * HAIKU_PER_CHAR))


def _entries(n, text_chars, per_account, end=datetime(2026, 11, 20, 12)):
    """*n* tweets in render order (by account, then time), newest at
    *end*, one every 30 seconds; each account posts *per_account* of them
    in a row. Each text is *text_chars* long."""
    out = []
    for i in range(n):
        posted = end - timedelta(seconds=30 * (n - 1 - i))
        words = f"tweet {i} " + "about pacing the mornings and reading " * 8
        out.append({"username": f"acct{i // per_account:05d}",
                    "tweet_id": str(10_000_000 + i),
                    "text": words[:text_chars], "posted_at": posted,
                    "display_name": None})
    return sorted(out, key=lambda e: (e["username"], e["posted_at"]))


def _render(entries, skipped=0):
    end = max(e["posted_at"] for e in entries)
    meta = {"export_id": "2026-11-20T12-00-00Z", "days": 1,
            "window_start": (end - timedelta(days=1)).strftime(
                "%Y-%m-%d %H:%M"),
            "window_end": end.strftime("%Y-%m-%d %H:%M"),
            "window_start_at": end - timedelta(days=1), "window_end_at": end,
            "excluded": 0, "skipped": skipped, "scope": "all"}
    return ca._format_render(entries, meta)


# ── The budget ───────────────────────────────────────────────────────────

def test_a_haiku_glean_s_budget_is_its_price_step_less_the_margin(priced):
    """98,000 tokens: Haiku 5.5's 100k step less 2 %, read from the price
    table (a different step there moves it; none there leaves the thread
    limit), and a thread limit below the step still decides."""
    models = priced.config["SUPPORTED_MODELS"]
    assert Config.SUPPORTED_MODELS[HAIKU]["long_context_threshold"] == STEP
    assert glean_prompt_budget(HAIKU, priced.config) == 98_000
    assert _budget_before(HAIKU, priced.config) == 245_000

    haiku = models[HAIKU]
    models[HAIKU] = dict(haiku, long_context_threshold=60_000)
    assert glean_prompt_budget(HAIKU, priced.config) == 58_800
    models[HAIKU] = {k: v for k, v in haiku.items()
                     if not k.startswith("long_context")}
    assert glean_prompt_budget(HAIKU, priced.config) == 245_000
    models[HAIKU] = haiku
    priced.config["THREAD_PROMPT_TOKEN_LIMIT"] = 80_000
    assert glean_prompt_budget(HAIKU, priced.config) == 78_400


def test_every_other_model_s_budget_is_unchanged_where_its_step_is_above_the_limit(priced):
    """Every model of the price table: where the step is above the thread
    limit (the OpenAI models' 272k) or there is none (Sonnet 5.5, flat
    across its 1M window; the other Anthropic models), the budget is what
    it was. Haiku 5.5 is the one model whose budget moves, and every
    other Glean model keeps 245,000 tokens."""
    changed = set()
    for model_id, cfg in Config.SUPPORTED_MODELS.items():
        now = glean_prompt_budget(model_id, priced.config)
        step = cfg.get("long_context_threshold")
        if step is None or step >= LIMIT:
            assert now == _budget_before(model_id, priced.config), model_id
        else:
            changed.add(model_id)
            assert now == int(step * (1 - GLEAN_BUDGET_MARGIN)), model_id
    assert changed == {HAIKU}

    glean_models = sorted(m for m, c in Config.SUPPORTED_MODELS.items()
                          if c.get("read"))
    assert {m: glean_prompt_budget(m, priced.config) for m in glean_models} \
        == {m: 98_000 if m == HAIKU else 245_000 for m in glean_models}
    assert {HAIKU, SONNET, LUNA, "gpt-5.6-luna"} <= set(glean_models)


# ── The list a Glean sends ───────────────────────────────────────────────

def _messages(rest_chars, listed):
    """A Glean's prompt as the task assembles it: the instructions,
    profile and intentions (*rest_chars* of them) with the list after
    them in one block, then the request."""
    rest = ("Profile and intentions of the reader. " * 2_000)[:rest_chars]
    return [
        {"role": "system",
         "content": [{"type": "text", "text": rest + "\n\n" + listed}]},
        {"role": "user", "content": [{"type": "text",
                                      "text": "Read today's tweets."}]},
    ]


def _rest_estimate(per_char):
    def estimate(messages):
        chars = sum(len(message_text(m)) for m in messages)
        return Size(int(chars * per_char), False, "length", chars)
    return estimate


def _fit(model_id, config, rest_chars, day):
    text, stats, refs = day
    messages = _messages(rest_chars, text)
    # The thread limit's estimate of the rest: the threads prior of the
    # model's tokenizer (its upper end).
    per_char = (TOKENS_PER_UNIT_PRIOR["threads"][tokenizer_family(model_id)]
                / CHARS_PER_UNIT)
    fit = _llm_task_mod._fit_read_list(
        messages, text, stats, refs, model_id, config,
        estimate=_rest_estimate(per_char))
    return messages, fit


def test_every_other_glean_model_sends_the_same_list_byte_for_byte(priced, monkeypatch):
    """A day larger than any Glean, fitted on each Glean model with the
    new budget and with #717's: the prompt is the same, byte for byte, on
    every model but Haiku 5.5 (both Lunas and the other OpenAI models by
    their local count, Sonnet 5.5 by the estimate). On Haiku the list is
    shorter and the prompt's estimate is under 98,000 tokens."""
    from backend.utils import tokens
    monkeypatch.setattr(tokens, "local_token_counter", _local_count)
    day = _render(_entries(12_000, 120, per_account=12))
    glean_models = sorted(m for m, c in Config.SUPPORTED_MODELS.items()
                          if c.get("read"))
    for model_id in glean_models:
        now_messages, now = _fit(model_id, priced.config, 40_000, day)
        with monkeypatch.context() as patched:
            patched.setattr(_llm_task_mod, "glean_prompt_budget",
                            _budget_before)
            before_messages, before = _fit(model_id, priced.config, 40_000,
                                           day)
        assert len(before.refs) < len(day[2]), model_id
        if model_id == HAIKU:
            assert len(now.refs) < len(before.refs)
            rest = _rest_estimate(
                TOKENS_PER_UNIT_PRIOR["threads"]["claude_new"]
                / CHARS_PER_UNIT)(_messages(40_000, ""))
            assert rest.tokens + _estimate(now.text) <= 98_000
            continue
        assert now_messages == before_messages, model_id
        assert (now.text, now.refs, now.counted) \
            == (before.text, before.refs, before.counted), model_id


# ── Through the completion task ──────────────────────────────────────────

# The task's day: 1,500 tweets of 120 characters, twelve a row per
# account. About 160,000 tokens at the Haiku estimate: more than a Haiku
# Glean holds, less than the thread limit's 245,000.
DAY_TWEETS = 1_500


@pytest.fixture
def archive_day(priced, monkeypatch, tmp_path):
    """A Community Archive snapshot holding DAY_TWEETS tweets, rendered by
    the code a Glean uses, with the price table and the 250k limit."""
    duckdb = pytest.importorskip("duckdb")
    from backend.utils import tokens
    entries = _entries(DAY_TWEETS, 120, per_account=12)
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    rows = ", ".join(
        f"('{e['tweet_id']}', '{e['username']}', "
        f"timestamptz '{e['posted_at']:%Y-%m-%d %H:%M:%S}+00', "
        f"'{e['text']}')" for e in entries)
    con.execute(
        f"copy (select * from (values {rows}) "
        "v(tweet_id, account_id, created_at, full_text)) "
        f"to '{tmp_path / 'tweets.parquet'}' (format parquet)")
    con.execute(
        "copy (select * from (values ('a0', 'alice')) "
        "v(account_id, username)) "
        f"to '{tmp_path / 'profiles.parquet'}' (format parquet)")
    (tmp_path / "export_id").write_text("2026-11-20T12-00-00Z")
    monkeypatch.setattr(tokens, "local_token_counter", _local_count)
    imports = sys.modules.get("backend.tasks.imports")
    if imports is None or isinstance(imports, MagicMock):
        imports = MagicMock()
        monkeypatch.setitem(sys.modules, "backend.tasks.imports", imports)
        imports.snapshot_dir_for = lambda config: tmp_path
    else:
        monkeypatch.setattr(imports, "snapshot_dir_for",
                            lambda config: tmp_path)
    monkeypatch.setattr(_llm_task_mod, "request_snapshot_refresh",
                        lambda *a, **k: False)
    return tmp_path


def _glean_on(monkeypatch, model_id, alice=None, read=None):
    """One Glean on *model_id* under *read* (a new thread when None) as
    the task runs it (live), with what the task estimated the whole
    prompt at (the #648 check's size). Returns (result, request, size,
    gleaning); request and size are None when nothing was sent."""
    if alice is None:
        alice = _mk_user("alice", approved=True, plan="alpha", is_admin=True)
    llm_user = (_mk_user(model_id, twitter_id=f"llm-{model_id}")
                if not User.query.filter_by(username=model_id).first()
                else User.query.filter_by(username=model_id).first())
    if read is None:
        read = _thread(alice)
    gleaning = _placeholder(llm_user, alice, read.id, meta=[{"name": "_live"}])
    sizes = []
    measure = _llm_task_mod.assembled_prompt_size

    def spy(*args, **kwargs):
        size = measure(*args, **kwargs)
        if kwargs.get("dense_chars"):
            sizes.append(size)
        return size
    monkeypatch.setattr(_llm_task_mod, "assembled_prompt_size", spy)
    _RecordingProvider.reset([_resp(json.dumps(
        {"verdict": "One thing today.", "picks": []}))])
    monkeypatch.setattr(_llm_task_mod, "LLMProvider", _RecordingProvider)
    result = generate_llm_response(_FakeSelf(), read.id, gleaning.id,
                                   model_id, alice.id, source_mode=None)
    sent = _RecordingProvider.requests
    assert len(sent) == len(sizes) <= 1
    return (result, sent[0] if sent else None, sizes[0] if sizes else None,
            _fresh(gleaning.id))


def test_a_haiku_glean_stays_under_its_price_step(archive_day, monkeypatch):
    """Haiku 5.5: the newest tweets that fit 98,000 tokens at the
    estimate; the size the task's own check measures is under it, and the
    Glean is not refused."""
    result, request, size, gleaning = _glean_on(monkeypatch, HAIKU)

    assert request is not None
    assert result.get("status") != "refused"
    assert gleaning.llm_task_status == "completed"
    assert not size.certain
    assert 90_000 < size.tokens <= 98_000
    row = FeedRender.query.filter_by(node_id=gleaning.id).one()
    assert row.tweet_count < DAY_TWEETS
    listed = sorted(int(i) for i in row.tweet_ids.split(","))
    assert listed == list(range(10_000_000 + DAY_TWEETS - len(listed),
                                10_000_000 + DAY_TWEETS))
    text = "".join(message_text(m) for m in request["messages"])
    assert f"This list holds the newest {row.tweet_count} of them" in text


def _long_thread(chars):
    """A Glean prompt under an entry of *chars* characters."""
    alice = _mk_user("alice", approved=True, plan="alpha", is_admin=True)
    return alice, _thread(alice, "pacing the mornings " * (chars // 20))


def test_a_haiku_glean_with_no_room_under_its_step_names_the_model_and_the_way_on(archive_day, monkeypatch):
    """A thread whose rest alone is past Haiku's 98,000 but well under the
    thread limit's 245,000: the Haiku Glean is refused before any call,
    nothing billed, with a sentence that names the model and what works,
    and a code no app reads as the thread's limit. A Glean on Sonnet 5.5
    in the same place then reads the day: the thread was never too long
    for it."""
    alice, read = _long_thread(208_000)

    result, request, size, gleaning = _glean_on(
        monkeypatch, HAIKU, alice=alice, read=read)

    assert request is None
    assert result["status"] == "refused"
    assert result["reason"] == GLEAN_TOO_LONG_FOR_MODEL_CODE
    assert gleaning.llm_task_status == "failed"
    assert gleaning.llm_task_error == (
        "This thread is too long for a Glean on Haiku 5.5. Pick another "
        "model for this Glean, or start a new thread.")
    meta = json.loads(gleaning.tool_calls_meta)
    assert {"name": REFUSED_MARKER,
            "code": GLEAN_TOO_LONG_FOR_MODEL_CODE} in meta
    # Not the thread limit's code, nor one the Voice screens say in a fixed
    # sentence: both apps show the server's words for it.
    assert GLEAN_TOO_LONG_FOR_MODEL_CODE not in (
        THREAD_TOO_LONG_CODE, GLEAN_DAY_DONE_CODE)
    assert GLEAN_TOO_LONG_FOR_MODEL_CODE not in SPOKEN_REFUSALS
    assert FeedRender.query.filter_by(node_id=gleaning.id).count() == 0
    assert APICostLog.query.filter_by(
        request_ref=f"node:{gleaning.id}").count() == 0
    assert APICostLog.query.filter(
        APICostLog.cost_microdollars > 0).count() == 0

    result, request, size, other = _glean_on(
        monkeypatch, SONNET, alice=alice, read=read)

    assert request is not None
    assert other.llm_task_status == "completed"
    assert 98_000 < size.tokens <= 245_000
    assert FeedRender.query.filter_by(node_id=other.id).one().tweet_count > 0


def test_a_thread_too_long_for_any_glean_keeps_the_thread_length_sentence(archive_day, monkeypatch):
    """Past the thread limit's 245,000 too, the thread itself is too long:
    the Haiku Glean gets the thread-length code and sentence as before."""
    from backend.utils.thread_size import _MESSAGES
    alice, read = _long_thread(460_000)

    result, request, size, gleaning = _glean_on(
        monkeypatch, HAIKU, alice=alice, read=read)

    assert request is None
    assert result["reason"] == THREAD_TOO_LONG_CODE
    assert gleaning.llm_task_error in _MESSAGES.values()
    assert {"name": REFUSED_MARKER, "code": THREAD_TOO_LONG_CODE} \
        in json.loads(gleaning.tool_calls_meta)


def test_the_model_s_sentence_only_where_its_step_refused(priced):
    """glean_too_long_for_model: the sentence while the rest fits the
    budget the thread limit alone gives (245,000), none past it, and none
    on a model whose budget the step does not set."""
    config = priced.config
    assert glean_too_long_for_model(HAIKU, config, 98_000) \
        == GLEAN_TOO_LONG_FOR_MODEL_MESSAGE.format(model="Haiku 5.5")
    assert glean_too_long_for_model(HAIKU, config, 244_999) is not None
    assert glean_too_long_for_model(HAIKU, config, 245_000) is None
    for model_id in (SONNET, LUNA, "gpt-5.6-luna"):
        assert glean_too_long_for_model(model_id, config, 100_000) is None
    # A thread limit at or below the step: the limit refuses, not the step.
    config["THREAD_PROMPT_TOKEN_LIMIT"] = 100_000
    assert glean_too_long_for_model(HAIKU, config, 99_000) is None


# ── The numbers ──────────────────────────────────────────────────────────

# A day shaped like the 2026-11-03 day as Haiku Gleans page through it
# (measured on it with this code: 26 Gleans of 537 to 766 tweets, 679 on
# average, at a 78,000-token room). Texts of 132 characters, twelve in a
# row per account, render at 147 characters a tweet with their numbers
# and account headers, so a Glean holds 686 of them.
TEXT_CHARS = 132
PER_ACCOUNT = 12
# The rest of the prompt (thread, instructions, profile, intentions), as
# estimated: the figure #717 assumed.
REST = 20_000


def test_the_estimate_keeps_a_haiku_glean_under_the_step_with_room_to_spare(priced):
    """The numbers, at Haiku's budget as the code sets it. With 20,000
    tokens for the rest of its prompt, a Haiku Glean holds 686 tweets of
    this day, so a 46,000-tweet day takes at least 68 Gleans (24 at the
    budget before #733). At least: in one thread each Glean sends the
    earlier Glean replies again, so the rest grows and later Gleans hold
    fewer. Its list is 100,642 characters, 77,998 tokens at the estimate.
    o200k_base makes about 27,400 tokens of it, and Claude's tokenizer 1.3
    to 1.9 times that: the Glean bills about 56,000 to 72,000 tokens. At
    the extreme 2.56 it bills about 90,200, still under the step; crossing
    it would take Claude making 2.92 times the o200k count of the list."""
    budget = glean_prompt_budget(HAIKU, priced.config)
    entries = _entries(3_000, TEXT_CHARS, PER_ACCOUNT)
    day = _render(entries)
    text, stats, refs = ca.newest_that_fit(*day, budget - REST, _estimate)
    assert len(refs) == 686
    assert len(text) == 100_642
    assert REST + _estimate(text) == 97_998 <= budget
    assert math.ceil(46_000 / len(refs)) == 68

    # Before #733: the thread limit's budget.
    _, _, before = ca.newest_that_fit(
        *day, _budget_before(HAIKU, priced.config) - REST, _estimate)
    assert len(before) == 1_978
    assert math.ceil(46_000 / len(before)) == 24

    # The estimate errs high: a character of the list counts 0.775
    # tokens; o200k_base makes 0.272 of it, and Claude's tokenizer at
    # most 2.56 times that, 0.698.
    assert HAIKU_PER_CHAR == pytest.approx(0.775)
    worst = O200K_PER_CHAR * CLAUDE_PER_O200K_MAX
    assert worst == pytest.approx(0.698, abs=0.001)
    assert worst < HAIKU_PER_CHAR
    o200k = len(text) * O200K_PER_CHAR
    assert round(o200k, -2) == 27_400
    assert round(REST + o200k * CLAUDE_PER_O200K_MAX, -2) == 90_200 < STEP
    assert round((STEP - REST) / o200k, 2) == 2.92
    assert [round(REST + o200k * ratio, -3) for ratio in (1.3, 1.9)] \
        == [56_000, 72_000]

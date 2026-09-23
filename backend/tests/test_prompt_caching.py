"""Tests for prompt caching (#187 provider-side, #192 backend cache).

Covers: cache-aware cost math, the Redis-backed system-prompt render
cache (fake client), render_system_message placeholder resolution against
pinned artifacts, and _call_anthropic content-block passthrough with
cache_control survival + cache usage fields (mocked Anthropic client).
"""
import os
import sys
from unittest.mock import MagicMock

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
    User, Node, NodeContextArtifact, UserTodo, APICostLog,
)
import backend.utils.prompt_cache as prompt_cache  # noqa: E402
from backend.utils.cost import (  # noqa: E402
    calculate_llm_cost_microdollars, llm_cost_from_response,
    llm_cost_log_fields)

_GLUE = ("backend.celery_app", "backend.llm_providers",
         "backend.tasks.llm_completion")
_saved_glue = {k: sys.modules.get(k) for k in _GLUE}
sys.modules["backend.celery_app"] = MagicMock()
sys.modules["backend.llm_providers"] = MagicMock()
sys.modules.pop("backend.tasks.llm_completion", None)
from backend.tasks.llm_completion import (  # noqa: E402
    render_system_message, gated_voice_tools, VOICE_TOOLS,
)
for _k, _v in _saved_glue.items():
    if _v is None:
        sys.modules.pop(_k, None)
    else:
        sys.modules[_k] = _v

SUPPORTED_MODELS = {
    "claude-opus-4.6": {
        "provider": "anthropic", "api_model": "claude-opus-4-6",
        "input_price_per_mtok": 5.00, "output_price_per_mtok": 25.00,
    },
    "claude-fable-5": {
        "provider": "anthropic", "api_model": "claude-fable-5",
        "input_price_per_mtok": 10.00, "output_price_per_mtok": 50.00,
    },
    # Mirrors backend/config.py: Fable 5.1 cache reads at 0.025x.
    "claude-fable-5.1": {
        "provider": "anthropic", "api_model": "claude-fable-5-1",
        "input_price_per_mtok": 10.00, "output_price_per_mtok": 50.00,
        "cache_read_multiplier": 0.025,
    },
}


@pytest.fixture
def app():
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["TESTING"] = True
    app.config["SUPPORTED_MODELS"] = SUPPORTED_MODELS
    _db.init_app(app)
    with app.app_context():
        _db.create_all()
        user = User(username="tester")
        user.timezone = "UTC"
        _db.session.add(user)
        _db.session.commit()
        yield app
        _db.session.rollback()
        _db.drop_all()


# ── Cost math (#187) ─────────────────────────────────────────────────────

def test_cost_cache_multipliers(app):
    with app.app_context():
        # Uncached baseline: 1M input = $5 = 5_000_000 microdollars
        base = calculate_llm_cost_microdollars(
            "claude-opus-4.6", 1_000_000, 0)
        assert base == 5_000_000
        # Cache read bills at 0.1x
        read = calculate_llm_cost_microdollars(
            "claude-opus-4.6", 0, 0, cache_read_tokens=1_000_000)
        assert read == 500_000
        # Cache write bills at 1.25x
        write = calculate_llm_cost_microdollars(
            "claude-opus-4.6", 0, 0, cache_write_tokens=1_000_000)
        assert write == 6_250_000
        # Mixed adds up
        mixed = calculate_llm_cost_microdollars(
            "claude-opus-4.6", 100_000, 10_000,
            cache_read_tokens=900_000, cache_write_tokens=50_000)
        assert mixed == (round(100_000 * 5 + 10_000 * 25
                               + 900_000 * 5 * 0.1 + 50_000 * 5 * 1.25))


def test_cost_cache_read_multiplier_override(app):
    # Fable 5.1 cache hits bill at 0.025x base input (pricing page,
    # 2026-09-02) instead of the 0.1x module default; the model entry
    # overrides it via cache_read_multiplier. Writes stay at 1.25x.
    with app.app_context():
        read = calculate_llm_cost_microdollars(
            "claude-fable-5.1", 0, 0, cache_read_tokens=1_000_000)
        assert read == 250_000
        write = calculate_llm_cost_microdollars(
            "claude-fable-5.1", 0, 0, cache_write_tokens=1_000_000)
        assert write == 12_500_000
        # Models without the override keep the module default.
        assert calculate_llm_cost_microdollars(
            "claude-fable-5", 0, 0, cache_read_tokens=1_000_000) == 1_000_000



def test_cost_opus_5_5_from_real_config():
    # Reads the real config entry (not a mirror) so a pricing edit there
    # is checked against the pricing page figures verified 2026-09-22:
    # $4 in / $20 out, cache hits $0.20 (0.05x), 5m writes $5 (1.25x),
    # batch half price.
    from backend.config import Config
    app = Flask(__name__)
    app.config["SUPPORTED_MODELS"] = {
        "claude-opus-5.5": Config.SUPPORTED_MODELS["claude-opus-5.5"]}
    with app.app_context():
        cost = calculate_llm_cost_microdollars
        assert cost("claude-opus-5.5", 1_000_000, 0) == 4_000_000
        assert cost("claude-opus-5.5", 0, 1_000_000) == 20_000_000
        assert cost("claude-opus-5.5", 0, 0,
                    cache_read_tokens=1_000_000) == 200_000
        assert cost("claude-opus-5.5", 0, 0,
                    cache_write_tokens=1_000_000) == 5_000_000
        assert cost("claude-opus-5.5", 1_000_000, 1_000_000,
                    batch=True) == 12_000_000

def test_gated_voice_tools_warm_matches_generation():
    # The pre-warm and generation BOTH build their tool list via
    # gated_voice_tools, so the cached tool prefix is byte-identical. A
    # divergence here (warm keeping semantic_search while generation drops it)
    # silently busts the whole cache — that was the read=0 bug.
    names = lambda ts: [t["name"] for t in ts]  # noqa: E731
    # Own-archive search is on for everyone (#329): the search tools are in
    # the default list, gated only by the SEMANTIC_SEARCH_AGENTIC killswitch
    # — never by the per-user "External references" toggle, which acts
    # inside the handler so the tool prefix is identical across users.
    default = gated_voice_tools({})
    assert "semantic_search" in names(default)
    assert "read_full" in names(default)
    killed = gated_voice_tools({"SEMANTIC_SEARCH_AGENTIC": False})
    assert "semantic_search" not in names(killed)
    assert "read_full" not in names(killed)
    assert "read_artifact" in names(killed)
    # With everything enabled, the full tool list is exposed.
    all_on = gated_voice_tools({"SHARE_V1": True})
    assert names(all_on) == names(VOICE_TOOLS)
    # Identical inputs -> identical list for both call sites (the invariant).
    cfg = {"SHARE_V1": False}
    assert gated_voice_tools(cfg) == gated_voice_tools(cfg)


def test_api_cost_log_persists_cache_breakdown(app):
    # The cache read/write split is recorded as its own columns (#187
    # observability) so cache hit-rate is queryable from the DB, not only
    # the logs. input_tokens stays the full prompt size.
    with app.app_context():
        uid = User.query.first().id
        row = APICostLog(
            user_id=uid,
            model_id="claude-opus-4.6",
            request_type="conversation",
            input_tokens=950_000,        # uncached 50k + read 900k + write 0
            output_tokens=1_000,
            cache_read_tokens=900_000,
            cache_write_tokens=0,
            cost_microdollars=123,
        )
        _db.session.add(row)
        _db.session.commit()
        fetched = APICostLog.query.get(row.id)
        assert fetched.cache_read_tokens == 900_000
        assert fetched.cache_write_tokens == 0
        # Columns default to 0 when a non-cached call omits them.
        plain = APICostLog(
            user_id=uid, model_id="gpt-4o-transcribe",
            request_type="transcription", cost_microdollars=5,
        )
        _db.session.add(plain)
        _db.session.commit()
        assert APICostLog.query.get(plain.id).cache_read_tokens == 0
        assert APICostLog.query.get(plain.id).cache_write_tokens == 0


# ── Backend render cache (#192) ──────────────────────────────────────────

class FakeRedis:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value


def test_render_cache_roundtrip_and_key_busting(app, monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(prompt_cache, "_client", lambda config: fake)
    with app.app_context():
        uid = User.query.first().id
        node = Node(user_id=uid, node_type="user")
        node.set_content("prompt")
        _db.session.add(node)
        _db.session.commit()

        assert prompt_cache.get_cached_render(app.config, node) is None
        prompt_cache.store_render(app.config, node, "rendered bytes")
        assert prompt_cache.get_cached_render(
            app.config, node) == ("rendered bytes", None)
        # The training-key verdict travels with the text (#326).
        prompt_cache.store_render(app.config, node, "rendered bytes",
                                  unlicensed="the memory artifact is 'chat'")
        assert prompt_cache.get_cached_render(app.config, node) == (
            "rendered bytes", "the memory artifact is 'chat'")
        # An entry that is not a text + verdict pair is a miss, never a
        # verdict-less hit.
        fake.store[prompt_cache._key(node)] = b"a plain render"
        assert prompt_cache.get_cached_render(app.config, node) is None

        # Editing the node (updated_at changes) busts the key naturally
        from datetime import datetime
        node.updated_at = datetime(2030, 1, 1)
        _db.session.commit()
        assert prompt_cache.get_cached_render(app.config, node) is None

        # The render variant (killswitch + share / external-references gates,
        # #329) is part of the key: a toggle flip mid-thread takes a fresh
        # key instead of serving the stale render for the rest of the TTL.
        prompt_cache.store_render(app.config, node, "no refs", "a1s0e0")
        prompt_cache.store_render(app.config, node, "with refs", "a1s0e1")
        assert prompt_cache.get_cached_render(
            app.config, node, "a1s0e0").text == "no refs"
        assert prompt_cache.get_cached_render(
            app.config, node, "a1s0e1").text == "with refs"
        assert prompt_cache.get_cached_render(app.config, node) is None


def test_render_cache_fails_open(app, monkeypatch):
    def boom(config):
        raise ConnectionError("redis down")
    monkeypatch.setattr(prompt_cache, "_client", boom)
    with app.app_context():
        uid = User.query.first().id
        node = Node(user_id=uid, node_type="user")
        node.set_content("prompt")
        _db.session.add(node)
        _db.session.commit()
        # Both directions silently degrade
        assert prompt_cache.get_cached_render(app.config, node) is None
        prompt_cache.store_render(app.config, node, "x")  # no raise


# ── render_system_message (#187 byte-identity source) ────────────────────

def test_render_system_message_resolves_pinned_placeholders(app):
    with app.app_context():
        uid = User.query.first().id
        todo = UserTodo(user_id=uid, generated_by="test", ai_usage="chat")
        todo.set_content("- [ ] pinned todo content")
        _db.session.add(todo)
        _db.session.flush()

        node = Node(user_id=uid, human_owner_id=uid, node_type="user")
        node.set_content("Prompt start. <todo>{user_todo}</todo> End.")
        _db.session.add(node)
        _db.session.flush()
        _db.session.add(NodeContextArtifact(
            node_id=node.id, artifact_type="todo", artifact_id=todo.id))
        _db.session.commit()

        text = render_system_message(node, uid)
        assert "pinned todo content" in text
        assert "{user_todo}" not in text
        assert "author tester:" in text
        # Deterministic: same call, same bytes
        assert render_system_message(node, uid) == text


# ── Provider block passthrough (#187) ────────────────────────────────────

def test_call_anthropic_preserves_blocks_and_cache_usage(app, monkeypatch):
    # Import the real provider module fresh (it may be mocked globally)
    monkeypatch.delitem(sys.modules, "backend.llm_providers", raising=False)
    import backend.llm_providers as providers

    captured = {}

    class FakeUsage:
        input_tokens = 100
        output_tokens = 10
        cache_read_input_tokens = 5000
        cache_creation_input_tokens = 300

    class FakeBlock:
        type = "text"
        text = "hello"

    class FakeResponse:
        content = [FakeBlock()]
        usage = FakeUsage()
        stop_reason = "end_turn"

    class FakeMessages:
        def create(self, **kwargs):
            captured.update(kwargs)
            return FakeResponse()

    class FakeClient:
        def __init__(self, api_key=None):
            self.messages = FakeMessages()

    monkeypatch.setattr(providers, "Anthropic", FakeClient)

    messages = [
        {"role": "user", "content": [
            {"type": "text", "text": "big stable prefix",
             "cache_control": {"type": "ephemeral"}},
        ]},
        {"role": "user", "content": [
            {"type": "text", "text": "part A"},
            {"type": "text", "text": "part B",
             "cache_control": {"type": "ephemeral"}},
        ]},
    ]
    result = providers.LLMProvider._call_anthropic(
        "claude-opus-4-6", messages, "fake-key")

    sent = captured["messages"]
    # Blocks passed through, not flattened; markers survive
    assert isinstance(sent[0]["content"], list)
    assert sent[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert len(sent[1]["content"]) == 2
    assert sent[1]["content"][1]["cache_control"] == {"type": "ephemeral"}
    # Cache usage surfaced
    assert result["cache_read_input_tokens"] == 5000
    assert result["cache_creation_input_tokens"] == 300
    assert result["input_tokens"] == 100


# ── OpenAI cached-input pricing (#189) ───────────────────────────────────

def test_openai_cached_input_discount(app):
    app.config["SUPPORTED_MODELS"]["gpt-5.5"] = {
        "provider": "openai", "api_model": "gpt-5.5",
        "input_price_per_mtok": 5.00, "output_price_per_mtok": 30.00,
        "cached_input_multiplier": 0.25,
    }
    with app.app_context():
        # 1M prompt tokens, 800k of them cached at 0.25x
        cost = calculate_llm_cost_microdollars(
            "gpt-5.5", 1_000_000, 0, cached_input_tokens=800_000)
        assert cost == round(200_000 * 5 + 800_000 * 5 * 0.25)
        # Default multiplier (0.5) when model doesn't specify one
        app.config["SUPPORTED_MODELS"]["gpt-5.4"] = {
            "provider": "openai", "api_model": "gpt-5.4",
            "input_price_per_mtok": 2.50, "output_price_per_mtok": 15.00,
        }
        cost54 = calculate_llm_cost_microdollars(
            "gpt-5.4", 1_000_000, 0, cached_input_tokens=1_000_000)
        assert cost54 == round(1_000_000 * 2.5 * 0.5)
        # cached subset can never exceed input_tokens
        capped = calculate_llm_cost_microdollars(
            "gpt-5.5", 100, 0, cached_input_tokens=10_000)
        assert capped == round(100 * 5 * 0.25)


# ── OpenAI cache writes (#286 / #241) ────────────────────────────────────

def _sol(app):
    # Mirrors backend/config.py gpt-5.6-sol: $4/M input, 0.1x cached,
    # long-context tier above 272k at 2x input / 1.5x output.
    app.config["SUPPORTED_MODELS"]["gpt-5.6-sol"] = {
        "provider": "openai", "api_model": "gpt-5.6-sol",
        "input_price_per_mtok": 4.00, "output_price_per_mtok": 20.00,
        "cached_input_multiplier": 0.10,
        "long_context_threshold": 272_000,
        "long_context_input_multiplier": 2.0,
        "long_context_output_multiplier": 1.5,
    }


def test_openai_cache_write_subset_no_double_billing(app):
    """OpenAI's cache_write_tokens is a SUBSET of input_tokens (like
    cached_tokens), so the written tokens leave the full-price remainder
    and bill once at 1.25x — never 1.0x + 1.25x."""
    _sol(app)
    with app.app_context():
        # 100k prompt, all of it written to the cache (first turn).
        cost = calculate_llm_cost_microdollars(
            "gpt-5.6-sol", 100_000, 0, cache_write_subset_tokens=100_000)
        assert cost == round(100_000 * 4 * 1.25)   # $0.50, not $1.125
        # Later turn: 80k served from cache, 15k written, 5k ordinary.
        mixed = calculate_llm_cost_microdollars(
            "gpt-5.6-sol", 100_000, 1_000,
            cached_input_tokens=80_000, cache_write_subset_tokens=15_000)
        assert mixed == round(5_000 * 4 + 80_000 * 4 * 0.1
                              + 15_000 * 4 * 1.25 + 1_000 * 20)
        # The two subsets together can never exceed input_tokens.
        capped = calculate_llm_cost_microdollars(
            "gpt-5.6-sol", 100, 0,
            cached_input_tokens=80, cache_write_subset_tokens=500)
        assert capped == round(80 * 4 * 0.1 + 20 * 4 * 1.25)
        # Pre-5.6 models report no writes: nothing changes for them.
        assert calculate_llm_cost_microdollars(
            "gpt-5.5", 1_000, 0) == round(1_000 * 5)


def test_openai_cache_write_long_context(app):
    """The long-context surcharge applies to the input price BEFORE the
    write multiplier: a 300k first-turn write on Sol is $8/M * 1.25."""
    _sol(app)
    with app.app_context():
        cost = calculate_llm_cost_microdollars(
            "gpt-5.6-sol", 300_000, 0, cache_write_subset_tokens=300_000)
        assert cost == round(300_000 * 4 * 2.0 * 1.25)


def test_cache_write_multiplier_override(app):
    """A model entry may override the 1.25x write premium (#241), on both
    the OpenAI subset and the Anthropic disjoint counter."""
    _sol(app)
    app.config["SUPPORTED_MODELS"]["gpt-5.6-sol"]["cache_write_multiplier"] = 1.5
    with app.app_context():
        assert calculate_llm_cost_microdollars(
            "gpt-5.6-sol", 1_000, 0, cache_write_subset_tokens=1_000
        ) == round(1_000 * 4 * 1.5)
        assert calculate_llm_cost_microdollars(
            "gpt-5.6-sol", 0, 0, cache_write_tokens=1_000
        ) == round(1_000 * 4 * 1.5)


def test_llm_cost_from_response_reads_every_counter(app):
    """The response-dict helper feeds every provider counter through, so
    single-shot pipeline calls price caching like conversation turns."""
    _sol(app)
    with app.app_context():
        openai_resp = {"input_tokens": 100_000, "output_tokens": 500,
                       "cached_tokens": 60_000,
                       "cache_write_subset_tokens": 40_000}
        assert llm_cost_from_response("gpt-5.6-sol", openai_resp) == (
            calculate_llm_cost_microdollars(
                "gpt-5.6-sol", 100_000, 500, cached_input_tokens=60_000,
                cache_write_subset_tokens=40_000))
        anthropic_resp = {"input_tokens": 1_000, "output_tokens": 10,
                          "cache_read_input_tokens": 90_000,
                          "cache_creation_input_tokens": 9_000}
        assert llm_cost_from_response("claude-opus-4.6", anthropic_resp) == (
            calculate_llm_cost_microdollars(
                "claude-opus-4.6", 1_000, 10, cache_read_tokens=90_000,
                cache_write_tokens=9_000))
        # batch: explicit flag wins, else the dict's own flag.
        batched = dict(anthropic_resp, batch=True)
        assert llm_cost_from_response("claude-opus-4.6", batched) == (
            calculate_llm_cost_microdollars(
                "claude-opus-4.6", 1_000, 10, batch=True,
                cache_read_tokens=90_000, cache_write_tokens=9_000))
        assert llm_cost_from_response(
            "claude-opus-4.6", batched, batch=False) == (
            llm_cost_from_response("claude-opus-4.6", anthropic_resp))


def test_call_openai_surfaces_cache_write_subset(app, monkeypatch):
    """_call_openai must pull input_tokens_details.cache_write_tokens out
    of the Responses usage under the subset key; a renamed field would
    silently drop OpenAI write billing back to 1.0x (#286)."""
    monkeypatch.delitem(sys.modules, "backend.llm_providers", raising=False)
    import backend.llm_providers as providers

    class FakeDetails:
        cached_tokens = 2815
        cache_write_tokens = 3000

    class FakeUsage:
        input_tokens = 6018
        output_tokens = 12
        total_tokens = 6030
        input_tokens_details = FakeDetails()

    class FakeBlock:
        type = "output_text"
        text = "hi"

    class FakeItem:
        type = "message"
        content = [FakeBlock()]

    class FakeResponse:
        output = [FakeItem()]
        usage = FakeUsage()
        status = "completed"
        incomplete_details = None

    class FakeResponses:
        def create(self, **kwargs):
            return FakeResponse()

    class FakeClient:
        def __init__(self, api_key=None):
            self.responses = FakeResponses()

    monkeypatch.setattr(providers, "OpenAI", FakeClient)
    with app.app_context():
        result = providers.LLMProvider._call_openai(
            "gpt-5.6-sol", [{"role": "user", "content": "x"}], "k")
    assert result["input_tokens"] == 6018
    assert result["cached_tokens"] == 2815
    assert result["cache_write_subset_tokens"] == 3000
    # Never the Anthropic-style disjoint key: that would double-bill.
    assert "cache_creation_input_tokens" not in result


def test_llm_cost_log_fields_unifies_columns_across_providers(app):
    """One helper fills the APICostLog token columns the same way for
    every request_type: full-prompt input_tokens, read/write unified."""
    _sol(app)
    with app.app_context():
        anthropic_resp = {"input_tokens": 1_000, "output_tokens": 10,
                          "cache_read_input_tokens": 90_000,
                          "cache_creation_input_tokens": 9_000}
        fields = llm_cost_log_fields("claude-opus-4.6", anthropic_resp)
        assert fields == {
            "input_tokens": 100_000, "output_tokens": 10,
            "cache_read_tokens": 90_000, "cache_write_tokens": 9_000,
            "cost_microdollars": llm_cost_from_response(
                "claude-opus-4.6", anthropic_resp)}
        openai_resp = {"input_tokens": 6_018, "output_tokens": 5,
                       "cached_tokens": 2_815,
                       "cache_write_subset_tokens": 3_000, "batch": True}
        fields = llm_cost_log_fields("gpt-5.6-sol", openai_resp)
        assert fields["input_tokens"] == 6_018  # already the full prompt
        assert fields["cache_read_tokens"] == 2_815
        assert fields["cache_write_tokens"] == 3_000
        assert fields["cost_microdollars"] == llm_cost_from_response(
            "gpt-5.6-sol", openai_resp)  # batch flag honored
        assert fields["cost_microdollars"] < llm_cost_from_response(
            "gpt-5.6-sol", openai_resp, batch=False)
        # A bare-count dict (no cache keys) still fills every column.
        assert llm_cost_log_fields("claude-opus-4.6", {
            "input_tokens": 3, "output_tokens": 4}) == {
            "input_tokens": 3, "output_tokens": 4, "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "cost_microdollars": calculate_llm_cost_microdollars(
                "claude-opus-4.6", 3, 4)}

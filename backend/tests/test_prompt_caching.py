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
from backend.utils.cost import calculate_llm_cost_microdollars  # noqa: E402

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


def test_gated_voice_tools_warm_matches_generation():
    # The pre-warm and generation BOTH build their tool list via
    # gated_voice_tools, so the cached tool prefix is byte-identical. A
    # divergence here (warm keeping semantic_search while generation drops it)
    # silently busts the whole cache — that was the read=0 bug.
    names = lambda ts: [t["name"] for t in ts]  # noqa: E731
    off = gated_voice_tools({"SEMANTIC_SEARCH_AGENTIC": False})
    on = gated_voice_tools({"SEMANTIC_SEARCH_AGENTIC": True})
    assert "semantic_search" not in names(off)        # dark (prod default)
    assert "semantic_search" in names(on)             # enabled (staging)
    assert names(on) == names(VOICE_TOOLS)
    assert names(gated_voice_tools({})) == names(off)  # unset == dark
    # Identical config -> identical list for both call sites (the invariant).
    cfg = {"SEMANTIC_SEARCH_AGENTIC": False}
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
            app.config, node) == "rendered bytes"

        # Editing the node (updated_at changes) busts the key naturally
        from datetime import datetime
        node.updated_at = datetime(2030, 1, 1)
        _db.session.commit()
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
    sys.modules.pop("backend.llm_providers", None)
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

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
    User, Node, NodeContextArtifact, UserTodo,
)
import backend.utils.prompt_cache as prompt_cache  # noqa: E402
from backend.utils.cost import calculate_llm_cost_microdollars  # noqa: E402

_GLUE = ("backend.celery_app", "backend.llm_providers",
         "backend.tasks.llm_completion")
_saved_glue = {k: sys.modules.get(k) for k in _GLUE}
sys.modules["backend.celery_app"] = MagicMock()
sys.modules["backend.llm_providers"] = MagicMock()
sys.modules.pop("backend.tasks.llm_completion", None)
from backend.tasks.llm_completion import render_system_message  # noqa: E402
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

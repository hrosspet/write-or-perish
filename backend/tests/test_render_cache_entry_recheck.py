"""The cached render of a conversation's system prompt (#192) is read as
a miss once an entry it holds has been deleted, set to AI usage None,
or (someone else's reply) made unreadable to the user, whatever the
render's training-key verdict. An unchanged conversation keeps hitting
with the same bytes, and an entry that was already left out when the
render was stored changes nothing.
"""
import json
import os
import sys
from datetime import datetime, timedelta
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
from backend.models import User, Node  # noqa: E402

T0 = datetime(2026, 5, 1, 9, 0, 0)
VARIANT = "a1uchats1e1"


@pytest.fixture
def app():
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["TESTING"] = True
    _db.init_app(app)
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()


def _user(name):
    u = User(username=name, twitter_id=f"{name}-twitter-id")
    _db.session.add(u)
    _db.session.commit()
    return u


@pytest.fixture
def alice(app):
    return _user("alice")


@pytest.fixture
def bob(app):
    return _user("bob")


_clock = {"n": 0}


def _node(user, content, parent=None, privacy="private", ai_usage="chat"):
    _clock["n"] += 1
    node = Node(user_id=user.id, human_owner_id=user.id,
                parent_id=parent.id if parent else None, node_type="user",
                privacy_level=privacy, ai_usage=ai_usage, token_count=5,
                created_at=T0 + timedelta(minutes=_clock["n"]))
    node.set_content(content)
    _db.session.add(node)
    _db.session.commit()
    return node


class _FakeRedis:
    def __init__(self):
        self.store = {}

    def setex(self, key, ttl, value):
        self.store[key] = value

    def get(self, key):
        return self.store.get(key)


@pytest.fixture
def cache(monkeypatch):
    import backend.utils.prompt_cache as pc
    fake = _FakeRedis()
    monkeypatch.setattr(pc, "_client", lambda config: fake)
    monkeypatch.setattr(pc, "fake", fake, raising=False)
    return pc


def _store(cache, system_node, text, entries, user=None, unlicensed="chat"):
    cache.store_render({}, system_node, text, VARIANT,
                       unlicensed=unlicensed,
                       rows={"node": [e.id for e in entries]},
                       user_id=user.id if user else None)


@pytest.mark.parametrize("change", ["none", "delete"])
@pytest.mark.parametrize("verdict", ["chat", None])
def test_withdrawn_entry_reads_as_a_miss(app, alice, cache, change, verdict):
    entry = _node(alice, "ENTRY", ai_usage="train")
    system_node = _node(alice, "SYSTEM PROMPT {user_recent_raw}")
    _store(cache, system_node, "PROMPT + ENTRY", [entry], alice,
           unlicensed=verdict)
    assert cache.get_cached_render({}, system_node, VARIANT,
                                   user_id=alice.id) is not None
    if change == "none":
        entry.ai_usage = "none"
    else:
        entry.deleted_at = T0 + timedelta(days=2)
    _db.session.commit()
    assert cache.get_cached_render({}, system_node, VARIANT,
                                   user_id=alice.id) is None


def test_reply_made_private_by_its_author_reads_as_a_miss(
        app, alice, bob, cache):
    thread = _node(alice, "alice's public piece", privacy="public")
    reply = _node(bob, "BOB'S REPLY", parent=thread, privacy="public")
    system_node = _node(alice, "SYSTEM PROMPT {user_recent_raw}")
    _store(cache, system_node, "PROMPT + BOB'S REPLY", [thread, reply], alice)
    assert cache.get_cached_render({}, system_node, VARIANT,
                                   user_id=alice.id) is not None
    reply.privacy_level = "private"
    _db.session.commit()
    assert cache.get_cached_render({}, system_node, VARIANT,
                                   user_id=alice.id) is None


def test_unchanged_conversation_keeps_its_bytes(app, alice, bob, cache):
    entry = _node(alice, "ENTRY")
    thread = _node(alice, "piece", privacy="public")
    reply = _node(bob, "reply", parent=thread, privacy="public")
    system_node = _node(alice, "SYSTEM PROMPT {user_recent_raw}")
    text = "PROMPT + ENTRY + reply\n"
    _store(cache, system_node, text, [entry, thread, reply], alice)
    for _ in range(3):
        hit = cache.get_cached_render({}, system_node, VARIANT,
                                      user_id=alice.id)
        assert hit is not None and hit.text == text


def test_entry_left_out_when_stored_changes_nothing(app, alice, bob, cache):
    # Tracked but not rendered as text: a quote of an entry at None, a
    # tombstone, someone else's private reply.
    blocked = _node(alice, "BLOCKED", ai_usage="none")
    gone = _node(alice, "GONE")
    gone.deleted_at = T0
    _db.session.commit()
    private_reply = _node(bob, "PRIVATE", parent=gone)
    entry = _node(alice, "ENTRY")
    system_node = _node(alice, "SYSTEM PROMPT {user_recent_raw}")
    _store(cache, system_node, "PROMPT", [blocked, gone, private_reply,
                                          entry], alice)
    stored = json.loads(cache.fake.store[next(iter(cache.fake.store))])
    assert stored["nodes"] == [entry.id]
    assert cache.get_cached_render({}, system_node, VARIANT,
                                   user_id=alice.id) is not None


def test_default_reader_is_the_prompts_owner(app, alice, cache):
    entry = _node(alice, "ENTRY")
    system_node = _node(alice, "SYSTEM PROMPT {user_recent_raw}")
    _store(cache, system_node, "PROMPT + ENTRY", [entry])
    assert cache.get_cached_render({}, system_node, VARIANT) is not None
    entry.ai_usage = "none"
    _db.session.commit()
    assert cache.get_cached_render({}, system_node, VARIANT) is None

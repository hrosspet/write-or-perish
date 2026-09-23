"""The user's own rows reach the training key only when they are licensed
for it (#326).

Follow-up to #325 (test_training_key_payload.py), which covers what the
chain points at. Here: the user's own context rows, by every door they
reach a payload through — the prompt placeholders ({user_memory},
{user_todo}, {user_profile}, ...), the #192 cached system render that
replays them on later turns, read_artifact / read_todo mid-turn, the
update_artifact echo, the {user_export} / {user_recent_raw} archive, and
the voice pre-warm. Each row is judged by its own ai_usage; one that is
not 'train' keeps the payload off the training key (strict, Peter
2026-09-21). Assertions are on the key the provider was actually handed.
"""
from datetime import datetime

import pytest

from backend.tests.test_retrieval_loop import (  # noqa: F401 (fixture)
    app, _llm_task_mod, generate_llm_response, _FakeSelf, _mk_user,
    _resp, _fresh, _mk_artifact, _mk_todo, _ScriptedProvider,
)
from backend.tests.test_training_key_payload import (  # noqa: F401
    keys, _keys_used, _payload, _train_chain, _reference, _entry,
)
from backend.extensions import db as _db
from backend.models import (
    Node, User, UserArtifact, UserProfile, UserRecentContext,
)

OWN = "OWN ROW TEXT"


def _train_user(alice):
    alice.default_ai_usage = "train"
    _db.session.commit()


def _set_prompt(system, text):
    """The system node renders its pinned prompt artifact."""
    system.get_artifact("prompt").set_content(text)
    _db.session.commit()


def _next_turn(alice, prev_llm_id, text="and then?"):
    """A follow-up user message + placeholder under the previous reply."""
    reply = _fresh(prev_llm_id)
    follow = Node(user_id=alice.id, human_owner_id=alice.id,
                  parent_id=reply.id, node_type="user",
                  privacy_level="private", ai_usage="train")
    follow.set_content(text)
    _db.session.add(follow)
    _db.session.flush()
    llm_user = User.query.filter_by(username="gpt-5").first()
    nxt = Node(user_id=llm_user.id, human_owner_id=alice.id,
               parent_id=follow.id, node_type="llm", llm_model="gpt-5",
               llm_task_status="pending", privacy_level="private",
               ai_usage="train")
    nxt.set_content("[LLM response generation pending...]")
    _db.session.add(nxt)
    _db.session.commit()
    return follow, nxt


def _seed_row(alice, placeholder, usage):
    """One own row, stamped *usage*, that *placeholder* resolves to and
    whose text is OWN."""
    kind = placeholder.strip("{}").replace("user_", "", 1)
    if kind in ("memory", "scratchpad", "intentions", "ai_preferences"):
        _mk_artifact(alice.id, kind, OWN, ai_usage=usage)
    elif kind == "artifacts_index":
        # The index lists a custom artifact by its title and description.
        _mk_artifact(alice.id, "reading-list", "the list itself",
                     title=OWN, ai_usage=usage)
    elif kind == "todo":
        _mk_todo(alice.id, OWN, ai_usage=usage)
    elif kind == "profile":
        row = UserProfile(user_id=alice.id, generated_by="test",
                          ai_usage=usage)
        row.set_content(OWN)
        _db.session.add(row)
    elif kind == "recent":
        row = UserRecentContext(user_id=alice.id, generated_by="test",
                                ai_usage=usage)
        row.set_content(OWN)
        _db.session.add(row)
    elif kind == "recent_raw":
        # An older entry of the archive's recent window.
        old = _entry(alice, OWN, usage)
        old.created_at = datetime(2020, 1, 1)
    else:  # pragma: no cover
        raise AssertionError(placeholder)
    _db.session.commit()


PLACEHOLDERS = [
    "{user_memory}", "{user_scratchpad}", "{user_intentions}",
    "{user_ai_preferences}", "{user_artifacts_index}", "{user_todo}",
    "{user_profile}", "{user_recent}", "{user_recent_raw}",
]


# ── the placeholders ─────────────────────────────────────────────────────

@pytest.mark.parametrize("placeholder", PLACEHOLDERS)
@pytest.mark.parametrize("usage, expected", [
    ("train", "sk-train"),
    ("chat", "sk-chat"),
])
def test_a_placeholder_row_is_judged_by_its_own_usage(app, keys, placeholder, usage, expected):  # noqa: F811
    alice, system, user_node, llm_node = _train_chain("textmode")
    _train_user(alice)
    _set_prompt(system, f"system prompt body\n{placeholder}")
    _seed_row(alice, placeholder, usage)

    _ScriptedProvider.reset([_resp("Hello.")])
    generate_llm_response(_FakeSelf(), user_node.id, llm_node.id, "gpt-5",
                          alice.id, source_mode="textmode")

    assert OWN in _payload(0)                  # the row really went out
    assert _keys_used() == [expected]


def test_a_placeholder_in_a_user_message_reports_too(app, keys):  # noqa: F811
    """Not only the system prompt: an ad-hoc {user_memory} in a message."""
    alice, system, user_node, llm_node = _train_chain("textmode")
    _train_user(alice)
    user_node.set_content("what do you remember? {user_memory}")
    _db.session.commit()
    _mk_artifact(alice.id, "memory", OWN, ai_usage="chat")
    _db.session.commit()

    _ScriptedProvider.reset([_resp("This.")])
    generate_llm_response(_FakeSelf(), user_node.id, llm_node.id, "gpt-5",
                          alice.id, source_mode="textmode")

    assert OWN in _payload(0)
    assert _keys_used() == ["sk-chat"]


# ── the #192 cached system render ───────────────────────────────────────

class _FakeRedis:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value


@pytest.mark.parametrize("usage, expected", [
    ("train", "sk-train"),
    ("chat", "sk-chat"),
])
def test_a_cached_system_render_replays_its_verdict(app, keys, monkeypatch, usage, expected):  # noqa: F811
    """Turn two is served from the cache and resolves nothing, so the
    verdict of the rows inside the render has to come with it."""
    import backend.utils.prompt_cache as prompt_cache
    fake = _FakeRedis()
    monkeypatch.setattr(prompt_cache, "_client", lambda config: fake)

    alice, system, user_node, llm_node = _train_chain("voice")
    _train_user(alice)
    _set_prompt(system, "system prompt body\n{user_memory}")
    _mk_artifact(alice.id, "memory", OWN, ai_usage=usage)
    _db.session.commit()

    _ScriptedProvider.reset([_resp("Turn one.")])
    generate_llm_response(_FakeSelf(), user_node.id, llm_node.id, "gpt-5",
                          alice.id, source_mode="voice")
    assert _keys_used() == [expected]
    assert len(fake.store) == 1                 # the render was cached

    def _no_resolution(*a, **kw):
        raise AssertionError("turn two re-resolved the artifacts")
    monkeypatch.setattr(_llm_task_mod, "get_user_artifacts_context",
                        _no_resolution)
    follow, reply2 = _next_turn(alice, llm_node.id)
    _ScriptedProvider.reset([_resp("Turn two.")])
    generate_llm_response(_FakeSelf(), follow.id, reply2.id, "gpt-5",
                          alice.id, source_mode="voice")

    assert _fresh(reply2.id).llm_task_status == "completed"
    assert OWN in _payload(0)                   # served from the cache
    assert _keys_used() == [expected]


# ── read_artifact / read_todo mid-turn ───────────────────────────────────

@pytest.mark.parametrize("tool, kind, usage, follow_up_key", [
    ("read_artifact", "memory", "train", "sk-train"),
    ("read_artifact", "memory", "chat", "sk-chat"),
    ("read_todo", None, "train", "sk-train"),
    ("read_todo", None, "chat", "sk-chat"),
    # The digest summarizes other people's writing: never the training
    # key, whatever its row says.
    ("read_artifact", UserArtifact.EXTERNAL_DIGEST_KIND, "train", "sk-chat"),
])
def test_a_tool_pull_of_an_own_row_moves_only_its_continuation(app, keys, tool, kind, usage, follow_up_key):  # noqa: F811
    alice, system, user_node, llm_node = _train_chain("textmode")
    _train_user(alice)
    if tool == "read_todo":
        _mk_todo(alice.id, OWN, ai_usage=usage)
    else:
        _mk_artifact(alice.id, kind, OWN, ai_usage=usage)
    _db.session.commit()

    _ScriptedProvider.reset([
        _resp("Pulling it.", tool_calls=[{
            "id": "t1", "name": tool,
            "input": {"kind": kind} if kind else {}}]),
        _resp("Done."),
    ])
    generate_llm_response(_FakeSelf(), user_node.id, llm_node.id, "gpt-5",
                          alice.id, source_mode="textmode")

    assert OWN not in _payload(0)
    assert OWN in _payload(1)
    assert _keys_used() == ["sk-train", follow_up_key]


# ── the update_artifact echo ─────────────────────────────────────────────

@pytest.mark.parametrize("previous_usage, follow_up_key", [
    (None, "sk-train"),        # a creation echoes only the new row
    ("train", "sk-train"),
    ("chat", "sk-chat"),       # the diff's context lines are the old row
])
def test_the_update_echo_reports_both_versions(app, keys, previous_usage, follow_up_key):  # noqa: F811
    alice, system, user_node, llm_node = _train_chain("textmode")
    _train_user(alice)
    if previous_usage:
        _mk_artifact(alice.id, "memory", "line one\nline two",
                     ai_usage=previous_usage)
        _db.session.commit()

    _ScriptedProvider.reset([
        _resp("Noting that.", tool_calls=[{
            "id": "t1", "name": "update_artifact",
            "input": {"kind": "memory",
                      "updated_content": "line one\nline two\nNEW LINE"}}]),
        _resp("Noted."),
    ])
    generate_llm_response(_FakeSelf(), user_node.id, llm_node.id, "gpt-5",
                          alice.id, source_mode="textmode")

    assert "NEW LINE" in _payload(1)            # the echo went out
    assert _keys_used() == ["sk-train", follow_up_key]
    # The writer stamped the owner's default (#326), not 'chat'.
    assert UserArtifact.latest_for(alice.id, "memory").ai_usage == "train"


# ── {user_export} ────────────────────────────────────────────────────────

def _export_thread(user_node):
    user_node.set_content(
        "Look back over this: {user_export?max_export_tokens=5000}")
    _db.session.commit()


def _archive(alice, text, usage):
    old = _entry(alice, text, usage)
    old.created_at = datetime(2020, 1, 1)
    _db.session.commit()
    return old


@pytest.mark.parametrize("usage, expected", [
    ("train", "sk-train"),
    ("chat", "sk-chat"),
])
def test_one_chat_entry_in_the_export_takes_the_thread_off_the_train_key(app, keys, usage, expected):  # noqa: F811
    alice, system, user_node, llm_node = _train_chain("textmode")
    _train_user(alice)
    _archive(alice, "ARCHIVE ENTRY ONE", "train")
    _archive(alice, "ARCHIVE ENTRY TWO", usage)
    _export_thread(user_node)

    _ScriptedProvider.reset([_resp("Looked.")])
    generate_llm_response(_FakeSelf(), user_node.id, llm_node.id, "gpt-5",
                          alice.id, source_mode="textmode")

    assert "ARCHIVE ENTRY TWO" in _payload(0)
    assert _keys_used() == [expected]


def test_someone_elses_chat_reply_in_the_export_takes_it_to_chat(app, keys):  # noqa: F811
    """The export carries other people's accessible replies in the
    user's threads; each is judged by its own setting."""
    alice, system, user_node, llm_node = _train_chain("textmode")
    _train_user(alice)
    root = _archive(alice, "MY PUBLIC POST", "train")
    root.privacy_level = "public"
    bob = _mk_user("bob", approved=True)
    reply = _entry(bob, "BOB'S REPLY", "chat", privacy_level="public")
    reply.parent_id = root.id
    reply.created_at = datetime(2020, 1, 2)
    _db.session.commit()
    _export_thread(user_node)

    _ScriptedProvider.reset([_resp("Looked.")])
    generate_llm_response(_FakeSelf(), user_node.id, llm_node.id, "gpt-5",
                          alice.id, source_mode="textmode")

    assert "BOB'S REPLY" in _payload(0)
    assert _keys_used() == ["sk-chat"]


def test_a_saved_reference_quoted_in_the_export_takes_it_to_chat(app, keys):  # noqa: F811
    alice, system, user_node, llm_node = _train_chain("textmode")
    _train_user(alice)
    ref = _reference(alice.id, "SOMEONE ELSE'S TWEET")
    _archive(alice, "My note on {quote_ext:%d}" % ref.id, "train")
    _export_thread(user_node)

    _ScriptedProvider.reset([_resp("Looked.")])
    generate_llm_response(_FakeSelf(), user_node.id, llm_node.id, "gpt-5",
                          alice.id, source_mode="textmode")

    assert "SOMEONE ELSE'S TWEET" in _payload(0)
    assert _keys_used() == ["sk-chat"]


def test_the_export_reports_quoted_entries_by_their_own_usage(app, keys):  # noqa: F811
    alice, system, user_node, llm_node = _train_chain("textmode")
    _train_user(alice)
    # Bob's public entry is no part of alice's threads, so it reaches
    # the export only as a quote the resolver embeds.
    bob = _mk_user("bob", approved=True)
    quoted = _entry(bob, "THE QUOTED CHAT ENTRY", "chat",
                    privacy_level="public")
    quoted.created_at = datetime(2019, 1, 1)
    _archive(alice, "Recalling {quote:%d}" % quoted.id, "train")
    _export_thread(user_node)

    _ScriptedProvider.reset([_resp("Looked.")])
    generate_llm_response(_FakeSelf(), user_node.id, llm_node.id, "gpt-5",
                          alice.id, source_mode="textmode")

    assert "THE QUOTED CHAT ENTRY" in _payload(0)
    assert _keys_used() == ["sk-chat"]


# ── the voice pre-warm ───────────────────────────────────────────────────

@pytest.mark.parametrize("usage, expected", [
    ("train", "ant-train"),
    ("chat", "ant-chat"),
])
def test_the_pre_warm_goes_out_on_the_key_its_render_allows(app, monkeypatch, usage, expected):  # noqa: F811
    import backend.utils.prompt_cache as prompt_cache
    fake = _FakeRedis()
    monkeypatch.setattr(prompt_cache, "_client", lambda config: fake)
    cfg = app.config
    monkeypatch.setitem(cfg, "ANTHROPIC_API_KEY_TRAIN", "ant-train")
    monkeypatch.setitem(cfg, "ANTHROPIC_API_KEY_CHAT", "ant-chat")
    monkeypatch.setitem(cfg, "SUPPORTED_MODELS", {
        **cfg["SUPPORTED_MODELS"],
        "claude-test": {"provider": "anthropic", "api_model": "claude-x"},
    })
    sent = []

    def _call_anthropic(api_model, messages, api_key, **kw):
        sent.append(api_key)
        raise RuntimeError("stop after capturing the key")
    monkeypatch.setattr(_ScriptedProvider, "_call_anthropic",
                        staticmethod(_call_anthropic), raising=False)

    alice, system, user_node, llm_node = _train_chain("voice")
    _train_user(alice)
    _set_prompt(system, "system prompt body\n{user_memory}")
    _mk_artifact(alice.id, "memory", OWN, ai_usage=usage)
    _db.session.commit()

    _llm_task_mod.prewarm_anthropic_cache(system.id, alice.id, "claude-test")
    # Served from the cache the second time: the stored verdict decides.
    _llm_task_mod.prewarm_anthropic_cache(system.id, alice.id, "claude-test")

    assert sent == [expected, expected]
    assert len(fake.store) == 1

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


# ── #326 review round 1 ──────────────────────────────────────────────────

def _cached_voice_thread(monkeypatch, prompt, seed):
    """A train user's voice thread whose system render is cached on turn
    one; returns what turn two needs."""
    import backend.utils.prompt_cache as prompt_cache
    fake = _FakeRedis()
    monkeypatch.setattr(prompt_cache, "_client", lambda config: fake)
    alice, system, user_node, llm_node = _train_chain("voice")
    _train_user(alice)
    _set_prompt(system, prompt)
    rows = seed(alice)
    _ScriptedProvider.reset([_resp("Turn one.")])
    generate_llm_response(_FakeSelf(), user_node.id, llm_node.id, "gpt-5",
                          alice.id, source_mode="voice")
    assert _keys_used() == ["sk-train"]
    assert len(fake.store) == 1                 # cached, verdict: train
    return alice, llm_node, rows, fake


def _turn_two(alice, llm_node):
    follow, reply2 = _next_turn(alice, llm_node.id)
    _ScriptedProvider.reset([_resp("Turn two.")])
    generate_llm_response(_FakeSelf(), follow.id, reply2.id, "gpt-5",
                          alice.id, source_mode="voice")
    assert _fresh(reply2.id).llm_task_status == "completed"


def test_an_entry_switched_to_chat_after_caching_takes_turn_two_to_chat(app, keys, monkeypatch):  # noqa: F811
    """Finding 1: the cache key does not change when an entry inside the
    {user_recent_raw} window loses its licence, so a hit re-checks the
    rows its 'train' verdict rests on and rebuilds the render."""
    alice, llm_node, entry, fake = _cached_voice_thread(
        monkeypatch, "system prompt body\n{user_recent_raw}",
        lambda alice: _archive(alice, "RAW ENTRY", "train"))

    entry.ai_usage = "chat"                     # the node editor's switch
    _db.session.commit()
    _turn_two(alice, llm_node)

    assert "RAW ENTRY" in _payload(0)
    assert _keys_used() == ["sk-chat"]


def test_an_entry_switched_to_none_after_caching_is_no_longer_sent(app, keys, monkeypatch):  # noqa: F811
    """The re-render also honors an opt-out: a 'none' entry leaves the
    window instead of riding along in the cached text."""
    alice, llm_node, entry, fake = _cached_voice_thread(
        monkeypatch, "system prompt body\n{user_recent_raw}",
        lambda alice: _archive(alice, "RAW ENTRY", "train"))

    entry.ai_usage = "none"
    _db.session.commit()
    _turn_two(alice, llm_node)

    assert "RAW ENTRY" not in _payload(0)
    assert _keys_used() == ["sk-train"]


def test_a_profile_switched_to_chat_after_caching_takes_turn_two_to_chat(app, keys, monkeypatch):  # noqa: F811
    def seed(alice):
        _seed_row(alice, "{user_profile}", "train")
        return UserProfile.query.filter_by(user_id=alice.id).one()
    alice, llm_node, profile, fake = _cached_voice_thread(
        monkeypatch, "system prompt body\n{user_profile}", seed)

    profile.ai_usage = "chat"                   # PUT /api/profile/<id>
    _db.session.commit()
    _turn_two(alice, llm_node)

    assert OWN in _payload(0)
    assert _keys_used() == ["sk-chat"]


def test_an_unchanged_train_cache_hit_is_still_served_from_the_cache(app, keys, monkeypatch):  # noqa: F811
    alice, llm_node, entry, fake = _cached_voice_thread(
        monkeypatch, "system prompt body\n{user_recent_raw}",
        lambda alice: _archive(alice, "RAW ENTRY", "train"))

    def _no_render(*a, **kw):
        raise AssertionError("turn two re-rendered the recent raw window")
    monkeypatch.setattr(_llm_task_mod, "get_user_recent_raw_content",
                        _no_render)
    _turn_two(alice, llm_node)

    assert "RAW ENTRY" in _payload(0)
    assert _keys_used() == ["sk-train"]


def test_a_train_owners_memory_written_in_a_chat_thread_stays_chat(app, keys):  # noqa: F811
    """Finding 3: memory the model writes in a thread the user set to
    Chat must not reach another thread on the training key."""
    from backend.tests.test_training_key_payload import _plain_thread
    alice, system, user_node, llm_node = _train_chain("textmode")
    _train_user(alice)
    user_node.ai_usage = "chat"                 # this thread: Chat
    _db.session.commit()
    _ScriptedProvider.reset([
        _resp("Noting that.", tool_calls=[{
            "id": "t1", "name": "update_artifact",
            "input": {"kind": "memory",
                      "updated_content": "FROM THE CHAT THREAD"}}]),
        _resp("Noted."),
    ])
    generate_llm_response(_FakeSelf(), user_node.id, llm_node.id, "gpt-5",
                          alice.id, source_mode="textmode")
    assert UserArtifact.latest_for(alice.id, "memory").ai_usage == "chat"

    llm_user = User.query.filter_by(username="gpt-5").first()
    note, llm = _plain_thread(alice, llm_user,
                              "What do you remember? {user_memory}")
    _ScriptedProvider.reset([_resp("That.")])
    generate_llm_response(_FakeSelf(), note.id, llm.id, "gpt-5", alice.id,
                          source_mode=None)

    assert "FROM THE CHAT THREAD" in _payload(0)
    assert _keys_used() == ["sk-chat"]


def test_a_none_default_user_in_a_chat_thread_can_read_their_memory_next_turn(app, keys):  # noqa: F811
    """Finding 3: a 'none' default must not make the memory written in a
    thread the user set to Chat unreadable on the next turn."""
    alice, system, user_node, llm_node = _train_chain("textmode")
    alice.default_ai_usage = "none"
    user_node.ai_usage = "chat"
    system.ai_usage = "chat"
    _db.session.commit()
    _set_prompt(system, "system prompt body\n{user_memory}")
    _ScriptedProvider.reset([
        _resp("Noting that.", tool_calls=[{
            "id": "t1", "name": "update_artifact",
            "input": {"kind": "memory",
                      "updated_content": "REMEMBER THIS"}}]),
        _resp("Noted."),
    ])
    generate_llm_response(_FakeSelf(), user_node.id, llm_node.id, "gpt-5",
                          alice.id, source_mode="textmode")
    memory = UserArtifact.latest_for(alice.id, "memory")
    assert memory.ai_usage == "chat"

    continuation = _fresh(_fresh(llm_node.id).continuation_node_id)
    follow, reply2 = _next_turn(alice, continuation.id)
    follow.ai_usage = "chat"
    _db.session.commit()
    _ScriptedProvider.reset([_resp("I remember.")])
    generate_llm_response(_FakeSelf(), follow.id, reply2.id, "gpt-5",
                          alice.id, source_mode="textmode")

    assert "REMEMBER THIS" in _payload(0)
    assert _keys_used() == ["sk-chat"]


def test_an_artifact_whose_placeholder_is_absent_does_not_report(app, keys, monkeypatch):  # noqa: F811
    """Finding 4: a prompt carrying only {user_intentions} never sends
    the memory, so a 'chat' memory does not move it — not on turn one,
    not from the cache on turn two."""
    def seed(alice):
        _mk_artifact(alice.id, "memory", "THE MEMORY", ai_usage="chat")
        _mk_artifact(alice.id, "intentions", OWN, ai_usage="train")
        _db.session.commit()
    alice, llm_node, _, fake = _cached_voice_thread(
        monkeypatch, "system prompt body\n{user_intentions}", seed)
    _turn_two(alice, llm_node)

    assert OWN in _payload(0)
    assert "THE MEMORY" not in _payload(0)
    assert _keys_used() == ["sk-train"]


def test_the_pre_warm_stores_the_same_filtered_verdict(app, monkeypatch):  # noqa: F811
    """Finding 4, pre-warm side: it must not cache an unfiltered 'chat'
    verdict that would keep every later turn off the training key."""
    import backend.utils.prompt_cache as prompt_cache
    fake = _FakeRedis()
    monkeypatch.setattr(prompt_cache, "_client", lambda config: fake)
    alice, system, user_node, llm_node = _train_chain("voice")
    _train_user(alice)
    _set_prompt(system, "system prompt body\n{user_intentions}")
    _mk_artifact(alice.id, "memory", "THE MEMORY", ai_usage="chat")
    _mk_artifact(alice.id, "intentions", OWN, ai_usage="train")
    _db.session.commit()
    monkeypatch.setitem(app.config, "SUPPORTED_MODELS", {
        **app.config["SUPPORTED_MODELS"],
        "claude-test": {"provider": "anthropic", "api_model": "claude-x"},
    })
    monkeypatch.setattr(_ScriptedProvider, "_call_anthropic",
                        staticmethod(lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("stop"))), raising=False)

    _llm_task_mod.prewarm_anthropic_cache(system.id, alice.id, "claude-test")
    cached = prompt_cache.get_cached_render(
        app.config, system, _llm_task_mod._render_variant(alice.id))
    assert cached is not None and cached.unlicensed is None

"""Personal context in an AI reply comes only from the user the reply is
for.

A system prompt carries personal placeholders ({user_profile},
{user_memory}, {user_recent_raw}, ...). Its owner's replies fill them
from the versions pinned on the prompt node. A reply asked for by
someone else in a public thread fills the placeholders of a node it does
not own with nothing: neither the node owner's pinned data nor the
requester's own. The requester's own nodes still resolve their own
placeholders, and the owner's replies keep the same text (and the same
cached render) as before.

Runs the real task body through the test_retrieval_loop harness
(identity @celery.task, scripted provider, test flask_app); nothing
reaches a real model.
"""
from datetime import datetime, timedelta

import pytest

from backend.tests.test_retrieval_loop import (  # noqa: F401 (fixture)
    app, _llm_task_mod, generate_llm_response, _FakeSelf,
    _ScriptedProvider, _mk_user, _mk_artifact, _mk_todo, _resp, _fresh,
)
from backend.tests.test_training_key_own_rows import _FakeRedis
from backend.extensions import db as _db
from backend.models import (
    Node, NodeContextArtifact, User, UserPrompt, UserProfile,
    UserRecentContext,
)
from backend.utils.context_artifacts import attach_context_artifacts

PLACEHOLDERS = (
    "{user_profile}", "{user_todo}", "{user_recent}", "{user_recent_raw}",
    "{user_ai_preferences}", "{user_memory}", "{user_scratchpad}",
    "{user_intentions}", "{user_artifacts_index}",
)
BODY = "SYSTEM PROMPT BODY"
PROMPT = BODY + "\n" + "\n".join(PLACEHOLDERS)

# What each user's personal data reads as, by placeholder.
FIELDS = ("PROFILE", "TODO", "RECENT", "DIARY", "PREFS", "MEMORY",
          "SCRATCH", "INTENTIONS", "INDEXED")


def _markers(name):
    return [f"{name} {field}" for field in FIELDS]


def _seed_personal_data(user, name, written_at):
    """Profile, todo, recent summary, raw writing and artifacts, each
    carrying "<NAME> <FIELD>". The raw entry (DIARY) is private and dated
    *written_at*, so it falls inside any later recent-raw window."""
    profile = UserProfile(user_id=user.id, generated_by="test",
                          ai_usage="chat")
    profile.set_content(f"{name} PROFILE")
    _db.session.add(profile)
    _db.session.flush()
    rc = UserRecentContext(user_id=user.id, generated_by="test",
                           profile_id=profile.id, ai_usage="chat")
    rc.set_content(f"{name} RECENT")
    _db.session.add(rc)
    _mk_todo(user.id, f"- [ ] {name} TODO")
    _mk_artifact(user.id, "ai_preferences", f"{name} PREFS")
    _mk_artifact(user.id, "memory", f"{name} MEMORY")
    _mk_artifact(user.id, "scratchpad", f"{name} SCRATCH")
    _mk_artifact(user.id, "intentions", f"{name} INTENTIONS")
    _mk_artifact(user.id, "reading-list", "the list",
                 title=f"{name} INDEXED")
    diary = Node(user_id=user.id, human_owner_id=user.id,
                 node_type="user", privacy_level="private",
                 ai_usage="chat", created_at=written_at,
                 updated_at=written_at, token_count=5)
    diary.set_content(f"{name} DIARY")
    _db.session.add(diary)
    _db.session.flush()


def _node(user, parent, text, privacy_level="public"):
    n = Node(user_id=user.id, human_owner_id=user.id,
             parent_id=parent.id if parent else None, node_type="user",
             privacy_level=privacy_level, ai_usage="chat")
    n.set_content(text)
    _db.session.add(n)
    _db.session.flush()
    return n


def _llm(parent, owner, text=None, status="pending"):
    llm_user = User.query.filter_by(username="gpt-5").first()
    n = Node(user_id=llm_user.id, human_owner_id=owner.id,
             parent_id=parent.id, node_type="llm", llm_model="gpt-5",
             llm_task_status=status, privacy_level=parent.privacy_level,
             ai_usage="chat")
    n.set_content(text or "[LLM response generation pending...]")
    _db.session.add(n)
    _db.session.commit()
    return n


def _system_node(owner, prompt_text=PROMPT, privacy_level="public"):
    """An agentic (Text mode) system node of *owner*'s, pinned the way
    /textmode/start pins it."""
    prompt = UserPrompt(user_id=owner.id, prompt_key="textmode",
                        title="Text", generated_by="default")
    prompt.set_content(prompt_text)
    _db.session.add(prompt)
    _db.session.flush()
    system = Node(user_id=owner.id, human_owner_id=owner.id,
                  node_type="user", privacy_level=privacy_level,
                  ai_usage="chat")
    _db.session.add(system)
    _db.session.flush()
    attach_context_artifacts(system.id, owner.id, prompt_record=prompt)
    _db.session.commit()
    return system


@pytest.fixture
def people(app):  # noqa: F811
    """alice's public Text-mode thread (system -> entry -> AI reply) and
    bob, each with their own personal data."""
    long_ago = datetime.utcnow() - timedelta(days=30)
    alice = _mk_user("alice", approved=True, plan="alpha")
    bob = _mk_user("bob", approved=True, plan="alpha")
    _mk_user("gpt-5", twitter_id="llm-gpt-5")
    _seed_personal_data(alice, "ALICE", long_ago)
    _seed_personal_data(bob, "BOB", long_ago)
    _db.session.commit()
    system = _system_node(alice)
    entry = _node(alice, system, "alice writes in public")
    reply = _llm(entry, alice, "An earlier answer.", status="completed")
    return alice, bob, system, entry, reply


@pytest.fixture
def fake_redis(monkeypatch):
    import backend.utils.prompt_cache as prompt_cache
    fake = _FakeRedis()
    monkeypatch.setattr(prompt_cache, "_client", lambda config: fake)
    return fake


def _ask(parent, user):
    llm_node = _llm(parent, user)
    _ScriptedProvider.reset([_resp("An answer.")])
    generate_llm_response(_FakeSelf(), parent.id, llm_node.id, "gpt-5",
                          user.id, source_mode="textmode")
    return llm_node


def _sent_text():
    return "\n".join(m["text"] for m in _ScriptedProvider.calls[-1]["messages"])


def _system_block():
    return next(m["text"] for m in _ScriptedProvider.calls[-1]["messages"]
                if BODY in m["text"])


# ── A reply someone else asks for ────────────────────────────────────────

def test_a_reply_in_someone_elses_thread_gets_none_of_their_pinned_data(app, people):  # noqa: F811
    alice, bob, system, entry, reply = people
    bob_reply = _node(bob, reply, "bob asks a question")

    _ask(bob_reply, bob)

    sent = _sent_text()
    assert "bob asks a question" in sent
    assert "alice writes in public" in sent
    for marker in _markers("ALICE"):
        assert marker not in sent, marker


def test_a_reply_in_someone_elses_thread_fills_their_placeholders_with_nothing(app, people):  # noqa: F811
    """Not the requester's own data either: the node is not theirs."""
    alice, bob, system, entry, reply = people
    bob_reply = _node(bob, reply, "bob asks a question")

    _ask(bob_reply, bob)

    block = _system_block()
    for marker in _markers("BOB"):
        assert marker not in block, marker
    for placeholder in PLACEHOLDERS:
        assert placeholder not in block, placeholder


def test_the_requesters_own_placeholder_still_resolves_to_their_own_data(app, people):  # noqa: F811
    alice, bob, system, entry, reply = people
    bob_reply = _node(bob, reply, "bob asks about {user_memory}")

    _ask(bob_reply, bob)

    sent = _sent_text()
    assert "bob asks about BOB MEMORY" in sent
    for marker in _markers("ALICE"):
        assert marker not in sent, marker


def test_someone_elses_message_with_a_placeholder_contributes_nothing(app, people):  # noqa: F811
    """An ad-hoc placeholder in another user's message (pinned to their
    data when they wrote it) reads as empty in my reply."""
    alice, bob, system, entry, reply = people
    plain = _node(alice, None, "alice's plain public post")
    note = _node(alice, plain, "my profile: {user_profile} {user_recent_raw}")
    from backend.utils.context_artifacts import sync_context_artifacts
    sync_context_artifacts(note.id, alice.id, note.get_content())
    _db.session.commit()
    assert note.has_artifact("profile")
    bob_reply = _node(bob, note, "bob replies")

    _ask(bob_reply, bob)

    sent = _sent_text()
    assert "my profile:" in sent
    assert "{user_profile}" not in sent
    assert "{user_recent_raw}" not in sent
    for marker in _markers("ALICE") + _markers("BOB"):
        assert marker not in sent, marker


def test_a_cached_render_of_the_owners_prompt_is_not_served_to_someone_else(app, people, fake_redis):  # noqa: F811
    alice, bob, system, entry, reply = people
    owner_entry = _node(alice, reply, "alice continues")
    _ask(owner_entry, alice)
    owner_block = _system_block()
    assert "ALICE PROFILE" in owner_block
    cached_before = dict(fake_redis.store)

    bob_reply = _node(bob, reply, "bob asks a question")
    _ask(bob_reply, bob)

    for marker in _markers("ALICE"):
        assert marker not in _sent_text(), marker
    # The owner's entry is untouched, so her next turn still hits it.
    for key, value in cached_before.items():
        assert fake_redis.store[key] == value
    again = _node(alice, reply, "alice once more")
    _ask(again, alice)
    assert _system_block() == owner_block


def test_the_pre_warm_for_someone_else_renders_none_of_the_owners_data(app, people, fake_redis, monkeypatch):  # noqa: F811
    alice, bob, system, entry, reply = people
    monkeypatch.setitem(app.config, "SUPPORTED_MODELS", {
        **app.config["SUPPORTED_MODELS"],
        "claude-test": {"provider": "anthropic", "api_model": "claude-x"},
    })
    sent = []

    def _call_anthropic(api_model, messages, api_key, **kw):
        sent.append(messages)
        raise RuntimeError("stop after capturing the request")
    monkeypatch.setattr(_ScriptedProvider, "_call_anthropic",
                        staticmethod(_call_anthropic), raising=False)

    _llm_task_mod.prewarm_anthropic_cache(system.id, bob.id, "claude-test")

    warm = sent[-1][0]["content"][0]["text"]
    assert BODY in warm
    for marker in _markers("ALICE") + _markers("BOB"):
        assert marker not in warm, marker


# ── The owner's own thread is unchanged ──────────────────────────────────

def test_the_owner_still_gets_every_pinned_artifact(app, people):  # noqa: F811
    alice, bob, system, entry, reply = people
    owner_entry = _node(alice, reply, "alice continues")

    _ask(owner_entry, alice)

    block = _system_block()
    for marker in _markers("ALICE"):
        assert marker in block, marker
    for marker in _markers("BOB"):
        assert marker not in block, marker


def test_the_owners_reply_and_pre_warm_render_the_same_bytes(app, people):  # noqa: F811
    alice, bob, system, entry, reply = people
    owner_entry = _node(alice, reply, "alice continues")

    _ask(owner_entry, alice)

    assert _system_block() == _llm_task_mod.render_system_message(
        system, alice.id)


# ── The archive (export) holds only the user's own pinned versions ──────

def _pin(node, *rows):
    """Pins *rows* on *node*, as the tool loop pins what a turn read."""
    from backend.models import UserTodo, UserArtifact
    for row in rows:
        _db.session.add(NodeContextArtifact(
            node_id=node.id,
            artifact_type=("todo" if isinstance(row, UserTodo)
                           else "user_artifact"),
            artifact_id=row.id))
    _db.session.commit()
    assert all(isinstance(r, (UserTodo, UserArtifact)) for r in rows)


def _todo_and_memory(user):
    from backend.models import UserTodo, UserArtifact
    return (UserTodo.query.filter_by(user_id=user.id).first(),
            UserArtifact.query.filter_by(user_id=user.id,
                                         kind="memory").first())


@pytest.fixture
def threads(people):
    """alice's AI reply (which read her todo and memory) under bob's reply
    in her thread, and again inside bob's own public thread; bob's own AI
    reply there read his."""
    alice, bob, system, entry, reply = people
    bob_reply = _node(bob, reply, "bob asks a question")
    alice_back = _node(alice, bob_reply, "alice answers bob")
    _pin(_llm(alice_back, alice, "her answer", status="completed"),
         *_todo_and_memory(alice))
    bob_root = _node(bob, None, "bob's own public post")
    alice_there = _node(alice, bob_root, "alice in bob's thread")
    _pin(_llm(alice_there, alice, "her answer there", status="completed"),
         *_todo_and_memory(alice))
    _pin(_llm(bob_root, bob, "his answer", status="completed"),
         *_todo_and_memory(bob))
    return alice, bob, system, bob_reply


@pytest.mark.parametrize("kwargs, shows_pins", [
    # {user_recent_raw}: the last ~10k tokens of the user's threads.
    (dict(max_tokens=10000, filter_ai_usage=True), True),
    # {user_export}: threads the user wrote in, budgeted or whole (the
    # whole one lists no pinned versions at all).
    (dict(max_tokens=10000, filter_ai_usage=True,
          include_strategy="engaged_threads"), True),
    (dict(filter_ai_usage=True, include_strategy="engaged_threads"), False),
    # The user's own download.
    (dict(filter_ai_usage=False), True),
])
def test_the_archive_holds_no_one_elses_pinned_versions(app, threads, kwargs, shows_pins):  # noqa: F811
    from backend.routes.export_data import build_user_export_content
    alice, bob, system, bob_reply = threads

    archive = build_user_export_content(bob, **kwargs)

    for marker in _markers("ALICE"):
        assert marker not in archive, marker
    assert ("BOB TODO" in archive) == shows_pins


def test_recent_writing_in_my_own_thread_holds_none_of_their_versions(app, threads):  # noqa: F811
    """{user_recent_raw} in bob's own Text-mode prompt."""
    alice, bob, system, bob_reply = threads
    own_system = _system_node(bob, BODY + "\n{user_recent_raw}",
                              privacy_level="private")
    own_entry = _node(bob, own_system, "bob in his own thread",
                      privacy_level="private")

    _ask(own_entry, bob)

    block = _system_block()
    assert "BOB TODO" in block
    for marker in _markers("ALICE"):
        assert marker not in block, marker

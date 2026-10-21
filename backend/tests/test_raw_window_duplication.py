"""The recent raw window carries the user's recent entries, not copies of
the prompt (#518).

Voice and text sessions start from the agentic system prompt, which holds
{user_recent_raw}: the last ~10k tokens of the user's threads. Those
threads are mostly earlier sessions, so the window used to repeat:

1. the full agentic template and the pinned profile (the export's
   "System Prompts Referenced" / "User Profiles Referenced" preamble);
2. the template's placeholders filled in again inside that copy, because
   the placeholders were filled one after another over the whole text
   (memory twice, intentions three times on a measured session);
3. while the budget counted the copied template, so few real entries fit,
   and when the newest session's entries plus the template passed the
   budget, its root was dropped and the window came out empty.

Now the window lists artifacts as reference lines, the budget counts only
the entries, and every placeholder is filled in one pass, in
render_system_message and in the generation loop alike (byte-identical,
#187). Runs the real task body through the test_retrieval_loop harness;
nothing reaches a real model.
"""
import os
from datetime import datetime, timedelta

import pytest

from backend.tests.test_retrieval_loop import (  # noqa: F401 (fixture)
    app, _llm_task_mod, generate_llm_response, _FakeSelf,
    _ScriptedProvider, _mk_user, _mk_artifact, _resp,
)
from backend.tests.test_training_key_own_rows import _FakeRedis
from backend.extensions import db as _db
from backend.models import (
    Node, User, UserPrompt, UserProfile, UserRecentContext,
)
from backend.utils.context_artifacts import attach_context_artifacts
from backend.utils.placeholders import (
    CA_TWEETS_PATTERN, FirstThen, fill_placeholders,
)
from backend.utils.quotes import ExportQuoteResolver

_PROMPTS = os.path.join(os.path.dirname(__file__), "..", "prompts")
with open(os.path.join(_PROMPTS, "agentic.txt")) as _f:
    AGENTIC = _f.read()

PROFILE = "ALICE PROFILE " + "synthesis " * 40
RECENT = "ALICE RECENT SUMMARY"
INTENTIONS = "ALICE INTENTIONS " + "aspiration " * 20
SCRATCH = "ALICE SCRATCHPAD notes"
PREFS = "ALICE PREFS be brief"
# Memory written by the model can name a placeholder, and user-written
# text is full of backslashes: none of it may be read as a placeholder
# or as a re replacement template.
MEMORY = ("ALICE MEMORY: asked how the prompt lists {user_intentions} and "
          "{user_memory}; keeps notes in C:\\Users\\ana\\notes.txt; "
          "typed \\g<0> and \\1 once.")

NOW = datetime.utcnow().replace(microsecond=0)


# ── One pass ─────────────────────────────────────────────────────────────

def test_a_value_naming_another_placeholder_is_not_filled():
    text = "a={a} b={b}"
    values = {"{a}": "has {b} inside", "{b}": "B"}
    assert fill_placeholders(text, values) == "a=has {b} inside b=B"


def test_values_are_inserted_literally():
    value = "C:\\Users\\ana \\g<0> \\1 \\U"
    assert fill_placeholders("x {a} y", {"{a}": value}) == f"x {value} y"


def test_unknown_braces_stay_as_written():
    text = "failed — {error}. {user_profile } {a}"
    assert fill_placeholders(text, {"{a}": "A"}) == (
        "failed — {error}. {user_profile } A")


def test_first_then_and_patterns_and_seen():
    seen = set()
    archive = FirstThen("ARCHIVE", "(stub)")
    text = ("{x} {x} {ca_tweets?days=1} {ca_tweets} {y}")
    out = fill_placeholders(
        text, {"{x}": archive, "{z}": "Z"},
        [(CA_TWEETS_PATTERN, lambda found: f"<{found[1:-1]}>")],
        seen=seen)
    assert out == "ARCHIVE (stub) <ca_tweets?days=1> <ca_tweets> {y}"
    assert seen == {"{x}"}
    # The archive's state carries over to the next text.
    assert fill_placeholders("{x}", {"{x}": archive}) == "(stub)"


# ── The budget counts only what is emitted ──────────────────────────────

def _prompt_and_entries(resolver):
    resolver.add_node(1, NOW, "", user_prompt_id=7,
                      prompt_content="P" * 2000,  # ~500 tokens
                      prompt_label="Text v1")
    for i in range(2, 6):  # 4 entries, ~100 tokens each, older
        resolver.add_node(i, NOW - timedelta(minutes=i), "E" * 400)


def test_a_collapsed_export_counts_no_prompt_preamble():
    with_copy = ExportQuoteResolver(user_id=1, max_tokens=450)
    _prompt_and_entries(with_copy)
    with_copy.resolve()
    collapsed = ExportQuoteResolver(user_id=1, max_tokens=450,
                                    with_preamble=False)
    _prompt_and_entries(collapsed)
    collapsed.resolve()

    # The full prompt text does not fit next to the entries ...
    assert 1 not in with_copy.included_ids
    # ... but a collapsed export shows only its reference line.
    assert collapsed.included_ids == {1, 2, 3, 4, 5}
    assert collapsed.get_artifacts_preamble() == ""


# ── Sessions on the real agentic template ───────────────────────────────

@pytest.fixture
def alice(app):  # noqa: F811
    user = _mk_user("alice", approved=True, plan="alpha")
    _mk_user("gpt-5", twitter_id="llm-gpt-5")
    profile = UserProfile(user_id=user.id, generated_by="test",
                          ai_usage="chat")
    profile.set_content(PROFILE)
    _db.session.add(profile)
    _db.session.flush()
    rc = UserRecentContext(user_id=user.id, generated_by="test",
                           profile_id=profile.id, ai_usage="chat")
    rc.set_content(RECENT)
    _db.session.add(rc)
    _mk_artifact(user.id, "memory", MEMORY)
    _mk_artifact(user.id, "intentions", INTENTIONS)
    _mk_artifact(user.id, "scratchpad", SCRATCH)
    _mk_artifact(user.id, "ai_preferences", PREFS)
    _db.session.commit()
    return user


@pytest.fixture
def frozen_export_time(monkeypatch):
    """The window's header carries the render time to the second; two
    renders compared byte for byte must see the same clock."""
    import backend.routes.export_data as export_data

    class _Frozen(datetime):
        @classmethod
        def utcnow(cls):
            return datetime(2026, 10, 16, 12, 0, 0)

    monkeypatch.setattr(export_data, "datetime", _Frozen)


@pytest.fixture
def fake_redis(monkeypatch):
    import backend.utils.prompt_cache as prompt_cache
    fake = _FakeRedis()
    monkeypatch.setattr(prompt_cache, "_client", lambda config: fake)
    return fake


def _prompt(user, key):
    """One prompt row per key and user, shared by their sessions."""
    row = UserPrompt.query.filter_by(user_id=user.id, prompt_key=key).first()
    if row is None:
        row = UserPrompt(user_id=user.id, prompt_key=key, title=key.title(),
                         generated_by="default")
        row.set_content(AGENTIC)
        _db.session.add(row)
        _db.session.flush()
    return row


def _node(user, parent, text, when, tokens=0, llm=False, deleted=False):
    author = User.query.filter_by(username="gpt-5").first() if llm else user
    n = Node(user_id=author.id, human_owner_id=user.id,
             parent_id=parent.id if parent else None,
             node_type="llm" if llm else "user",
             llm_model="gpt-5" if llm else None,
             llm_task_status="completed" if llm else None,
             privacy_level="private", ai_usage="chat",
             created_at=when, updated_at=when, token_count=tokens,
             deleted_at=when if deleted else None)
    n.set_content(text)
    _db.session.add(n)
    _db.session.flush()
    return n


def _session(user, key, when, turns, deleted=False):
    """An agentic session started at *when*: the system node, pinned the
    way /textmode/start pins it, then *turns* as (text, tokens, is_ai).
    Returns (system, last node)."""
    system = _node(user, None, "", when, deleted=deleted)
    attach_context_artifacts(system.id, user.id,
                             prompt_record=_prompt(user, key))
    last = system
    for i, (text, tokens, is_ai) in enumerate(turns):
        last = _node(user, last, text, when + timedelta(minutes=i + 1),
                     tokens=tokens, llm=is_ai, deleted=deleted)
    _db.session.commit()
    return system, last


def _new_session(user):
    """Today's session: its system node, an entry, and the AI reply
    being generated."""
    system, entry = _session(user, "textmode", NOW - timedelta(hours=1),
                             [("today's entry", 3, False)])
    reply = _node(user, entry, "[pending]", NOW, llm=True)
    reply.llm_task_status = "pending"
    _db.session.commit()
    return system, entry, reply


def _render(system, user):
    return _llm_task_mod.render_system_message(system, user.id)


def _window(text):
    start = text.index("<recent_raw_data>")
    return text[start:text.index("</recent_raw_data>", start)]


def _earlier_session(user):
    return _session(user, "textmode", NOW - timedelta(days=2), [
        ("EARLIER ENTRY ONE", 20, False),
        ("EARLIER REPLY", 20, True),
        ("EARLIER ENTRY TWO", 20, False),
    ])


def test_the_window_holds_no_copy_of_the_prompt_or_profile(app, alice):  # noqa: F811
    _earlier_session(alice)
    system, _entry, _reply = _new_session(alice)

    text = _render(system, alice)
    window = _window(text)

    for entry in ("EARLIER ENTRY ONE", "EARLIER REPLY", "EARLIER ENTRY TWO"):
        assert entry in window
    # The earlier session's prompt and profile are reference lines.
    assert "[System Prompt: Textmode v1 (ref #" in window
    assert "[User Profile v1 (ref #" in window
    assert "System Prompts Referenced" not in window
    assert "User Profiles Referenced" not in window
    # One copy of the instructions, the profile and every artifact.
    assert text.count("<recent_raw_data>") == 1
    for block in (PROFILE, RECENT, INTENTIONS, SCRATCH, PREFS, MEMORY):
        assert text.count(block) == 1, block[:20]


def test_text_filled_in_is_never_filled_again(app, alice):  # noqa: F811
    """The memory names {user_intentions} and {user_memory}; they stay
    as written, and the intentions appear once."""
    _earlier_session(alice)
    system, _entry, _reply = _new_session(alice)

    text = _render(system, alice)

    assert MEMORY in text
    assert text.count(INTENTIONS) == 1
    assert text.count("{user_intentions}") == 1
    assert text.count("{user_memory}") == 1
    assert "{share_guidance}" not in text
    assert "{external_content_guidance}" not in text


def test_the_reply_and_the_pre_warm_render_the_same_bytes(
        app, alice, frozen_export_time, fake_redis):  # noqa: F811
    _earlier_session(alice)
    system, entry, reply = _new_session(alice)

    _ScriptedProvider.reset([_resp("An answer.")])
    generate_llm_response(_FakeSelf(), entry.id, reply.id, "gpt-5",
                          alice.id, source_mode="textmode")
    sent = _ScriptedProvider.calls[-1]["messages"][0]["text"]

    assert sent == _render(system, alice)
    assert MEMORY in sent and sent.count(INTENTIONS) == 1


def test_the_window_carries_10k_of_entries_next_to_the_prompt(app, alice):  # noqa: F811
    """Nine 1k-token entries fit the 10k window. The copied template
    (~4k tokens) used to count too: the budget then dropped the
    session's root, and with it the whole session (empty window)."""
    _session(alice, "voice", NOW - timedelta(days=1), [
        (f"ENTRY {i}", 1000, False) for i in range(9)])
    system, _entry, _reply = _new_session(alice)

    window = _window(_render(system, alice))

    for i in range(9):
        assert f"ENTRY {i}" in window


def test_a_deleted_session_no_longer_empties_the_window(app, alice):  # noqa: F811
    """The newest thread is a deleted voice session, the one before it a
    text session. The deleted session's prompt (another prompt row, ~4k
    tokens) and the text session's used to count against the budget, so
    the text session's root was dropped and nothing was left to show."""
    _session(alice, "textmode", NOW - timedelta(days=2), [
        (f"KEPT {i}", 1000, False) for i in range(3)])
    _session(alice, "voice", NOW - timedelta(days=1), [
        ("GONE", 10, False)], deleted=True)
    system, _entry, _reply = _new_session(alice)

    window = _window(_render(system, alice))

    for i in range(3):
        assert f"KEPT {i}" in window
    assert "GONE" not in window


def test_a_user_export_is_not_filled_again(app, monkeypatch):  # noqa: F811
    """The generation loop fills a message's placeholders in one pass
    too: the archive's own placeholder names stay as written, and the
    message's {user_memory} still gets the memory."""
    archive = "ARCHIVE names {user_memory} and {user_export}"
    monkeypatch.setattr(_llm_task_mod, "build_user_export_content",
                        lambda *a, **k: archive)
    user = _mk_user("alice", approved=True, plan="pro")
    _mk_user("gpt-5", twitter_id="llm-gpt-5")
    _mk_artifact(user.id, "memory", "THE MEMORY")
    _db.session.commit()
    entry = _node(user, None, "read {user_export} with {user_memory}",
                  NOW - timedelta(minutes=1))
    reply = _node(user, entry, "[pending]", NOW, llm=True)
    reply.llm_task_status = "pending"
    _db.session.commit()

    _ScriptedProvider.reset([_resp("An answer.")])
    generate_llm_response(_FakeSelf(), entry.id, reply.id, "gpt-5",
                          user.id)

    sent = "\n".join(m["text"] for m in _ScriptedProvider.calls[-1]["messages"])
    assert f"read {archive} with THE MEMORY" in sent


# ── Exports that show the preamble keep it ──────────────────────────────

@pytest.mark.parametrize("kwargs", [
    dict(max_tokens=10000),
    dict(max_tokens=10000, include_strategy="engaged_threads"),
])
def test_an_export_that_is_not_collapsed_keeps_its_preamble(app, alice, kwargs):  # noqa: F811
    """{user_export} and the data download pass no collapse: they still
    get the referenced prompt and profile in full. (Profile builds,
    intentions and poll drafts omit them since #550:
    test_writing_only_exports.)"""
    from backend.routes.export_data import build_user_export_content
    _earlier_session(alice)

    archive = build_user_export_content(alice, filter_ai_usage=True, **kwargs)

    assert "## System Prompts Referenced" in archive
    assert PROFILE in archive
    assert "EARLIER ENTRY ONE" in archive

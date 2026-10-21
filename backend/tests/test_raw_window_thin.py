"""The recent raw window holds the newest ~10k tokens of entries, also
when entries were deleted or a thread started before the window (#525).

Two causes left the window thinner than its budget, or empty, after
#523:

1. A deleted entry counted at its former size until the purge (30 days
   after the deletion at the earliest), though the window shows it as a
   one-line placeholder, or not at all. Deleting a session of ~10k
   tokens emptied the window.
2. A thread rendered only from its root, which is the thread's oldest
   node and the first to fall outside a newest-first window. The newest
   part of a session longer than the window was counted but not shown,
   so the next session's window came out empty.

Now a deleted entry costs its placeholder line, and the newest part of
a thread whose start is outside the window shows under a continuation
line. The window is built as every agentic prompt builds it
(render_system_message on the real template); nothing reaches a model.
"""
from datetime import timedelta

import pytest

from backend.tests.test_retrieval_loop import (  # noqa: F401 (fixture)
    app, _llm_task_mod, _mk_user,
)
from backend.tests.test_raw_window_duplication import (  # noqa: F401 (fixtures)
    NOW, alice, frozen_export_time, _node, _session, _new_session,
    _render, _window, _prompt,
)
from backend.extensions import db as _db
from backend.routes.export_data import build_user_export_content
from backend.utils.context_artifacts import attach_context_artifacts
from backend.utils.tokens import approximate_token_count

CONTINUATION = ("> [Continuation of thread started {:%Y-%m-%d} — earlier "
                "context in this thread is not shown.]")


def _text(label, tokens):
    """An entry starting with *label*, *tokens* long by the window's
    measure (chars/4), as its stored token_count says."""
    return label + " " + "w" * (4 * tokens - len(label) - 1)


# ── Cause 2: a thread that started before the window ────────────────────

def test_a_session_longer_than_the_window_shows_its_newest_part(app, alice):  # noqa: F811
    """Yesterday's voice session is 14k tokens long and the window holds
    its newest 9k. The session's start is outside the window, so the
    window used to come out empty."""
    start = NOW - timedelta(days=1)
    _session(alice, "voice", start, [
        (_text(f"ENTRY {i:02d}", 1000), 1000, i % 2 == 1)
        for i in range(14)])
    system, _entry, _reply = _new_session(alice)

    window = _window(_render(system, alice))

    for i in range(5, 14):
        assert f"ENTRY {i:02d}" in window
    for i in range(5):
        assert f"ENTRY {i:02d}" not in window
    assert CONTINUATION.format(start) in window
    # Still within the window's budget: nine 1k entries, their header
    # lines and the export's own lines.
    assert approximate_token_count(window) <= 10_000

    # The "[Covers …]" dates are those of the entries shown.
    raw = _llm_task_mod.get_user_recent_raw_content(
        alice.id, created_before=system.created_at)
    assert raw["earliest"] == start + timedelta(minutes=6)    # ENTRY 05
    assert raw["latest"] == start + timedelta(minutes=14)     # ENTRY 13


def test_short_sessions_after_a_long_one_leave_room_for_its_end(app, alice):  # noqa: F811
    long_start = NOW - timedelta(days=2)
    _session(alice, "voice", long_start, [
        (f"LONG {i:02d}", 1000, False) for i in range(12)])
    _session(alice, "textmode", NOW - timedelta(days=1), [
        ("SHORT ENTRY", 20, False), ("SHORT REPLY", 20, True)])
    system, _entry, _reply = _new_session(alice)

    window = _window(_render(system, alice))

    assert "SHORT ENTRY" in window and "SHORT REPLY" in window
    for i in range(3, 12):
        assert f"LONG {i:02d}" in window
    assert "LONG 02" not in window
    # The short session starts at its root, the long one continues.
    assert window.count("> [Continuation of thread") == 1
    assert CONTINUATION.format(long_start) in window


def test_a_new_reply_in_an_old_thread_is_shown(app, alice):  # noqa: F811
    """A thread started ten days ago, answered yesterday; a long session
    in between fills the rest of the window."""
    old_start = NOW - timedelta(days=10)
    _root, last = _session(alice, "textmode", old_start, [
        ("OLD START", 50, False)])
    _session(alice, "voice", NOW - timedelta(days=3), [
        (f"MIDDLE {i:02d}", 1000, False) for i in range(10)])
    _node(alice, last, "NEW REPLY IN AN OLD THREAD",
          NOW - timedelta(days=1), tokens=30)
    _db.session.commit()
    system, _entry, _reply = _new_session(alice)

    window = _window(_render(system, alice))

    assert "NEW REPLY IN AN OLD THREAD" in window
    assert CONTINUATION.format(old_start) in window
    assert "OLD START" not in window
    for i in range(1, 10):
        assert f"MIDDLE {i:02d}" in window


def test_two_renders_of_a_continued_window_are_the_same_bytes(
        app, alice, frozen_export_time):  # noqa: F811
    """The prompt cache needs a byte-identical prefix on every turn."""
    _session(alice, "voice", NOW - timedelta(days=1), [
        (f"ENTRY {i:02d}", 1000, i % 2 == 1) for i in range(14)])
    system, _entry, _reply = _new_session(alice)

    first = _render(system, alice)
    _db.session.expire_all()

    assert _render(system, alice) == first
    assert "> [Continuation of thread" in _window(first)


# ── Cause 1: deleted entries ─────────────────────────────────────────────

def test_a_deleted_long_session_leaves_the_window_to_the_one_before(app, alice):  # noqa: F811
    """The newest session, 12k tokens, was deleted. Counted at its former
    size, it filled the window, and nothing was shown."""
    _session(alice, "textmode", NOW - timedelta(days=2), [
        (f"KEPT {i}", 1000, False) for i in range(3)])
    _session(alice, "voice", NOW - timedelta(days=1), [
        (f"GONE {i:02d}", 1000, False) for i in range(12)], deleted=True)
    system, _entry, _reply = _new_session(alice)

    window = _window(_render(system, alice))

    for i in range(3):
        assert f"KEPT {i}" in window
    assert "GONE" not in window
    # A deleted thread leaves no placeholder behind (§5a).
    assert "[Node deleted by author]" not in window


def test_a_deleted_entry_counts_as_its_placeholder(app, alice):  # noqa: F811
    """An 8k-token entry deleted in the middle of yesterday's session:
    its placeholder shows, its text does not, and the session before
    still fits."""
    _session(alice, "textmode", NOW - timedelta(days=2), [
        (f"OLDER {i}", 1000, False) for i in range(3)])
    when = NOW - timedelta(days=1)
    start = _node(alice, None, "", when)
    attach_context_artifacts(start.id, alice.id,
                             prompt_record=_prompt(alice, "voice"))
    before = _node(alice, start, "BEFORE", when + timedelta(minutes=1),
                   tokens=500)
    gone = _node(alice, before, "SECRET DELETED WORDS",
                 when + timedelta(minutes=2), tokens=8000, deleted=True)
    _node(alice, gone, "AFTER", when + timedelta(minutes=3), tokens=500)
    _db.session.commit()
    system, _entry, _reply = _new_session(alice)

    window = _window(_render(system, alice))

    for label in ("BEFORE", "AFTER", "OLDER 0", "OLDER 1", "OLDER 2"):
        assert label in window
    assert "[Node deleted by author]" in window
    assert "SECRET DELETED WORDS" not in window


@pytest.mark.parametrize("strategy", ["authored_threads", "engaged_threads"])
def test_a_quote_of_a_deleted_entry_never_carries_its_text(app, alice, strategy):  # noqa: F811
    """The newest entry quotes an older one and a deleted one. Embedding
    the first pushes the deleted one out of the window, so its quote is
    looked up: a deleted entry's text is never read for that."""
    quoted = _node(alice, None, _text("QUOTED", 4000),
                   NOW - timedelta(days=5), tokens=4000)
    gone = _node(alice, None, "DELETED WORDS", NOW - timedelta(days=3),
                 tokens=900, deleted=True)
    _node(alice, None, "FILLER", NOW - timedelta(days=2), tokens=3900)
    _node(alice, None, f"see {{quote:{quoted.id}}} and {{quote:{gone.id}}}",
          NOW - timedelta(days=1), tokens=5000)
    _db.session.commit()

    export = build_user_export_content(
        alice, max_tokens=10000, filter_ai_usage=True,
        collapse_artifacts=True, include_strategy=strategy)

    assert "QUOTED" in export
    assert "DELETED WORDS" not in export


# ── What stays out, as when the thread renders whole ─────────────────────

def test_a_reply_in_someone_elses_thread_stays_out(app, alice):  # noqa: F811
    """The window covers the user's own threads (authored_threads)."""
    bob = _mk_user("bob", approved=True)
    post = _node(bob, None, "BOB'S POST", NOW - timedelta(days=3))
    post.privacy_level = "public"
    _session(alice, "textmode", NOW - timedelta(days=2), [
        ("HER OWN ENTRY", 20, False)])
    _node(alice, post, "ALICE IN BOB'S THREAD", NOW - timedelta(days=1),
          tokens=20)
    _db.session.commit()
    system, _entry, _reply = _new_session(alice)

    window = _window(_render(system, alice))

    assert "HER OWN ENTRY" in window
    assert "ALICE IN BOB'S THREAD" not in window
    assert "BOB'S POST" not in window


def test_replies_under_an_entry_marked_none_stay_out(app, alice):  # noqa: F811
    """An entry marked None hides the replies under it, as when its
    thread renders from the root, also when its thread started before
    the window."""
    _root, note = _session(alice, "textmode", NOW - timedelta(days=4), [
        ("PRIVATE NOTE", 50, False)])
    note.ai_usage = "none"
    _session(alice, "voice", NOW - timedelta(days=3), [
        (f"MIDDLE {i:02d}", 1000, False) for i in range(10)])
    _node(alice, note, "REPLY UNDER THE NOTE", NOW - timedelta(days=1),
          tokens=20)
    _db.session.commit()
    system, _entry, _reply = _new_session(alice)

    window = _window(_render(system, alice))

    assert "MIDDLE 09" in window
    assert "PRIVATE NOTE" not in window
    assert "REPLY UNDER THE NOTE" not in window

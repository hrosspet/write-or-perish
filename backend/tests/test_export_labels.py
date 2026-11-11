"""Position labels in export headers (#592).

Every entry an export renders starts with a header holding its place in
its thread. The label used to list one ".1" per level from the thread's
first entry, so the 200th message of an imported ChatGPT conversation
carried a 400-character label and that conversation's headers took
~52,000 characters. Now a linear stretch is numbered in order (4, 4.1,
4.2 ... 4.199) and a label grows only at a branch point, where each
branch gets a letter (the replies to 2.2 are 2.2a.1 and 2.2b.1).

A quote the export resolves by reference names the quoted entry by its
label when the export shows it as an entry, wherever it renders, and
by its author and time otherwise.
"""
import re
from datetime import timedelta

import pytest

from backend.tests.test_exports_incremental import (  # noqa: F401 (fixture)
    app, _make_user, _make_node, _build, DEC_15, APR_07, APR_18,
)
from backend.extensions import db as _db
from backend.utils.tokens import approximate_token_count

HEADER = re.compile(r"^(#{2,6}) \[([^\]]+)\](.*)$", re.M)


def _headers(text):
    """(heading, label) of every entry and placeholder header."""
    return [(m.group(1), m.group(2)) for m in HEADER.finditer(text)]


def _labels(text):
    return [label for _, label in _headers(text)]


def _chain(user, n, start=DEC_15, parent=None, origin=None, tokens=3,
           prefix="message"):
    """*n* messages, each the only reply to the one before, alternating
    the user and an AI as an imported chat does. Returns the nodes."""
    nodes = []
    for i in range(n):
        llm = i % 2 == 1
        node = _make_node(
            user, parent_id=parent.id if parent else None,
            content=f"{prefix} {i}", node_type="llm" if llm else "user",
            llm_model="gpt-4o" if llm else None, ai_usage="chat",
            created_at=start + timedelta(minutes=i), token_count=tokens,
            origin=origin)
        nodes.append(node)
        parent = node
    _db.session.commit()
    return nodes


def _reply(user, parent, content, minutes, **kwargs):
    return _make_node(user, parent_id=parent.id, content=content,
                      ai_usage="chat", token_count=3,
                      created_at=DEC_15 + timedelta(minutes=minutes),
                      **kwargs)


# ── The label format ─────────────────────────────────────────────────────

def test_branch_letters_run_past_z(app):  # noqa: F811
    from backend.routes.export_data import _branch_letters
    assert [_branch_letters(i) for i in (0, 1, 25, 26, 27, 51, 52)] == [
        "a", "b", "z", "aa", "ab", "az", "ba"]
    assert _branch_letters(701) == "zz"
    assert _branch_letters(702) == "aaa"


def test_reply_labels(app):  # noqa: F811
    from backend.routes.export_data import _reply_labels
    assert _reply_labels("4", 1) == ["4.1"]
    assert _reply_labels("4.1", 1) == ["4.2"]
    assert _reply_labels("4.199", 1) == ["4.200"]
    assert _reply_labels("2.2", 2) == ["2.2a.1", "2.2b.1"]
    assert _reply_labels("2.2b.1", 1) == ["2.2b.2"]
    assert _reply_labels("2", 3) == ["2a.1", "2b.1", "2c.1"]
    assert _reply_labels("2a.1", 2) == ["2a.1a.1", "2a.1b.1"]


# ── A linear conversation ────────────────────────────────────────────────

def test_a_linear_thread_of_200_messages_numbers_in_order(app):  # noqa: F811
    """The issue's case: an imported ChatGPT conversation, each message
    the only reply to the one before. Its headers took 52,090
    characters with one ".1" per level; now 12,188."""
    alice = _make_user("alice")
    _chain(alice, 200, origin="chatgpt")

    text = _build(alice, filter_ai_usage=True)

    headers = _headers(text)
    assert [label for _, label in headers] == (
        ["1"] + [f"1.{i}" for i in range(1, 200)])
    # One heading level along the whole stretch.
    assert {heading for heading, _ in headers} == {"##"}
    lines = HEADER.findall(text)
    assert len(lines) == 200
    header_lines = [m.group(0) for m in HEADER.finditer(text)]
    assert max(len(line) for line in header_lines) <= 61
    # "## [1.199] AI (gpt-4o) via chatgpt - 2025-12-15 13:19:00 UTC"
    assert sum(len(line) + 1 for line in header_lines) <= 12_200
    assert "message 199" in text


# ── Branches ─────────────────────────────────────────────────────────────

def _branching_thread(alice):
    """Thread 2 of alice's export: 2, 2.1, 2.2, then two replies to 2.2
    (Claude's and GPT's), a reply on branch b, and two replies to that.
    Thread 1 is a one-entry thread before it."""
    _make_node(alice, content="FIRST THREAD", ai_usage="chat",
               token_count=3, created_at=DEC_15 - timedelta(days=1))
    root = _make_node(alice, content="ROOT", ai_usage="chat",
                      token_count=3, created_at=DEC_15)
    one = _reply(alice, root, "ONE", 1, node_type="llm", llm_model="gpt-4o")
    two = _reply(alice, one, "TWO", 2)
    claude = _reply(alice, two, "CLAUDE", 3, node_type="llm",
                    llm_model="claude-opus-4.6")
    gpt = _reply(alice, two, "GPT", 4, node_type="llm", llm_model="gpt-4o")
    on_b = _reply(alice, gpt, "ON B", 5)
    _reply(alice, on_b, "B ONE", 6, node_type="llm", llm_model="gpt-4o")
    _reply(alice, on_b, "B TWO", 7, node_type="llm", llm_model="gpt-4o")
    _db.session.commit()
    return claude


def test_a_branching_thread_letters_each_branch(app):  # noqa: F811
    alice = _make_user("alice")
    _branching_thread(alice)

    text = _build(alice, filter_ai_usage=True)

    assert _headers(text) == [
        ("##", "1"),
        ("##", "2"), ("##", "2.1"), ("##", "2.2"),
        ("###", "2.2a.1"),
        ("###", "2.2b.1"), ("###", "2.2b.2"),
        ("####", "2.2b.2a.1"), ("####", "2.2b.2b.1"),
    ]
    # Branch a renders whole before branch b; each later branch opens
    # with a separator naming the entry it replies to.
    separator = "---\n**BRANCH: another reply to {}**\n---\n\n"
    assert text.count("**BRANCH") == 2
    i_claude = text.index("CLAUDE")
    i_sep = text.index(separator.format("2.2"))
    i_gpt = text.index("GPT\n")
    assert i_claude < i_sep < i_gpt
    assert text.index(separator.format("2.2b.2")) < text.index("B TWO")
    assert "### [2.2a.1] AI (claude-opus-4.6) - 2025-12-15 10:03:00 UTC\n" \
        "CLAUDE" in text


def test_more_than_26_replies_continue_with_two_letters(app):  # noqa: F811
    alice = _make_user("alice")
    root = _make_node(alice, content="ROOT", ai_usage="chat",
                      token_count=3, created_at=DEC_15)
    for i in range(28):
        _reply(alice, root, f"REPLY {i}", i + 1)
    _db.session.commit()

    text = _build(alice, filter_ai_usage=True)

    letters = [chr(ord("a") + i) for i in range(26)] + ["aa", "ab"]
    assert _labels(text) == ["1"] + [f"1{x}.1" for x in letters]
    assert text.count("**BRANCH: another reply to 1**") == 27
    assert "### [1ab.1] User (alice)" in text


def test_replies_with_one_timestamp_keep_their_order(app):  # noqa: F811
    """Siblings created in the same second are ordered by id, so two
    renders give the same labels (the prompt cache needs the same
    bytes)."""
    alice = _make_user("alice")
    root = _make_node(alice, content="ROOT", ai_usage="chat",
                      token_count=3, created_at=DEC_15)
    first = _reply(alice, root, "FIRST", 1)
    second = _reply(alice, root, "SECOND", 1)
    _db.session.commit()
    assert first.id < second.id

    text = _build(alice, filter_ai_usage=True)

    assert text.index("[1a.1]") < text.index("FIRST") \
        < text.index("[1b.1]") < text.index("SECOND")


# ── Windows that start mid-thread ────────────────────────────────────────

def _strip_date(text):
    return re.sub(r"\*\*Export Date:\*\* .*\n", "", text)


def test_a_budgeted_window_numbers_from_where_it_starts(app):  # noqa: F811
    """The newest part of a thread longer than the window: the block's
    first entry is numbered as its thread's first, the rest follow in
    order, and two renders are the same."""
    alice = _make_user("alice")
    _chain(alice, 10, tokens=100, prefix="link")

    def window():
        return _build(alice, max_tokens=550, filter_ai_usage=True,
                      collapse_artifacts=True)

    text = window()

    labels = _labels(text)
    assert 1 < len(labels) < 10
    assert labels == ["1"] + [f"1.{i}" for i in range(1, len(labels))]
    assert "Continuation of thread started 2025-12-15" in text
    assert "link 0\n" not in text
    assert "link 9" in text
    _db.session.expire_all()
    assert _strip_date(window()) == _strip_date(text)


def test_an_incremental_window_numbers_from_its_entry_point(app):  # noqa: F811
    alice = _make_user("alice")
    root = _make_node(alice, content="OLD ROOT", ai_usage="chat",
                      token_count=3, created_at=DEC_15)
    _chain(alice, 3, start=APR_18, parent=root, prefix="april")

    text = _build(alice, filter_ai_usage=True, created_after=APR_07)

    assert "OLD ROOT" not in text
    assert _labels(text) == ["1", "1.1", "1.2"]


# ── Deleted entries ──────────────────────────────────────────────────────

def _thread_with_deleted_middle(alice):
    root = _make_node(alice, content="KEPT ROOT", ai_usage="chat",
                      token_count=3, created_at=DEC_15)
    last = root
    for i in range(3):
        last = _reply(alice, last, f"GONE {i}", i + 1)
        last.deleted_at = DEC_15 + timedelta(days=1)
    _reply(alice, last, "KEPT LEAF", 10)
    _db.session.commit()


def test_a_pruned_run_of_deleted_entries_takes_one_number(app):  # noqa: F811
    """The budgeted window shows a run of deleted entries as one
    placeholder line under one header (#525): one number."""
    alice = _make_user("alice")
    _thread_with_deleted_middle(alice)

    text = _build(alice, max_tokens=10000, filter_ai_usage=True,
                  collapse_artifacts=True)

    assert _labels(text) == ["1", "1.1", "1.2"]
    assert re.search(r"## \[1\.1\] User \(alice\) - [^\n]*\n"
                     r"\[3 nodes deleted by author\]", text)
    assert "## [1.2] User (alice)" in text
    assert "GONE" not in text


def test_each_deleted_entry_has_its_number_in_the_download(app):  # noqa: F811
    alice = _make_user("alice")
    _thread_with_deleted_middle(alice)

    text = _build(alice, filter_ai_usage=False)

    assert _labels(text) == ["1", "1.1", "1.2", "1.3", "1.4"]
    assert text.count("[Node deleted by author]") == 3
    assert "## [1.4] User (alice)" in text


# ── Quote references ─────────────────────────────────────────────────────

def _window(user, strategy="authored_threads"):
    # Oldest first on both paths (the incremental path renders newest
    # first unless asked).
    result = _build(user, max_tokens=10000, filter_ai_usage=True,
                    collapse_artifacts=True, return_metadata=True,
                    include_strategy=strategy, chronological_order=True)
    content = result["content"]
    # Every reference is filled in, and the text is measured after.
    assert "\ue000" not in content and "\ue001" not in content
    assert result["token_count"] == approximate_token_count(content)
    return content


def test_a_quote_of_an_entry_rendered_before_names_its_label(app):  # noqa: F811
    alice = _make_user("alice")
    quoted = _make_node(alice, content="QUOTED TEXT", ai_usage="chat",
                        token_count=3, created_at=DEC_15)
    _make_node(alice, content=f"see {{quote:{quoted.id}}}",
               ai_usage="chat", token_count=3,
               created_at=DEC_15 + timedelta(hours=1))
    _db.session.commit()

    text = _window(alice)

    assert "see [See entry 1]" in text
    assert text.count("QUOTED TEXT") == 1
    assert "node #" not in text


@pytest.mark.parametrize("strategy", ["authored_threads", "engaged_threads"])
def test_a_quote_of_an_entry_rendered_after_names_its_label(app, strategy):  # noqa: F811
    """The quoting entry renders first: its reference is filled in once
    the whole document has its labels. Both export paths."""
    alice = _make_user("alice")
    claude = _branching_thread(alice)
    # An entry before both threads quoting one of thread 2's branches.
    _make_node(alice, content=f"earlier, {{quote:{claude.id}}}",
               ai_usage="chat", token_count=3,
               created_at=DEC_15 - timedelta(days=2))
    _db.session.commit()

    text = _window(alice, strategy)

    assert "earlier, [See entry 3.2a.1]" in text
    assert text.index("[See entry 3.2a.1]") < text.index("### [3.2a.1]")
    assert text.count("CLAUDE") == 1


def test_a_quote_of_an_entry_not_shown_as_one_gives_author_and_time(app):  # noqa: F811
    """A tweet in the window renders in the compact tweet run, with no
    header: the reference gives its author and time."""
    alice = _make_user("alice")
    tweet = _make_node(alice, content="A TWEET", ai_usage="chat",
                       token_count=3, created_at=DEC_15, origin="twitter")
    _make_node(alice, content=f"about {{quote:{tweet.id}}}",
               ai_usage="chat", token_count=3,
               created_at=DEC_15 + timedelta(hours=1))
    _db.session.commit()

    text = _window(alice)

    assert ("about [Quoted entry by User (alice) via twitter - "
            "2025-12-15 10:00:00 UTC]") in text
    assert text.count("A TWEET") == 1


def test_a_quote_of_an_inaccessible_entry_names_no_author(app):  # noqa: F811
    alice = _make_user("alice")
    bob = _make_user("bob")
    private = _make_node(bob, content="BOB PRIVATE", ai_usage="chat",
                         token_count=3, created_at=DEC_15)
    _make_node(alice, content=f"quoting {{quote:{private.id}}}",
               ai_usage="chat", token_count=3,
               created_at=DEC_15 + timedelta(hours=1))
    _db.session.commit()

    text = _window(alice)

    assert f"quoting [Quoted node inaccessible: node {private.id}]" in text
    assert "bob" not in text
    assert "BOB PRIVATE" not in text


def test_stored_text_cannot_forge_a_reference(app):  # noqa: F811
    """A marker's tag is drawn per export: text that looks like one is
    left as written."""
    alice = _make_user("alice")
    quoted = _make_node(alice, content="QUOTED", ai_usage="chat",
                        token_count=3, created_at=DEC_15)
    forged = f"\ue000{'0' * 16}:{quoted.id}\ue001"
    _make_node(alice, content=f"{{quote:{quoted.id}}} {forged}",
               ai_usage="chat", token_count=3,
               created_at=DEC_15 + timedelta(hours=1))
    _db.session.commit()

    result = _build(alice, max_tokens=10000, filter_ai_usage=True,
                    collapse_artifacts=True)

    assert f"[See entry 1] {forged}" in result

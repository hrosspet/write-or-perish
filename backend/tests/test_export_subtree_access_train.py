"""The raw window's tombstone pruning (#581) applies the export's access
rule: a deleted reply the user could not see before is never counted,
joined or walked into, and takes no child slot.

Uses the fixtures of test_export_subtree_access.py (in-memory SQLite,
ENCRYPTION_DISABLED, no outside calls).
"""
from backend.tests.test_export_subtree_access import (  # noqa: F401
    app, alice, bob, _node)


def _raw(user):
    from backend.routes.export_data import build_user_export_content
    r = build_user_export_content(
        user, max_tokens=10000, filter_ai_usage=True,
        return_metadata=True, collapse_artifacts=True)
    return r["content"] if r else ""


def test_pruned_window_never_counts_or_walks_an_unseen_deleted_reply(
        app, alice, bob):  # noqa: F811
    root = _node(alice, "ALICEROOT", deleted=True)
    gone = _node(bob, "BOBGONE", parent=root, deleted=True)
    _node(alice, "ALICEBELOW", parent=gone)
    _node(alice, "OTHERTHREAD")

    content = _raw(alice)
    assert "OTHERTHREAD" in content
    assert "bob" not in content
    assert "ALICEBELOW" not in content
    assert "deleted by author" not in content   # nothing left to hold a place


def test_an_unseen_deleted_reply_takes_no_slot_and_keeps_a_run_joined(
        app, alice, bob):  # noqa: F811
    """A deleted entry with two children: another user's deleted private
    reply with nothing below it, and the user's own deleted reply with a
    live reply below it. The run of the user's two deleted nodes is one
    line, and the live reply is its only child."""
    root = _node(alice, "ALICEROOT", deleted=True)
    _node(bob, "BOBGONE", parent=root, deleted=True)
    mid = _node(alice, "ALICEMID", parent=root, deleted=True)
    _node(alice, "ALICELIVE", parent=mid)

    content = _raw(alice)
    assert "ALICELIVE" in content
    assert "bob" not in content
    assert content.count("deleted by author") == 1
    assert "[2 nodes deleted by author]" in content
    assert "[1.1] User (alice)" in content
    assert "[1.2]" not in content

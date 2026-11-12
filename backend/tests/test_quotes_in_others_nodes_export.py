"""In the export of an account's threads ({user_export}, profile builds,
recent context, the data download), text the account did not write
resolves only what every reader of that text can read.

- In someone else's reply in the account's thread, a {quote:ID}
  resolves only when the quoted node is public; any other stays as
  written. A {quote_ext:ID} there stays as written.
- The same holds inside someone else's text that the account's own
  entry quotes, at every depth.
- The account's own text resolves as before, and the export of an
  account whose threads hold only its own text is unchanged.

In-memory SQLite, ENCRYPTION_DISABLED; the export runs for real
(test_export_subtree_access harness).
"""
import pytest

from backend.tests.test_export_subtree_access import (  # noqa: F401
    app, _user, _node, _download, _ai_export, _incremental_export,
)
from backend.extensions import db as _db
from backend.models import ExternalItem


def _item(user, content):
    item = ExternalItem(user_id=user.id, source="document",
                        external_id=f"x{content[:12]}",
                        author_handle="someone", url="https://example.com/1")
    item.set_content(content)
    _db.session.add(item)
    _db.session.commit()
    return item


def _window(user, budget=10000, **kwargs):
    from backend.routes.export_data import build_user_export_content
    result = build_user_export_content(
        user, max_tokens=budget, filter_ai_usage=True,
        return_metadata=True, collapse_artifacts=True, **kwargs)
    return result["content"] if result else ""


def _all_exports(app, user):  # noqa: F811
    return {
        "download": _download(app, user),
        "ai_export": _ai_export(user),
        "incremental": _incremental_export(user),
        "window": _window(user),
    }


@pytest.fixture
def people(app):  # noqa: F811
    return _user("alice"), _user("bob")


def test_someone_elses_reply_in_my_thread_keeps_its_quotes_of_my_data(app, people):  # noqa: F811,E501
    alice, bob = people
    bobs_private = _node(bob, "BOBS PRIVATE DIARY")
    bobs_item = _item(bob, "BOBS UPLOADED DOCUMENT")
    root = _node(bob, "BOB ASKS IN PUBLIC", privacy="public")
    _node(alice, "ALICE REPLIES {quote:%d} {quote_ext:%d}"
          % (bobs_private.id, bobs_item.id), parent=root, privacy="public")

    for name, content in _all_exports(app, bob).items():
        assert "ALICE REPLIES {quote:%d} {quote_ext:%d}" % (
            bobs_private.id, bobs_item.id) in content, name
        assert "BOBS UPLOADED DOCUMENT" not in content, name
        # bob's private entry is in his export once: as his own entry.
        assert content.count("BOBS PRIVATE DIARY") <= 1, name


def test_someone_elses_reply_still_quotes_public_entries(app, people):  # noqa: F811,E501
    alice, bob = people
    alices_public = _node(alice, "ALICES PUBLIC NOTE", privacy="public")
    root = _node(bob, "BOB ASKS IN PUBLIC", privacy="public")
    _node(alice, "ALICE REPLIES {quote:%d}" % alices_public.id, parent=root,
          privacy="public")

    content = _download(app, bob)
    assert "Quoted from @alice (node #%d" % alices_public.id in content
    assert "ALICES PUBLIC NOTE" in content
    assert "{quote:%d}" % alices_public.id not in content


def test_my_entry_quoting_someone_elses_text_keeps_its_quotes_of_my_data(app, people):  # noqa: F811,E501
    """(a), (b), (c) in the export: alice's public note names bob's item
    and bob's private entry and holds a placeholder; bob's own entry
    quotes the note, and resolves his own literal reference."""
    alice, bob = people
    bobs_private = _node(bob, "BOBS PRIVATE DIARY")
    bobs_item = _item(bob, "BOBS UPLOADED DOCUMENT")
    bobs_other = _item(bob, "BOBS OTHER DOCUMENT")
    note = _node(alice, "ALICES NOTE {quote_ext:%d} {quote:%d} {user_profile}"
                 % (bobs_item.id, bobs_private.id), privacy="public")
    _node(bob, "BOB QUOTES {quote:%d} {quote_ext:%d}"
          % (note.id, bobs_other.id))

    for name, content in _all_exports(app, bob).items():
        assert ("ALICES NOTE {quote_ext:%d} {quote:%d} {user_profile}"
                % (bobs_item.id, bobs_private.id)) in content, name
        assert "BOBS UPLOADED DOCUMENT" not in content, name
        assert "BOBS OTHER DOCUMENT" in content, name
        assert content.count("BOBS PRIVATE DIARY") <= 1, name
        assert "" not in content, name


def test_a_quote_two_levels_into_someone_elses_text_stays_as_written(app, people):  # noqa: F811,E501
    """bob's entry quotes alice's note, which quotes her other note,
    which quotes bob's private entry. In the budgeted window the
    private entry is outside the window, so it could only be embedded."""
    alice, bob = people
    bobs_private = _node(bob, "BOBS PRIVATE DIARY")
    inner = _node(alice, "ALICES INNER {quote:%d}" % bobs_private.id,
                  privacy="public")
    note = _node(alice, "ALICES NOTE {quote:%d}" % inner.id,
                 privacy="public")
    for i in range(12):
        _node(bob, "BOB FILLER %d" % i)
    _node(bob, "BOB QUOTES {quote:%d}" % note.id)

    for name, content in list(_all_exports(app, bob).items()) + [
            ("window_small", _window(bob, budget=120))]:
        assert "BOB QUOTES" in content, name
        # bob's entry, up to the separator before the next thread.
        block = content.split("BOB QUOTES", 1)[1].split("\n---\n", 1)[0]
        assert "ALICES NOTE" in block, name
        assert "BOBS PRIVATE DIARY" not in block, name
        if "ALICES INNER" in block:
            assert "ALICES INNER {quote:%d}" % bobs_private.id in block, name
    # The budgeted window embeds both levels of alice's text.
    assert "ALICES INNER {quote:%d}" % bobs_private.id in _window(bob)


def test_my_own_text_quoting_my_own_text_resolves_as_before(app, people, monkeypatch):  # noqa: F811,E501
    """An account whose threads hold only its own text (its own entries
    and AI replies, quoting each other and its references): every export
    is the same with the rule and without it."""
    import backend.routes.export_data as export_data
    alice, bob = people
    item = _item(bob, "BOBS UPLOADED DOCUMENT")
    alices_public = _node(alice, "ALICES PUBLIC NOTE", privacy="public")
    old = _node(bob, "BOBS OLD PRIVATE {quote_ext:%d}" % item.id)
    inner = _node(bob, "INNER {quote:%d} {quote:%d}"
                  % (old.id, alices_public.id))
    root = _node(bob, "BOB ROOT {quote:%d} {quote_ext:%d}"
                 % (inner.id, item.id))
    ai = _node(bob, "AI REPLY {quote_ext:%d} {quote:%d}"
               % (item.id, inner.id), parent=root, node_type="llm")
    _node(bob, "BOB AGAIN {quote:%d} {quote:%d}" % (ai.id, old.id),
          parent=ai)

    def _exports():
        out = _all_exports(app, bob)
        for budget in (400, 150, 120):
            out[f"window_{budget}"] = _window(bob, budget=budget)
        return {k: v.split("\n", 4)[-1] for k, v in out.items()}

    with_rule = _exports()
    monkeypatch.setattr(export_data, "quotes_inside",
                        lambda *a, **k: (lambda _node_id: None))
    without_rule = _exports()
    assert with_rule == without_rule
    assert "BOBS UPLOADED DOCUMENT" in with_rule["download"]
    assert "Quoted from @bob" in with_rule["download"]

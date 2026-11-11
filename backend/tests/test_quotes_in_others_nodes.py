"""Quotes inside another person's entry resolve only what that entry's
readers can read.

In a public thread, an entry someone else wrote may hold {quote:ID} and
{quote_ext:ID} markers. In a reply the requester asks for below it:

- a {quote:ID} there resolves only when the quoted node is readable by
  every reader of that entry (public), at every depth of nested quotes;
  any other stays as written, as the thread page shows it to a reader
  who cannot open the quoted node;
- a {quote_ext:ID} there, or in another person's AI reply, is never
  resolved, and adds none of the requester's marks to the prompt;
- in the requester's own entries, quotes resolve as before, and a
  private thread's prompt is unchanged.

Runs the real task body through the test_retrieval_loop harness
(identity @celery.task, scripted provider, test flask_app); nothing
reaches a real model.
"""
from datetime import datetime

import pytest

from backend.tests.test_retrieval_loop import (  # noqa: F401 (fixture)
    app, _llm_task_mod, generate_llm_response, _FakeSelf,
    _ScriptedProvider, _mk_user, _resp, _fresh,
)
from backend.extensions import db as _db
from backend.models import ExternalItem, Node, User
from backend.utils.quotes import resolve_ext_quotes, resolve_quotes


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


def _item(user, content, source="document"):
    item = ExternalItem(user_id=user.id, source=source,
                        external_id=f"x{content[:12]}", author_handle="someone",
                        url="https://example.com/1")
    item.set_content(content)
    _db.session.add(item)
    _db.session.flush()
    return item


def _ask(parent, user):
    llm_node = _llm(parent, user)
    _ScriptedProvider.reset([_resp("An answer.")])
    result = generate_llm_response(_FakeSelf(), parent.id, llm_node.id,
                                   "gpt-5", user.id)
    assert result["status"] == "completed", result
    return llm_node


def _messages():
    return _ScriptedProvider.calls[-1]["messages"]


def _sent():
    return "\n".join(m["text"] for m in _messages())


def _message_with(fragment):
    found = [m["text"] for m in _messages() if fragment in m["text"]]
    assert len(found) == 1, (fragment, found)
    return found[0]


@pytest.fixture
def people(app):  # noqa: F811
    """alice and bob; bob has a private entry, alice a public one."""
    alice = _mk_user("alice", approved=True, plan="alpha")
    bob = _mk_user("bob", approved=True, plan="alpha")
    _mk_user("gpt-5", twitter_id="llm-gpt-5")
    bobs_private = _node(bob, None, "BOBS PRIVATE DIARY",
                         privacy_level="private")
    alices_public = _node(alice, None, "ALICES PUBLIC NOTE")
    _db.session.commit()
    return alice, bob, bobs_private, alices_public


# ── {quote:ID} in someone else's entry ───────────────────────────────────

def test_a_quote_of_my_private_entry_in_someone_elses_entry_stays_as_written(app, people):  # noqa: F811,E501
    alice, bob, bobs_private, _ = people
    root = _node(alice, None, "alice opens a public thread")
    quoting = _node(alice, root,
                    "alice quotes {quote:%d}" % bobs_private.id)
    bobs_reply = _node(bob, quoting, "bob answers")
    _db.session.commit()

    _ask(bobs_reply, bob)

    sent = _sent()
    assert "BOBS PRIVATE DIARY" not in sent
    alices = _message_with("alice quotes")
    assert "{quote:%d}" % bobs_private.id in alices
    assert "bob answers" in sent


def test_a_quote_of_a_public_entry_in_someone_elses_entry_resolves(app, people):  # noqa: F811,E501
    alice, bob, bobs_private, alices_public = people
    bobs_public = _node(bob, None, "BOBS PUBLIC NOTE")
    root = _node(alice, None, "alice opens a public thread")
    quoting = _node(alice, root, "alice quotes {quote:%d} and {quote:%d}"
                    % (alices_public.id, bobs_public.id))
    bobs_reply = _node(bob, quoting, "bob answers")
    _db.session.commit()

    _ask(bobs_reply, bob)

    alices = _message_with("alice quotes")
    assert '<quoted_node id="%d" author="alice"' % alices_public.id in alices
    assert "ALICES PUBLIC NOTE" in alices
    assert '<quoted_node id="%d" author="bob"' % bobs_public.id in alices
    assert "BOBS PUBLIC NOTE" in alices


def test_my_own_entry_quoting_my_private_entry_resolves_as_before(app, people):  # noqa: F811,E501
    alice, bob, bobs_private, _ = people
    root = _node(alice, None, "alice opens a public thread")
    text = "bob quotes his own {quote:%d}" % bobs_private.id
    bobs_reply = _node(bob, root, text)
    _db.session.commit()

    _ask(bobs_reply, bob)

    bobs = _message_with("bob quotes his own")
    assert "BOBS PRIVATE DIARY" in bobs
    # The very text the resolver gives with no restriction.
    expected, _ = resolve_quotes(text, bob.id, for_llm=True, tz_name="UTC")
    assert bobs.endswith(expected)


def test_a_nested_quote_of_my_private_entry_stays_as_written(app, people):  # noqa: F811,E501
    """Someone else's entry quotes a public entry that itself quotes my
    private entry: the public entry resolves, the quote inside it stays
    as written. The same chain in my own entry resolves in full."""
    alice, bob, bobs_private, _ = people
    bobs_public = _node(bob, None,
                        "BOBS PUBLIC NOTE quoting {quote:%d}"
                        % bobs_private.id)
    root = _node(alice, None, "alice opens a public thread")
    quoting = _node(alice, root, "alice quotes {quote:%d}" % bobs_public.id)
    bobs_reply = _node(bob, quoting,
                       "bob quotes {quote:%d} himself" % bobs_public.id)
    _db.session.commit()

    _ask(bobs_reply, bob)

    alices = _message_with("alice quotes")
    assert "BOBS PUBLIC NOTE quoting {quote:%d}" % bobs_private.id in alices
    assert "BOBS PRIVATE DIARY" not in alices
    bobs = _message_with("himself")
    assert "BOBS PUBLIC NOTE quoting <quoted_node" in bobs
    assert "BOBS PRIVATE DIARY" in bobs


def test_a_deeper_nested_quote_stays_as_written(app, people):  # noqa: F811
    """Two public entries deep, the private one still stays as written."""
    alice, bob, bobs_private, _ = people
    inner = _node(bob, None, "INNER PUBLIC {quote:%d}" % bobs_private.id)
    outer = _node(alice, None, "OUTER PUBLIC {quote:%d}" % inner.id)
    root = _node(alice, None, "alice opens a public thread")
    quoting = _node(alice, root, "alice quotes {quote:%d}" % outer.id)
    bobs_reply = _node(bob, quoting, "bob answers")
    _db.session.commit()

    _ask(bobs_reply, bob)

    alices = _message_with("alice quotes")
    assert "OUTER PUBLIC" in alices and "INNER PUBLIC" in alices
    assert "INNER PUBLIC {quote:%d}" % bobs_private.id in alices
    assert "BOBS PRIVATE DIARY" not in _sent()


def test_a_deleted_quoted_entry_in_someone_elses_entry_stays_as_written(app, people):  # noqa: F811,E501
    alice, bob, _, _ = people
    gone = _node(alice, None, "ALICES DELETED NOTE")
    gone.deleted_at = datetime.utcnow()
    root = _node(alice, None, "alice opens a public thread")
    quoting = _node(alice, root, "alice quotes {quote:%d}" % gone.id)
    bobs_reply = _node(bob, quoting, "bob answers")
    _db.session.commit()

    _ask(bobs_reply, bob)

    alices = _message_with("alice quotes")
    assert "{quote:%d}" % gone.id in alices
    assert "ALICES DELETED NOTE" not in _sent()


# ── {quote_ext:ID} ───────────────────────────────────────────────────────

def test_a_saved_reference_in_someone_elses_entry_is_not_resolved(app, people):  # noqa: F811,E501
    alice, bob, _, _ = people
    bobs_item = _item(bob, "BOBS UPLOADED DOCUMENT")
    alices_item = _item(alice, "ALICES UPLOADED DOCUMENT")
    root = _node(alice, None, "alice opens a public thread")
    quoting = _node(alice, root, "alice quotes {quote_ext:%d} {quote_ext:%d}"
                    % (bobs_item.id, alices_item.id))
    text = "bob quotes his own {quote_ext:%d}" % bobs_item.id
    bobs_reply = _node(bob, quoting, text)
    _db.session.commit()

    _ask(bobs_reply, bob)

    alices = _message_with("alice quotes")
    assert "{quote_ext:%d}" % bobs_item.id in alices
    assert "{quote_ext:%d}" % alices_item.id in alices
    assert "UPLOADED DOCUMENT" not in alices
    # In bob's own entry his reference resolves, as before.
    bobs = _message_with("bob quotes his own")
    expected, _ = resolve_ext_quotes(text, bob.id, for_llm=True)
    assert bobs.endswith(expected)
    assert "BOBS UPLOADED DOCUMENT" in bobs


def test_someone_elses_ai_reply_adds_none_of_my_marks(app, people):  # noqa: F811
    """A {quote_ext:ID} in another person's AI reply names an item of
    mine: the prompt carries none of my marks on it."""
    alice, bob, _, _ = people
    bobs_item = _item(bob, "BOBS BOOKMARK", source="twitter_bookmark")
    bobs_item.read_at = datetime(2026, 11, 1, 8, 0)
    bobs_item.feedback = "good"
    root = _node(alice, None, "alice opens a public thread")
    alices_ai = _llm(root, alice, "Look: {quote_ext:%d}" % bobs_item.id,
                     status="completed")
    bobs_reply = _node(bob, alices_ai, "bob answers")
    _db.session.commit()

    _ask(bobs_reply, bob)

    sent = _sent()
    assert "marks on references" not in sent
    assert "BOBS BOOKMARK" not in sent
    assert "{quote_ext:%d}" % bobs_item.id in sent


def test_someone_elses_read_reply_resolves_none_of_my_references(app, monkeypatch, tmp_path):  # noqa: F811,E501
    """alice's Glean read reply, in a thread she made public, carries a
    {quote_ext:ID} of bob's item. bob's chat turn below it renders none
    of bob's references into it."""
    from backend.tests.test_read_turns import _first_read, _live, _placeholder
    from backend.tests.test_read_context import _user_node
    _, alice, llm_user, read, reply, _, _ = _first_read(monkeypatch, tmp_path)
    bob = _mk_user("bob", approved=True, plan="alpha")
    bobs_item = _item(bob, "BOBS UPLOADED DOCUMENT")
    reply = _fresh(reply.id)
    read = _fresh(read.id)
    read.privacy_level = "public"
    reply.privacy_level = "public"
    reply.set_content(reply.get_content()
                      + "\n{quote_ext:%d}" % bobs_item.id)
    question = _user_node(bob, reply.id, "bob asks about the picks")
    question.privacy_level = "public"
    _db.session.commit()
    chat = _placeholder(llm_user, bob, question.id)

    call, _ = _live(monkeypatch, bob, question, chat, "ok")

    joined = "\n".join(m["text"] for m in call["messages"])
    assert "bob asks about the picks" in joined
    assert "BOBS UPLOADED DOCUMENT" not in joined
    assert "{quote_ext:%d}" % bobs_item.id in joined


# ── a private thread is unchanged ────────────────────────────────────────

def test_a_private_thread_resolves_every_quote_as_before(app, people):  # noqa: F811,E501
    """bob's own private thread: every quote of his resolves with his
    access, nested ones too, byte for byte as the unrestricted
    resolvers give it."""
    alice, bob, bobs_private, alices_public = people
    item = _item(bob, "BOBS UPLOADED DOCUMENT")
    inner = _node(bob, None, "INNER PRIVATE {quote:%d}" % bobs_private.id,
                  privacy_level="private")
    root = _node(bob, None, "bob's private thread", privacy_level="private")
    text = ("bob quotes {quote:%d} {quote:%d} {quote_ext:%d}"
            % (inner.id, alices_public.id, item.id))
    entry = _node(bob, root, text, privacy_level="private")
    _db.session.commit()

    _ask(entry, bob)

    bobs = _message_with("bob quotes")
    expected, _ = resolve_quotes(text, bob.id, for_llm=True, tz_name="UTC")
    expected, _ = resolve_ext_quotes(expected, bob.id, for_llm=True)
    assert bobs.endswith(expected)
    assert "INNER PRIVATE" in bobs and "BOBS PRIVATE DIARY" in bobs
    assert "ALICES PUBLIC NOTE" in bobs and "BOBS UPLOADED DOCUMENT" in bobs

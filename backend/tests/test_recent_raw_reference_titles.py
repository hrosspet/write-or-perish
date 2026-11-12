"""Recent entries show a saved reference by title; the model opens it
when it needs the text (Peter, voice review 2026-11-12).

The recent raw window ({user_recent_raw}) carries the user's recent
entries as written. A {quote_ext:ID} in them (a clipped page, a
bookmark, a Glean pick) used to bring the reference's whole text into
the window. In every call with tools (any voice / textmode chain, and
its pre-warm) it is now one line: the title (with no title, the
author), where the reference came from, and, while the reply has the
read_full tool, the name read_full opens it by.

Everything else keeps the text: the user's data download, {user_export},
the thread's own quotes, calls without tools. A window that quotes no
reference is the same bytes as before (prompt cache).
Runs the real task body through the test_retrieval_loop harness;
nothing reaches a real model.
"""
from datetime import datetime, timedelta

import pytest

from backend.tests.test_retrieval_loop import (  # noqa: F401 (fixture)
    app, _llm_task_mod, generate_llm_response, _FakeSelf,
    _ScriptedProvider, _mk_user, _resp, _build_chain,
)
from backend.tests.test_raw_window_duplication import (  # noqa: F401 (fixtures)
    NOW, alice, frozen_export_time, fake_redis, _node, _session,
    _new_session, _render, _window,
)
from backend.tests.test_training_key_payload import (  # noqa: F401
    keys, _keys_used, _payload, _train_chain, _entry, _plain_thread,
)
from backend.tests.test_training_key_own_rows import (
    _set_prompt, _train_user,
)
from backend.extensions import db as _db
from backend.models import ExternalItem, Node, User
from backend.routes.export_data import (
    build_user_export_content, iter_user_export,
)
from backend.utils import document_reference
from backend.utils.quotes import (
    REFERENCE_LINE, REFERENCE_LINE_NAMED,
    reference_id_from_name, reference_name, reference_title_lines,
)

CLIP_TEXT = "THE CLIPPED PAGE BODY " + "paragraph " * 50
TWEET_TEXT = "THE GLEANED TWEET about attention"
DOC_BODY = "THE DOCUMENT BODY " + "chapter " * 50


def _item(user, source, text, title=None, author=None, url=None,
          external_id=None):
    item = ExternalItem(user_id=user.id, source=source,
                        external_id=external_id or f"{source}-{text[:12]}",
                        title=title, author_handle=author, url=url)
    item.set_content(text)
    _db.session.add(item)
    _db.session.flush()
    return item


def _clip(user):
    return _item(user, "web_clip", CLIP_TEXT, title="How to Do Great Work",
                 author="Paul Graham",
                 url="https://www.paulgraham.com/greatwork.html")


def _pick(user):
    return _item(user, "read_pick", TWEET_TEXT, author="visa",
                 url="https://x.com/visa/status/1", external_id="1")


def _document(user):
    return _item(user, "document", document_reference.compose(
        "A Pattern Language", "Christopher Alexander", DOC_BODY))


def _line(item, what, source, named=True):
    opener = f'; read_full "reference {item.id}" opens it' if named else ""
    return f"[Saved reference {what} ({source}), text not included{opener}]"


CLIP_LINE = ('"How to Do Great Work"', "page from paulgraham.com")


def _earlier_session_quoting(user, *items, llm=False):
    """An earlier textmode session whose entry (or AI reply) quotes
    *items*."""
    markers = " ".join("{quote_ext:%d}" % i.id for i in items)
    return _session(user, "textmode", NOW - timedelta(days=2), [
        ("EARLIER ENTRY", 20, False),
        (f"Here is what I kept: {markers} and why", 20, llm),
    ])


# ── the line ─────────────────────────────────────────────────────────────

def test_the_window_names_each_quoted_reference_in_one_line(app, alice):  # noqa: F811
    clip, pick, doc = _clip(alice), _pick(alice), _document(alice)
    _earlier_session_quoting(alice, clip, pick, doc)
    system, _entry, _reply = _new_session(alice)

    text = _render(system, alice)
    window = _window(text)

    assert "EARLIER ENTRY" in window
    assert _line(clip, '"How to Do Great Work"',
                 "page from paulgraham.com") in window
    # A Glean pick (a tweet) has no title: its author.
    assert _line(pick, "by @visa",
                 "tweet from the Community Archive") in window
    # A document's title lives in its text.
    assert _line(doc, '"A Pattern Language"',
                 "document the user uploaded") in window
    for body in ("THE CLIPPED PAGE BODY", "THE GLEANED TWEET",
                 "THE DOCUMENT BODY", "--- Saved reference"):
        assert body not in text
    assert "{quote_ext:" not in text


def test_a_glean_reply_names_its_picks_the_same_way(app, alice):  # noqa: F811
    pick = _pick(alice)
    _earlier_session_quoting(alice, pick, llm=True)
    system, _entry, _reply = _new_session(alice)

    window = _window(_render(system, alice))

    assert _line(pick, "by @visa",
                 "tweet from the Community Archive") in window
    assert TWEET_TEXT not in window


def test_a_marker_inside_a_title_stays_as_written(app, alice):  # noqa: F811
    other = _clip(alice)
    clip = _item(alice, "web_clip", "OUTER BODY",
                 title="Notes on {quote_ext:%d}" % other.id,
                 url="https://example.com/a")
    _earlier_session_quoting(alice, clip)
    system, _entry, _reply = _new_session(alice)

    text = _render(system, alice)

    assert '"Notes on {quote_ext:%d}"' % other.id in text
    assert "THE CLIPPED PAGE BODY" not in text
    assert "\ue000" not in text


def test_reference_title_lines_unit(app):  # noqa: F811
    alice = _mk_user("alice", approved=True)  # noqa: F811
    bob = _mk_user("bob", approved=True)
    mine = _item(alice, "twitter_bookmark", "MINE", author="visa")
    bare = _item(alice, "web_clip", "BARE", url=None)
    theirs = _item(bob, "twitter_bookmark", "THEIRS", author="bob")
    _db.session.commit()

    text, named, titled = reference_title_lines(
        "a {quote_ext:%d} b {quote_ext:%d} c {quote_ext:%d}"
        % (mine.id, bare.id, theirs.id), alice.id)

    assert _line(mine, "by @visa",
                 "tweet from the user's Twitter bookmarks") in text
    assert (f"[Saved reference (saved page), text not included; "
            f'read_full "reference {bare.id}" opens it]') in text
    assert f"[Quoted reference inaccessible: item {theirs.id}]" in text
    assert "MINE" not in text and "THEIRS" not in text
    assert named == [mine.id, bare.id]
    assert titled == []

    plain, _named, _titled = reference_title_lines(
        "a {quote_ext:%d}" % mine.id, alice.id, named=False)
    assert plain == "a " + _line(
        mine, "by @visa", "tweet from the user's Twitter bookmarks",
        named=False)
    assert reference_title_lines("no markers", alice.id) == (
        "no markers", [], [])


def test_reference_names_round_trip():
    assert reference_name(12) == "reference 12"
    for ref in ("reference 12", "Reference 12", "reference #12",
                '"reference 12"', " REFERENCE12 "):
        assert reference_id_from_name(ref) == 12, ref
    for ref in ("12", "A", "ref 12", "reference", "reference 12 and 13"):
        assert reference_id_from_name(ref) is None, ref


# ── the prompt cache ─────────────────────────────────────────────────────

def test_a_window_quoting_no_reference_is_the_same_bytes(
        app, alice, frozen_export_time, monkeypatch):  # noqa: F811
    own = _session(alice, "textmode", NOW - timedelta(days=3), [
        ("AN OLDER ENTRY", 20, False)])[1]
    _session(alice, "textmode", NOW - timedelta(days=2), [
        ("EARLIER ENTRY quoting {quote:%d}" % own.id, 20, False),
        ("EARLIER REPLY", 20, True),
    ])
    _clip(alice)   # saved, but quoted nowhere
    system, _entry, _reply = _new_session(alice)
    assert (_llm_task_mod._recent_raw_reference_lines(True)
            == REFERENCE_LINE_NAMED)

    with_titles = _render(system, alice)
    monkeypatch.setattr(_llm_task_mod, "_recent_raw_reference_lines",
                        lambda tools: None)
    without = _render(system, alice)

    assert "AN OLDER ENTRY" in _window(with_titles)
    assert with_titles == without
    raw = _llm_task_mod.get_user_recent_raw_content
    windows = [raw(alice.id, created_before=system.created_at,
                   reference_titles=mode)
               for mode in (None, REFERENCE_LINE, REFERENCE_LINE_NAMED)]
    assert windows[0] == windows[1] == windows[2]


def test_the_reply_and_the_pre_warm_render_the_same_title_lines(
        app, alice, frozen_export_time, fake_redis):  # noqa: F811
    clip = _clip(alice)
    _earlier_session_quoting(alice, clip)
    system, entry, reply = _new_session(alice)

    _ScriptedProvider.reset([_resp("An answer.")])
    generate_llm_response(_FakeSelf(), entry.id, reply.id, "gpt-5",
                          alice.id, source_mode="textmode")
    sent = _ScriptedProvider.calls[-1]["messages"][0]["text"]

    assert sent == _render(system, alice)
    assert _line(clip, '"How to Do Great Work"',
                 "page from paulgraham.com") in sent
    assert "THE CLIPPED PAGE BODY" not in sent


# ── what keeps the text ──────────────────────────────────────────────────

def test_the_data_download_keeps_the_reference_text(app, alice):  # noqa: F811
    clip = _clip(alice)
    _earlier_session_quoting(alice, clip)
    _new_session(alice)

    built = build_user_export_content(alice, filter_ai_usage=False)
    streamed = "".join(iter_user_export(alice))

    for text in (built, streamed):
        assert '--- Saved reference "How to Do Great Work"' in text
        assert "THE CLIPPED PAGE BODY" in text
        assert 'read_full "reference' not in text


def test_user_export_keeps_the_text_next_to_the_window(app, alice):  # noqa: F811
    clip = _clip(alice)
    _earlier_session_quoting(alice, clip)
    system, entry, reply = _new_session(alice)
    entry.set_content("today's entry {user_export?max_export_tokens=5000}")
    _db.session.commit()

    _ScriptedProvider.reset([_resp("An answer.")])
    generate_llm_response(_FakeSelf(), entry.id, reply.id, "gpt-5",
                          alice.id, source_mode="textmode")
    messages = [m["text"] for m in _ScriptedProvider.calls[-1]["messages"]]

    window = _window(messages[0])
    assert _line(clip, '"How to Do Great Work"',
                 "page from paulgraham.com") in window
    assert "THE CLIPPED PAGE BODY" not in messages[0]
    archive = next(m for m in messages if "today's entry" in m)
    assert "THE CLIPPED PAGE BODY" in archive


@pytest.mark.parametrize("key", ["reflect", "orient"])
def test_a_prompt_without_read_full_keeps_the_text(app, alice, key):  # noqa: F811
    clip = _clip(alice)
    _earlier_session_quoting(alice, clip)
    system, _last = _session(alice, key, NOW - timedelta(hours=1),
                             [("today", 3, False)])

    window = _window(_render(system, alice))

    assert "THE CLIPPED PAGE BODY" in window
    assert 'read_full "reference' not in window


def test_a_reply_without_tools_keeps_the_text(app, alice, frozen_export_time):  # noqa: F811
    """A Reflect chain's reply has no tools, and its render is not the
    one an agentic reply of the same prompt would read."""
    clip = _clip(alice)
    _earlier_session_quoting(alice, clip)
    system, entry = _session(alice, "reflect", NOW - timedelta(hours=1),
                             [("today", 3, False)])
    reply = _node(alice, entry, "[pending]", NOW, llm=True)
    reply.llm_task_status = "pending"
    _db.session.commit()

    _ScriptedProvider.reset([_resp("An answer.")])
    generate_llm_response(_FakeSelf(), entry.id, reply.id, "gpt-5",
                          alice.id)
    call = _ScriptedProvider.calls[-1]

    assert not call["tools"]
    assert "THE CLIPPED PAGE BODY" in _window(call["messages"][0]["text"])
    assert 'read_full "reference' not in call["messages"][0]["text"]
    variant = _llm_task_mod._system_render_variant
    assert variant(system, alice.id).endswith("t0")
    agentic = _new_session(alice)[0]
    assert variant(agentic, alice.id) == _llm_task_mod._render_variant(
        alice.id)


def test_a_placeholder_typed_into_a_message(app):  # noqa: F811
    """With no agentic prompt the call has no tools: the text. Typed
    into a message of a voice / textmode chain, the call has tools: the
    line."""
    alice, system, user_node, llm_node = _build_chain("textmode")  # noqa: F811
    clip = _clip(alice)
    old = _entry(alice, "MY NOTE on {quote_ext:%d}" % clip.id, "chat")
    old.created_at = datetime(2020, 1, 1)
    user_node.set_content("look back {user_recent_raw}")
    note, plain_llm = _plain_thread(
        alice, User.query.filter_by(username="gpt-5").first(),
        "look back {user_recent_raw}", ai_usage="chat")
    _db.session.commit()

    _ScriptedProvider.reset([_resp("Agentic."), _resp("Plain.")])
    generate_llm_response(_FakeSelf(), user_node.id, llm_node.id, "gpt-5",
                          alice.id, source_mode="textmode")
    generate_llm_response(_FakeSelf(), note.id, plain_llm.id, "gpt-5",
                          alice.id)

    agentic, plain = _payload(0), _payload(1)
    assert _ScriptedProvider.calls[0]["tools"]
    assert _line(clip, *CLIP_LINE) in agentic
    assert "THE CLIPPED PAGE BODY" not in agentic
    assert not _ScriptedProvider.calls[1]["tools"]
    assert "THE CLIPPED PAGE BODY" in plain
    assert 'read_full "reference' not in plain


def test_without_read_full_the_line_leaves_out_its_name(
        app, alice, frozen_export_time, fake_redis, monkeypatch):  # noqa: F811
    """The SEMANTIC_SEARCH_AGENTIC killswitch drops read_full; the reply
    still has the tools that write, so it gets the line, without the
    name read_full would open it by. The pre-warm renders the same
    bytes."""
    clip = _clip(alice)
    _earlier_session_quoting(alice, clip)
    system, entry, reply = _new_session(alice)
    monkeypatch.setitem(app.config, "SEMANTIC_SEARCH_AGENTIC", False)

    _ScriptedProvider.reset([_resp("An answer.")])
    generate_llm_response(_FakeSelf(), entry.id, reply.id, "gpt-5",
                          alice.id, source_mode="textmode")
    call = _ScriptedProvider.calls[-1]
    sent = call["messages"][0]["text"]

    tools = [t["name"] for t in call["tools"]]
    assert "read_full" not in tools and tools
    assert _line(clip, *CLIP_LINE, named=False) in _window(sent)
    assert "THE CLIPPED PAGE BODY" not in sent
    assert "read_full" not in sent
    fake_redis.store.clear()
    assert sent == _render(system, alice)


def test_with_the_references_toggle_off_the_line_keeps_its_name(app, alice):  # noqa: F811
    """The toggle decides what a search returns; read_full stays in the
    tool list and opens the reference by its name."""
    alice.external_content_enabled = False
    clip = _clip(alice)
    _earlier_session_quoting(alice, clip)
    system, _entry, _reply = _new_session(alice)

    assert _line(clip, *CLIP_LINE) in _window(_render(system, alice))


# ── the training key ─────────────────────────────────────────────────────

def _ask_with_window(item_factory):
    alice, system, user_node, llm_node = _train_chain("textmode")  # noqa: F811
    _train_user(alice)
    _set_prompt(system, "system prompt body\n{user_recent_raw}")
    item = item_factory(alice)
    old = _entry(alice, "MY NOTE on {quote_ext:%d}" % item.id, "train")
    old.created_at = datetime(2020, 1, 1)
    _db.session.commit()

    _ScriptedProvider.reset([_resp("Hello.")])
    generate_llm_response(_FakeSelf(), user_node.id, llm_node.id, "gpt-5",
                          alice.id, source_mode="textmode")
    return item


def test_a_title_keeps_the_call_off_the_training_key(app, keys):  # noqa: F811
    """A title is the reference's own words (Training licence)."""
    clip = _ask_with_window(_clip)

    sent = _payload(0)
    assert "MY NOTE on" in sent
    assert f'read_full "reference {clip.id}"' in sent
    assert "THE CLIPPED PAGE BODY" not in sent
    assert _keys_used() == ["sk-chat"]


def test_an_author_and_source_line_leaves_the_training_key(app, keys):  # noqa: F811
    """A line without a title carries no words of the reference, only
    its author and source: the user's own 'train' entries decide."""
    pick = _ask_with_window(_pick)

    sent = _payload(0)
    assert "MY NOTE on" in sent
    assert f'read_full "reference {pick.id}"' in sent
    assert TWEET_TEXT not in sent
    assert _keys_used() == ["sk-train"]


# ── read_full opens a reference by its name ──────────────────────────────

def test_read_full_opens_a_reference_by_its_name(app):  # noqa: F811
    """Also with the "External references" toggle off (the default): the
    toggle decides what a search returns."""
    alice, system, user_node, llm_node = _build_chain("textmode")  # noqa: F811
    alice.external_content_enabled = False
    clip = _clip(alice)
    _db.session.commit()
    clip_id = clip.id

    _ScriptedProvider.reset([
        _resp("Opening that page.",
              tool_calls=[{"id": "t1", "name": "read_full",
                           "input": {"ref": reference_name(clip_id)}}]),
        _resp("It argues for curiosity."),
    ])
    generate_llm_response(_FakeSelf(), user_node.id, llm_node.id, "gpt-5",
                          alice.id, source_mode="textmode")

    assert len(_ScriptedProvider.calls) == 2
    round2 = "\n".join(
        m["text"] for m in _ScriptedProvider.calls[1]["messages"])
    assert "THE CLIPPED PAGE BODY" in round2
    tools = [t["name"] for t in _ScriptedProvider.calls[0]["tools"]]
    assert "read_full" in tools
    _db.session.expire_all()
    meta = Node.query.get(llm_node.id).tool_calls_meta
    assert '"kind": "external"' in meta


def test_read_full_by_name_opens_only_the_users_own_reference(app):  # noqa: F811
    alice, system, user_node, llm_node = _build_chain("textmode")  # noqa: F811
    bob = _mk_user("bob", approved=True)
    theirs = _clip(bob)
    _db.session.commit()

    results = _llm_task_mod._execute_tool_calls(
        [{"id": "t1", "name": "read_full",
          "input": {"ref": reference_name(theirs.id)}}],
        llm_node, [system, user_node, llm_node], alice.id, quote_labels={})

    assert results[0]["status"] == "error"
    assert "Unknown reference" in results[0]["error"]
    assert "url" not in results[0]

"""Turns of a read thread (2026-09-19, the second feedback round).

Only a read feeds the day of tweets to the model: the first reply under
the read prompt, and a reply asked for directly under a read reply (a
read-again, where the model sees its earlier picks and the reader's
marks and decides itself whether to repeat one). A user message after a
read reply makes the next reply a chat turn: the picks and the marks are
in the context, the day is not, and the agentic prompt stays when the
chain has one (a Text-mode session under the read reply, #323). What the reader has already seen (read
marks, bookmarks) never reaches the model as a candidate, and each
render's numbering is pinned on the reply (FeedRender).

Runs the real task body through the test_retrieval_loop harness on the
live path (ca_live=True), like test_read_context.
"""
import json
from datetime import datetime

import pytest  # noqa: F401

from backend.tests.test_retrieval_loop import (  # noqa: F401 (fixture)
    app, _llm_task_mod, generate_llm_response, _FakeSelf, _mk_user, _resp,
    _fresh,
)
from backend.tests.test_read_context import (
    _CapturingProvider, _prompt_node, _user_node, _stub_archive, _feed_json,
    _voice_thread_with_read, CORPUS, REFS,
)
from backend.extensions import db as _db
from backend.models import User, Node, ExternalItem, FeedPick, FeedRender
from backend.utils.ca_feed import (
    CA_CHAT_TURN_NOTE, CA_READ_AGAIN_TURN, CA_TWEETS_CHAT_STUB,
    READ_FURTHER_MARKER, seen_tweet_ids, refresh_snapshot_for_read,
)


def _capture_render(monkeypatch, tmp_path):
    """The two-tweet stub archive, recording each render's kwargs."""
    _stub_archive(monkeypatch, tmp_path)
    from backend.utils import community_archive as ca
    calls = []

    def _render(*args, **kwargs):
        calls.append(kwargs)
        seen = kwargs.get("exclude_tweet_ids") or ()
        refs = {n: r for n, r in REFS.items() if r["tweet_id"] not in set(seen)}
        stats = {"export_id": "exp-1", "tweets": len(refs), "accounts": 2,
                 "excluded": len(REFS) - len(refs), "scope": "all",
                 "window_start_at": datetime(2026, 9, 18, 7, 0),
                 "window_end_at": datetime(2026, 9, 19, 7, 0)}
        return CORPUS, stats, refs
    monkeypatch.setattr(ca, "render_recent_tweets", _render)
    return calls


def _placeholder(llm_user, alice, parent_id):
    n = Node(user_id=llm_user.id, human_owner_id=alice.id,
             parent_id=parent_id, node_type="llm", llm_model="gpt-5",
             llm_task_status="pending", privacy_level="private",
             ai_usage="chat")
    n.set_content("[LLM response generation pending...]")
    _db.session.add(n)
    _db.session.commit()
    return n


def _live(monkeypatch, alice, parent, llm_node, response_text):
    _CapturingProvider.kwargs = []
    _CapturingProvider.reset([_resp(response_text)])
    monkeypatch.setattr(_llm_task_mod, "LLMProvider", _CapturingProvider)
    generate_llm_response(_FakeSelf(), parent.id, llm_node.id, "gpt-5",
                          alice.id, source_mode=None, ca_live=True)
    return _CapturingProvider.calls[-1], _CapturingProvider.kwargs[-1]


def _first_read(monkeypatch, tmp_path):
    renders = _capture_render(monkeypatch, tmp_path)
    alice = _mk_user("alice", approved=True, plan="alpha", is_admin=True)
    llm_user = _mk_user("gpt-5", twitter_id="llm-gpt-5")
    read = _prompt_node(alice, "read")
    reply = _placeholder(llm_user, alice, read.id)
    call, kwargs = _live(monkeypatch, alice, read, reply, _feed_json([
        {"n": 2, "qt": "Meets your pacing question.", "relevance": 40,
         "recommend": True},
    ]))
    return renders, alice, llm_user, read, _fresh(reply.id), call, kwargs


def _texts(call):
    return [m["text"] for m in call["messages"]]


# ── the first read ───────────────────────────────────────────────────────

def test_first_read_pins_its_render(app, monkeypatch, tmp_path):  # noqa: F811
    renders, alice, _, _, reply, call, kwargs = _first_read(monkeypatch, tmp_path)

    assert reply.llm_task_status == "completed"
    assert kwargs["output_schema"]["required"] == ["verdict", "picks"]
    assert "[#1] first tweet" in "\n".join(_texts(call))
    # Nothing seen yet: nothing excluded.
    assert set(renders[0]["exclude_tweet_ids"]) == set()
    row = FeedRender.query.filter_by(node_id=reply.id).one()
    assert row.tweet_ids == "111,222"
    assert row.tweet_id_for(2) == "222"
    assert row.tweet_id_for(3) is None
    assert (row.tweet_count, row.account_count, row.excluded_count) == (2, 2, 0)
    assert row.export_id == "exp-1"
    assert row.window_end == datetime(2026, 9, 19, 7, 0)
    assert reply.feed_render is row


def test_seen_tweets_never_reach_the_model(app, monkeypatch, tmp_path):  # noqa: F811
    renders = _capture_render(monkeypatch, tmp_path)
    alice = _mk_user("alice", approved=True, plan="alpha", is_admin=True)
    llm_user = _mk_user("gpt-5", twitter_id="llm-gpt-5")
    # Only what the reader marked read is seen (#352), from any tweet
    # row: a bookmark not marked read stays in (111, in the day), a read
    # clipped tweet (stored as a bookmark) goes; so do a read-marked pick
    # (222, in the day) and a read-marked archive import. An unread pick
    # and a web clip (not a tweet) don't count.
    for source, ext_id, read_at in [
        ("twitter_bookmark", "111", None),
        ("twitter_bookmark", "555", datetime(2026, 9, 18, 9, 0)),
        ("read_pick", "222", datetime(2026, 9, 18, 8, 0)),
        ("read_pick", "444", None),
        ("community_archive", "666", datetime(2026, 9, 18, 8, 0)),
        ("web_clip", "a" * 64, datetime(2026, 9, 18, 8, 0)),
    ]:
        item = ExternalItem(user_id=alice.id, source=source, external_id=ext_id,
                            read_at=read_at)
        item.set_content("x")
        _db.session.add(item)
    # Someone else's marks are not the reader's.
    bob = _mk_user("bob", approved=True, plan="alpha")
    other = ExternalItem(user_id=bob.id, source="twitter_bookmark",
                         external_id="111", read_at=datetime(2026, 9, 18, 8, 0))
    other.set_content("y")
    _db.session.add(other)
    _db.session.commit()

    assert seen_tweet_ids(alice.id) == {"555", "222", "666"}

    read = _prompt_node(alice, "read")
    reply = _placeholder(llm_user, alice, read.id)
    _live(monkeypatch, alice, read, reply, _feed_json([]))
    assert set(renders[0]["exclude_tweet_ids"]) == {"555", "222", "666"}
    row = FeedRender.query.filter_by(node_id=reply.id).one()
    assert row.tweet_ids == "111"
    assert row.excluded_count == 1


# ── after the first read ─────────────────────────────────────────────────

def test_reply_directly_under_a_read_reply_is_another_read(app, monkeypatch, tmp_path):  # noqa: F811
    renders, alice, llm_user, read, reply, _, _ = _first_read(monkeypatch, tmp_path)
    pick = ExternalItem.query.filter_by(user_id=alice.id, external_id="222").one()
    pick.read_at = datetime(2026, 9, 19, 8, 0)
    pick.feedback = "good"
    _db.session.commit()

    again = _placeholder(llm_user, alice, reply.id)
    call, kwargs = _live(monkeypatch, alice, _fresh(reply.id), again,
                         _feed_json([{"n": 1, "qt": "The other one.",
                                      "relevance": 30, "recommend": False}]))
    texts = _texts(call)
    joined = "\n".join(texts)
    # The day is rendered again, without the pick the reader marked read.
    assert len(renders) == 2
    assert set(renders[1]["exclude_tweet_ids"]) == {"222"}
    assert "[#1] first tweet" in joined
    assert kwargs["output_schema"]["required"] == ["verdict", "picks"]
    # The earlier reply is in the context with its quoted tweet resolved
    # (stable: no marks in the assistant turn) ...
    assistant = [m for m in call["messages"] if m["role"] == "assistant"]
    assert len(assistant) == 1
    assert "second tweet" in assistant[0]["text"]
    assert '<quoted_reference id="%d"' % pick.id in assistant[0]["text"]
    assert 'read="' not in assistant[0]["text"]
    assert "{quote_ext:" not in assistant[0]["text"]
    # ... and the closing user turn carries the marks and the request.
    last = call["messages"][-1]
    assert last["role"] == "user"
    assert ("reference %d (@bob_b) — read 2026-09-19, rated a good quote"
            % pick.id) in last["text"]
    assert CA_READ_AGAIN_TURN in last["text"]
    assert "[continue]" not in last["text"]

    again = _fresh(again.id)
    assert again.llm_task_status == "completed"
    assert FeedRender.query.filter_by(node_id=again.id).one().tweet_ids == "111"
    picks = FeedPick.query.filter_by(node_id=again.id).all()
    assert [(p.rank, p.item.external_id) for p in picks] == [(1, "111")]


def test_user_message_after_a_read_reply_is_chat(app, monkeypatch, tmp_path):  # noqa: F811
    renders, alice, llm_user, read, reply, _, _ = _first_read(monkeypatch, tmp_path)
    pick = ExternalItem.query.filter_by(user_id=alice.id, external_id="222").one()
    pick.feedback = "bad"
    _db.session.commit()

    question = _user_node(alice, reply.id, "why the second one?")
    _db.session.commit()
    chat = _placeholder(llm_user, alice, question.id)
    call, kwargs = _live(monkeypatch, alice, question, chat, "Because it fit.")
    texts = _texts(call)
    joined = "\n".join(texts)
    # No second render, no day in the context, no feed schema.
    assert len(renders) == 1
    assert "[#1] first tweet" not in joined
    assert CA_TWEETS_CHAT_STUB in texts[0]
    assert "{ca_tweets" not in joined
    assert kwargs.get("output_schema") is None
    # The picks and the reader's marks are.
    assistant = [m for m in call["messages"] if m["role"] == "assistant"]
    assert "second tweet" in assistant[0]["text"]
    assert "why the second one?" in joined
    last = call["messages"][-1]
    assert last["role"] == "user"
    assert ("reference %d (@bob_b) — rated a bad quote" % pick.id) in last["text"]
    assert CA_READ_AGAIN_TURN not in last["text"]
    # ... and the closing note says this is a conversation, not a read
    # (the prompt above still asks for a verdict and picks).
    assert CA_CHAT_TURN_NOTE in last["text"]

    chat = _fresh(chat.id)
    assert chat.llm_task_status == "completed"
    assert chat.get_content() == "Because it fit."
    assert FeedRender.query.filter_by(node_id=chat.id).first() is None
    assert FeedPick.query.filter_by(node_id=chat.id).count() == 0


def test_read_further_from_a_comment_is_a_read_with_the_thread_in_view(app, monkeypatch, tmp_path):  # noqa: F811
    """read prompt -> read reply -> the user's comment -> the Read button
    (a placeholder marked "_read"): a read, not a chat. The day is
    rendered again with the comment, the earlier picks and the marks in
    view, the feed shape is asked for, and the render is pinned."""
    renders, alice, llm_user, read, reply, _, _ = _first_read(monkeypatch, tmp_path)
    pick = ExternalItem.query.filter_by(user_id=alice.id, external_id="222").one()
    # As the feedback route leaves it: a verdict marks the pick read.
    pick.feedback = "bad"
    pick.read_at = datetime(2026, 9, 21, 9, 0)
    _db.session.commit()
    comment = _user_node(alice, reply.id, "more on people building tools, please")
    _db.session.commit()
    further = _placeholder(llm_user, alice, comment.id)
    further.tool_calls_meta = json.dumps([{"name": READ_FURTHER_MARKER}])
    _db.session.commit()
    call, kwargs = _live(monkeypatch, alice, comment, further,
                         _feed_json([{"n": 1, "qt": "The other one.",
                                      "relevance": 30, "recommend": False}]))
    joined = "\n".join(_texts(call))
    assert len(renders) == 2
    assert "[#1] first tweet" in joined
    assert "more on people building tools, please" in joined
    assert kwargs["output_schema"]["required"] == ["verdict", "picks"]
    last = call["messages"][-1]
    assert last["role"] == "user"
    assert ("reference %d (@bob_b) — read 2026-09-21, rated a bad quote"
            % pick.id) in last["text"]
    assert CA_READ_AGAIN_TURN in last["text"]
    assert CA_CHAT_TURN_NOTE not in last["text"]
    # The read pick is out of the list; the render is pinned on the reply.
    assert set(renders[1]["exclude_tweet_ids"]) == {"222"}
    assert FeedRender.query.filter_by(node_id=further.id).one().tweet_ids == "111"
    # The marker survives the run: a rerun classifies the same way.
    assert any(m.get("name") == READ_FURTHER_MARKER
               for m in json.loads(_fresh(further.id).tool_calls_meta))


def test_reply_under_a_chat_reply_stays_chat(app, monkeypatch, tmp_path):  # noqa: F811
    renders, alice, llm_user, read, reply, _, _ = _first_read(monkeypatch, tmp_path)
    question = _user_node(alice, reply.id, "and the first?")
    _db.session.commit()
    chat = _placeholder(llm_user, alice, question.id)
    _live(monkeypatch, alice, question, chat, "Not for you.")
    follow = _placeholder(llm_user, alice, chat.id)
    call, kwargs = _live(monkeypatch, alice, _fresh(chat.id), follow, "Still not.")
    assert len(renders) == 1
    assert kwargs.get("output_schema") is None
    # The chat note closes the prompt (a user turn), so no "[continue]".
    last = call["messages"][-1]
    assert last["role"] == "user"
    assert CA_CHAT_TURN_NOTE in last["text"]
    assert "[continue]" not in "\n".join(_texts(call))
    assert _fresh(follow.id).get_content() == "Still not."


def test_user_message_under_the_prompt_before_any_reply_is_a_read(app, monkeypatch, tmp_path):  # noqa: F811
    renders = _capture_render(monkeypatch, tmp_path)
    alice = _mk_user("alice", approved=True, plan="alpha", is_admin=True)
    llm_user = _mk_user("gpt-5", twitter_id="llm-gpt-5")
    read = _prompt_node(alice, "read")
    note = _user_node(alice, read.id, "focus on people building tools")
    _db.session.commit()
    reply = _placeholder(llm_user, alice, note.id)
    call, kwargs = _live(monkeypatch, alice, note, reply, _feed_json([]))
    assert len(renders) == 1
    assert "[#1] first tweet" in "\n".join(_texts(call))
    assert kwargs["output_schema"]["required"] == ["verdict", "picks"]
    assert FeedRender.query.filter_by(node_id=reply.id).one()


# ── the snapshot refresh before a read ───────────────────────────────────

def test_refresh_skips_without_a_snapshot(app, monkeypatch, tmp_path):  # noqa: F811
    from backend.utils import community_archive as ca
    monkeypatch.setattr(ca, "fetch_latest_manifest",
                        lambda: (_ for _ in ()).throw(AssertionError("no fetch")))
    assert refresh_snapshot_for_read(tmp_path) is None


def test_refresh_brings_an_existing_snapshot_up_to_date(app, monkeypatch, tmp_path):  # noqa: F811
    from backend.utils import community_archive as ca
    for name in ca.SNAPSHOT_FILES:
        (tmp_path / name).write_bytes(b"")
    (tmp_path / "export_id").write_text("old")
    monkeypatch.setattr(ca, "fetch_latest_manifest",
                        lambda: {"export_id": "new", "package_paths": []})
    downloaded = []

    def _download(d, manifest, on_progress):
        downloaded.append(manifest["export_id"])
        (d / "export_id").write_text(manifest["export_id"])
        return manifest["export_id"]
    monkeypatch.setattr(ca, "_download_snapshot", _download)

    assert refresh_snapshot_for_read(tmp_path) == "new"
    assert downloaded == ["new"]
    assert (tmp_path / ".lock").exists()
    # Current already: one manifest GET, no download.
    assert refresh_snapshot_for_read(tmp_path) == "new"
    assert downloaded == ["new"]


def test_refresh_failure_leaves_the_cached_export(app, monkeypatch, tmp_path):  # noqa: F811
    from backend.utils import community_archive as ca
    for name in ca.SNAPSHOT_FILES:
        (tmp_path / name).write_bytes(b"")
    (tmp_path / "export_id").write_text("old")

    def _boom():
        raise OSError("network down")
    monkeypatch.setattr(ca, "fetch_latest_manifest", _boom)
    assert refresh_snapshot_for_read(tmp_path) is None
    assert ca.snapshot_export_id(tmp_path) == "old"


def test_refresh_waits_for_batches_from_before_pinning(app, monkeypatch, tmp_path):  # noqa: F811
    """A batch submitted before renders were pinned re-renders the day on
    collect, so the snapshot must not change under it."""
    from backend.utils import community_archive as ca
    import json
    for name in ca.SNAPSHOT_FILES:
        (tmp_path / name).write_bytes(b"")
    (tmp_path / "export_id").write_text("old")
    monkeypatch.setattr(ca, "fetch_latest_manifest",
                        lambda: (_ for _ in ()).throw(AssertionError("no fetch")))
    alice = _mk_user("alice", approved=True, plan="alpha", is_admin=True)
    llm_user = _mk_user("gpt-5", twitter_id="llm-gpt-5")
    read = _prompt_node(alice, "read")
    legacy = _placeholder(llm_user, alice, read.id)
    legacy.llm_task_status = "processing"
    legacy.tool_calls_meta = json.dumps([{
        "name": "_batch", "batch_id": "b1", "custom_id": "node-1",
        "provider": "openai", "status": "submitted"}])
    _db.session.commit()
    assert refresh_snapshot_for_read(tmp_path) is None


def test_chat_turn_under_a_voice_thread_is_agentic(app, monkeypatch, tmp_path):  # noqa: F811
    """voice prompt -> sharing -> read_thread -> read reply -> question:
    the read ran without the agentic prompt; the chat turn about the
    picks keeps it (persona, tools), the day stays out, and the chat
    note rides with the agentic notes (#323)."""
    _capture_render(monkeypatch, tmp_path)
    alice, read, reply = _voice_thread_with_read()
    read_call, _ = _live(monkeypatch, alice, read, reply, _feed_json([
        {"n": 2, "qt": "Meets your pacing question.", "relevance": 40,
         "recommend": True}]))
    assert "AGENTIC PERSONA AND TOOLS" not in "\n".join(_texts(read_call))
    assert read_call["tools"] is None
    llm_user = User.query.get(reply.user_id)
    question = _user_node(alice, reply.id, "why that one?")
    _db.session.commit()
    chat = _placeholder(llm_user, alice, question.id)
    call, kwargs = _live(monkeypatch, alice, question, chat, "Because.")
    joined = "\n".join(_texts(call))
    assert "AGENTIC PERSONA AND TOOLS" in joined
    assert "my morning: unsure about pacing" in joined
    assert call["tools"] is not None
    assert kwargs.get("output_schema") is None
    assert "[#1] first tweet" not in joined
    assert CA_CHAT_TURN_NOTE in call["messages"][-1]["text"]
    assert _fresh(chat.id).get_content() == "Because."


def test_chat_turn_under_a_read_root_is_agentic_once_text_mode_is_attached(app, monkeypatch, tmp_path):  # noqa: F811
    """read prompt -> read reply -> textmode prompt (what POST
    /textmode/from-node attaches under the reply) -> question: the chat
    turn runs under the agentic prompt with tools, the day stays a stub,
    and the reader's marks and the chat note ride with the agentic
    notes. A read prompt attached under that chat reply is a read: the
    textmode prompt is stripped and the feed shape is back."""
    renders, alice, llm_user, read, reply, _, _ = _first_read(monkeypatch, tmp_path)
    pick = ExternalItem.query.filter_by(user_id=alice.id, external_id="222").one()
    pick.feedback = "bad"
    _db.session.commit()
    text = _prompt_node(alice, "textmode", parent_id=reply.id,
                        body="AGENTIC PERSONA AND TOOLS")
    question = _user_node(alice, text.id, "why the second one?")
    _db.session.commit()
    chat = _placeholder(llm_user, alice, question.id)
    call, kwargs = _live(monkeypatch, alice, question, chat, "Because it fit.")
    texts = _texts(call)
    joined = "\n".join(texts)
    assert "AGENTIC PERSONA AND TOOLS" in joined
    assert call["tools"] is not None
    assert kwargs.get("output_schema") is None
    assert len(renders) == 1
    assert CA_TWEETS_CHAT_STUB in texts[0]
    assert "[#1] first tweet" not in joined
    last = call["messages"][-1]
    assert last["role"] == "user"
    assert ("reference %d (@bob_b) — rated a bad quote" % pick.id) in last["text"]
    assert CA_CHAT_TURN_NOTE in last["text"]
    assert _fresh(chat.id).get_content() == "Because it fit."
    assert FeedRender.query.filter_by(node_id=chat.id).first() is None

    read2 = _prompt_node(alice, "read_thread", parent_id=chat.id)
    _db.session.commit()
    reply2 = _placeholder(llm_user, alice, read2.id)
    call2, kwargs2 = _live(monkeypatch, alice, read2, reply2, _feed_json([]))
    joined2 = "\n".join(_texts(call2))
    assert "AGENTIC PERSONA AND TOOLS" not in joined2
    assert call2["tools"] is None
    assert kwargs2["output_schema"]["required"] == ["verdict", "picks"]
    assert len(renders) == 2
    assert "[#1] first tweet" in joined2


def test_second_read_prompt_under_a_read_reply_is_a_read(app, monkeypatch, tmp_path):  # noqa: F811
    """"Read the archive" on a read reply attaches a read_thread prompt
    under it: a new read (the newest prompt carries the day; the older
    prompt's placeholder reads as the stub, its day went to the reply
    below it)."""
    renders, alice, llm_user, read, reply, _, _ = _first_read(monkeypatch, tmp_path)
    second = _prompt_node(alice, "read_thread", parent_id=reply.id)
    _db.session.commit()
    again = _placeholder(llm_user, alice, second.id)
    call, kwargs = _live(monkeypatch, alice, second, again, _feed_json([
        {"n": 1, "qt": "The other one.", "relevance": 20, "recommend": False}]))
    assert len(renders) == 2
    assert kwargs["output_schema"]["required"] == ["verdict", "picks"]
    texts = _texts(call)
    assert "[#1] first tweet" in texts[-1]          # the new prompt
    assert CA_TWEETS_CHAT_STUB in texts[0]          # the old prompt
    assert "[#1] first tweet" not in texts[0]
    assert "{ca_tweets" not in "\n".join(texts)
    again = _fresh(again.id)
    assert again.llm_task_status == "completed"
    assert FeedRender.query.filter_by(node_id=again.id).one().tweet_ids == "111,222"
    assert [p.item.external_id
            for p in FeedPick.query.filter_by(node_id=again.id)] == ["111"]


# ── the training key never carries other people's tweets ─────────────────

def _watch_key_type(monkeypatch):
    """Record the key_type llm_completion resolves for each call."""
    seen = []
    real = _llm_task_mod.get_api_keys_for_usage

    def _spy(config, key_type):
        seen.append(key_type)
        return real(config, key_type)
    monkeypatch.setattr(_llm_task_mod, "get_api_keys_for_usage", _spy)
    return seen


def _train(*nodes):
    for n in nodes:
        n.ai_usage = "train"
    _db.session.commit()


def test_a_read_in_a_train_thread_still_uses_chat_keys(app, monkeypatch, tmp_path):  # noqa: F811
    """The day of tweets never goes out on the training key. A read
    prompt from before #307 carries the user's 'train' default and a
    read further attaches no prompt of its own, so the whole chain can
    be 'train'; determine_api_key_type reads user nodes only, so the
    placeholder's FEED_AI_USAGE never reaches key selection."""
    _capture_render(monkeypatch, tmp_path)
    alice = _mk_user("alice", approved=True, plan="alpha", is_admin=True)
    llm_user = _mk_user("gpt-5", twitter_id="llm-gpt-5")
    note = _user_node(alice, None, "where am I stuck?")
    read = _prompt_node(alice, "read_thread", parent_id=note.id)
    _train(note, read)
    keys = _watch_key_type(monkeypatch)
    reply = _placeholder(llm_user, alice, read.id)
    _live(monkeypatch, alice, read, reply, _feed_json([
        {"n": 1, "qt": "This one.", "relevance": 30, "recommend": True}]))

    assert _fresh(reply.id).llm_task_status == "completed"
    assert keys == ["chat"]


def test_a_chat_turn_in_a_train_read_thread_still_uses_chat_keys(app, monkeypatch, tmp_path):  # noqa: F811
    """A chat turn renders no day, but the read reply's {quote_ext:ID}
    markers resolve to the picked tweets, so the payload still carries
    them — the guard is on the thread, not on needs_ca."""
    _, alice, llm_user, read, reply, _, _ = _first_read(monkeypatch, tmp_path)
    question = _user_node(alice, reply.id, "why the second one?")
    _train(read, reply, question)
    keys = _watch_key_type(monkeypatch)
    chat = _placeholder(llm_user, alice, question.id)
    call, _ = _live(monkeypatch, alice, question, chat, "Because it fit.")

    assistant = [m for m in call["messages"] if m["role"] == "assistant"]
    assert "second tweet" in assistant[0]["text"]   # the tweets are in there
    assert keys == ["chat"]


def test_an_ordinary_train_thread_still_uses_train_keys(app, monkeypatch, tmp_path):  # noqa: F811
    """The guard is scoped to read threads: a thread the user wrote
    themselves and marked 'train' keeps its training key."""
    alice = _mk_user("alice", approved=True, plan="alpha", is_admin=True)
    llm_user = _mk_user("gpt-5", twitter_id="llm-gpt-5")
    note = _user_node(alice, None, "a thought of my own")
    _train(note)
    keys = _watch_key_type(monkeypatch)
    llm = _placeholder(llm_user, alice, note.id)
    _live(monkeypatch, alice, note, llm, "Quite.")

    assert _fresh(llm.id).llm_task_status == "completed"
    assert keys == ["train"]


def test_is_feed_node_knows_the_poc_shape(app, monkeypatch, tmp_path):  # noqa: F811
    """The 2026-09-13 threads copied the prompt text into the node: no
    key, no link, {ca_tweets} in the content. The editor and the cascade
    must still refuse to raise them to 'train'."""
    from backend.utils.ca_feed import is_feed_node
    from backend.utils.node_settings import apply_settings_to_descendants
    alice = _mk_user("alice", approved=True, plan="alpha", is_admin=True)
    root = _user_node(alice, None, "the thread so far")
    poc = _user_node(alice, root.id, "Read these.\n\n{ca_tweets?days=1}")
    plain = _user_node(alice, root.id, "an ordinary note")
    _db.session.commit()

    assert is_feed_node(poc) is True
    assert is_feed_node(plain) is False

    apply_settings_to_descendants(root, alice.id, ai_usage="train")
    _db.session.commit()
    assert _fresh(poc.id).ai_usage == "chat"
    assert _fresh(plain.id).ai_usage == "train"

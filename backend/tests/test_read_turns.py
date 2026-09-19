"""Turns of a read thread (2026-09-19, the second feedback round).

Only a read feeds the day of tweets to the model: the first reply under
the read prompt, and a reply asked for directly under a read reply (a
read-again, where the model sees its earlier picks and the reader's
marks and decides itself whether to repeat one). A user message after a
read reply makes the next reply a chat turn: the picks and the marks are
in the context, the day is not. What the reader has already seen (read
marks, bookmarks) never reaches the model as a candidate, and each
render's numbering is pinned on the reply (FeedRender).

Runs the real task body through the test_retrieval_loop harness on the
live path (ca_live=True), like test_read_context.
"""
from datetime import datetime

import pytest  # noqa: F401

from backend.tests.test_retrieval_loop import (  # noqa: F401 (fixture)
    app, _llm_task_mod, generate_llm_response, _FakeSelf, _mk_user, _resp,
    _fresh,
)
from backend.tests.test_read_context import (
    _CapturingProvider, _prompt_node, _user_node, _stub_archive, _feed_json,
    CORPUS, REFS,
)
from backend.extensions import db as _db
from backend.models import Node, ExternalItem, FeedPick, FeedRender
from backend.utils.ca_feed import (
    CA_READ_AGAIN_TURN, CA_TWEETS_CHAT_STUB, seen_tweet_ids,
    refresh_snapshot_for_read,
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
    # A bookmark, a clipped tweet (stored as a bookmark), a read-marked
    # pick, an unread pick, and a web clip (not a tweet).
    for source, ext_id, read_at in [
        ("twitter_bookmark", "111", None),
        ("twitter_bookmark", "555", None),
        ("community_archive", "333", datetime(2026, 9, 18, 8, 0)),
        ("community_archive", "444", None),
        ("web_clip", "a" * 64, datetime(2026, 9, 18, 8, 0)),
    ]:
        item = ExternalItem(user_id=alice.id, source=source, external_id=ext_id,
                            read_at=read_at)
        item.set_content("x")
        _db.session.add(item)
    # Someone else's marks are not the reader's.
    bob = _mk_user("bob", approved=True, plan="alpha")
    other = ExternalItem(user_id=bob.id, source="twitter_bookmark",
                         external_id="222")
    other.set_content("y")
    _db.session.add(other)
    _db.session.commit()

    assert seen_tweet_ids(alice.id) == {"111", "555", "333"}

    read = _prompt_node(alice, "read")
    reply = _placeholder(llm_user, alice, read.id)
    _live(monkeypatch, alice, read, reply, _feed_json([]))
    assert set(renders[0]["exclude_tweet_ids"]) == {"111", "555", "333"}
    row = FeedRender.query.filter_by(node_id=reply.id).one()
    assert row.tweet_ids == "222"
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

    chat = _fresh(chat.id)
    assert chat.llm_task_status == "completed"
    assert chat.get_content() == "Because it fit."
    assert FeedRender.query.filter_by(node_id=chat.id).first() is None
    assert FeedPick.query.filter_by(node_id=chat.id).count() == 0


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
    assert call["messages"][-1]["text"].endswith("[continue]")
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

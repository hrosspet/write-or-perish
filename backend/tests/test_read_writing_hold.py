"""A Read while "Delete all my writing" waits (#268, review of the rework).

A Read pick must never bring back a reference the request hid, and no
Read result may be saved while the writing is on hold: a Read the user
starts does not run, and a result that arrives meanwhile (a batch in
flight at the request, or a live call the request overtook) is dropped
with its cost row written. Runs the real task body through the
test_retrieval_loop harness with the provider scripted, as
test_read_batch_poll does.
"""
import json

import pytest  # noqa: F401

from backend.tests.test_retrieval_loop import (  # noqa: F401 (fixture)
    app, _llm_task_mod, generate_llm_response, _mk_user,
)
from backend.tests.test_read_context import (
    _prompt_node, _stub_archive, _feed_json,
)
from backend.tests.test_read_batch_poll import (
    _Task, _batch_resp, _read_thread, _reload, _script, _cost_rows,
)
from backend.extensions import db as _db
from backend.models import (
    APICostLog, ExternalItem, FeedPick, Node, UserDataPurgeHidden, User,
)
from backend.utils.hidden_rows import including_hidden_rows


def _storage(monkeypatch, tmp_path):
    from backend.utils import audio_storage, twitter_archive
    monkeypatch.setattr(audio_storage, "AUDIO_STORAGE_ROOT",
                        tmp_path / "audio")
    monkeypatch.setattr(twitter_archive, "STASH_ROOT",
                        tmp_path / "data" / "imports")


def _saved_bookmark(user, tweet_id="222"):
    """A tweet the user saved before the request (the Read picks it)."""
    item = ExternalItem(user_id=user.id, source="twitter_bookmark",
                        external_id=tweet_id, author_handle="bob_b",
                        url=f"https://x.com/bob_b/status/{tweet_id}")
    item.set_content("second tweet")
    _db.session.add(item)
    _db.session.commit()
    return item.id


def _delete_all_writing(user):
    from backend.utils import user_purge
    job, _ = user_purge.schedule_purge(user, requested_by_id=user.id,
                                       source="self")
    return job


def _still_hidden(item_id):
    return UserDataPurgeHidden.query.filter_by(
        kind="external_item", row_id=item_id).count() == 1


def test_a_batch_collected_while_the_writing_is_on_hold_saves_nothing(app, monkeypatch, tmp_path):  # noqa: F811
    """The batch was in flight at the request: when it is collected, the
    result is dropped. The cost row is written, nothing is saved, and
    the reference the user had saved stays hidden, so the purge deletes
    it."""
    _storage(monkeypatch, tmp_path)
    _script(monkeypatch, tmp_path,
            collect=lambda: ("completed", _batch_resp()))
    alice, read, llm_node = _read_thread()
    item_id = _saved_bookmark(alice)
    _delete_all_writing(alice)

    result = generate_llm_response(_Task(), read.id, llm_node.id, "gpt-5",
                                   alice.id)

    assert result["status"] == "cancelled"
    assert result["reason"] == "writing_on_hold"
    assert FeedPick.query.count() == 0
    with including_hidden_rows():
        assert ExternalItem.query.filter_by(user_id=alice.id).count() == 1
    assert _still_hidden(item_id)
    rows = _cost_rows(llm_node.id)
    assert [(r.request_type, r.input_tokens, r.output_tokens)
            for r in rows] == [("conversation", 100, 50)]
    node = _reload(llm_node.id)
    assert node.llm_task_status == "failed"
    assert node.llm_task_error == _llm_task_mod.READ_ON_HOLD_TEXT
    # Collected again (a duplicate poll), it logs nothing more.
    generate_llm_response(_Task(), read.id, llm_node.id, "gpt-5", alice.id)
    assert len(_cost_rows(llm_node.id)) == 1


def test_a_read_started_during_the_hold_does_not_run(app, monkeypatch, tmp_path):  # noqa: F811
    """A Read the user starts while the writing waits (here the admin's
    live run, which skips the batch) makes no model call."""
    from backend.tests.test_read_context import _CapturingProvider
    from backend.tests.test_retrieval_loop import _FakeSelf, _resp
    _storage(monkeypatch, tmp_path)
    _stub_archive(monkeypatch, tmp_path)
    _CapturingProvider.kwargs = []
    _CapturingProvider.reset([_resp(_feed_json([
        {"n": 2, "qt": "q", "relevance": 40, "recommend": True}]))])
    monkeypatch.setattr(_llm_task_mod, "LLMProvider", _CapturingProvider)
    alice = _mk_user("alice", approved=True, plan="alpha", is_admin=True)
    _delete_all_writing(alice)
    llm_user = _mk_user("gpt-5", twitter_id="llm-gpt-5")
    read = _prompt_node(alice, "read")
    llm_node = Node(user_id=llm_user.id, human_owner_id=alice.id,
                    parent_id=read.id, node_type="llm", llm_model="gpt-5",
                    llm_task_status="pending", privacy_level="private",
                    ai_usage="chat")
    llm_node.set_content("[LLM response generation pending...]")
    _db.session.add(llm_node)
    _db.session.commit()

    result = generate_llm_response(
        _FakeSelf(), read.id, llm_node.id, "gpt-5", alice.id,
        source_mode=None, ca_live=True)

    assert result["status"] == "refused"
    assert result["reason"] == "writing_on_hold"
    assert _CapturingProvider.kwargs == []
    assert APICostLog.query.count() == 0
    assert FeedPick.query.count() == 0


def test_a_live_read_the_request_overtook_saves_nothing(app, monkeypatch, tmp_path):  # noqa: F811
    """"Delete all my writing" is pressed while a live Read's model call
    runs: the billed result is dropped, the hidden reference stays
    hidden, and no new pick row is saved."""
    from backend.tests.test_read_context import _CapturingProvider
    from backend.tests.test_retrieval_loop import _FakeSelf, _resp
    _storage(monkeypatch, tmp_path)
    _stub_archive(monkeypatch, tmp_path)
    alice, read, llm_node = _read_thread(submitted=False)
    item_id = _saved_bookmark(alice)
    content = _feed_json([
        {"n": 1, "qt": "a tweet she never saved", "relevance": 30,
         "recommend": False},
        {"n": 2, "qt": "the tweet she saved", "relevance": 40,
         "recommend": True},
    ])

    class _Overtaken(_CapturingProvider):
        @classmethod
        def get_completion(cls, *args, **kwargs):
            out = super().get_completion(*args, **kwargs)
            _delete_all_writing(User.query.get(alice.id))
            return out
    _Overtaken.kwargs = []
    _Overtaken.reset([_resp(content, inp=70, out=30)])
    monkeypatch.setattr(_llm_task_mod, "LLMProvider", _Overtaken)

    result = generate_llm_response(
        _FakeSelf(), read.id, llm_node.id, "gpt-5", alice.id,
        source_mode=None, ca_live=True)

    assert result["status"] == "cancelled"
    assert FeedPick.query.count() == 0
    with including_hidden_rows():
        assert [i.id for i in ExternalItem.query.filter_by(
            user_id=alice.id)] == [item_id]
    assert _still_hidden(item_id)
    assert [(r.input_tokens, r.output_tokens)
            for r in _cost_rows(llm_node.id)] == [(70, 30)]


def test_a_read_pick_never_makes_a_hidden_reference_visible(app, monkeypatch, tmp_path):  # noqa: F811
    """find_tweet_row leaves a hidden row hidden (only the user's own save
    or import reclaims), and save_feed_picks refuses while on hold."""
    from backend.utils.ca_feed import save_feed_picks
    from backend.utils.hidden_rows import WritingOnHold
    from backend.utils.reference_rows import find_tweet_row
    _storage(monkeypatch, tmp_path)
    alice, read, llm_node = _read_thread(submitted=False)
    item_id = _saved_bookmark(alice)
    _delete_all_writing(alice)

    assert find_tweet_row(alice.id, "222") is None
    assert _still_hidden(item_id)
    with pytest.raises(WritingOnHold):
        save_feed_picks(alice.id, llm_node, [{
            "rank": 1, "relevance": 40, "recommend": True, "qt": "q",
            "ref": {"tweet_id": "222", "username": "bob_b",
                    "text": "second tweet", "posted_at": None}}])
    _db.session.rollback()
    assert _still_hidden(item_id)
    assert FeedPick.query.count() == 0
    assert json.loads(json.dumps({"ok": True}))["ok"]

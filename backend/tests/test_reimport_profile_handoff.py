"""A re-import's profile hand-off counts only what the import changed (#625).

Peter, 2026-11-11: "only what's changed by the import should count towards
the decision whether / how much of the profile should be rebuilt". A
re-upload with one new conversation: only that conversation counts, and
the profile is regenerated from its place in the user's whole history on;
the part before it stays. A re-import that moves entries from None to Chat
starts a profile update from the oldest of them, also for Twitter.

And for Chat to None (Peter, 2026-11-11): no rebuild on its own; the
import's result asks the user whether to rebuild, saying the entries may
be in the current profile, not necessarily. A yes rebuilds from the
oldest of them on; a no, or no answer, changes nothing.

Every text is synthetic.
"""
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from backend.tests.test_chatgpt_import import (  # noqa: F401
    app, _db, _login, _make_user)
from backend.models import Node, User, UserProfile


# ── helpers ──────────────────────────────────────────────────────────────

@pytest.fixture
def hand_offs(app, monkeypatch):
    """Every profile hand-off: (user id, oldest changed date, units)."""
    from backend.routes import import_data
    calls = []

    def spy(user_obj, earliest_ts, tokens):
        calls.append((user_obj.id, earliest_ts, tokens))
        return None

    monkeypatch.setattr(import_data, "_hand_off_profile_update_after_import",
                        spy)
    return calls


def _conversation(conv_id, day):
    """An analyzed ChatGPT conversation of two messages on ``day``."""
    return {
        "name": f"Synthetic {conv_id}",
        "created_at": f"{day}T10:00:00",
        "messages": [
            {"text": f"synthetic question {conv_id}", "role": "user",
             "created_at": f"{day}T10:00:00", "model": "",
             "mapping_id": f"{conv_id}-u"},
            {"text": f"synthetic answer {conv_id}", "role": "assistant",
             "created_at": f"{day}T10:00:05", "model": "gpt-4o",
             "mapping_id": f"{conv_id}-a"},
        ],
        "message_count": 2,
    }


_RUNS = [0]


def _import_conversations(user_id, convs, ai_usage):
    from backend.routes import import_data
    token = import_data._stash_conversations(
        user_id, "chatgpt", convs, sum(len(c["messages"]) for c in convs))
    _RUNS[0] += 1
    return import_data.run_conversation_import(
        user_id, token, f"t{_RUNS[0]}",
        {"privacy_level": "private", "ai_usage": ai_usage,
         "on_deleted": None})


FILES = [
    {"filename_without_ext": "older note", "content": "synthetic note one",
     "modified_at": "2021-03-01T12:00:00"},
    {"filename_without_ext": "newer note", "content": "synthetic note two",
     "modified_at": "2023-06-01T12:00:00"},
]


def _import_files(client, files, ai_usage):
    return client.post("/api/import/confirm", json={
        "files": files, "import_type": "separate_nodes",
        "privacy_level": "private", "ai_usage": ai_usage})


def _tweet_row(i, created):
    return {"id_str": str(i), "full_text": f"synthetic tweet {i}",
            "created_at": created, "is_reply": False, "token_count": 5}


TWEETS = [_tweet_row(1, "Mon Mar 01 10:00:00 +0000 2021"),
          _tweet_row(2, "Tue Jun 01 10:00:00 +0000 2021"),
          _tweet_row(3, "Wed Jan 05 10:00:00 +0000 2022")]


def _import_tweets(user_id, rows, ai_usage):
    from backend.routes.import_data import create_twitter_nodes
    return create_twitter_nodes(
        user_id=user_id, rows=iter(rows), total=len(rows),
        import_type="separate_nodes", include_replies=False,
        privacy_level="private", ai_usage=ai_usage, on_deleted=None,
        provenance="archive_upload")


def _user(app, name, ai_usage="chat"):
    u = _make_user(name)
    u.default_ai_usage = ai_usage
    _db.session.commit()
    client = app.test_client()
    _login(client, u.id)
    return u, client


def _chain(user, cutoffs):
    """A chained profile: versions one day apart, then its integration."""
    versions, parent = [], None
    for i, cutoff in enumerate(cutoffs):
        p = UserProfile(user_id=user.id, generated_by="m", tokens_used=0,
                        generation_type="iterative" if i == 0 else "update",
                        source_data_cutoff=cutoff, source_tokens_used=100_000,
                        parent_profile_id=parent.id if parent else None,
                        created_at=datetime(2026, 8, 1 + i))
        p.set_content(f"synthetic version {i}")
        _db.session.add(p)
        _db.session.flush()
        versions.append(p)
        parent = p
    integ = UserProfile(user_id=user.id, generated_by="m", tokens_used=0,
                        generation_type="integration",
                        source_data_cutoff=cutoffs[-1],
                        source_tokens_used=100_000,
                        parent_profile_id=parent.id,
                        created_at=datetime(2026, 8, 20))
    integ.set_content("synthetic integration")
    _db.session.add(integ)
    _db.session.commit()
    return versions


def _tip(user):
    return (UserProfile.query.filter(
        UserProfile.user_id == user.id,
        UserProfile.generation_type != "integration")
        .order_by(UserProfile.created_at.desc()).first())


# ── an unchanged re-import starts no profile work ─────────────────────────

def test_an_unchanged_conversation_reimport_starts_no_profile_work(
        app, hand_offs):
    u, _client = _user(app, "same_chats")
    convs = [_conversation("a", "2021-02-01"), _conversation("b", "2022-02-01")]
    first = _import_conversations(u.id, convs, "chat")
    assert len(hand_offs) == 1 and first["created"] == 4
    again = _import_conversations(u.id, convs, "chat")
    assert again["created"] == 0 and again["skipped"] == 4
    assert len(hand_offs) == 1
    assert again["profile_update_task_id"] is None
    assert again["profile_waits_for_imports"] is False
    assert again["ask_profile_rebuild"] is None


def test_an_unchanged_file_reimport_starts_no_profile_work(app, hand_offs):
    u, client = _user(app, "same_files")
    assert _import_files(client, FILES, "chat").status_code == 201
    assert len(hand_offs) == 1
    body = _import_files(client, FILES, "chat").get_json()
    assert body["created"] == 0 and body["skipped"] == 2
    assert len(hand_offs) == 1 and body["ask_profile_rebuild"] is None


def test_an_unchanged_twitter_reimport_starts_no_profile_work(app, hand_offs):
    u, _client = _user(app, "same_tweets")
    _import_tweets(u.id, TWEETS, "chat")
    assert len(hand_offs) == 1
    again = _import_tweets(u.id, TWEETS, "chat")
    assert again["created"] == 0 and again["skipped"] == 3
    assert len(hand_offs) == 1 and again["ask_profile_rebuild"] is None


def test_a_privacy_or_train_change_alone_is_no_profile_change(app, hand_offs):
    """Chat to Train, or a privacy change, leaves what the profile reads
    as it was: the entries are updated, nothing is handed off."""
    u, _client = _user(app, "train_change")
    _import_tweets(u.id, TWEETS, "chat")
    again = _import_tweets(u.id, TWEETS, "train")
    assert again["updated"] == 3 and len(hand_offs) == 1


# ── new entries date the hand-off from the oldest of them ─────────────────

def test_three_new_conversations_date_the_hand_off_from_the_oldest_of_them(
        app, hand_offs):
    u, _client = _user(app, "three_new")
    old = [_conversation("a", "2021-02-01"), _conversation("b", "2022-02-01")]
    _import_conversations(u.id, old, "chat")
    new = [_conversation("c", "2023-03-01"), _conversation("d", "2023-01-10"),
           _conversation("e", "2023-06-01")]
    result = _import_conversations(u.id, old + new, "chat")
    assert result["created"] == 6
    # Not the archive's oldest message (2021): the oldest new one.
    assert hand_offs[-1][1] == datetime(2023, 1, 10, 10, 0, 0)


def test_the_rebuild_keeps_the_profile_before_the_new_conversations(
        app, monkeypatch):
    """The real hand-off: the chain is re-tipped at its last version that
    ends before the oldest new conversation; the versions before stay and
    nothing is rebuilt from scratch."""
    import backend.tasks.profile_batch as pb
    seed = MagicMock()
    monkeypatch.setattr(pb.seed_profile_batch_for_user, "apply_async", seed)
    app.config["PROFILE_USE_BATCH"] = True
    u, _client = _user(app, "keeps_before")
    old = [_conversation("a", "2021-02-01"), _conversation("b", "2022-02-01")]
    _import_conversations(u.id, old, "chat")
    versions = _chain(u, [datetime(2022, 6, 1), datetime(2022, 12, 1),
                          datetime(2023, 2, 1), datetime(2023, 5, 1)])
    new = [_conversation("c", "2023-03-01"), _conversation("d", "2023-01-10"),
           _conversation("e", "2023-06-01")]
    _import_conversations(u.id, old + new, "chat")
    tip = _tip(u)
    assert tip.generation_type == "revert"
    assert tip.parent_profile_id == versions[1].id    # cutoff 2022-12-01
    assert User.query.get(u.id).profile_needs_full_regen is False
    assert seed.called


# ── None to Chat: the changed entries count ───────────────────────────────

def test_none_to_chat_hands_off_from_the_oldest_changed_conversation(
        app, hand_offs):
    u, _client = _user(app, "none_to_chat")
    convs = [_conversation("a", "2021-02-01"), _conversation("b", "2022-02-01")]
    _import_conversations(u.id, convs, "none")
    assert hand_offs == []
    result = _import_conversations(u.id, convs, "chat")
    assert result["created"] == 0 and result["updated"] == 4
    assert [(uid, ts) for uid, ts, _t in hand_offs] == [
        (u.id, datetime(2021, 2, 1, 10, 0, 0))]


def test_none_to_chat_hands_off_from_the_oldest_changed_file(app, hand_offs):
    u, client = _user(app, "none_to_chat_files")
    _import_files(client, FILES, "none")
    assert hand_offs == []
    body = _import_files(client, FILES, "chat").get_json()
    assert body["updated"] == 2
    assert [ts for _u, ts, _t in hand_offs] == [datetime(2021, 3, 1, 12, 0)]


def test_none_to_chat_hands_off_on_a_twitter_reimport_too(app, hand_offs):
    u, _client = _user(app, "none_to_chat_tweets")
    _import_tweets(u.id, TWEETS, "none")
    assert hand_offs == []
    # Only the two newer tweets change: the oldest is set to Chat already.
    oldest = Node.query.filter_by(human_owner_id=u.id).order_by(
        Node.created_at).first()
    oldest.ai_usage = "chat"
    _db.session.commit()
    result = _import_tweets(u.id, TWEETS, "chat")
    assert result["created"] == 0 and result["updated"] == 2
    assert [(uid, ts, t) for uid, ts, t in hand_offs] == [
        (u.id, datetime(2021, 6, 1, 10, 0, 0), 10)]


# ── Chat to None: ask, don't rebuild ─────────────────────────────────────

def _covered_by_a_profile(user, cutoff):
    p = UserProfile(user_id=user.id, generated_by="m", tokens_used=0,
                    generation_type="initial", source_data_cutoff=cutoff,
                    source_tokens_used=100_000)
    p.set_content("synthetic profile")
    _db.session.add(p)
    _db.session.commit()
    return p


def test_chat_to_none_asks_and_does_not_rebuild(app, hand_offs):
    u, _client = _user(app, "chat_to_none")
    convs = [_conversation("a", "2021-02-01"), _conversation("b", "2022-02-01")]
    _import_conversations(u.id, convs, "chat")
    _covered_by_a_profile(u, datetime(2022, 6, 1))
    hand_offs.clear()
    result = _import_conversations(u.id, convs, "none")
    assert result["updated"] == 4
    assert hand_offs == []                      # no rebuild on its own
    assert result["profile_update_task_id"] is None
    question = result["ask_profile_rebuild"]
    assert question["entries"] == 4 and question["token"]


def test_chat_to_none_asks_on_every_importer(app, hand_offs):
    u, client = _user(app, "chat_to_none_all")
    _import_files(client, FILES, "chat")
    _import_tweets(u.id, TWEETS, "chat")
    _covered_by_a_profile(u, datetime(2024, 1, 1))
    hand_offs.clear()
    files = _import_files(client, FILES, "none").get_json()
    tweets = _import_tweets(u.id, TWEETS, "none")
    assert hand_offs == []
    assert files["ask_profile_rebuild"]["entries"] == 2
    assert tweets["ask_profile_rebuild"]["entries"] == 3


@pytest.mark.parametrize("case", ["ends_before", "no_profile", "hand_written",
                                  "account_none"])
def test_no_question_when_the_profile_cannot_hold_the_entries(
        app, hand_offs, case):
    u, _client = _user(app, f"no_question_{case}")
    _import_tweets(u.id, TWEETS, "chat")
    if case == "ends_before":
        _covered_by_a_profile(u, datetime(2021, 1, 1))  # before every tweet
    elif case == "hand_written":
        p = UserProfile(user_id=u.id, generated_by="user", tokens_used=0)
        p.set_content("synthetic profile written by hand")
        _db.session.add(p)
    elif case == "account_none":
        _covered_by_a_profile(u, datetime(2024, 1, 1))
        u.default_ai_usage = "none"
    _db.session.commit()
    hand_offs.clear()
    result = _import_tweets(u.id, TWEETS, "none")
    assert result["updated"] == 3
    assert result["ask_profile_rebuild"] is None and hand_offs == []


def test_the_question_is_asked_when_only_some_entries_may_be_in_it(
        app, hand_offs):
    """A profile that reaches the oldest of the entries may hold them."""
    u, _client = _user(app, "partly_covered")
    _import_tweets(u.id, TWEETS, "chat")
    _covered_by_a_profile(u, datetime(2021, 4, 1))   # covers tweet 1 only
    assert _import_tweets(u.id, TWEETS, "none")["ask_profile_rebuild"]


def test_a_yes_rebuilds_from_the_oldest_entry_set_to_none(app, monkeypatch):
    import backend.tasks.profile_batch as pb
    seed = MagicMock()
    monkeypatch.setattr(pb.seed_profile_batch_for_user, "apply_async", seed)
    app.config["PROFILE_USE_BATCH"] = True
    u, client = _user(app, "says_yes")
    _import_tweets(u.id, TWEETS, "chat")
    versions = _chain(u, [datetime(2021, 4, 1), datetime(2021, 9, 1),
                          datetime(2022, 3, 1)])
    seed.reset_mock()
    # Only the two newer tweets leave what AI reads.
    oldest = Node.query.filter_by(human_owner_id=u.id).order_by(
        Node.created_at).first()
    oldest.ai_usage = "none"
    _db.session.commit()
    result = _import_tweets(u.id, TWEETS, "none")
    assert _tip(u).id == versions[-1].id and not seed.called   # nothing yet
    token = result["ask_profile_rebuild"]["token"]
    resp = client.post("/api/import/profile-rebuild", json={"token": token})
    assert resp.status_code == 202, resp.get_json()
    # Re-tipped before 2021-06-01: the version that ends 2021-04-01 stays.
    tip = _tip(u)
    assert tip.generation_type == "revert"
    assert tip.parent_profile_id == versions[0].id
    assert seed.called


def test_the_answer_is_refused_for_another_user_or_a_bad_token(
        app, hand_offs):
    u, _client = _user(app, "asker")
    _import_tweets(u.id, TWEETS, "chat")
    _covered_by_a_profile(u, datetime(2024, 1, 1))
    hand_offs.clear()
    token = _import_tweets(u.id, TWEETS, "none")["ask_profile_rebuild"]["token"]
    other, other_client = _user(app, "someone_else")
    assert other_client.post("/api/import/profile-rebuild",
                             json={"token": token}).status_code == 400
    _u, own = _user(app, "asker_again")
    _login(own, u.id)
    assert own.post("/api/import/profile-rebuild",
                    json={"token": token + "x"}).status_code == 400
    assert own.post("/api/import/profile-rebuild", json={}).status_code == 400
    assert hand_offs == []


def test_the_answer_expires(app, hand_offs, monkeypatch):
    from backend.routes import import_data
    u, client = _user(app, "late_answer")
    _import_tweets(u.id, TWEETS, "chat")
    _covered_by_a_profile(u, datetime(2024, 1, 1))
    hand_offs.clear()
    token = _import_tweets(u.id, TWEETS, "none")["ask_profile_rebuild"]["token"]
    monkeypatch.setattr(import_data, "PROFILE_REBUILD_QUESTION_MAX_AGE", -1)
    assert client.post("/api/import/profile-rebuild",
                       json={"token": token}).status_code == 400
    assert hand_offs == []


# ── one question starts at most one rebuild (review of #625) ─────────────

def _asked(app, name):
    u, client = _user(app, name)
    _import_tweets(u.id, TWEETS, "chat")
    _covered_by_a_profile(u, datetime(2024, 1, 1))
    token = _import_tweets(u.id, TWEETS, "none")["ask_profile_rebuild"]["token"]
    return u, client, token


def test_the_same_answer_sent_twice_starts_one_rebuild(app, hand_offs):
    u, client, token = _asked(app, "twice")
    hand_offs.clear()
    first = client.post("/api/import/profile-rebuild", json={"token": token})
    second = client.post("/api/import/profile-rebuild", json={"token": token})
    assert first.status_code == 202
    assert first.get_json()["status"] == "rebuilding"
    assert second.status_code == 200
    assert second.get_json()["status"] == "skipped"
    assert "answered already" in second.get_json()["message"]
    assert len(hand_offs) == 1
    assert User.query.get(u.id).profile_rebuild_offer_id is None


def test_a_newer_question_replaces_an_open_one(app, hand_offs):
    u, client, older = _asked(app, "replaced")
    _import_tweets(u.id, TWEETS, "chat")
    newer = _import_tweets(u.id, TWEETS, "none")["ask_profile_rebuild"]["token"]
    hand_offs.clear()
    assert client.post("/api/import/profile-rebuild",
                       json={"token": older}).get_json()["status"] == "skipped"
    assert client.post("/api/import/profile-rebuild",
                       json={"token": newer}).status_code == 202
    assert len(hand_offs) == 1


@pytest.mark.parametrize("state", ["full_regen", "batch_pending", "task",
                                   "new_version"])
def test_a_yes_starts_nothing_while_the_profile_moves_on(
        app, hand_offs, state):
    u, client, token = _asked(app, f"busy_{state}")
    hand_offs.clear()
    user = User.query.get(u.id)
    if state == "full_regen":
        user.profile_needs_full_regen = True
    elif state == "batch_pending":
        user.profile_batch_pending = True
    elif state == "task":
        user.profile_generation_task_id = "t-running"
    else:
        _covered_by_a_profile(u, datetime(2024, 6, 1))   # saved after asking
    _db.session.commit()
    resp = client.post("/api/import/profile-rebuild", json={"token": token})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "skipped" and "no" in body["message"]
    assert hand_offs == []
    # The question is settled: sending it again starts nothing either.
    assert User.query.get(u.id).profile_rebuild_offer_id is None


def test_a_rerun_with_other_settings_keeps_what_the_first_run_changed(
        app, hand_offs):
    """A conversation import confirmed again with other settings starts
    from its first conversation, and skips what an earlier run created:
    those entries still count (they were never handed off)."""
    from backend.routes import import_data
    from backend.utils import conversation_stash as cs
    u, _client = _user(app, "rerun")
    convs = [_conversation("a", "2021-02-01"), _conversation("b", "2022-02-01")]
    token = import_data._stash_conversations(u.id, "chatgpt", convs, 4)
    chat = {"privacy_level": "private", "ai_usage": "chat", "on_deleted": None}
    state = import_data.create_conversation_nodes(
        u.id, "chatgpt", enumerate(convs[:1]), "private", "chat", None,
        state=import_data._new_conversation_state(chat))
    cs.write_state(u.id, token, dict(state, task_id="t-first"))
    circles = dict(chat, privacy_level="circles")
    import_data.run_conversation_import(u.id, token, "t-second", circles)
    assert [ts for _u, ts, _t in hand_offs] == [datetime(2021, 2, 1, 10, 0)]


@pytest.mark.parametrize("covered", [False, True])
def test_a_rerun_with_none_after_a_chat_run_asks_and_starts_nothing(
        app, hand_offs, covered):
    """Review of #637: the earlier run created entries with Chat and was
    never handed off; the import confirmed again with None moves them out
    of what AI reads. No rebuild starts; the question follows the Chat to
    None rule (asked only when the profile in use reaches those entries)."""
    from backend.routes import import_data
    from backend.utils import conversation_stash as cs
    u, _client = _user(app, f"rerun_none_{covered}")
    if covered:
        _covered_by_a_profile(u, datetime(2024, 1, 1))
    convs = [_conversation("a", "2021-02-01"), _conversation("b", "2022-02-01")]
    token = import_data._stash_conversations(u.id, "chatgpt", convs, 4)
    chat = {"privacy_level": "private", "ai_usage": "chat", "on_deleted": None}
    state = import_data.create_conversation_nodes(
        u.id, "chatgpt", enumerate(convs[:1]), "private", "chat", None,
        state=import_data._new_conversation_state(chat))
    cs.write_state(u.id, token, dict(state, task_id="t-first"))
    result = import_data.run_conversation_import(
        u.id, token, "t-second", dict(chat, ai_usage="none"))
    assert hand_offs == []
    assert {n.ai_usage for n in Node.query.filter_by(
        human_owner_id=u.id).all()} == {"none"}
    question = result["ask_profile_rebuild"]
    if covered:
        assert question["entries"] == 2      # the earlier run's two entries
    else:
        assert question is None

"""Tweets fetched through the X API are never stored at Train (#689).

- create_twitter_nodes (every pre-fill and the user's own upload): the X
  API pre-fill creates them at Chat at most; None stays None.
- The user's own archive upload at Train leaves the X API copies it
  skips at Chat, lowers one stored at Train before the rule, and says so
  in its result (``kept_from_train``); its own new tweets get Train.
- backend/scripts/cap_x_api_train.py: lowers stored Train to Chat on the
  X API entries (dry run by default, counts only, idempotent), reports
  the unlabelled tweets of accounts with an X API pre-fill, and labels
  them only for the accounts named.

Uses the minimal Flask app of test_twitter_import.
"""
import os
import sys
from datetime import datetime

from backend.tests.test_twitter_import import (  # noqa: F401 (fixture)
    app, _make_user, _rows,
)
from backend.extensions import db as _db
from backend.models import APICostLog, Node


def _import(user, provenance, ai_usage, include_replies=True,
            on_deleted=None):
    from backend.routes.import_data import create_twitter_nodes
    return create_twitter_nodes(
        user_id=user.id, rows=iter(_rows()), total=3,
        import_type="separate_nodes", include_replies=include_replies,
        privacy_level="private", ai_usage=ai_usage, on_deleted=on_deleted,
        provenance=provenance,
    )


def _usage(user):
    return {n.source_key: (n.provenance, n.ai_usage)
            for n in Node.query.filter_by(human_owner_id=user.id)}


def test_an_x_api_pre_fill_never_creates_train(app):  # noqa: F811
    u = _make_user("alice")

    result = _import(u, "prefill_x", "train")

    assert result["created"] == 3
    assert result["kept_from_train"] == 3
    assert {usage for _, usage in _usage(u).values()} == {"chat"}


def test_an_x_api_pre_fill_keeps_none(app):  # noqa: F811
    """Control: an account set to None keeps None."""
    u = _make_user("alice")

    _import(u, "prefill_x", "none")

    assert {usage for _, usage in _usage(u).values()} == {"none"}


def test_the_users_own_upload_can_be_train(app):  # noqa: F811
    """Control: the user's own archive upload goes to Train."""
    u = _make_user("alice")

    result = _import(u, "archive_upload", "train")

    assert result["created"] == 3
    assert {usage for _, usage in _usage(u).values()} == {"train"}


def test_an_own_upload_at_train_never_leaves_an_x_api_copy_at_train(app):  # noqa: F811
    """The pre-fill brought in tweets 1 and 3 (no replies) through the X
    API; the user's own upload at Train adds the reply (4) at Train. The
    tweets it skips as already imported are never X API copies at Train:
    here they stay at Chat and the result counts them. (With #662 the
    upload claims them as the user's own, provenance archive_upload,
    and they may take Train; the count is then 0. Either way holds.)"""
    u = _make_user("alice")
    _import(u, "prefill_x", "chat", include_replies=False)

    result = _import(u, "archive_upload", "train")

    usage = _usage(u)
    assert result["created"] == 1
    assert usage["twitter:4"] == ("archive_upload", "train")
    for key in ("twitter:1", "twitter:3"):
        assert usage[key] in {("prefill_x", "chat"), ("archive_upload", "train")}
    assert result["kept_from_train"] == sum(
        usage[k][0] == "prefill_x" for k in ("twitter:1", "twitter:3"))


def test_a_re_import_at_train_lowers_an_x_api_copy_stored_at_train(app):  # noqa: F811
    """An X API tweet stored at Train before the rule is lowered to Chat
    by a re-import asking Train that would otherwise have left it as it
    was, and the result counts every X API tweet it kept at Chat."""
    u = _make_user("alice")
    _import(u, "prefill_x", "chat")
    Node.query.filter_by(source_key="twitter:1").update({"ai_usage": "train"})
    _db.session.commit()

    result = _import(u, "prefill_x", "train")

    assert result["updated"] == 1 and result["kept_from_train"] == 3
    assert {usage for _, usage in _usage(u).values()} == {"chat"}


def test_an_own_upload_at_chat_says_nothing_about_train(app):  # noqa: F811
    """Control: an import that doesn't ask for Train reports nothing."""
    u = _make_user("alice")
    _import(u, "prefill_x", "chat")

    result = _import(u, "archive_upload", "none")

    assert result.get("kept_from_train", 0) == 0
    assert {usage for _, usage in _usage(u).values()} == {"none"}


def test_a_restore_from_the_users_own_upload_is_theirs(app):  # noqa: F811
    """Control: a deleted X API tweet that the user's own upload restores
    comes back as the upload's copy, so it may be Train."""
    u = _make_user("alice")
    _import(u, "prefill_x", "chat")
    Node.query.filter_by(source_key="twitter:1").update(
        {"deleted_at": datetime.utcnow()})
    _db.session.commit()

    result = _import(u, "archive_upload", "train", on_deleted="restore")

    assert result["restored"] == 1
    assert _usage(u)["twitter:1"] == ("archive_upload", "train")


def test_a_restore_of_an_x_api_copy_comes_back_at_chat(app):  # noqa: F811
    from backend.routes.import_data import _restore_node
    u = _make_user("alice")
    n = Node(user_id=u.id, human_owner_id=u.id, node_type="user",
             source_key="twitter:1", deleted_at=datetime.utcnow(),
             ai_usage="none", provenance="archive_upload")
    n.set_content("stale")
    _db.session.add(n)
    _db.session.commit()

    _restore_node(n.id, "fresh", "private", "train", provenance="prefill_x")

    assert Node.query.get(n.id).ai_usage == "chat"


# ── the admin command ────────────────────────────────────────────────────

def _script():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
                                    "scripts"))
    import cap_x_api_train
    return cap_x_api_train


def _row(user, key, provenance, ai_usage, origin="twitter"):
    n = Node(user_id=user.id, human_owner_id=user.id, node_type="user",
             privacy_level="private", ai_usage=ai_usage, origin=origin,
             source_key=key, provenance=provenance,
             updated_at=datetime(2026, 9, 1))
    n.set_content("text")
    _db.session.add(n)
    _db.session.flush()
    return n


def _seed():
    """alice: X API tweets (2 at Train, 1 at Chat), her own upload at
    Train, two unlabelled tweets; she had an X API pre-fill. bob: an
    unlabelled tweet at Train and no X API pre-fill."""
    alice, bob = _make_user("alice"), _make_user("bob")
    x1 = _row(alice, "twitter:1", "prefill_x", "train")
    x2 = _row(alice, "twitter:2", "prefill_x", "train")
    _row(alice, "twitter:3", "prefill_x", "chat")
    own = _row(alice, "twitter:4", "archive_upload", "train")
    old = _row(alice, "twitter:5", None, "train")
    _row(alice, "twitter:6", None, "chat")
    bobs = _row(bob, "twitter:7", None, "train")
    _db.session.add(APICostLog(user_id=alice.id, model_id="x-api/timeline",
                               request_type="x_prefill", cost_microdollars=1))
    _db.session.commit()
    return alice, bob, x1, x2, own, old, bobs


def test_the_command_lowers_x_api_train_and_nothing_else(app):  # noqa: F811
    script = _script()
    alice, bob, x1, x2, own, old, bobs = _seed()

    assert script.cap(apply=False) == 2                   # dry run
    assert Node.query.get(x1.id).ai_usage == "train"

    assert script.cap(apply=True, batch_size=1) == 2
    _db.session.expire_all()
    assert Node.query.get(x1.id).ai_usage == "chat"
    assert Node.query.get(x2.id).ai_usage == "chat"
    assert Node.query.get(x1.id).updated_at == datetime(2026, 9, 1)
    assert Node.query.get(own.id).ai_usage == "train"     # her own upload
    assert Node.query.get(old.id).ai_usage == "train"     # unlabelled
    assert Node.query.get(bobs.id).ai_usage == "train"
    assert script.cap(apply=True) == 0                    # idempotent


def test_the_command_reports_unlabelled_tweets_of_x_prefilled_accounts(app):  # noqa: F811
    script = _script()
    _seed()

    labelled, unlabelled = script.survey()

    assert labelled == {"train": 2, "chat": 1}
    # bob had no X API pre-fill: his unlabelled tweet is not counted.
    assert unlabelled == {"train": (1, 1), "chat": (1, 1)}
    alice_id = Node.query.filter_by(source_key="twitter:5").one().human_owner_id
    assert script.unlabelled_by_account() == [
        (alice_id, "never", {"train": 1, "chat": 1})]


def test_a_run_labels_accounts_nobody_signed_into_and_lists_the_rest(app):  # noqa: F811
    """alice was pre-filled and never signed in: her unlabelled tweets came
    from a pre-fill and are labelled. carol signed in, so her unlabelled
    tweet may be her own upload: it keeps Train and is listed."""
    script = _script()
    alice, bob, x1, x2, own, old, bobs = _seed()
    carol = _make_user("carol")
    carol.last_seen_at = datetime(2026, 10, 1)
    carols = _row(carol, "twitter:8", None, "train")
    _db.session.add(APICostLog(user_id=carol.id, model_id="x-api/timeline",
                               request_type="x_prefill", cost_microdollars=1))
    _db.session.commit()
    lines = []

    script.run(apply=False, out=lines.append)
    assert Node.query.get(old.id).provenance is None      # dry run
    assert lines[-1].startswith("dry run")

    lines.clear()
    script.run(apply=True, out=lines.append)
    _db.session.expire_all()
    assert (Node.query.get(old.id).provenance,
            Node.query.get(old.id).ai_usage) == ("prefill_x", "chat")
    assert (Node.query.get(carols.id).provenance,
            Node.query.get(carols.id).ai_usage) == (None, "train")
    assert Node.query.get(bobs.id).provenance is None     # no X pre-fill
    assert any(f"account(s) {carol.id}" in line for line in lines)


def test_the_command_labels_only_the_accounts_named(app):  # noqa: F811
    script = _script()
    alice, bob, x1, x2, own, old, bobs = _seed()

    dry = script.label_user(alice.id, apply=False)
    assert dict(dry) == {"train": 1, "chat": 1}
    assert Node.query.get(old.id).provenance is None

    script.label_user(alice.id, apply=True)
    _db.session.expire_all()
    assert (Node.query.get(old.id).provenance,
            Node.query.get(old.id).ai_usage) == ("prefill_x", "chat")
    assert Node.query.get(own.id).provenance == "archive_upload"
    assert Node.query.get(bobs.id).provenance is None
    assert dict(script.label_user(alice.id, apply=True)) == {}

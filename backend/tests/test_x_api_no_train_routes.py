"""An entry fetched through the X API can't be set to Train by any route
(#689): the node editor refuses it with a plain explanation, the "apply
to my replies" cascade leaves it out and says so, and the node JSON tells
the web and the iPhone not to offer Train (and shows the AI usage the
training licence reads). The user's own archive upload can still be set
to Train.

Uses the minimal Flask app of test_apply_settings_to_descendants.
"""
import pytest

from backend.tests.test_apply_settings_to_descendants import (  # noqa: F401
    app, users, _login, _node, _put,
)
from backend.extensions import db as _db
from backend.models import Node

MESSAGE = ("Posts fetched from X can't be used for training, under X's "
           "rules for developers.")


def _tweet(user, provenance, ai_usage="chat", parent=None):
    n = _node(user, parent, ai_usage=ai_usage, content="a tweet")
    n.origin = "twitter"
    n.provenance = provenance
    _db.session.commit()
    return n


def test_the_message_is_the_one_peter_approved():
    from backend.utils.privacy import TRAIN_REFUSED_MESSAGE
    assert TRAIN_REFUSED_MESSAGE == MESSAGE


def test_the_editor_refuses_train_for_an_x_api_tweet(app, users):  # noqa: F811
    alice, _, _ = users
    tweet = _tweet(alice, "prefill_x")

    resp = _put(app, alice, tweet, ai_usage="train")

    assert resp.status_code == 400
    assert resp.json["error"] == MESSAGE
    assert resp.json["code"] == "x_api_no_train"
    assert Node.query.get(tweet.id).ai_usage == "chat"


@pytest.mark.parametrize("provenance", ["archive_upload", "prefill_ca", None])
def test_the_editor_still_sets_train_on_other_entries(app, users, provenance):  # noqa: F811
    """Control: the user's own upload, the Community Archive and writing
    in Loore."""
    alice, _, _ = users
    tweet = _tweet(alice, provenance)

    resp = _put(app, alice, tweet, ai_usage="train")

    assert resp.status_code == 200
    assert Node.query.get(tweet.id).ai_usage == "train"


@pytest.mark.parametrize("usage", ["none", "chat"])
def test_the_editor_sets_none_and_chat_on_an_x_api_tweet(app, users, usage):  # noqa: F811
    """Control: only Train is refused."""
    alice, _, _ = users
    tweet = _tweet(alice, "prefill_x", ai_usage="chat" if usage == "none"
                   else "none")

    resp = _put(app, alice, tweet, ai_usage=usage)

    assert resp.status_code == 200
    assert Node.query.get(tweet.id).ai_usage == usage


def test_a_text_edit_of_a_tweet_stored_at_train_saves_and_lowers_it(app, users):  # noqa: F811
    """A form opened on a tweet stored at Train before the rule sends
    'train' back with the text: the edit is saved, the tweet takes Chat."""
    alice, _, _ = users
    tweet = _tweet(alice, "prefill_x", ai_usage="train")

    resp = _put(app, alice, tweet, content="a tweet, edited",
                ai_usage="train")

    assert resp.status_code == 200
    fresh = Node.query.get(tweet.id)
    assert fresh.get_content() == "a tweet, edited"
    assert fresh.ai_usage == "chat"


def test_the_cascade_to_train_leaves_x_api_replies_and_says_so(app, users):  # noqa: F811
    alice, _, _ = users
    root = _node(alice, content="root")
    mine = _node(alice, root, content="mine")
    tweet = _tweet(alice, "prefill_x", parent=root)
    _db.session.commit()

    resp = _put(app, alice, root, ai_usage="train",
                apply_to_descendants=True)

    assert resp.status_code == 200
    assert resp.json["descendants_updated"] == 1
    assert resp.json["kept_from_train"] == 1
    assert resp.json["kept_from_train_message"] == MESSAGE
    assert Node.query.get(mine.id).ai_usage == "train"
    assert Node.query.get(tweet.id).ai_usage == "chat"


def test_a_cascade_to_none_still_reaches_x_api_replies(app, users):  # noqa: F811
    """Control: lowering is never held back."""
    alice, _, _ = users
    root = _node(alice, content="root")
    tweet = _tweet(alice, "prefill_x", parent=root)

    resp = _put(app, alice, root, ai_usage="none", apply_to_descendants=True)

    assert resp.status_code == 200
    assert "kept_from_train" not in resp.json
    assert Node.query.get(tweet.id).ai_usage == "none"


def test_the_node_json_says_train_is_refused_and_shows_chat(app, users):  # noqa: F811
    """GET /nodes/<id>: the focal node, its children and ancestors carry
    the AI usage the licence reads and ``train_refused``; the stored Train
    from before the rule is shown as Chat."""
    alice, _, _ = users
    tweet = _tweet(alice, "prefill_x", ai_usage="train")
    reply = _node(alice, tweet, ai_usage="train", content="my reply")
    under = _node(alice, reply, ai_usage="train", content="under")
    _db.session.commit()
    client = app.test_client()
    _login(client, alice)

    focal = client.get(f"/nodes/{tweet.id}").json
    assert focal["ai_usage"] == "chat"
    assert focal["train_refused"] is True
    assert focal["children"][0]["ai_usage"] == "train"     # alice's reply
    assert "train_refused" not in focal["children"][0]

    deeper = client.get(f"/nodes/{under.id}").json
    assert deeper["ancestors"][0]["id"] == tweet.id
    assert deeper["ancestors"][0]["train_refused"] is True
    assert deeper["ancestors"][0]["ai_usage"] == "chat"
    assert "train_refused" not in deeper


def test_the_put_response_carries_the_flag(app, users):  # noqa: F811
    alice, _, _ = users
    tweet = _tweet(alice, "prefill_x")

    resp = _put(app, alice, tweet, privacy_level="private", ai_usage="none")

    assert resp.json["node"]["train_refused"] is True

"""Content fetched through the X API never reaches the training key (#689).

X's Developer Agreement forbids using the X API or X Content to train a
foundation or frontier model. An entry Loore fetched through the X API
(Node.provenance 'prefill_x') counts as 'chat' for the training licence
(the rule of #326: a call goes to the training key only if everything in
its prompt allows it), whatever ai_usage is stored: in the chain, quoted,
in the archive window, in a cached render. The user's own archive upload
and the Community Archive keep their Train.

Runs the real task body through the test_retrieval_loop harness, with
distinct train and chat keys (test_training_key_payload.keys), so each
assertion is on the key the provider was actually handed.
"""
from types import SimpleNamespace

import pytest

from backend.tests.test_retrieval_loop import (  # noqa: F401 (fixture)
    app, generate_llm_response, _FakeSelf, _mk_user, _resp, _fresh,
    _ScriptedProvider,
)
from backend.tests.test_training_key_payload import (  # noqa: F401
    keys, _keys_used, _payload, _train_chain,
)
from backend.tests.test_training_key_own_rows import (
    _archive, _export_thread, _train_user,
)
from backend.extensions import db as _db
from backend.models import Node
from backend.utils.api_keys import determine_api_key_type, rows_lost_licence

TWEET = "A TWEET OF MINE"


def _tweet(owner, provenance, ai_usage="train"):
    """An imported tweet of *owner*, stored at *ai_usage*."""
    n = Node(user_id=owner.id, human_owner_id=owner.id, node_type="user",
             privacy_level="private", ai_usage=ai_usage, origin="twitter",
             source_key="twitter:1", provenance=provenance)
    n.set_content(TWEET)
    _db.session.add(n)
    _db.session.flush()
    return n


def _reply_under(alice, parent, text="what did I mean by that?"):
    """alice's 'train' reply under *parent* and its LLM placeholder."""
    llm_user = _mk_user("gpt-5", twitter_id="llm-gpt-5")
    reply = Node(user_id=alice.id, human_owner_id=alice.id,
                 parent_id=parent.id if parent else None, node_type="user",
                 privacy_level="private", ai_usage="train")
    reply.set_content(text)
    _db.session.add(reply)
    _db.session.flush()
    llm = Node(user_id=llm_user.id, human_owner_id=alice.id,
               parent_id=reply.id, node_type="llm", llm_model="gpt-5",
               llm_task_status="pending", privacy_level="private",
               ai_usage="train")
    llm.set_content("[LLM response generation pending...]")
    _db.session.add(llm)
    _db.session.commit()
    return reply, llm


@pytest.mark.parametrize("provenance, expected", [
    ("prefill_x", "sk-chat"),
    # Controls: the user's own upload and the Community Archive may train.
    ("archive_upload", "sk-train"),
    ("prefill_ca", "sk-train"),
    (None, "sk-train"),
])
def test_a_reply_under_a_tweet_uses_the_training_key_unless_x_fetched_it(app, keys, provenance, expected):  # noqa: F811
    """A reply whose chain holds an X API tweet goes out on the chat key,
    even with the tweet stored at Train and the account set to Train;
    one under the user's own uploaded tweet still uses the training key.
    On main, the prefill_x case went out on the training key."""
    alice = _mk_user("alice", approved=True, plan="alpha",
                     default_ai_usage="train")
    tweet = _tweet(alice, provenance)
    reply, llm = _reply_under(alice, tweet)

    _ScriptedProvider.reset([_resp("I see.")])
    generate_llm_response(_FakeSelf(), reply.id, llm.id, "gpt-5", alice.id,
                          source_mode=None)

    assert _fresh(llm.id).llm_task_status == "completed"
    assert TWEET in _payload(0)
    assert _keys_used() == [expected]


@pytest.mark.parametrize("provenance, expected", [
    ("prefill_x", "sk-chat"),
    ("archive_upload", "sk-train"),           # control
])
def test_a_quoted_tweet_is_judged_by_where_it_came_from(app, keys, provenance, expected):  # noqa: F811
    """A {quote:ID} of an X API tweet stored at Train keeps the call off
    the training key (PayloadLicence.note_nodes)."""
    alice = _mk_user("alice", approved=True, plan="alpha")
    tweet = _tweet(alice, provenance)
    _db.session.commit()
    reply, llm = _reply_under(alice, None, "Reacting to {quote:%d}" % tweet.id)

    _ScriptedProvider.reset([_resp("Seen.")])
    generate_llm_response(_FakeSelf(), reply.id, llm.id, "gpt-5", alice.id,
                          source_mode=None)

    assert TWEET in _payload(0)
    assert _keys_used() == [expected]


@pytest.mark.parametrize("provenance, expected", [
    ("prefill_x", "sk-chat"),
    ("archive_upload", "sk-train"),           # control
])
def test_an_x_api_tweet_in_the_archive_window_takes_the_thread_to_chat(app, keys, provenance, expected):  # noqa: F811
    """{user_export}: every rendered entry reports to the licence by its
    effective AI usage (export_data._note_entry), tweets in the compact
    tweet runs included."""
    alice, system, user_node, llm_node = _train_chain("textmode")
    _train_user(alice)
    _archive(alice, "ARCHIVE ENTRY", "train")
    tweet = _archive(alice, TWEET, "train")
    tweet.origin = "twitter"
    tweet.provenance = provenance
    _db.session.commit()
    _export_thread(user_node)

    _ScriptedProvider.reset([_resp("Looked.")])
    generate_llm_response(_FakeSelf(), user_node.id, llm_node.id, "gpt-5",
                          alice.id, source_mode="textmode")

    assert TWEET in _payload(0)
    assert _keys_used() == [expected]


def test_a_cached_render_resting_on_an_x_api_tweet_is_rebuilt(app):  # noqa: F811
    """A render cached with a 'train' verdict before the cap re-checks its
    rows on a hit: an X API tweet stored at Train no longer licenses it."""
    alice = _mk_user("alice", approved=True, plan="alpha")
    x_tweet = _tweet(alice, "prefill_x")
    own = Node(user_id=alice.id, human_owner_id=alice.id, node_type="user",
               privacy_level="private", ai_usage="train",
               provenance="archive_upload")
    own.set_content("mine")
    _db.session.add(own)
    _db.session.commit()

    assert rows_lost_licence({"node": [own.id]}) is None          # control
    assert rows_lost_licence({"node": [own.id, x_tweet.id]}) is not None


def test_the_chain_verdict_reads_an_x_api_tweet_as_chat():
    """determine_api_key_type, which also decides whether the model's
    memory writes are 'train' (llm_completion update_artifact)."""
    def node(provenance):
        return SimpleNamespace(id=1, node_type="user", ai_usage="train",
                               provenance=provenance)
    assert determine_api_key_type([node("archive_upload")]) == "train"
    assert determine_api_key_type([node(None), node("prefill_ca")]) == "train"
    assert determine_api_key_type([node(None), node("prefill_x")]) == "chat"

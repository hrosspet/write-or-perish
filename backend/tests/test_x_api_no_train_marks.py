"""Words of X API posts never reach the training key through text a
model writes from them (#689, review of PR #691).

- AI replies: a reply written from a payload that held X API posts (or
  text written from them) is marked ``x_api`` and stored at Chat, and the
  next turn that carries it goes out on the chat key.
- Other people's saved writing marks nothing: a Train account with saved
  references and no X API content chooses the same key as on main, turn
  after turn (Peter, 2026-09-29: replies written from references may be
  Train; the call that carries the reference itself is on the chat key).
- Tool writes in such a turn (update_artifact) carry the mark, are never
  Train, and a later version of the kind inherits it.
- Derived documents: every document a model wrote from the writing of an
  account that has had an X API pre-fill (its ``x_prefill`` cost record)
  reads as Chat in the licence, whatever the account's setting.

Runs the real task body through the test_retrieval_loop harness with
distinct train and chat keys, as test_training_key_payload does.
"""
import pytest

from backend.tests.test_retrieval_loop import (  # noqa: F401 (fixture)
    app, generate_llm_response, _FakeSelf, _mk_user, _resp, _fresh,
    _mk_artifact, _ScriptedProvider,
)
from backend.tests.test_training_key_payload import (  # noqa: F401
    keys, _keys_used, _payload, _train_chain, _reference, _entry,
)
from backend.tests.test_training_key_own_rows import (
    OWN, _next_turn, _seed_row, _set_prompt, _train_user,
)
from backend.extensions import db as _db
from backend.models import APICostLog, Node, UserArtifact

TWEET = "XAPI TWEET WORDS VERBATIM"
SAVED = "SOMEONE ELSE'S SAVED WORDS"


def _x_tweet(owner, ai_usage="chat"):
    n = Node(user_id=owner.id, human_owner_id=owner.id, node_type="user",
             privacy_level="private", ai_usage=ai_usage, origin="twitter",
             source_key="twitter:99", provenance="prefill_x")
    n.set_content(TWEET)
    _db.session.add(n)
    _db.session.flush()
    return n


def _x_prefilled(user):
    _db.session.add(APICostLog(user_id=user.id, model_id="x-api/timeline",
                               request_type="x_prefill",
                               cost_microdollars=5000))
    _db.session.commit()


def _turn_one_reads(alice, user_node, llm_node, ref, words):
    """Turn 1: the model reads *ref* in full and repeats *words*. The
    reply's placeholder takes the thread's Train, as a real one does."""
    llm_node.ai_usage = "train"
    _db.session.commit()
    _ScriptedProvider.reset([
        _resp("Reading.", tool_calls=[{
            "id": "t1", "name": "read_full", "input": {"ref": ref}}]),
        _resp(f'You wrote: "{words}".'),
    ])
    generate_llm_response(_FakeSelf(), user_node.id, llm_node.id, "gpt-5",
                          alice.id, source_mode="textmode")
    cont = _fresh(_fresh(llm_node.id).continuation_node_id)
    assert words in cont.get_content()
    return cont


def _turn_two(alice, cont):
    follow, nxt = _next_turn(alice, cont.id)
    _ScriptedProvider.reset([_resp("ok")])
    generate_llm_response(_FakeSelf(), follow.id, nxt.id, "gpt-5",
                          alice.id, source_mode="textmode")


# ── 2. AI replies ────────────────────────────────────────────────────────

def test_the_turn_after_a_pulled_x_api_tweet_stays_off_the_training_key(app, keys):  # noqa: F811
    """The review's probe: on main and on #691's first head, turn 2 went
    out on sk-train with the tweet's words in the payload."""
    alice, system, user_node, llm_node = _train_chain("textmode")
    _train_user(alice)
    tweet = _x_tweet(alice)
    _db.session.commit()

    cont = _turn_one_reads(alice, user_node, llm_node, str(tweet.id), TWEET)
    assert (cont.unlicensed_input, cont.ai_usage) == ("x_api", "chat")

    _turn_two(alice, cont)
    assert TWEET in _payload(0)
    assert _keys_used() == ["sk-chat"]


def test_a_reference_reading_turn_changes_no_later_key(app, keys):  # noqa: F811
    """Control (the re-check's measurement): a Train account with a saved
    reference and no X API content. Turn 1 reads the reference: its first
    call on sk-train, the call carrying the reference on sk-chat, as on
    main. The five turns after it go out on sk-train, as on main, and a
    memory written there stays Train."""
    alice, system, user_node, llm_node = _train_chain("textmode")
    _train_user(alice)
    ref = _reference(alice.id, SAVED)
    _db.session.commit()

    cont = _turn_one_reads(alice, user_node, llm_node,
                           f"reference {ref.id}", SAVED)
    assert _keys_used() == ["sk-train", "sk-chat"]
    assert (getattr(cont, "unlicensed_input", None), cont.ai_usage) == (
        None, "train")

    previous = cont
    for turn in range(5):
        follow, nxt = _next_turn(alice, previous.id, f"turn {turn + 2}")
        calls = [_resp("ok")]
        if turn == 4:
            calls = [_resp("Noting.", tool_calls=[{
                "id": "t1", "name": "update_artifact",
                "input": {"kind": "memory",
                          "updated_content": "A NOTE FROM THIS THREAD"}}]),
                _resp("Noted.")]
        _ScriptedProvider.reset(calls)
        generate_llm_response(_FakeSelf(), follow.id, nxt.id, "gpt-5",
                              alice.id, source_mode="textmode")
        assert set(_keys_used()) == {"sk-train"}, turn
        reply = _fresh(nxt.id)
        previous = (_fresh(reply.continuation_node_id)
                    if reply.continuation_node_id else reply)
        assert getattr(previous, "unlicensed_input", None) is None

    memory = UserArtifact.latest_for(alice.id, "memory")
    assert (getattr(memory, "unlicensed_input", None), memory.ai_usage) == (
        None, "train")


def test_the_turn_after_a_pulled_own_train_entry_keeps_the_training_key(app, keys):  # noqa: F811
    """Control: the user's own Train writing marks nothing."""
    alice, system, user_node, llm_node = _train_chain("textmode")
    _train_user(alice)
    own = _entry(alice, "MY OWN TRAIN WORDS", "train")
    _db.session.commit()

    cont = _turn_one_reads(alice, user_node, llm_node, str(own.id),
                           "MY OWN TRAIN WORDS")
    assert (cont.unlicensed_input, cont.ai_usage) == (None, "train")

    _turn_two(alice, cont)
    assert _keys_used() == ["sk-train"]


# ── 3. Tool writes ───────────────────────────────────────────────────────

def test_memory_written_from_a_quoted_x_api_tweet_never_trains(app, keys):  # noqa: F811
    """The review's probe 3: the memory row was stored 'train' and a later
    {user_memory} turn went out on sk-train."""
    alice, system, user_node, llm_node = _train_chain("textmode")
    _train_user(alice)
    tweet = _x_tweet(alice)
    user_node.set_content("Remember this: {quote:%d}" % tweet.id)
    _db.session.commit()
    _ScriptedProvider.reset([
        _resp("Saving.", tool_calls=[{
            "id": "t1", "name": "update_artifact",
            "input": {"kind": "memory", "updated_content": TWEET}}]),
        _resp("Saved."),
    ])
    generate_llm_response(_FakeSelf(), user_node.id, llm_node.id, "gpt-5",
                          alice.id, source_mode="textmode")
    memory = UserArtifact.latest_for(alice.id, "memory")
    assert (memory.unlicensed_input, memory.ai_usage) == ("x_api", "chat")

    _set_prompt(system, "Memory: {user_memory}")
    cont_id = _fresh(llm_node.id).continuation_node_id
    follow, nxt = _next_turn(alice, cont_id, "new topic")
    _fresh(user_node.id).set_content("Remember this.")
    _db.session.commit()
    _ScriptedProvider.reset([_resp("ok")])
    generate_llm_response(_FakeSelf(), follow.id, nxt.id, "gpt-5",
                          alice.id, source_mode="textmode")
    assert TWEET in _payload(0)
    assert _keys_used() == ["sk-chat"]


@pytest.mark.parametrize("previous_mark, expected", [
    ("x_api", ("x_api", "chat")),
    (None, (None, "train")),                    # control
])
def test_a_later_version_inherits_the_mark(app, keys, previous_mark, expected):  # noqa: F811
    """update_artifact rewrites the whole text, so a version written in a
    clean turn over a marked one keeps the mark."""
    alice, system, user_node, llm_node = _train_chain("textmode")
    _train_user(alice)
    prev = _mk_artifact(alice.id, "memory", "OLD LINE",
                        ai_usage="chat" if previous_mark else "train")
    prev.unlicensed_input = previous_mark
    _db.session.commit()
    _ScriptedProvider.reset([
        _resp("Updating.", tool_calls=[{
            "id": "t1", "name": "update_artifact",
            "input": {"kind": "memory", "base_version": 1,
                      "updated_content": "OLD LINE\nNEW LINE"}}]),
        _resp("Done."),
    ])
    generate_llm_response(_FakeSelf(), user_node.id, llm_node.id, "gpt-5",
                          alice.id, source_mode="textmode")
    memory = UserArtifact.latest_for(alice.id, "memory")
    assert memory.id != prev.id
    assert (memory.unlicensed_input, memory.ai_usage) == expected


# ── 1. Derived documents ─────────────────────────────────────────────────

@pytest.mark.parametrize("placeholder", [
    "{user_profile}", "{user_intentions}", "{user_recent}", "{user_todo}",
    "{user_memory}",
])
@pytest.mark.parametrize("x_prefilled, expected", [
    (True, "sk-chat"),
    (False, "sk-train"),                        # control
])
def test_documents_of_an_x_prefilled_account_never_train(app, keys, placeholder, x_prefilled, expected):  # noqa: F811
    """An account set to Train that has had an X API pre-fill: its own
    entries may train, its profile, intentions and other documents a
    model wrote from its writing do not, whatever their stored value. The
    reply written from them is marked."""
    alice, system, user_node, llm_node = _train_chain("textmode")
    _train_user(alice)
    if x_prefilled:
        _x_prefilled(alice)
    _set_prompt(system, f"system prompt body\n{placeholder}")
    _seed_row(alice, placeholder, "train")

    _ScriptedProvider.reset([_resp("Hello.")])
    generate_llm_response(_FakeSelf(), user_node.id, llm_node.id, "gpt-5",
                          alice.id, source_mode="textmode")

    assert OWN in _payload(0)
    assert _keys_used() == [expected]
    assert _fresh(llm_node.id).unlicensed_input == (
        "x_api" if x_prefilled else None)


def test_the_signal_outlives_the_tweets(app, keys):  # noqa: F811
    """The cost record stays when the pre-filled tweets are deleted, so
    the documents written from them stay capped."""
    from backend.utils.privacy import effective_ai_usage, train_refused
    alice = _mk_user("alice", approved=True, plan="alpha",
                     default_ai_usage="train")
    tweet = _x_tweet(alice)
    intentions = _mk_artifact(alice.id, "intentions", "in their words: X",
                              ai_usage="train")
    _x_prefilled(alice)
    _db.session.delete(tweet)
    _db.session.commit()

    assert effective_ai_usage(intentions) == "chat"
    assert train_refused(intentions)


def test_an_account_without_an_x_prefill_keeps_its_documents_at_train(app, keys):  # noqa: F811
    """Control: a Community Archive pre-fill, or none, changes nothing."""
    from backend.utils.privacy import effective_ai_usage
    bob = _mk_user("bob", approved=True, plan="alpha",
                   default_ai_usage="train")
    intentions = _mk_artifact(bob.id, "intentions", "mine", ai_usage="train")
    _db.session.commit()

    assert effective_ai_usage(intentions) == "train"

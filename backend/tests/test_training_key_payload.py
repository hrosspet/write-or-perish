"""The training key never carries content the chain did not license (#325).

determine_api_key_type reads the nodes in the chain. What those nodes
resolve to — a {quote:ID} of a node marked 'chat', a {quote_ext:ID} saved
reference, what read_full / semantic_search pull in mid-turn, a pull from
last turn re-injected this turn — joins the payload after that decision.
PayloadLicence carries the verdict through assembly and the tool loop, and
the keys are re-read from it before every provider call.

Runs the real task body through the test_retrieval_loop harness, with
distinct train and chat keys in config so each assertion is on the key the
provider was actually handed.
"""
import json

import pytest

from backend.tests.test_retrieval_loop import (  # noqa: F401 (fixture)
    app, _llm_task_mod, generate_llm_response, _FakeSelf, _mk_user,
    _build_chain, _resp, _fresh, _mk_external_item, _mk_artifact, _mk_todo,
    _ScriptedProvider,
)
from backend.extensions import db as _db
from backend.models import Node, NodeEmbedding, ExternalItem
from backend.utils.api_keys import PayloadLicence
from backend.utils.embeddings import pack_vector


@pytest.fixture
def keys(app):  # noqa: F811
    """Distinct typed keys, so the key a call went out on is visible."""
    cfg = app.config
    names = ("OPENAI_API_KEY_TRAIN", "OPENAI_API_KEY_CHAT")
    saved = {k: cfg.get(k) for k in names}
    cfg["OPENAI_API_KEY_TRAIN"] = "sk-train"
    cfg["OPENAI_API_KEY_CHAT"] = "sk-chat"
    yield
    for k, v in saved.items():
        if v is None:
            cfg.pop(k, None)
        else:
            cfg[k] = v


def _keys_used():
    return [c["api_keys"]["openai"] for c in _ScriptedProvider.calls]


def _payload(i):
    return "\n".join(m["text"] for m in _ScriptedProvider.calls[i]["messages"])


def _train(*nodes):
    for n in nodes:
        n.ai_usage = "train"
    _db.session.commit()


def _train_chain(source_mode="textmode"):
    """An agentic chain whose user nodes are all 'train'."""
    alice, system, user_node, llm_node = _build_chain(source_mode)
    _train(system, user_node)
    return alice, system, user_node, llm_node


def _plain_thread(alice, llm_user, text, ai_usage="train"):
    """A bare user node and its placeholder: the single-shot path."""
    note = Node(user_id=alice.id, human_owner_id=alice.id, node_type="user",
                privacy_level="private", ai_usage=ai_usage)
    note.set_content(text)
    _db.session.add(note)
    _db.session.flush()
    llm = Node(user_id=llm_user.id, human_owner_id=alice.id,
               parent_id=note.id, node_type="llm", llm_model="gpt-5",
               llm_task_status="pending", privacy_level="private",
               ai_usage=ai_usage)
    llm.set_content("[LLM response generation pending...]")
    _db.session.add(llm)
    _db.session.commit()
    return note, llm


def _reference(user_id, text, external_id="t1"):
    item = ExternalItem(user_id=user_id, source="twitter_bookmark",
                        external_id=external_id, author_handle="visa",
                        url="https://twitter.com/i/status/1")
    item.set_content(text)
    _db.session.add(item)
    _db.session.flush()
    return item


def _entry(owner, text, ai_usage, privacy_level="private"):
    n = Node(user_id=owner.id, human_owner_id=owner.id, node_type="text",
             privacy_level=privacy_level, ai_usage=ai_usage)
    n.set_content(text)
    _db.session.add(n)
    _db.session.flush()
    return n


# ── the licence itself ───────────────────────────────────────────────────

def test_licence_starts_from_the_chain_and_only_ever_drops(app):  # noqa: F811
    alice = _mk_user("alice", approved=True, plan="alpha")
    licensed = _entry(alice, "mine, train", "train")
    withheld = _entry(alice, "mine, chat", "chat")
    _db.session.commit()

    lic = PayloadLicence("train")
    lic.note_nodes([])
    lic.note_external([])
    lic.note_nodes([licensed.id])
    lic.note_usage("train", "an artifact")
    assert lic.key_type == "train"
    lic.note_nodes([licensed.id, withheld.id])
    assert lic.key_type == "chat"
    lic.note_usage("train", "an artifact")       # never goes back
    assert lic.key_type == "chat"

    assert PayloadLicence("chat").key_type == "chat"
    lic = PayloadLicence("train")
    lic.note_usage("chat", "the todo list")
    assert lic.key_type == "chat"
    lic = PayloadLicence("train")
    lic.note_external([5])
    assert lic.key_type == "chat"


def test_every_retrieval_pull_reports_to_the_licence(app):  # noqa: F811
    """_retrieval_injection_text is the one place mid-turn pulls become
    payload: a node reports by its own setting, a reference always. The
    user's own artifacts and todo list do not (#326 decides both doors:
    this one and the prompt placeholders that pull the same rows)."""
    inject = _llm_task_mod._retrieval_injection_text
    alice = _mk_user("alice", approved=True, plan="alpha")
    chat_art = _mk_artifact(alice.id, "memory", "m", ai_usage="chat")
    train_art = _mk_artifact(alice.id, "scratchpad", "s", ai_usage="train")
    todo = _mk_todo(alice.id, "t", ai_usage="chat")
    ref = _reference(alice.id, "someone's tweet")
    licensed = _entry(alice, "mine, train", "train")
    withheld = _entry(alice, "mine, chat", "chat")
    _db.session.commit()

    def after(tr):
        lic = PayloadLicence("train")
        assert inject(tr, licence=lic) is not None
        return lic.key_type

    assert after({"name": "read_artifact", "artifact_id": train_art.id,
                  "kind": "scratchpad"}) == "train"
    assert after({"name": "read_artifact", "artifact_id": chat_art.id,
                  "kind": "memory"}) == "train"
    assert after({"name": "read_todo", "todo_id": todo.id}) == "train"
    assert after({"name": "read_full", "kind": "node", "ref_id": licensed.id,
                  "user_id": alice.id, "ref": str(licensed.id)}) == "train"
    assert after({"name": "read_full", "kind": "node", "ref_id": withheld.id,
                  "user_id": alice.id, "ref": str(withheld.id)}) == "chat"
    assert after({"name": "read_full", "kind": "external", "ref_id": ref.id,
                  "user_id": alice.id, "ref": "A"}) == "chat"
    assert after({"name": "semantic_search", "query": "q",
                  "matches": [{"node_id": licensed.id, "score": 0.9}],
                  "ext_matches": []}) == "train"
    assert after({"name": "semantic_search", "query": "q",
                  "matches": [{"node_id": withheld.id, "score": 0.9}],
                  "ext_matches": []}) == "chat"
    assert after({"name": "semantic_search", "query": "q", "matches": [],
                  "ext_matches": [{"item_id": ref.id, "score": 0.9}]}) == "chat"
    # No licence: the text still renders (callers that only display it).
    assert inject({"name": "read_todo", "todo_id": todo.id}) is not None


# ── leg 1: {quote_ext:ID} ────────────────────────────────────────────────

def test_a_quoted_saved_reference_takes_the_train_thread_to_chat_keys(app, keys):  # noqa: F811
    alice = _mk_user("alice", approved=True, plan="alpha")
    llm_user = _mk_user("gpt-5", twitter_id="llm-gpt-5")
    ref = _reference(alice.id, "SOMEONE ELSE'S TWEET")
    note, llm = _plain_thread(
        alice, llm_user, "On this: {quote_ext:%d}" % ref.id)

    _ScriptedProvider.reset([_resp("Noted.")])
    generate_llm_response(_FakeSelf(), note.id, llm.id, "gpt-5", alice.id,
                          source_mode=None)

    assert _fresh(llm.id).llm_task_status == "completed"
    assert "SOMEONE ELSE'S TWEET" in _payload(0)   # it really went out
    assert _keys_used() == ["sk-chat"]


# ── leg 2: {quote:ID} ────────────────────────────────────────────────────

@pytest.mark.parametrize("author, quoted_usage, expected", [
    ("bob", "chat", "sk-chat"),
    ("bob", "train", "sk-train"),
    ("alice", "chat", "sk-chat"),
])
def test_a_quoted_node_is_judged_by_its_own_usage(app, keys, author, quoted_usage, expected):  # noqa: F811
    """A node in the payload counts the same whether it is in the chain
    or quoted into it, and whoever wrote it: bob's public 'chat' entry
    and alice's own 'chat' entry both withhold the training key; bob's
    'train' entry does not."""
    alice = _mk_user("alice", approved=True, plan="alpha")
    llm_user = _mk_user("gpt-5", twitter_id="llm-gpt-5")
    owner = alice if author == "alice" else _mk_user("bob", approved=True)
    quoted = _entry(owner, "THE QUOTED ENTRY", quoted_usage,
                    privacy_level="public" if owner is not alice
                    else "private")
    note, llm = _plain_thread(
        alice, llm_user, "Reacting to {quote:%d}" % quoted.id)

    _ScriptedProvider.reset([_resp("Seen.")])
    generate_llm_response(_FakeSelf(), note.id, llm.id, "gpt-5", alice.id,
                          source_mode=None)

    assert "THE QUOTED ENTRY" in _payload(0)
    assert _keys_used() == [expected]


def test_a_thread_of_the_users_own_train_writing_keeps_the_train_key(app, keys):  # noqa: F811
    alice = _mk_user("alice", approved=True, plan="alpha")
    llm_user = _mk_user("gpt-5", twitter_id="llm-gpt-5")
    note, llm = _plain_thread(alice, llm_user, "a thought of my own")

    _ScriptedProvider.reset([_resp("Quite.")])
    generate_llm_response(_FakeSelf(), note.id, llm.id, "gpt-5", alice.id,
                          source_mode=None)

    assert _keys_used() == ["sk-train"]


# ── leg 3: pulled mid-turn ───────────────────────────────────────────────

@pytest.mark.parametrize("entry_usage, follow_up_key", [
    ("chat", "sk-chat"),
    ("train", "sk-train"),
])
def test_read_full_moves_only_the_call_that_carries_the_pull(app, keys, entry_usage, follow_up_key):  # noqa: F811
    """The first call held only the chain and went out on the train key;
    the continuation carries the pulled entry and goes out on its key."""
    alice, system, user_node, llm_node = _train_chain("textmode")
    archive = _entry(alice, "THE FULL ARCHIVE ENTRY", entry_usage)
    _db.session.commit()

    _ScriptedProvider.reset([
        _resp("Reading.", tool_calls=[{
            "id": "t1", "name": "read_full",
            "input": {"ref": str(archive.id)}}]),
        _resp("Done."),
    ])
    generate_llm_response(_FakeSelf(), user_node.id, llm_node.id, "gpt-5",
                          alice.id, source_mode="textmode")

    assert len(_ScriptedProvider.calls) == 2
    assert "THE FULL ARCHIVE ENTRY" not in _payload(0)
    assert "THE FULL ARCHIVE ENTRY" in _payload(1)
    assert _keys_used() == ["sk-train", follow_up_key]


def test_search_previews_of_references_move_the_follow_up_to_chat_keys(app, keys, monkeypatch):  # noqa: F811
    import backend.utils.embeddings as emb_mod
    monkeypatch.setattr(
        emb_mod, "embed_texts", lambda texts, key, **kw: [[1.0, 0.0]])
    alice, system, user_node, llm_node = _train_chain("textmode")
    _mk_external_item(alice.id, "the perfect saved tweet", [1.0, 0.0])
    _db.session.commit()

    _ScriptedProvider.reset([
        _resp("Searching.", tool_calls=[{
            "id": "t1", "name": "semantic_search",
            "input": {"query": "zen"}}]),
        _resp("Found it."),
    ])
    generate_llm_response(_FakeSelf(), user_node.id, llm_node.id, "gpt-5",
                          alice.id, source_mode="textmode")

    assert "saved reference by @visa" in _payload(1)
    assert _keys_used() == ["sk-train", "sk-chat"]


def test_search_previews_of_the_users_train_entries_keep_the_train_key(app, keys, monkeypatch):  # noqa: F811
    import backend.utils.embeddings as emb_mod
    monkeypatch.setattr(
        emb_mod, "embed_texts", lambda texts, key, **kw: [[1.0, 0.0]])
    alice, system, user_node, llm_node = _train_chain("textmode")
    archive = _entry(alice, "my old zen writing", "train")
    _db.session.add(NodeEmbedding(
        node_id=archive.id, user_id=alice.id, model="test",
        content_hash="h", vector=pack_vector([1.0, 0.0])))
    _db.session.commit()

    _ScriptedProvider.reset([
        _resp("Searching.", tool_calls=[{
            "id": "t1", "name": "semantic_search",
            "input": {"query": "zen"}}]),
        _resp("Found it."),
    ])
    generate_llm_response(_FakeSelf(), user_node.id, llm_node.id, "gpt-5",
                          alice.id, source_mode="textmode")

    assert "my old zen writing" in _payload(1)
    assert _keys_used() == ["sk-train", "sk-train"]


def test_a_pull_from_last_turn_is_re_injected_on_chat_keys(app, keys):  # noqa: F811
    """A retrieval the loop did not deliver (its budget was spent, so the
    final reply's read_full ran in _finalize, unreported) is re-resolved
    into the NEXT turn's payload during assembly — before any call."""
    alice, system, user_node, llm_node = _train_chain("voice")
    ref = _reference(alice.id, "LAST TURN'S TWEET")
    # Last turn's reply, carrying the undelivered pull.
    prev = _fresh(llm_node.id)
    prev.set_content("Let me read that one.")
    prev.llm_task_status = "completed"
    prev.tool_calls_meta = json.dumps([{
        "name": "read_full", "status": "success", "kind": "external",
        "ref_id": ref.id, "user_id": alice.id, "ref": "A"}])
    follow = Node(user_id=alice.id, human_owner_id=alice.id,
                  parent_id=prev.id, node_type="user",
                  privacy_level="private", ai_usage="train")
    follow.set_content("and?")
    _db.session.add(follow)
    _db.session.flush()
    llm_user = _mk_user("gpt-5b", twitter_id="llm-gpt-5b")
    reply = Node(user_id=llm_user.id, human_owner_id=alice.id,
                 parent_id=follow.id, node_type="llm", llm_model="gpt-5",
                 llm_task_status="pending", privacy_level="private",
                 ai_usage="train")
    reply.set_content("[LLM response generation pending...]")
    _db.session.add(reply)
    _db.session.commit()

    _ScriptedProvider.reset([_resp("Right.")])
    generate_llm_response(_FakeSelf(), follow.id, reply.id, "gpt-5",
                          alice.id, source_mode="voice")

    assert len(_ScriptedProvider.calls) == 1
    assert "LAST TURN'S TWEET" in _payload(0)
    assert _keys_used() == ["sk-chat"]


def test_a_drop_holds_for_every_later_round_of_the_turn(app, keys):  # noqa: F811
    """Once dropped, a later round pulling only 'train' content does not
    bring the train key back."""
    alice, system, user_node, llm_node = _train_chain("textmode")
    withheld = _entry(alice, "MY CHAT ENTRY", "chat")
    licensed = _entry(alice, "MY TRAIN ENTRY", "train")
    _db.session.commit()

    _ScriptedProvider.reset([
        _resp("Reading one.", tool_calls=[{
            "id": "t1", "name": "read_full",
            "input": {"ref": str(withheld.id)}}]),
        _resp("And another.", tool_calls=[{
            "id": "t2", "name": "read_full",
            "input": {"ref": str(licensed.id)}}]),
        _resp("Done."),
    ])
    generate_llm_response(_FakeSelf(), user_node.id, llm_node.id, "gpt-5",
                          alice.id, source_mode="textmode")

    assert "MY TRAIN ENTRY" in _payload(2)
    assert _keys_used() == ["sk-train", "sk-chat", "sk-chat"]


def test_a_prompt_quoting_a_reference_is_never_served_from_the_render_cache(app, keys, monkeypatch):  # noqa: F811
    """The #192 cached system render replays the resolved text on later
    turns, past the resolution that reports to the licence. {quote:ID}
    already made a prompt uncacheable; {quote_ext:ID} must too, or turn
    two goes out on the train key with the tweet in it (#325 review)."""
    import backend.utils.prompt_cache as prompt_cache
    store = {}
    monkeypatch.setattr(prompt_cache, "get_cached_render",
                        lambda config, node: store.get(node.id))
    monkeypatch.setattr(prompt_cache, "store_render",
                        lambda config, node, text: store.__setitem__(node.id, text))

    alice, system, user_node, llm_node = _train_chain("voice")
    ref = _reference(alice.id, "THE PROMPT'S TWEET")
    # The system node renders its pinned prompt artifact, not its own text.
    system.get_artifact("prompt").set_content(
        "system prompt body; consider {quote_ext:%d}" % ref.id)
    _db.session.commit()

    _ScriptedProvider.reset([_resp("Turn one.")])
    generate_llm_response(_FakeSelf(), user_node.id, llm_node.id, "gpt-5",
                          alice.id, source_mode="voice")
    assert "THE PROMPT'S TWEET" in _payload(0)
    assert _keys_used() == ["sk-chat"]
    assert store == {}                      # not cached: it is volatile

    reply = _fresh(llm_node.id)
    follow = Node(user_id=alice.id, human_owner_id=alice.id,
                  parent_id=reply.id, node_type="user",
                  privacy_level="private", ai_usage="train")
    follow.set_content("and then?")
    _db.session.add(follow)
    _db.session.flush()
    llm_user = _mk_user("gpt-5b", twitter_id="llm-gpt-5b")
    reply2 = Node(user_id=llm_user.id, human_owner_id=alice.id,
                  parent_id=follow.id, node_type="llm", llm_model="gpt-5",
                  llm_task_status="pending", privacy_level="private",
                  ai_usage="train")
    reply2.set_content("[LLM response generation pending...]")
    _db.session.add(reply2)
    _db.session.commit()

    _ScriptedProvider.reset([_resp("Turn two.")])
    generate_llm_response(_FakeSelf(), follow.id, reply2.id, "gpt-5",
                          alice.id, source_mode="voice")
    assert "THE PROMPT'S TWEET" in _payload(0)
    assert _keys_used() == ["sk-chat"]

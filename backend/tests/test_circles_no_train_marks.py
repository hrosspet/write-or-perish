"""Text a model writes from a turn whose input held another person's
writing in a circle-shared thread never reaches the training key later
(Circles; the review of #631 after the merge of main, finding 1).

The turn itself already goes out on the chat key. What it writes is
marked like text written from X API posts (#689): ``unlicensed_input =
"circle"``, stored at Chat at most, and every later licence decision reads
it as Chat, also in a later version the user edits.

- The reader's memory, written with update_artifact from Text mode under
  his reply in the author's thread and approved by him.
- The author's memory, written the same way under her answer to a
  reader's reply.
- The author's own AI reply in her thread, written from a reader's reply.
- The todo merge of a proposal made in a shared thread.
- Control: a private thread writes at the account's default, unmarked.

Each case ends with a later turn on the training key that does not carry
the text. Runs the real task body through the test_retrieval_loop harness
(scripted provider, no model is called) with distinct train and chat
keys.
"""
import json
import sys

import pytest

from backend.tests.test_retrieval_loop import (  # noqa: F401 (fixture)
    app, _llm_task_mod, generate_llm_response, _FakeSelf,
    _ScriptedProvider, _mk_user, _resp, _fresh,
)
from backend.extensions import db as _db
from backend.models import (
    Draft, Node, NodeReader, User, UserArtifact, UserPrompt, UserTodo)

ANNA = "ANNA SHARED WORDS"
BEN = "BEN REPLY WORDS"


@pytest.fixture
def setup(app, monkeypatch):  # noqa: F811
    """Circles on for everyone, distinct train and chat keys, and the
    approval code reading the harness's task module."""
    cfg = app.config
    names = ("CIRCLES_V1", "OPENAI_API_KEY_TRAIN", "OPENAI_API_KEY_CHAT")
    saved = {k: cfg.get(k) for k in names}
    cfg["CIRCLES_V1"] = True
    cfg["OPENAI_API_KEY_TRAIN"] = "sk-train"
    cfg["OPENAI_API_KEY_CHAT"] = "sk-chat"
    monkeypatch.setitem(sys.modules, "backend.tasks.llm_completion",
                        _llm_task_mod)
    from backend.utils import privacy
    privacy._pointers_seen["at"] = 0.0
    yield
    for k, v in saved.items():
        if v is None:
            cfg.pop(k, None)
        else:
            cfg[k] = v


def _keys_used():
    return [c["api_keys"]["openai"] for c in _ScriptedProvider.calls]


def _payload(i=0):
    return "\n".join(m["text"] for m in _ScriptedProvider.calls[i]["messages"])


def _node(user, parent, text, privacy="private", ai_usage="train"):
    n = Node(user_id=user.id, human_owner_id=user.id,
             parent_id=parent.id if parent else None, node_type="user",
             privacy_level=privacy, ai_usage=ai_usage)
    # A reply into a shared thread joins it (circles.join_reply).
    if privacy == "circles" and parent is not None:
        n.audience_root_id = parent.audience_root_id
    n.set_content(text)
    _db.session.add(n)
    _db.session.flush()
    return n


def _prompt_under(owner, parent, prompt_text="system prompt body"):
    """The Text-mode prompt node /textmode/from-node puts under *parent*
    (session_helpers.attach_agentic_prompt_under): the writer's alone."""
    from backend.utils.context_artifacts import attach_context_artifacts
    prompt = UserPrompt(user_id=owner.id, prompt_key="textmode",
                        title="Text", generated_by="default")
    prompt.set_content(prompt_text)
    _db.session.add(prompt)
    _db.session.flush()
    system = Node(user_id=owner.id, human_owner_id=owner.id,
                  parent_id=parent.id if parent else None, node_type="user",
                  privacy_level="private", ai_usage="train")
    _db.session.add(system)
    _db.session.flush()
    attach_context_artifacts(system.id, owner.id, prompt_record=prompt)
    return system


def _world():
    """anna (Train) shares an entry at Train with ben (Train); ben
    replies in her thread."""
    anna = _mk_user("anna", approved=True, plan="alpha",
                    default_ai_usage="train")
    ben = _mk_user("ben", approved=True, plan="alpha",
                   default_ai_usage="train")
    _mk_user("gpt-5", twitter_id="llm-gpt-5")
    entry = _node(anna, None, ANNA)
    entry.privacy_level = "circles"
    entry.audience_root_id = entry.id
    _db.session.add(NodeReader(node_id=entry.id, user_id=ben.id))
    _db.session.flush()
    reply = _node(ben, entry, BEN, privacy="circles")
    _db.session.commit()
    return anna, ben, entry, reply


def _ask(parent, user, responses):
    """An AI reply for *user* under *parent*, placed as the routes place
    it (create_llm_placeholder: it joins a shared thread it is asked in),
    then the task."""
    from backend.utils.llm_nodes import create_llm_placeholder
    llm_node, _ = create_llm_placeholder(
        parent.id, "gpt-5", user.id, ai_usage="train", enqueue=False)
    _db.session.commit()
    _ScriptedProvider.reset(responses)
    generate_llm_response(_FakeSelf(), parent.id, llm_node.id, "gpt-5",
                          user.id, source_mode="textmode")
    return _fresh(llm_node.id)


def _written(reply):
    """The node that holds the reply's final text (a tool round finalizes
    a continuation)."""
    return (_fresh(reply.continuation_node_id)
            if reply.continuation_node_id else reply)


def _approve_held(user):
    from backend.utils.tool_approvals import decide
    _db.session.expire_all()
    [held] = Draft.query.filter_by(user_id=user.id,
                                   label="tool_pending").all()
    outcome, result = decide(held, user.id, approve=True)
    _db.session.commit()
    assert outcome == "approved", result


def _later_private_turn(user, placeholder):
    """A later turn in *user*'s own private Train thread whose prompt
    renders *placeholder*: the training key unless what it renders may
    not train."""
    system = _prompt_under(user, None, f"system prompt body\n{placeholder}")
    msg = _node(user, system, "a new topic")
    _db.session.commit()
    _ScriptedProvider.reset([_resp("ok")])
    llm_user = User.query.filter_by(username="gpt-5").first()
    llm = Node(user_id=llm_user.id, human_owner_id=user.id,
               parent_id=msg.id, node_type="llm", llm_model="gpt-5",
               llm_task_status="pending", privacy_level="private",
               ai_usage="train")
    llm.set_content("[LLM response generation pending...]")
    _db.session.add(llm)
    _db.session.commit()
    generate_llm_response(_FakeSelf(), msg.id, llm.id, "gpt-5", user.id,
                          source_mode="textmode")
    return _payload(0), _keys_used()


def _memory_write(text):
    return {"id": "w1", "name": "update_artifact",
            "input": {"kind": "memory", "updated_content": text}}


def _edited_by_hand(row):
    """A version the user saves on the Artifacts page (routes/artifacts):
    the account's default, the mark carried on."""
    copy = UserArtifact(user_id=row.user_id, kind=row.kind, title=row.title,
                        generated_by="user", ai_usage="train",
                        unlicensed_input=row.unlicensed_input)
    copy.set_content(row.get_content() + "\n- my own line")
    _db.session.add(copy)
    _db.session.commit()
    return copy


# ── The reader ───────────────────────────────────────────────────────────

def test_a_readers_memory_written_in_a_shared_thread_never_trains(app, setup):  # noqa: F811,E501
    """ben opens Text mode from his reply in anna's thread and approves a
    memory that holds anna's words."""
    anna, ben, entry, reply = _world()
    prompt = _prompt_under(ben, reply)
    msg = _node(ben, prompt, "remember what anna said")
    _db.session.commit()
    answer = _ask(msg, ben, [
        _resp("Noting.", tool_calls=[_memory_write(f"- anna: {ANNA}")]),
        _resp("Proposed."),
    ])
    assert answer.audience_root_id == entry.id
    assert _keys_used() == ["sk-chat", "sk-chat"]
    assert (_written(answer).unlicensed_input,
            _written(answer).ai_usage) == ("circle", "chat")
    _approve_held(ben)

    memory = UserArtifact.latest_for(ben.id, "memory")
    assert ANNA in memory.get_content()
    assert (memory.unlicensed_input, memory.ai_usage) == ("circle", "chat")
    sent, keys = _later_private_turn(ben, "{user_memory}")
    assert ANNA in sent
    assert keys == ["sk-chat"]

    # A version ben edits by hand keeps the mark: still never Train.
    _edited_by_hand(memory)
    sent, keys = _later_private_turn(ben, "{user_memory}")
    assert ANNA in sent and "my own line" in sent
    assert keys == ["sk-chat"]


# ── The author ───────────────────────────────────────────────────────────

def test_the_authors_memory_written_from_a_readers_reply_never_trains(app, setup):  # noqa: F811,E501
    """anna answers ben's reply and opens Text mode under her answer: her
    memory then holds ben's words."""
    anna, ben, entry, reply = _world()
    her_answer = _node(anna, reply, "anna answers ben", privacy="circles")
    prompt = _prompt_under(anna, her_answer)
    msg = _node(anna, prompt, "remember what ben said")
    _db.session.commit()
    answer = _ask(msg, anna, [
        _resp("Noting.", tool_calls=[_memory_write(f"- ben: {BEN}")]),
        _resp("Proposed."),
    ])
    assert BEN in _payload(0)
    _approve_held(anna)

    memory = UserArtifact.latest_for(anna.id, "memory")
    assert BEN in memory.get_content()
    assert (memory.unlicensed_input, memory.ai_usage) == ("circle", "chat")
    sent, keys = _later_private_turn(anna, "{user_memory}")
    assert BEN in sent
    assert keys == ["sk-chat"]
    assert answer.llm_task_status == "completed"


def test_the_authors_ai_reply_written_from_a_readers_reply_never_trains(app, setup):  # noqa: F811,E501
    """anna asks for an AI reply under her answer to ben's reply. It is
    hers and private, and it repeats ben's words: Chat, marked, and her
    {user_export} that carries it is off the training key."""
    from backend.routes.export_data import build_user_export_content
    from backend.utils.api_keys import PayloadLicence
    anna, ben, entry, reply = _world()
    her_answer = _node(anna, reply, "anna answers ben", privacy="circles")
    _db.session.commit()
    answer = _ask(her_answer, anna, [_resp(f'ben wrote: "{BEN}".')])
    assert BEN in _payload(0)
    answer = _written(answer)
    assert BEN in answer.get_content()
    assert (answer.unlicensed_input, answer.ai_usage) == ("circle", "chat")

    lic = PayloadLicence("train")
    text = build_user_export_content(anna, filter_ai_usage=True,
                                     licence=lic)
    text = text.get("content") if isinstance(text, dict) else text
    assert BEN in text
    assert lic.key_type == "chat"
    assert lic.unlicensed_input == "circle"


def test_the_readers_ai_reply_is_marked_too(app, setup):  # noqa: F811
    """ben's own AI reply in anna's thread was already Chat; it now
    carries the mark, so what is written from it later is marked."""
    anna, ben, entry, reply = _world()
    answer = _written(_ask(reply, ben, [_resp(f'anna wrote "{ANNA}".')]))
    assert ANNA in _payload(0)
    assert (answer.unlicensed_input, answer.ai_usage) == ("circle", "chat")


# ── The todo merge ───────────────────────────────────────────────────────

def test_a_todo_merged_from_a_proposal_made_in_a_shared_thread_never_trains(app, setup, monkeypatch):  # noqa: F811,E501
    """A proposal reply written in anna's thread (it holds her words)
    is merged into ben's todo list: the new list carries the mark and is
    Chat, and a later {user_todo} turn is off the training key."""
    import backend.tasks.voice_todo_merge as vtm
    import backend.utils.prompts as prompts
    anna, ben, entry, reply = _world()
    proposal = _written(_ask(reply, ben, [
        _resp(f"### New Tasks\n- call anna about {ANNA}")]))
    assert proposal.unlicensed_input == "circle"
    proposal.tool_calls_meta = json.dumps([{"name": "propose_todo"}])
    old = UserTodo(user_id=ben.id, generated_by="user", ai_usage="train")
    old.set_content("- an old task")
    _db.session.add(old)
    _db.session.commit()

    class _Merger:
        @classmethod
        def get_completion(cls, model_id, messages, api_keys, **kwargs):
            return {"content": json.dumps({"edits": [{
                "old_text": "- an old task",
                "new_text": f"- an old task\n- call anna about {ANNA}"}],
                "updated_content": ""}),
                "truncated": False, "input_tokens": 10,
                "output_tokens": 10, "total_tokens": 20}
    monkeypatch.setattr(vtm, "LLMProvider", _Merger)
    monkeypatch.setattr(vtm, "get_api_keys_for_usage", lambda *a, **k: {})
    monkeypatch.setattr(prompts, "get_user_prompt", lambda uid, key: "MERGE")
    vtm._run_merge(proposal, proposal.get_content(), ben.id, "gpt-5", None)
    _db.session.expire_all()

    todo = UserTodo.query.filter_by(user_id=ben.id).order_by(
        UserTodo.id.desc()).first()
    assert todo.id != old.id
    assert ANNA in todo.get_content()
    assert (todo.unlicensed_input, todo.ai_usage) == ("circle", "chat")
    sent, keys = _later_private_turn(ben, "{user_todo}")
    assert ANNA in sent
    assert keys == ["sk-chat"]


# ── Control ──────────────────────────────────────────────────────────────

def test_control_a_private_thread_writes_at_the_accounts_default(app, setup):  # noqa: F811,E501
    """ben's own private Text-mode thread, nobody else's words: the reply
    and the memory are Train and unmarked, and a later turn carrying the
    memory goes out on the training key."""
    anna, ben, entry, reply = _world()
    prompt = _prompt_under(ben, None)
    msg = _node(ben, prompt, "remember that I like tea")
    _db.session.commit()
    answer = _ask(msg, ben, [
        _resp("Noting.", tool_calls=[_memory_write("- BEN LIKES TEA")]),
        _resp("Noted."),
    ])
    assert answer.audience_root_id is None
    assert _keys_used() == ["sk-train", "sk-train"]
    assert (_written(answer).unlicensed_input,
            _written(answer).ai_usage) == (None, "train")
    memory = UserArtifact.latest_for(ben.id, "memory")
    assert (memory.unlicensed_input, memory.ai_usage) == (None, "train")
    sent, keys = _later_private_turn(ben, "{user_memory}")
    assert "BEN LIKES TEA" in sent
    assert keys == ["sk-train"]

"""Circles and the approval of AI writes (Peter, 2026-11-12): "let's keep
the confirmation dialog only for nodes directly in history, including
quotes of public / circles nodes in private entries".

A quote of a circle-shared node inside the requester's own private entry
counts as another person's text exactly when the message builder shows
its text: the history check (foreign_text.history_by_others) uses the
quote resolver's access rule for a model acting for the user (as_ai), so
the approval hold and the payload agree. Runs the real task body through
the test_retrieval_loop harness (scripted provider, no model is called).
"""
import pytest

from backend.tests.test_retrieval_loop import (  # noqa: F401 (fixture)
    app, generate_llm_response, _FakeSelf, _ScriptedProvider, _mk_user,
    _mk_artifact, _resp,
)
from backend.tests.test_personal_context_owner import _llm, _system_node
from backend.tests.test_circles_task import (  # noqa: F401 (fixture)
    circles, _node, _share_with,
)
from backend.extensions import db as _db
from backend.models import Draft, UserArtifact
from backend.utils.foreign_text import OTHER_USER, history_by_others


@pytest.fixture
def people(app, circles):  # noqa: F811
    anna = _mk_user("anna", approved=True, plan="alpha",
                    default_ai_usage="chat")
    ben = _mk_user("ben", approved=True, plan="alpha",
                   default_ai_usage="chat")
    _mk_user("gpt-5", twitter_id="llm-gpt-5")
    _db.session.commit()
    return anna, ben


def _memory(user):
    _mk_artifact(user.id, "memory", "- likes tea", title="Memory")
    _db.session.commit()


def _write_memory():
    return {"id": "w1", "name": "update_artifact",
            "input": {"kind": "memory",
                      "updated_content": "- likes tea\n- x",
                      "base_version": 1}}


def _ask(parent, user):
    """*user*'s private AI reply under *parent* that tries one memory
    write; returns the text of the first call's messages."""
    llm_node = _llm(parent, user)
    llm_node.privacy_level = "private"
    _db.session.commit()
    _ScriptedProvider.reset([
        _resp("Noting.", tool_calls=[_write_memory()]), _resp("Done.")])
    generate_llm_response(_FakeSelf(), parent.id, llm_node.id, "gpt-5",
                          user.id, source_mode="textmode")
    return "\n".join(m["text"]
                     for m in _ScriptedProvider.calls[0]["messages"])


def _held(user):
    _db.session.expire_all()
    return Draft.query.filter_by(user_id=user.id,
                                 label="tool_pending").all()


def _memory_now(user):
    _db.session.expire_all()
    return UserArtifact.latest_for(user.id, "memory").get_content()


def test_a_quote_of_someone_elses_circle_entry_is_not_shown_and_holds_nothing(
        app, people):  # noqa: F811
    """anna's entry is shared with ben; ben quotes it in his own private
    Text-mode thread. A model acting for ben never reads another
    person's shared entry outside its thread: the quote is not resolved,
    so his history holds none of her text and his write runs."""
    anna, ben = people
    _memory(ben)
    entry = _node(anna, None, "ANNAS CIRCLE WORDS")
    _share_with(entry, ben)
    system = _system_node(ben, privacy_level="private")
    mine = _node(ben, system, f"thinking about {{quote:{entry.id}}}")
    _db.session.commit()

    assert history_by_others([system, mine], ben.id) == set()
    sent = _ask(mine, ben)
    assert "ANNAS CIRCLE WORDS" not in sent
    assert f"[Quote #{entry.id}: inaccessible]" in sent
    assert _held(ben) == []
    assert _memory_now(ben) == "- likes tea\n- x"


def test_a_quote_of_a_reply_in_her_own_shared_thread_is_shown_and_holds_the_write(
        app, people):  # noqa: F811
    """ben's reply in anna's shared thread is read by her model (the
    author reads every reply written to her thread). Quoted in her own
    private entry it is resolved, so it is another person's text in the
    history and her write waits for her approval."""
    anna, ben = people
    _memory(anna)
    entry = _node(anna, None, "ANNAS ENTRY")
    _share_with(entry, ben)
    bens = _node(ben, entry, "BENS REPLY IN HER THREAD", privacy="circles")
    system = _system_node(anna, privacy_level="private")
    hers = _node(anna, system, f"about ben's reply {{quote:{bens.id}}}")
    _db.session.commit()

    assert history_by_others([system, hers], anna.id) == {OTHER_USER}
    sent = _ask(hers, anna)
    assert "BENS REPLY IN HER THREAD" in sent
    assert len(_held(anna)) == 1
    assert _memory_now(anna) == "- likes tea"


def test_the_hold_and_the_payload_agree_for_circle_quotes(app, people):  # noqa: F811
    """For each shape of a circle quote in the requester's own private
    entry, the check flags the history exactly when the quoted text is
    in what the model is sent."""
    anna, ben = people
    entry = _node(anna, None, "ANNAS SHARED ENTRY TEXT")
    _share_with(entry, ben)
    bens_reply = _node(ben, entry, "BENS REPLY TEXT", privacy="circles")
    annas_own_reply = _node(anna, entry, "ANNAS OWN REPLY TEXT",
                            privacy="circles")
    bens_own_entry = _node(ben, None, "BENS OWN SHARED ENTRY TEXT")
    _share_with(bens_own_entry, anna)
    _db.session.commit()
    cases = [
        # (requester, quoted node, its text)
        (ben, entry, "ANNAS SHARED ENTRY TEXT"),        # hers, shared with him
        (anna, bens_reply, "BENS REPLY TEXT"),          # his, in her thread
        (anna, annas_own_reply, "ANNAS OWN REPLY TEXT"),  # her own
        (ben, bens_own_entry, "BENS OWN SHARED ENTRY TEXT"),  # his own
        (anna, bens_own_entry, "BENS OWN SHARED ENTRY TEXT"),  # his, shared with her
    ]
    for who, quoted, text in cases:
        system = _system_node(who, privacy_level="private")
        mine = _node(who, system, f"see {{quote:{quoted.id}}}")
        _db.session.commit()
        flagged = bool(history_by_others([system, mine], who.id))
        llm_node = _llm(mine, who)
        llm_node.privacy_level = "private"
        _db.session.commit()
        _ScriptedProvider.reset([_resp("ok")])
        generate_llm_response(_FakeSelf(), mine.id, llm_node.id, "gpt-5",
                              who.id, source_mode="textmode")
        sent = "\n".join(m["text"]
                         for m in _ScriptedProvider.calls[0]["messages"])
        shown = text in sent
        mine_text = quoted.human_owner_id == who.id
        assert flagged == (shown and not mine_text), (
            who.username, quoted.id, flagged, shown)

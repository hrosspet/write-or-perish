"""Circles in the completion task: the review's end-to-end probes of
#631, kept as tests. The real generate_llm_response runs through the
test_retrieval_loop harness (scripted provider, no model is called)."""
from datetime import datetime

import pytest

from backend.tests.test_retrieval_loop import (  # noqa: F401 (fixture)
    app, _llm_task_mod, generate_llm_response, _FakeSelf,
    _ScriptedProvider, _mk_user, _resp, _fresh,
)
from backend.extensions import db as _db
from backend.models import Node, NodeReader


@pytest.fixture
def circles(app):  # noqa: F811
    cfg = app.config
    saved = {k: cfg.get(k) for k in (
        "CIRCLES_V1", "OPENAI_API_KEY_TRAIN", "OPENAI_API_KEY_CHAT",
        "ANTHROPIC_API_KEY_TRAIN", "ANTHROPIC_API_KEY_CHAT",
        "SUPPORTED_MODELS")}
    cfg["CIRCLES_V1"] = True
    cfg["OPENAI_API_KEY_TRAIN"] = "sk-train"
    cfg["OPENAI_API_KEY_CHAT"] = "sk-chat"
    cfg["ANTHROPIC_API_KEY_TRAIN"] = "ant-train"
    cfg["ANTHROPIC_API_KEY_CHAT"] = "ant-chat"
    cfg["SUPPORTED_MODELS"] = {
        "gpt-5": {"provider": "openai", "api_model": "gpt-5"},
        "claude-x": {"provider": "anthropic", "api_model": "claude-x"},
    }
    yield
    for k, v in saved.items():
        if v is None:
            cfg.pop(k, None)
        else:
            cfg[k] = v


def _node(user, parent, text, privacy="private", ai_usage="chat",
          human_owner=None, node_type="user", llm_model=None):
    n = Node(user_id=user.id, human_owner_id=(human_owner or user).id,
             parent_id=parent.id if parent else None, node_type=node_type,
             privacy_level=privacy, ai_usage=ai_usage, llm_model=llm_model)
    # A reply into a shared thread joins it explicitly (the routes call
    # circles.join_reply; nothing joins by being created under a node).
    if privacy == "circles" and parent is not None \
            and parent.audience_root_id is not None:
        n.audience_root_id = parent.audience_root_id
    n.set_content(text)
    _db.session.add(n)
    _db.session.flush()
    return n


def _share_with(entry, *readers):
    entry.privacy_level = "circles"
    entry.audience_root_id = entry.id
    for r in readers:
        _db.session.add(NodeReader(node_id=entry.id, user_id=r.id))
    _db.session.flush()


def _placeholder(parent, owner, model, privacy, ai_usage, meta=None):
    import json as _json
    llm_user = _db.session.query(__import__("backend.models", fromlist=["User"]).User).filter_by(username=model).first()
    n = Node(user_id=llm_user.id, human_owner_id=owner.id,
             parent_id=parent.id, node_type="llm", llm_model=model,
             llm_task_status="pending", privacy_level=privacy,
             ai_usage=ai_usage,
             tool_calls_meta=_json.dumps(meta) if meta else None)
    n.set_content("[LLM response generation pending...]")
    _db.session.add(n)
    _db.session.commit()
    return n


def _sent(i=0):
    return "\n".join(m["text"] for m in _ScriptedProvider.calls[i]["messages"])


def _world():
    anna = _mk_user("anna", approved=True, plan="alpha",
                    default_ai_usage="train")
    ben = _mk_user("ben", approved=True, plan="alpha",
                   default_ai_usage="train", preferred_model="gpt-5")
    chidi = _mk_user("chidi", approved=True, plan="alpha",
                     default_ai_usage="train")
    gpt = _mk_user("gpt-5", twitter_id="llm-gpt-5")
    claude = _mk_user("claude-x", twitter_id="llm-claude-x")
    root = _node(anna, None, "ANNA PRIVATE ROOT", ai_usage="train")
    her_ai = _node(claude, root, "ANNA PRIVATE AI REPLY", ai_usage="train",
                   human_owner=anna, node_type="llm", llm_model="claude-x")
    entry = _node(anna, her_ai, "ANNA SHARED ENTRY", ai_usage="train")
    _share_with(entry, ben, chidi)
    c_none = _node(chidi, entry, "CHIDI SAYS NONE", privacy="circles",
                   ai_usage="none")
    c_chat = _node(chidi, c_none, "CHIDI SAYS CHAT", privacy="circles",
                   ai_usage="train")
    b = _node(ben, c_chat, "BEN ASKS", privacy="circles", ai_usage="train")
    _db.session.commit()
    return dict(anna=anna, ben=ben, chidi=chidi, gpt=gpt, claude=claude,
                root=root, her_ai=her_ai, entry=entry, c_none=c_none,
                c_chat=c_chat, b=b)


def test_ask_my_ai_end_to_end(app, circles):  # noqa: F811
    from backend.utils.llm_nodes import (
        CIRCLE_ASK_MARKER, create_llm_placeholder, circle_ask_ai_usage)
    from backend.utils.llm_nodes import own_chat_model
    w = _world()
    ben = w["ben"]
    # The route's rule for an Ask my AI: the asker's account model.
    model = own_chat_model(ben)
    from backend.utils.llm_nodes import reply_refusal
    print("refusal for the ask:", reply_refusal(
        w["b"], ben.id, circle_ask_ai_usage(ben), circle_ask=True))
    llm_node = _placeholder(
        w["b"], ben, model, "circles", circle_ask_ai_usage(ben),
        meta=[{"name": CIRCLE_ASK_MARKER, "entry_id": w["entry"].id}])
    _ScriptedProvider.reset([_resp("An answer.")])
    generate_llm_response(_FakeSelf(), w["b"].id, llm_node.id, model, ben.id)
    assert len(_ScriptedProvider.calls) == 1
    sent = _sent()
    call = _ScriptedProvider.calls[0]
    assert "ANNA SHARED ENTRY" in sent
    assert "CHIDI SAYS CHAT" in sent and "BEN ASKS" in sent
    assert "CHIDI SAYS NONE" not in sent
    assert "ANNA PRIVATE" not in sent
    keys = call["api_keys"]
    assert "sk-train" not in keys.values() and "ant-train" not in keys.values()
    assert model == "gpt-5", "ben's model was switched by anna's private reply"


def test_authors_quote_of_readers_reply_goes_out_on_train_key(app, circles):  # noqa: F811,E501
    """Decision 4: no prompt carrying another person's circle-shared
    writing goes out on the training key. anna quotes ben's reply (marked
    train) from her own private thread."""
    from backend.utils.llm_nodes import create_llm_placeholder
    w = _world()
    anna = w["anna"]
    note = _node(anna, None, "anna thinks about {quote:%d}" % w["b"].id,
                 ai_usage="train")
    _db.session.commit()
    llm_node = _placeholder(note, anna, "gpt-5", "private", "train")
    _ScriptedProvider.reset([_resp("ok")])
    generate_llm_response(_FakeSelf(), note.id, llm_node.id, "gpt-5",
                          anna.id)
    sent = _sent()
    keys = _ScriptedProvider.calls[0]["api_keys"]
    assert "BEN ASKS" in sent
    assert "sk-train" not in keys.values()


def test_authors_quote_of_readers_none_reply_is_blocked(app, circles):  # noqa: F811,E501
    from backend.utils.llm_nodes import create_llm_placeholder
    w = _world()
    anna = w["anna"]
    note = _node(anna, None, "anna thinks about {quote:%d}"
                 % w["c_none"].id, ai_usage="chat")
    _db.session.commit()
    llm_node = _placeholder(note, anna, "gpt-5", "private", "chat")
    _ScriptedProvider.reset([_resp("ok")])
    generate_llm_response(_FakeSelf(), note.id, llm_node.id, "gpt-5",
                          anna.id)
    sent = _sent()
    assert "CHIDI SAYS NONE" not in sent


def test_authors_export_carries_readers_replies(app, circles):  # noqa: F811
    from backend.routes.export_data import build_user_export_content
    from backend.utils.api_keys import PayloadLicence
    w = _world()
    lic = PayloadLicence("train")
    text = build_user_export_content(w["anna"], filter_ai_usage=True,
                                     licence=lic)
    if isinstance(text, dict):
        text = text.get("content")
    assert "CHIDI SAYS NONE" not in text


def test_authors_export_carries_direct_reader_reply(app, circles):  # noqa: F811,E501
    from backend.routes.export_data import build_user_export_content
    from backend.utils.api_keys import PayloadLicence
    w = _world()
    direct = _node(w["ben"], w["entry"], "BEN DIRECT REPLY", privacy="circles",
                   ai_usage="train")
    _db.session.commit()
    lic = PayloadLicence("train")
    out = build_user_export_content(w["anna"], filter_ai_usage=True,
                                    licence=lic)
    text = out.get("content") if isinstance(out, dict) else out
    assert "BEN DIRECT REPLY" in text and lic.key_type == "chat"
    lic2 = PayloadLicence("train")
    out = build_user_export_content(w["anna"], filter_ai_usage=True,
                                    licence=lic2, max_tokens=10000,
                                    return_metadata=True,
                                    collapse_artifacts=True,
                                    chronological_order=False)
    text = out.get("content") if isinstance(out, dict) else out


def test_full_ask_prompt(app, circles):  # noqa: F811
    from backend.utils.llm_nodes import CIRCLE_ASK_MARKER, circle_ask_ai_usage
    w = _world()
    ben = w["ben"]
    llm_node = _placeholder(
        w["b"], ben, "gpt-5", "circles", circle_ask_ai_usage(ben),
        meta=[{"name": CIRCLE_ASK_MARKER, "entry_id": w["entry"].id}])
    _ScriptedProvider.reset([_resp("An answer.")])
    generate_llm_response(_FakeSelf(), w["b"].id, llm_node.id, "gpt-5",
                          ben.id)
    for m in _ScriptedProvider.calls[0]["messages"]:
        print("MSG", m["role"], repr(m["text"][:400]))
    a = _fresh(llm_node.id)
    assert a.audience_root_id == w["entry"].id
    assert "_circle_ask" in (a.tool_calls_meta or "")
    assert a.privacy_level == "circles" and a.ai_usage == "chat"


def test_ask_my_ai_runs_on_the_readers_provider(app, circles):  # noqa: F811
    """anna's thread is on Anthropic, ben's account on OpenAI: through
    create_llm_placeholder and the task, ben's answer is made on his
    model and sent to his provider, whatever model the caller passes."""
    from backend.utils.llm_nodes import (
        CIRCLE_ASK_MARKER, create_llm_placeholder, circle_ask_ai_usage)
    w = _world()
    ben = w["ben"]
    llm_node, _ = create_llm_placeholder(
        w["b"].id, "claude-x", ben.id, privacy_level="circles",
        ai_usage=circle_ask_ai_usage(ben), enqueue=False,
        meta=[{"name": CIRCLE_ASK_MARKER, "entry_id": w["entry"].id}])
    assert llm_node.llm_model == "gpt-5"
    _ScriptedProvider.reset([_resp("An answer.")])
    generate_llm_response(_FakeSelf(), w["b"].id, llm_node.id,
                          llm_node.llm_model, ben.id)
    call = _ScriptedProvider.calls[0]
    assert call["model_id"] == "gpt-5"
    assert set(call["api_keys"].values()) & {"ant-train", "ant-chat",
                                             "sk-train"} <= {"ant-chat"}
    assert call["api_keys"].get("openai") == "sk-chat"


def test_read_full_of_a_readers_reply_uses_the_chat_key(app, circles):  # noqa: F811,E501
    from backend.utils.api_keys import PayloadLicence
    w = _world()
    lic = PayloadLicence("train")
    lic.note_nodes([w["entry"].id])            # anna's own entry: licensed
    assert lic.key_type == "train"
    lic.note_nodes([w["b"].id], what="read entry")
    assert lic.key_type == "chat"

"""A Community Archive read inside an agentic thread (2026-09-16).

The read's context must not include the Voice / Text mode system prompt:
the model reads the tweets against the user's own sharing, not against
the agentic persona and its tools. The reply text carries everything the
page shows: the verdict, then each quote-tweet over the {quote_ext:ID}
of the tweet it quotes, which the finalize path records as a surfacing.

Runs the real task body through the test_retrieval_loop harness
(identity @celery.task, scripted provider, test flask_app), on the live
path (ca_live=True, the admin's rerun) so no batch round-trip is needed.
"""
import json
import sys
from unittest.mock import MagicMock

import pytest  # noqa: F401

from backend.tests.test_retrieval_loop import (  # noqa: F401 (fixture)
    app, _llm_task_mod, generate_llm_response, _FakeSelf,
    _ScriptedProvider, _mk_user, _resp, _fresh,
)
from backend.extensions import db as _db
from backend.models import (
    Node, UserPrompt, NodeContextArtifact, ExternalItem, FeedPick,
)


class _CapturingProvider(_ScriptedProvider):
    """The scripted provider plus the keyword arguments of each call
    (the live read passes the feed's JSON schema)."""
    kwargs = []

    @classmethod
    def get_completion(cls, model_id, messages, api_keys, tools=None,
                       prompt_cache_key=None, **kwargs):
        cls.kwargs.append(dict(kwargs))
        return super().get_completion(model_id, messages, api_keys,
                                      tools=tools,
                                      prompt_cache_key=prompt_cache_key)


def _prompt_node(user, prompt_key, parent_id=None, body=None):
    """A prompt node attached by reference, like the routes do."""
    if body is None:
        from backend.utils.prompts import get_user_prompt_record
        record = get_user_prompt_record(user.id, prompt_key)
    else:
        record = UserPrompt(user_id=user.id, prompt_key=prompt_key,
                            title=prompt_key, generated_by="default")
        record.set_content(body)
        _db.session.add(record)
        _db.session.flush()
    n = Node(user_id=user.id, human_owner_id=user.id, parent_id=parent_id,
             node_type="user", privacy_level="private", ai_usage="chat",
             prompt_key=prompt_key)
    _db.session.add(n)
    _db.session.flush()
    _db.session.add(NodeContextArtifact(
        node_id=n.id, artifact_type="prompt", artifact_id=record.id))
    _db.session.flush()
    return n


def _user_node(user, parent_id, text):
    n = Node(user_id=user.id, human_owner_id=user.id, parent_id=parent_id,
             node_type="user", privacy_level="private", ai_usage="chat")
    n.set_content(text)
    _db.session.add(n)
    _db.session.flush()
    return n


REFS = {
    1: {"username": "alice_w", "tweet_id": "111", "text": "first tweet",
        "posted_at": None},
    2: {"username": "bob_b", "tweet_id": "222", "text": "second tweet",
        "posted_at": None},
}
CORPUS = "# Community Archive\n[#1] first tweet\n[#2] second tweet\n"


def _stub_archive(monkeypatch, tmp_path):
    """No parquet in tests: the placeholder renders a two-tweet corpus."""
    from backend.utils import community_archive as ca
    monkeypatch.setattr(ca, "render_recent_tweets",
                        lambda *a, **k: (CORPUS, {"tweets": 2}, dict(REFS)))
    imports = sys.modules.get("backend.tasks.imports")
    if imports is None or isinstance(imports, MagicMock):
        imports = MagicMock()
        monkeypatch.setitem(sys.modules, "backend.tasks.imports", imports)
        imports.snapshot_dir_for = lambda config: tmp_path
    else:
        monkeypatch.setattr(imports, "snapshot_dir_for", lambda config: tmp_path)


def _feed_json(picks):
    return json.dumps({"verdict": "One thing today.", "picks": picks})


def _run(monkeypatch, tmp_path, chain_builder):
    _stub_archive(monkeypatch, tmp_path)
    _CapturingProvider.kwargs = []
    _CapturingProvider.reset([_resp(_feed_json([
        {"n": 2, "qt": "This meets your question about pacing.",
         "relevance": 40, "recommend": True},
    ]))])
    monkeypatch.setattr(_llm_task_mod, "LLMProvider", _CapturingProvider)
    alice, parent, llm_node = chain_builder()
    generate_llm_response(_FakeSelf(), parent.id, llm_node.id, "gpt-5",
                          alice.id, source_mode=None, ca_live=True)
    return alice, llm_node


def _voice_thread_with_read():
    """voice prompt -> user's sharing -> read_thread prompt -> reply."""
    alice = _mk_user("alice", approved=True, plan="alpha", is_admin=True)
    llm_user = _mk_user("gpt-5", twitter_id="llm-gpt-5")
    voice = _prompt_node(alice, "voice", body="AGENTIC PERSONA AND TOOLS")
    sharing = _user_node(alice, voice.id, "my morning: unsure about pacing")
    read = _prompt_node(alice, "read_thread", parent_id=sharing.id)
    llm_node = Node(user_id=llm_user.id, human_owner_id=alice.id,
                    parent_id=read.id, node_type="llm", llm_model="gpt-5",
                    llm_task_status="pending", privacy_level="private",
                    ai_usage="chat")
    llm_node.set_content("[LLM response generation pending...]")
    _db.session.add(llm_node)
    _db.session.commit()
    return alice, read, llm_node


def test_read_under_voice_thread_drops_the_agentic_prompt(app, monkeypatch, tmp_path):  # noqa: F811
    alice, llm_node = _run(monkeypatch, tmp_path, _voice_thread_with_read)

    assert len(_CapturingProvider.calls) == 1
    call = _CapturingProvider.calls[0]
    texts = [m["text"] for m in call["messages"]]
    joined = "\n".join(texts)
    assert "AGENTIC PERSONA AND TOOLS" not in joined
    assert "my morning: unsure about pacing" in joined
    assert "[#2] second tweet" in joined
    # Not an agentic call: no tools, and the live read asks for the shape.
    assert call["tools"] is None
    assert _CapturingProvider.kwargs[0]["output_schema"]["required"] == [
        "verdict", "picks"]


def test_reply_text_is_the_whole_feed(app, monkeypatch, tmp_path):  # noqa: F811
    alice, llm_node = _run(monkeypatch, tmp_path, _voice_thread_with_read)

    node = _fresh(llm_node.id)
    assert node.llm_task_status == "completed"
    item = ExternalItem.query.filter_by(
        user_id=alice.id, source="read_pick", external_id="222").one()
    assert item.get_content() == "second tweet"
    assert item.url == "https://x.com/bob_b/status/222"
    assert node.get_content() == (
        "One thing today.\n\n"
        "This meets your question about pacing.\n\n"
        f"{{quote_ext:{item.id}}}")
    picks = FeedPick.query.filter_by(node_id=node.id).all()
    assert [(p.rank, p.relevance, p.recommended, p.get_why()) for p in picks] == [
        (1, 40, True, "This meets your question about pacing.")]
    # Surfaced exactly once: by the quote in the reply, not a second time
    # by the pick row.
    assert item.surfaced_count == 1
    assert item.last_surfaced_at is not None


def test_read_root_thread_unchanged(app, monkeypatch, tmp_path):  # noqa: F811
    """No agentic prompt in the chain: nothing is dropped."""
    def build():
        alice = _mk_user("alice", approved=True, plan="alpha", is_admin=True)
        llm_user = _mk_user("gpt-5", twitter_id="llm-gpt-5")
        read = _prompt_node(alice, "read")
        llm_node = Node(user_id=llm_user.id, human_owner_id=alice.id,
                        parent_id=read.id, node_type="llm",
                        llm_model="gpt-5", llm_task_status="pending",
                        privacy_level="private", ai_usage="chat")
        llm_node.set_content("[LLM response generation pending...]")
        _db.session.add(llm_node)
        _db.session.commit()
        return alice, read, llm_node

    alice, llm_node = _run(monkeypatch, tmp_path, build)
    texts = [m["text"] for m in _CapturingProvider.calls[0]["messages"]]
    assert len(texts) == 1
    assert "Community Archive, last day" in texts[0]
    assert "[#1] first tweet" in texts[0]
    assert _fresh(llm_node.id).llm_task_status == "completed"

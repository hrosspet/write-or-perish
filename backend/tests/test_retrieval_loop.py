"""Tests for the within-turn tool loop in generate_llm_response (#158).

Both agentic modes (textmode + voice): when the model calls ANY tool, the
task executes it, injects the result back into `messages` as a plain user
message (full content for retrieval tools, a one-line status for action
tools), finalizes the calling node as an INTERIM step, creates a
CONTINUATION node, and re-calls the model so it answers WITH the results in
the SAME turn. Non-agentic callers keep their single-shot behavior.

Harness notes:
  - We stub backend.celery_app so @celery.task is an identity decorator
    (generate_llm_response stays a plain function) and flask_app is the test
    app. The task body is then called directly with a stub `self`.
  - We stub backend.llm_providers with a scripted LLMProvider.get_completion
    and a real PromptTooLongError class. The task is imported against these
    stubs, then sys.modules is restored (mirrors test_artifacts.py).
"""
import json
import os
import sys
from unittest.mock import MagicMock

# ── Environment ──────────────────────────────────────────────────────────
os.environ["ENCRYPTION_DISABLED"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("TWITTER_API_KEY", "fake")
os.environ.setdefault("TWITTER_API_SECRET", "fake")

sys.modules.setdefault("celery", MagicMock())
sys.modules.setdefault("celery.utils", MagicMock())
sys.modules.setdefault("celery.utils.log", MagicMock())
sys.modules.setdefault("celery.result", MagicMock())

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

from backend.extensions import db as _db  # noqa: E402
from backend.models import (  # noqa: E402
    User, Node, UserArtifact, UserPrompt, NodeContextArtifact, APICostLog,
    UserTodo, ExternalItem, ExternalItemEmbedding, NodeEmbedding,
)
from backend.utils.embeddings import pack_vector  # noqa: E402


# ── Scriptable LLM provider stub ─────────────────────────────────────────
class _ScriptedProvider:
    """LLMProvider stand-in returning a queued sequence of responses and
    recording the `messages` passed to each call."""
    responses = []
    calls = []

    @classmethod
    def reset(cls, responses):
        cls.responses = list(responses)
        cls.calls = []

    @classmethod
    def get_completion(cls, model_id, messages, api_keys, tools=None,
                       prompt_cache_key=None, **kwargs):
        # Deep-copy the message texts so later mutation of `messages` in the
        # task doesn't retroactively change what we captured.
        cls.calls.append({
            "model_id": model_id,
            "messages": [
                {
                    "role": m["role"],
                    "text": "".join(
                        b.get("text", "") for b in m["content"]
                    ) if isinstance(m["content"], list) else m["content"],
                }
                for m in messages
            ],
            "tools": tools,
            # The keys this call went out on (#325: the training key
            # never carries content the chain did not license).
            "api_keys": api_keys,
            # #348: the baseline response id sent for cache diagnostics.
            "cache_comparison_response_id": kwargs.get(
                "cache_comparison_response_id"),
        })
        nxt = cls.responses.pop(0)
        # A queued Exception is raised (e.g. to drive a PromptTooLong on the
        # continuation call); a dict is returned as a normal response.
        if isinstance(nxt, Exception):
            raise nxt
        # Like the real provider: whether a cache comparison went out (#348).
        nxt["cache_comparison_sent"] = bool(
            kwargs.get("cache_comparison_response_id"))
        return nxt


class _PromptTooLongError(Exception):
    def __init__(self, actual_tokens=0, max_tokens=0):
        super().__init__("prompt too long")
        self.actual_tokens = actual_tokens
        self.max_tokens = max_tokens


# ── Build the app + import the task against stubbed glue ─────────────────
def _make_app():
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["DEFAULT_LLM_MODEL"] = "gpt-5"
    app.config["SUPPORTED_MODELS"] = {
        "gpt-5": {"provider": "openai", "api_model": "gpt-5"},
    }
    app.config["OPENAI_API_KEY"] = "sk-test"
    app.config["ANTHROPIC_API_KEY"] = "sk-ant-test"
    # Agentic semantic_search is per-user opt-in (#208) under this env
    # killswitch (defaults on). _build_chain opts alice in; the opted-out
    # path has its own dedicated test.
    app.config["SEMANTIC_SEARCH_AGENTIC"] = True
    _db.init_app(app)
    return app


# Stub the celery glue (identity @celery.task, real test flask_app) and a
# scripted llm_providers, import the REAL task module against them, then
# restore sys.modules so sibling tests are undisturbed.
#
# IMPORTANT (collection-order robustness): sibling test modules churn
# sys.modules — test_textmode.py installs a MagicMock at
# "backend.tasks.llm_completion", others pop+reimport the real one. Whatever
# collection order pytest picks, we must end up with the REAL module object
# (its @celery.task decorated function is a plain callable, not a Mock). We
# force a clean reimport against our stubs and assert we got the real thing.
_app = _make_app()

_celery_stub = MagicMock()
_celery_stub.celery.task = lambda *a, **k: (lambda fn: fn)  # identity
_celery_stub.flask_app = _app

_providers_stub = MagicMock()
_providers_stub.LLMProvider = _ScriptedProvider
_providers_stub.PromptTooLongError = _PromptTooLongError


def _import_real_task_module():
    """Import backend.tasks.llm_completion as the REAL module against our
    stubbed celery/providers glue, restoring sibling sys.modules after."""
    import importlib
    glue = ("backend.celery_app", "backend.llm_providers",
            "backend.tasks.llm_completion")
    saved = {k: sys.modules.get(k) for k in glue}
    sys.modules["backend.celery_app"] = _celery_stub
    sys.modules["backend.llm_providers"] = _providers_stub
    # Drop any cached/mocked task module so it re-imports for real.
    sys.modules.pop("backend.tasks.llm_completion", None)
    try:
        mod = importlib.import_module("backend.tasks.llm_completion")
        # If a sibling's MagicMock got returned (it shadowed the import),
        # force a true reload from source.
        if isinstance(mod, MagicMock) or isinstance(
                getattr(mod, "generate_llm_response", None), MagicMock):
            sys.modules.pop("backend.tasks.llm_completion", None)
            mod = importlib.import_module("backend.tasks.llm_completion")
        return mod
    finally:
        for _k, _v in saved.items():
            if _v is None:
                sys.modules.pop(_k, None)
            else:
                sys.modules[_k] = _v


_llm_task_mod = _import_real_task_module()
assert not isinstance(_llm_task_mod, MagicMock), (
    "expected the real llm_completion module")
assert callable(getattr(_llm_task_mod, "generate_llm_response", None)) and \
    not isinstance(_llm_task_mod.generate_llm_response, MagicMock), (
    "expected the real generate_llm_response function")

generate_llm_response = _llm_task_mod.generate_llm_response


class _FakeSelf:
    """Stand-in for the bound Celery task `self` (update_state is a no-op)."""
    def update_state(self, *args, **kwargs):
        pass


@pytest.fixture
def app():
    # Bind our scripted provider + test flask_app onto the imported task
    # module's globals at RUNTIME. The module references LLMProvider /
    # PromptTooLongError / flask_app as module globals, so overriding them
    # here makes the task use our stubs regardless of the import-time
    # sys.modules state (which other test modules churn). Order-independent.
    saved = {
        "LLMProvider": _llm_task_mod.LLMProvider,
        "PromptTooLongError": _llm_task_mod.PromptTooLongError,
        "flask_app": _llm_task_mod.flask_app,
    }
    _llm_task_mod.LLMProvider = _ScriptedProvider
    _llm_task_mod.PromptTooLongError = _PromptTooLongError
    _llm_task_mod.flask_app = _app
    with _app.app_context():
        _db.create_all()
        yield _app
        _db.session.remove()
        _db.drop_all()
    _llm_task_mod.LLMProvider = saved["LLMProvider"]
    _llm_task_mod.PromptTooLongError = saved["PromptTooLongError"]
    _llm_task_mod.flask_app = saved["flask_app"]


# ── Helpers ──────────────────────────────────────────────────────────────
def _mk_user(username, **kwargs):
    u = User(username=username, **kwargs)
    _db.session.add(u)
    _db.session.flush()
    return u


def _mk_artifact(user_id, kind, content, title=None, ai_usage="chat"):
    a = UserArtifact(
        user_id=user_id, kind=kind, title=title or kind.title(),
        generated_by="test", ai_usage=ai_usage,
    )
    a.set_content(content)
    _db.session.add(a)
    _db.session.flush()
    return a


def _mk_todo(user_id, content, ai_usage="chat"):
    t = UserTodo(user_id=user_id, generated_by="test", ai_usage=ai_usage)
    t.set_content(content)
    _db.session.add(t)
    _db.session.flush()
    return t


def _build_chain(source_mode="textmode"):
    """Build an agentic conversation chain:
        system(prompt artifact) -> user message -> llm placeholder.

    Returns (alice, system_node, user_node, llm_node).
    """
    alice = _mk_user("alice", approved=True, plan="alpha",
                     external_content_enabled=True)
    llm_user = _mk_user("gpt-5", twitter_id="llm-gpt-5")

    # System node carrying a textmode/voice prompt artifact (enables agentic).
    prompt = UserPrompt(
        user_id=alice.id, prompt_key=source_mode, title="P",
        generated_by="default",
    )
    prompt.set_content("system prompt body")
    _db.session.add(prompt)
    _db.session.flush()

    system = Node(user_id=alice.id, human_owner_id=alice.id,
                  node_type="system", privacy_level="private",
                  ai_usage="chat")
    system.set_content("(system)")
    _db.session.add(system)
    _db.session.flush()
    _db.session.add(NodeContextArtifact(
        node_id=system.id, artifact_type="prompt", artifact_id=prompt.id))

    user_node = Node(user_id=alice.id, human_owner_id=alice.id,
                     parent_id=system.id, node_type="user",
                     privacy_level="private", ai_usage="chat")
    user_node.set_content("what's on my reading list?")
    _db.session.add(user_node)
    _db.session.flush()

    llm_node = Node(user_id=llm_user.id, human_owner_id=alice.id,
                    parent_id=user_node.id, node_type="llm",
                    llm_model="gpt-5", llm_task_status="pending",
                    privacy_level="private", ai_usage="chat")
    llm_node.set_content("[LLM response generation pending...]")
    _db.session.add(llm_node)
    _db.session.commit()
    return alice, system, user_node, llm_node


def _fresh(node_id):
    """Re-read a node from the DB. The task commits on a different
    app-context-scoped session, so the test session must drop its cached
    (stale) view before re-querying."""
    _db.session.expire_all()
    return Node.query.get(node_id)


def _resp(content, tool_calls=None, total=10, inp=5, out=5):
    return {
        "content": content,
        "tool_calls": tool_calls or [],
        "total_tokens": total,
        "input_tokens": inp,
        "output_tokens": out,
        "truncated": False,
    }


# ── Tests ──────────────────────────────────────────────────────────────


def test_textmode_retrieval_loop_injects_and_continues(app):
    alice, system, user_node, llm_node = _build_chain("textmode")
    _mk_artifact(alice.id, "reading-list",
                 "1. Gravity's Rainbow\n2. Dune", title="Reading List")

    _ScriptedProvider.reset([
        _resp("Let me pull that up.", tool_calls=[{
            "id": "t1", "name": "read_artifact",
            "input": {"kind": "reading-list"},
        }]),
        _resp("Based on your list, start with Dune."),
    ])

    result = generate_llm_response(
        _FakeSelf(), user_node.id, llm_node.id, "gpt-5", alice.id,
        source_mode="textmode",
    )

    # Two model calls were made (interim + continuation).
    assert len(_ScriptedProvider.calls) == 2

    # (a) interim node carries read_artifact meta + a continuation link.
    interim = _fresh(llm_node.id)
    assert interim.llm_task_status == "completed"
    assert interim.continuation_node_id is not None
    meta = json.loads(interim.tool_calls_meta)
    assert any(e.get("name") == "read_artifact" for e in meta)
    # No _mode marker on the interim node — that's the final answer's job.
    assert not any(e.get("name") == "_mode" for e in meta)

    # (b) the FINAL node holds the second response's content, and the result
    # dict points at it (not the original placeholder).
    final = Node.query.get(interim.continuation_node_id)
    assert final.get_content() == "Based on your list, start with Dune."
    assert final.llm_task_status == "completed"
    assert result["llm_node_id"] == final.id
    assert result["parent_node_id"] == user_node.id
    assert result["status"] == "completed"

    # (c) the SECOND call's messages contained the injected artifact content.
    second_texts = "\n".join(m["text"] for m in _ScriptedProvider.calls[1]["messages"])
    assert "[Contents of artifact 'reading-list'" in second_texts
    assert "Gravity's Rainbow" in second_texts

    # Two cost rows logged (one per model call).
    assert APICostLog.query.count() == 2

    # The retrieved artifact is pinned to the interim node.
    pins = NodeContextArtifact.query.filter_by(
        node_id=interim.id, artifact_type="user_artifact").all()
    assert len(pins) == 1


def test_textmode_retrieval_loop_read_todo(app):
    """read_todo rides the same within-turn loop: the model's first call
    requests the todo, the loop injects it, and the continuation answers
    with it. The exact todo version read is pinned to the interim node."""
    alice, system, user_node, llm_node = _build_chain("textmode")
    todo = _mk_todo(alice.id, "1. ship slice 3\n2. water the plants")
    _db.session.commit()

    _ScriptedProvider.reset([
        _resp("Let me pull up your todo.", tool_calls=[{
            "id": "t1", "name": "read_todo", "input": {},
        }]),
        _resp("Start with shipping slice 3."),
    ])

    result = generate_llm_response(
        _FakeSelf(), user_node.id, llm_node.id, "gpt-5", alice.id,
        source_mode="textmode",
    )

    # Interim + continuation calls.
    assert len(_ScriptedProvider.calls) == 2

    interim = _fresh(llm_node.id)
    assert interim.continuation_node_id is not None
    meta = json.loads(interim.tool_calls_meta)
    assert any(e.get("name") == "read_todo" for e in meta)

    final = Node.query.get(interim.continuation_node_id)
    assert final.get_content() == "Start with shipping slice 3."
    assert result["llm_node_id"] == final.id

    # The second call's messages carried the injected todo content.
    second_texts = "\n".join(
        m["text"] for m in _ScriptedProvider.calls[1]["messages"])
    assert "current todo list" in second_texts
    assert "ship slice 3" in second_texts

    # The exact todo version is pinned to the interim node (artifact_type
    # "todo", not "user_artifact") for a faithful export.
    pins = NodeContextArtifact.query.filter_by(
        node_id=interim.id, artifact_type="todo").all()
    assert len(pins) == 1
    assert pins[0].artifact_id == todo.id


def test_textmode_no_retrieval_single_node(app):
    """A textmode turn with no retrieval tool call produces one node."""
    alice, system, user_node, llm_node = _build_chain("textmode")

    _ScriptedProvider.reset([
        _resp("A direct answer, no lookup needed."),
    ])

    result = generate_llm_response(
        _FakeSelf(), user_node.id, llm_node.id, "gpt-5", alice.id,
        source_mode="textmode",
    )

    assert len(_ScriptedProvider.calls) == 1
    node = _fresh(llm_node.id)
    assert node.continuation_node_id is None
    assert node.get_content() == "A direct answer, no lookup needed."
    assert result["llm_node_id"] == llm_node.id
    # _mode marker present on the final answer (agentic + source_mode).
    meta = json.loads(node.tool_calls_meta)
    assert any(e.get("name") == "_mode"
               and e.get("source_mode") == "textmode" for e in meta)
    assert APICostLog.query.count() == 1


def test_textmode_retrieval_budget_caps_at_max_rounds(app):
    """The loop stops after MAX_RETRIEVAL_ROUNDS interim rounds and finalizes
    even if the model keeps requesting retrieval."""
    alice, system, user_node, llm_node = _build_chain("textmode")
    _mk_artifact(alice.id, "reading-list", "books here", title="Reading List")

    rt = {"id": "t", "name": "read_artifact", "input": {"kind": "reading-list"}}
    # Always asks for retrieval; loop must still terminate and finalize.
    _ScriptedProvider.reset([
        _resp(f"looking {i}", tool_calls=[rt]) for i in range(1, 8)
    ])

    result = generate_llm_response(
        _FakeSelf(), user_node.id, llm_node.id, "gpt-5", alice.id,
        source_mode="textmode",
    )

    # MAX_RETRIEVAL_ROUNDS (5) interim calls + 1 finalizing call = 6 calls.
    assert _llm_task_mod.MAX_RETRIEVAL_ROUNDS == 5
    assert len(_ScriptedProvider.calls) == 6
    # Walk the 5 interim continuations down to the final node.
    node = _fresh(llm_node.id)
    for _ in range(5):
        assert node.continuation_node_id is not None
        node = Node.query.get(node.continuation_node_id)
    final = node
    assert final.continuation_node_id is None
    # Final node finalized with the 6th response's content (budget exhausted,
    # so its retrieval tool call is executed as a normal final tool call).
    assert final.get_content() == "looking 6"
    assert result["llm_node_id"] == final.id
    assert APICostLog.query.count() == 6


def test_textmode_action_tool_continues(app):
    """Action tools (update_artifact) ride the loop too: the model gets a
    result round and the turn ends on its first no-tool message — instead of
    a one-line preamble ("noting this in memory") being the whole answer."""
    alice, system, user_node, llm_node = _build_chain("textmode")

    _ScriptedProvider.reset([
        _resp("Noting this milestone in memory.", tool_calls=[{
            "id": "t1", "name": "update_artifact",
            "input": {"kind": "memory", "updated_content": "milestone!"},
        }]),
        _resp("Here's the fuller response you deserve."),
    ])

    result = generate_llm_response(
        _FakeSelf(), user_node.id, llm_node.id, "gpt-5", alice.id,
        source_mode="textmode",
    )

    # Interim + continuation calls.
    assert len(_ScriptedProvider.calls) == 2

    interim = _fresh(llm_node.id)
    assert interim.llm_task_status == "completed"
    assert interim.get_content() == "Noting this milestone in memory."
    assert interim.continuation_node_id is not None
    meta = json.loads(interim.tool_calls_meta)
    entry = next(e for e in meta if e.get("name") == "update_artifact")
    assert entry["status"] == "success"
    # Reported same-turn — the cross-turn scan must not re-inject it.
    assert entry["status_reported"] is True

    # The artifact write actually happened.
    art = UserArtifact.latest_for(alice.id, "memory")
    assert art is not None
    assert art.get_content() == "milestone!"

    final = Node.query.get(interim.continuation_node_id)
    assert final.get_content() == "Here's the fuller response you deserve."
    assert final.llm_task_status == "completed"
    assert result["llm_node_id"] == final.id

    # The result round told the model the update landed.
    second_texts = "\n".join(
        m["text"] for m in _ScriptedProvider.calls[1]["messages"])
    assert "[update_artifact: artifact 'memory' created.]" in second_texts

    # NEXT turn: the outcome is a durable part of the rendered history, not
    # just that turn's in-flight injection — asked "did that save?", the
    # model can answer from the record instead of "I got no feedback".
    user2 = Node(user_id=alice.id, human_owner_id=alice.id,
                 parent_id=final.id, node_type="user",
                 privacy_level="private", ai_usage="chat")
    user2.set_content("did that save?")
    _db.session.add(user2)
    _db.session.flush()
    llm2 = Node(user_id=interim.user_id, human_owner_id=alice.id,
                parent_id=user2.id, node_type="llm", llm_model="gpt-5",
                llm_task_status="pending", privacy_level="private",
                ai_usage="chat")
    llm2.set_content("[LLM response generation pending...]")
    _db.session.add(llm2)
    _db.session.commit()

    _ScriptedProvider.reset([_resp("Yes, it saved.")])
    generate_llm_response(
        _FakeSelf(), user2.id, llm2.id, "gpt-5", alice.id,
        source_mode="textmode",
    )
    assert len(_ScriptedProvider.calls) == 1
    history = _ScriptedProvider.calls[0]["messages"]
    interim_msgs = [m for m in history
                    if m["role"] == "assistant"
                    and "Noting this milestone" in m["text"]]
    assert len(interim_msgs) == 1
    assert ("[update_artifact: artifact 'memory' created.]"
            in interim_msgs[0]["text"])
    # Already reported same-turn: the cross-turn scan adds no second note.
    assert sum("artifact 'memory'" in m["text"].lower()
               or "Artifact 'memory'" in m["text"] for m in history) == 1


def test_continuation_terminal_failure_fails_continuation_node(
        app, monkeypatch):
    """A terminal provider failure on the continuation call fails the
    CONTINUATION node. The completed interim keeps its status and content,
    and nothing is stranded at 'processing' (the pre-fix behavior failed
    the interim and left the continuation pending forever)."""
    alice, system, user_node, llm_node = _build_chain("textmode")
    _mk_artifact(alice.id, "reading-list", "books", title="Reading List")
    monkeypatch.setattr(_llm_task_mod, "CONTINUATION_RETRY_DELAYS", ())

    _ScriptedProvider.reset([
        _resp("Let me pull that up.", tool_calls=[{
            "id": "t1", "name": "read_artifact",
            "input": {"kind": "reading-list"},
        }]),
        RuntimeError("provider overloaded"),
    ])

    with pytest.raises(RuntimeError):
        generate_llm_response(
            _FakeSelf(), user_node.id, llm_node.id, "gpt-5", alice.id,
            source_mode="textmode",
        )

    interim = _fresh(llm_node.id)
    assert interim.llm_task_status == "completed"
    assert interim.get_content() == "Let me pull that up."
    assert interim.continuation_node_id is not None
    cont = Node.query.get(interim.continuation_node_id)
    assert cont.llm_task_status == "failed"
    assert "provider overloaded" in (cont.llm_task_error or "")


def test_continuation_transient_failure_retries(app, monkeypatch):
    """A transient provider failure on the continuation call is retried
    (per CONTINUATION_RETRY_DELAYS) and the turn completes normally."""
    alice, system, user_node, llm_node = _build_chain("textmode")
    _mk_artifact(alice.id, "reading-list", "books", title="Reading List")
    monkeypatch.setattr(_llm_task_mod, "CONTINUATION_RETRY_DELAYS", (0,))

    _ScriptedProvider.reset([
        _resp("Let me pull that up.", tool_calls=[{
            "id": "t1", "name": "read_artifact",
            "input": {"kind": "reading-list"},
        }]),
        RuntimeError("provider overloaded"),
        _resp("Here's your answer."),
    ])

    result = generate_llm_response(
        _FakeSelf(), user_node.id, llm_node.id, "gpt-5", alice.id,
        source_mode="textmode",
    )

    assert result["status"] == "completed"
    interim = _fresh(llm_node.id)
    assert interim.llm_task_status == "completed"
    final = Node.query.get(interim.continuation_node_id)
    assert final.llm_task_status == "completed"
    assert final.get_content() == "Here's your answer."
    assert result["llm_node_id"] == final.id


def test_read_full_by_entry_id_resolves_and_continues(app):
    """read_full with a numeric entry id (query intent, explicit): the
    full node content is injected and the model answers with it in the
    continuation. A bare {quote:ID} in the reply triggers NOTHING — quote
    markers are presentation only."""
    alice, system, user_node, llm_node = _build_chain("textmode")
    # An archive node that is NOT part of the conversation chain.
    archive = Node(user_id=alice.id, human_owner_id=alice.id,
                   node_type="text", privacy_level="private", ai_usage="chat")
    archive.set_content("THE FULL ARCHIVE ENTRY about leaving my job.")
    _db.session.add(archive)
    _db.session.commit()
    aid = archive.id

    _ScriptedProvider.reset([
        _resp("Let me read that entry fully.",
              tool_calls=[{"id": "t1", "name": "read_full",
                           "input": {"ref": str(aid)}}]),
        _resp("Based on that entry, here's my read. {quote:%d}" % aid),
    ])

    result = generate_llm_response(
        _FakeSelf(), user_node.id, llm_node.id, "gpt-5", alice.id,
        source_mode="textmode",
    )

    # read_full (interim) + final answer = 2 calls; the quote marker in
    # the final answer does NOT cause a third.
    assert len(_ScriptedProvider.calls) == 2
    interim = _fresh(llm_node.id)
    assert interim.continuation_node_id is not None
    # The full node content was injected into the continuation call.
    cont_msgs = "\n".join(
        m["text"] for m in _ScriptedProvider.calls[1]["messages"])
    assert "THE FULL ARCHIVE ENTRY" in cont_msgs
    final = Node.query.get(interim.continuation_node_id)
    assert final.continuation_node_id is None
    assert ("{quote:%d}" % aid) in final.get_content()
    assert result["llm_node_id"] == final.id


def test_read_full_by_label_reads_external_reference(app, monkeypatch):
    """read_full with a search-result label resolves through the turn's
    label map — including external references."""
    import backend.utils.embeddings as emb_mod
    monkeypatch.setattr(
        emb_mod, "embed_texts", lambda texts, key, **kw: [[1.0, 0.0]])

    alice, system, user_node, llm_node = _build_chain("textmode")
    item = _mk_external_item(
        alice.id, "LONG SAVED NOTE-TWEET " + "x" * 900, [1.0, 0.0])
    _db.session.commit()
    item_id = item.id

    _ScriptedProvider.reset([
        _resp("Searching.",
              tool_calls=[{"id": "t1", "name": "semantic_search",
                           "input": {"query": "note"}}]),
        _resp("That preview is truncated — reading it fully.",
              tool_calls=[{"id": "t2", "name": "read_full",
                           "input": {"ref": "A"}}]),
        _resp("Here it is: {quote:A} — and why it matters."),
    ])

    generate_llm_response(
        _FakeSelf(), user_node.id, llm_node.id, "gpt-5", alice.id,
        source_mode="textmode",
    )

    assert len(_ScriptedProvider.calls) == 3
    # The search preview was truncated at 800 and marked.
    round2 = "\n".join(
        m["text"] for m in _ScriptedProvider.calls[1]["messages"])
    assert "(preview truncated)" in round2
    # read_full injected the FULL reference text.
    round3 = "\n".join(
        m["text"] for m in _ScriptedProvider.calls[2]["messages"])
    assert "LONG SAVED NOTE-TWEET " + "x" * 900 in round3
    # Final node carries the canonical presentation marker.
    _db.session.expire_all()
    nodes = Node.query.filter(Node.human_owner_id == alice.id).all()
    all_text = "\n".join(n.get_content() or "" for n in nodes)
    assert ("{quote_ext:%d}" % item_id) in all_text


def test_read_full_unknown_ref_errors_cleanly(app):
    alice, system, user_node, llm_node = _build_chain("textmode")
    _ScriptedProvider.reset([
        _resp("Reading.",
              tool_calls=[{"id": "t1", "name": "read_full",
                           "input": {"ref": "Z"}}]),
        _resp("Never mind, answering directly."),
    ])
    generate_llm_response(
        _FakeSelf(), user_node.id, llm_node.id, "gpt-5", alice.id,
        source_mode="textmode",
    )
    assert len(_ScriptedProvider.calls) == 2
    cont = "\n".join(
        m["text"] for m in _ScriptedProvider.calls[1]["messages"])
    assert "Unknown reference" in cont


def test_read_full_is_depth_1_no_recursive_expansion(app):
    """read_full resolves the read node's OWN content only; a {quote:ID}
    nested INSIDE it stays as a placeholder rather than being recursively
    inlined. This is the guard against the combinatorial blowup that
    produced a 2.77M-token continuation prompt — depth-3 resolution
    inlined cross-quoted nodes once per path."""
    alice, system, user_node, llm_node = _build_chain("textmode")
    nested = Node(user_id=alice.id, human_owner_id=alice.id,
                  node_type="text", privacy_level="private", ai_usage="chat")
    nested.set_content("NESTED SECRET about my childhood.")
    _db.session.add(nested)
    _db.session.flush()
    outer = Node(user_id=alice.id, human_owner_id=alice.id,
                 node_type="text", privacy_level="private", ai_usage="chat")
    outer.set_content("OUTER ENTRY about leaving my job. {quote:%d}" % nested.id)
    _db.session.add(outer)
    _db.session.commit()
    outer_id, nested_id = outer.id, nested.id

    _ScriptedProvider.reset([
        _resp("Let me read that fully.",
              tool_calls=[{"id": "t1", "name": "read_full",
                           "input": {"ref": str(outer_id)}}]),
        _resp("Here's my read."),
    ])

    generate_llm_response(
        _FakeSelf(), user_node.id, llm_node.id, "gpt-5", alice.id,
        source_mode="textmode",
    )

    assert len(_ScriptedProvider.calls) == 2
    cont_msgs = "\n".join(
        m["text"] for m in _ScriptedProvider.calls[1]["messages"])
    # The pulled node's own content is injected...
    assert "OUTER ENTRY" in cont_msgs
    # ...with the nested reference left as a placeholder (model can pull it
    # next round)...
    assert ("{quote:%d}" % nested_id) in cont_msgs
    # ...but the nested node's content is NOT recursively expanded.
    assert "NESTED SECRET" not in cont_msgs


def test_continuation_prompt_too_long_degrades_gracefully(app):
    """If the continuation call overflows the context window, the loop drops
    that round's injection and answers with what it has — rather than failing
    the whole turn with a raw multi-million-token PromptTooLong (the bug)."""
    alice, system, user_node, llm_node = _build_chain("textmode")
    archive = Node(user_id=alice.id, human_owner_id=alice.id,
                   node_type="text", privacy_level="private", ai_usage="chat")
    archive.set_content("THE FULL ARCHIVE ENTRY about leaving my job.")
    _db.session.add(archive)
    _db.session.commit()
    aid = archive.id

    _ScriptedProvider.reset([
        _resp("Let me read that fully.",
              tool_calls=[{"id": "t1", "name": "read_full",
                           "input": {"ref": str(aid)}}]),
        _PromptTooLongError(2769518, 1000000),  # continuation overflows
        _resp("Here's a partial read."),         # retry after dropping inject
    ])

    result = generate_llm_response(
        _FakeSelf(), user_node.id, llm_node.id, "gpt-5", alice.id,
        source_mode="textmode",
    )

    # interim + overflowing continuation + retry continuation = 3 calls.
    assert len(_ScriptedProvider.calls) == 3
    interim = _fresh(llm_node.id)
    final = Node.query.get(interim.continuation_node_id)
    # The turn completed (did NOT fail) with the retry's answer.
    assert final.llm_task_status == "completed"
    assert final.get_content() == "Here's a partial read."
    assert result["status"] == "completed"
    assert result["llm_node_id"] == final.id
    # The retry dropped the oversized injection and swapped in the fallback
    # note; the archive content is no longer in the final call.
    retry_msgs = "\n".join(
        m["text"] for m in _ScriptedProvider.calls[2]["messages"])
    assert "too large to fit in context" in retry_msgs
    assert "THE FULL ARCHIVE ENTRY" not in retry_msgs


def test_archive_search_on_without_references_opt_in(app, monkeypatch):
    """Own-archive search is on for everyone (#329): with the "External
    references" toggle off (the default), semantic_search and read_full are
    still in the tool list, a search returns the user's own entries — and
    NOT their saved references, even when one matches — so the model never
    sees an external label it could not follow. (Manual Cmd+K search is a
    separate endpoint, also unaffected.)"""
    import backend.utils.embeddings as emb_mod
    monkeypatch.setattr(
        emb_mod, "embed_texts", lambda texts, key, **kw: [[1.0, 0.0]])

    alice, system, user_node, llm_node = _build_chain("textmode")
    alice.external_content_enabled = False  # default for every user
    archive = Node(user_id=alice.id, human_owner_id=alice.id,
                   node_type="text", privacy_level="private", ai_usage="chat")
    archive.set_content("AN OUT-OF-CHAIN ENTRY about zen.")
    _db.session.add(archive)
    _db.session.flush()
    _db.session.add(NodeEmbedding(
        node_id=archive.id, user_id=alice.id, model="test",
        content_hash="h", vector=pack_vector([1.0, 0.0])))
    # A saved reference that would rank first if references were on.
    item = _mk_external_item(
        alice.id, "the perfect saved tweet about zen", [1.0, 0.0])
    _db.session.commit()
    aid, item_id = archive.id, item.id

    _ScriptedProvider.reset([
        _resp("Checking your archive.",
              tool_calls=[{"id": "t1", "name": "semantic_search",
                           "input": {"query": "zen"}}]),
        _resp("You wrote about this before: {quote:A}."),
    ])
    generate_llm_response(
        _FakeSelf(), user_node.id, llm_node.id, "gpt-5", alice.id,
        source_mode="textmode",
    )

    # The search tools are exposed regardless of the toggle.
    tool_names = [t["name"] for t in (_ScriptedProvider.calls[0]["tools"] or [])]
    assert "semantic_search" in tool_names
    assert "read_full" in tool_names
    assert "read_artifact" in tool_names
    # Round 2 saw the archive entry as [A] and no reference at all.
    assert len(_ScriptedProvider.calls) == 2
    round2 = "\n".join(
        m["text"] for m in _ScriptedProvider.calls[1]["messages"])
    assert "[A]" in round2 and "AN OUT-OF-CHAIN ENTRY" in round2
    assert "[B]" not in round2
    assert "saved reference by" not in round2
    # The quote canonicalized to the archive node; the reference was never
    # surfaced.
    _db.session.expire_all()
    nodes = Node.query.filter(Node.human_owner_id == alice.id).all()
    all_text = "\n".join(n.get_content() or "" for n in nodes)
    assert ("{quote:%d}" % aid) in all_text
    assert "{quote_ext:" not in all_text
    assert ExternalItem.query.get(item_id).surfaced_count == 0


def test_read_full_external_refused_without_references_opt_in(app):
    """Defense in depth (#329): a search never hands an external label to a
    user without the toggle, so read_full on one (stale or forged) is
    refused instead of leaking the reference into the prompt."""
    alice, system, user_node, llm_node = _build_chain("textmode")
    alice.external_content_enabled = False
    item = _mk_external_item(alice.id, "a saved tweet", [1.0, 0.0])
    _db.session.commit()
    chain = [system, user_node, llm_node]

    results = _llm_task_mod._execute_tool_calls(
        [{"id": "t1", "name": "read_full", "input": {"ref": "A"}}],
        llm_node, chain, alice.id, quote_labels={"A": ("external", item.id)})
    assert results[0]["status"] == "error"
    assert "not enabled" in results[0]["error"]

    # Numeric archive-entry ids stay readable for everyone.
    results = _llm_task_mod._execute_tool_calls(
        [{"id": "t2", "name": "read_full",
          "input": {"ref": str(user_node.id)}}],
        llm_node, chain, alice.id, quote_labels={})
    assert results[0]["status"] == "success"
    assert results[0]["kind"] == "node"

    # With the toggle on, the same label resolves.
    alice.external_content_enabled = True
    _db.session.commit()
    results = _llm_task_mod._execute_tool_calls(
        [{"id": "t3", "name": "read_full", "input": {"ref": "A"}}],
        llm_node, chain, alice.id, quote_labels={"A": ("external", item.id)})
    assert results[0]["status"] == "success"
    assert results[0]["kind"] == "external"


def test_external_guidance_splits_on_references_toggle(app):
    """{external_content_guidance} renders the archive-search paragraph for
    everyone and appends the saved-references paragraph only when the
    owner's toggle is on (#329) — identically in render_system_message
    (pre-warm) and the generation loop's substitution."""
    alice, system, user_node, llm_node = _build_chain("textmode")
    # The system node renders its attached prompt artifact's text.
    prompt = UserPrompt.query.filter_by(user_id=alice.id).one()
    prompt.set_content("intro\n{external_content_guidance}\nend")
    alice.external_content_enabled = False
    _db.session.commit()

    off = _llm_task_mod.render_system_message(system, alice.id)
    assert "## Archive search" in off
    assert "saved external references" not in off
    assert "{external_content_guidance}" not in off

    alice.external_content_enabled = True
    _db.session.commit()
    on = _llm_task_mod.render_system_message(system, alice.id)
    assert "## Archive search" in on
    assert "saved external references" in on
    assert on.startswith(off.split("## Archive search")[0])

    # The generation loop substitutes the same bytes: the system message
    # of the first call equals the pre-warm render.
    _ScriptedProvider.reset([_resp("hi")])
    generate_llm_response(
        _FakeSelf(), user_node.id, llm_node.id, "gpt-5", alice.id,
        source_mode="textmode",
    )
    sys_msg = _ScriptedProvider.calls[0]["messages"][0]["text"]
    assert sys_msg == on

    # The env killswitch empties the section for everyone.
    app.config["SEMANTIC_SEARCH_AGENTIC"] = False
    try:
        killed = _llm_task_mod.render_system_message(system, alice.id)
        assert "Archive search" not in killed
        assert "{external_content_guidance}" not in killed
    finally:
        app.config["SEMANTIC_SEARCH_AGENTIC"] = True


def test_voice_mode_runs_loop(app):
    """Voice runs the SAME within-turn loop as text mode (Slice 4): interim
    node + continuation, content injected and answered same turn. The _mode
    marker lands on the final node, not the interim."""
    alice, system, user_node, llm_node = _build_chain("voice")
    _mk_artifact(alice.id, "reading-list", "Dune; Gravity's Rainbow",
                 title="Reading List")

    _ScriptedProvider.reset([
        _resp("Let me pull that up.", tool_calls=[{
            "id": "t1", "name": "read_artifact",
            "input": {"kind": "reading-list"},
        }]),
        _resp("Start with Dune."),
    ])

    result = generate_llm_response(
        _FakeSelf(), user_node.id, llm_node.id, "gpt-5", alice.id,
        source_mode="voice",
    )

    # Two model calls — the loop ran for voice just like text mode.
    assert len(_ScriptedProvider.calls) == 2
    interim = _fresh(llm_node.id)
    assert interim.continuation_node_id is not None
    final = Node.query.get(interim.continuation_node_id)
    assert final.get_content() == "Start with Dune."
    assert result["llm_node_id"] == final.id
    # The continuation call carried the injected artifact content.
    second = "\n".join(m["text"] for m in _ScriptedProvider.calls[1]["messages"])
    assert "[Contents of artifact 'reading-list'" in second
    # _mode marker (voice) lands on the FINAL node, not the interim.
    assert any(e.get("name") == "_mode" and e.get("source_mode") == "voice"
               for e in json.loads(final.tool_calls_meta))
    assert not any(e.get("name") == "_mode"
                   for e in json.loads(interim.tool_calls_meta))


def test_non_agentic_mode_single_shot(app):
    """Only the agentic modes (textmode/voice) run the loop. A non-agentic
    caller (source_mode=None) stays single-shot — one node, no continuation,
    even if the model emits a retrieval tool call (delivered cross-turn)."""
    alice, system, user_node, llm_node = _build_chain("voice")
    _mk_artifact(alice.id, "reading-list", "secret books", title="Reading List")

    _ScriptedProvider.reset([
        _resp("Pulling it up, talk next turn.", tool_calls=[{
            "id": "t1", "name": "read_artifact",
            "input": {"kind": "reading-list"},
        }]),
    ])

    result = generate_llm_response(
        _FakeSelf(), user_node.id, llm_node.id, "gpt-5", alice.id,
        source_mode=None,
    )

    # Exactly one model call — no within-turn re-call.
    assert len(_ScriptedProvider.calls) == 1
    node = _fresh(llm_node.id)
    assert node.continuation_node_id is None
    assert result["llm_node_id"] == llm_node.id
    meta = json.loads(node.tool_calls_meta)
    assert any(e.get("name") == "read_artifact" for e in meta)
    only_call = "\n".join(m["text"] for m in _ScriptedProvider.calls[0]["messages"])
    assert "[Contents of artifact 'reading-list'" not in only_call
    assert APICostLog.query.count() == 1


def test_user_export_deduped_to_first_occurrence(app, monkeypatch):
    """#139: the heavy {user_export} archive is injected in FULL only on its
    FIRST occurrence across the whole prompt — repeats (same message OR a
    later one) become a '(see archive above)' stub, so the ~10k archive isn't
    duplicated. The dedup flag persists across messages (per-conversation)."""
    MARKER = "<<WAVE6_ARCHIVE_MARKER>>"
    # Resolve {user_export} to a known marker so we can count occurrences.
    monkeypatch.setattr(_llm_task_mod, "build_user_export_content",
                        lambda *a, **k: MARKER)

    # Pro: an uncapped {user_export} is refused for other plans (see
    # check_user_export_plan); this test is about dedup, not the gate.
    alice = _mk_user("alice", approved=True, plan="pro",
                     external_content_enabled=True)
    llm_user = _mk_user("gpt-5", twitter_id="llm-gpt-5")
    prompt = UserPrompt(user_id=alice.id, prompt_key="textmode", title="P",
                        generated_by="default")
    prompt.set_content("system prompt body")
    _db.session.add(prompt)
    _db.session.flush()
    system = Node(user_id=alice.id, human_owner_id=alice.id,
                  node_type="system", privacy_level="private", ai_usage="chat")
    system.set_content("(system)")
    _db.session.add(system)
    _db.session.flush()
    _db.session.add(NodeContextArtifact(
        node_id=system.id, artifact_type="prompt", artifact_id=prompt.id))

    def _user(parent_id, content):
        n = Node(user_id=alice.id, human_owner_id=alice.id, parent_id=parent_id,
                 node_type="user", privacy_level="private", ai_usage="chat")
        n.set_content(content)
        _db.session.add(n)
        _db.session.flush()
        return n

    # First user turn references the archive TWICE (within-message dedup)...
    user1 = _user(system.id, "first {user_export} and again {user_export}")
    llm1 = Node(user_id=llm_user.id, human_owner_id=alice.id,
                parent_id=user1.id, node_type="llm", llm_model="gpt-5",
                llm_task_status="completed", privacy_level="private",
                ai_usage="chat")
    llm1.set_content("prior answer")
    _db.session.add(llm1)
    _db.session.flush()
    # ...and a LATER turn references it once more (cross-message dedup).
    user2 = _user(llm1.id, "later: {user_export}")
    placeholder = Node(user_id=llm_user.id, human_owner_id=alice.id,
                       parent_id=user2.id, node_type="llm", llm_model="gpt-5",
                       llm_task_status="pending", privacy_level="private",
                       ai_usage="chat")
    placeholder.set_content("[pending]")
    _db.session.add(placeholder)
    _db.session.commit()

    _ScriptedProvider.reset([_resp("final answer")])
    generate_llm_response(_FakeSelf(), user2.id, placeholder.id, "gpt-5",
                          alice.id, source_mode="textmode")

    text = "\n".join(
        m["text"] for m in _ScriptedProvider.calls[0]["messages"])
    # The archive is injected exactly ONCE across the whole prompt...
    assert text.count(MARKER) == 1
    # ...and the other three occurrences (1 in user1, 1 in user2) are stubs.
    assert text.count("(see archive above)") == 2


# ── Quote-as-response: labels, canonicalization, external references ────


def _mk_external_item(user_id, content, vector, author="visa", source="twitter_bookmark"):
    item = ExternalItem(
        user_id=user_id, source=source, external_id=f"x{content[:8]}",
        author_handle=author, url="https://twitter.com/i/status/1",
    )
    item.set_content(content)
    _db.session.add(item)
    _db.session.flush()
    _db.session.add(ExternalItemEmbedding(
        item_id=item.id, user_id=user_id, model="test",
        content_hash="h", vector=pack_vector(vector),
    ))
    _db.session.flush()
    return item


def test_search_labels_canonicalize_and_bump_surfaced(app, monkeypatch):
    """The full quote-as-response round-trip: semantic_search returns node
    + external matches labeled [A]/[B]; the model quotes {quote:B}; the
    stored content carries the canonical {quote_ext:<id>}; the reference's
    surfacing history is bumped; the full reference text is injected into
    the continuation call."""
    import backend.utils.embeddings as emb_mod
    monkeypatch.setattr(
        emb_mod, "embed_texts", lambda texts, key, **kw: [[1.0, 0.0]])

    alice, system, user_node, llm_node = _build_chain("textmode")
    # Archive node (out of chain) with an embedding -> match [A].
    archive = Node(user_id=alice.id, human_owner_id=alice.id,
                   node_type="text", privacy_level="private",
                   ai_usage="chat")
    archive.set_content("my old zen writing")
    _db.session.add(archive)
    _db.session.flush()
    _db.session.add(NodeEmbedding(
        node_id=archive.id, user_id=alice.id, model="test",
        content_hash="h", vector=pack_vector([1.0, 0.0])))
    # Saved reference with an embedding -> match [B].
    item = _mk_external_item(
        alice.id, "the perfect saved tweet about zen", [0.9, 0.1])
    _db.session.commit()
    item_id = item.id

    _ScriptedProvider.reset([
        _resp("Checking your archive.",
              tool_calls=[{"id": "t1", "name": "semantic_search",
                           "input": {"query": "zen"}}]),
        _resp("Someone you saved said it better: {quote:B} — and here's "
              "why it matters right now."),
    ])

    result = generate_llm_response(
        _FakeSelf(), user_node.id, llm_node.id, "gpt-5", alice.id,
        source_mode="textmode",
    )

    # Search round + the quoting reply. Quoting a REFERENCE does NOT
    # trigger a pull round (one-step quote-as-response; a third call here
    # produced near-duplicate interim/final nodes on staging).
    assert len(_ScriptedProvider.calls) == 2
    # Round 2 saw labeled previews with surfacing metadata semantics.
    round2 = "\n".join(
        m["text"] for m in _ScriptedProvider.calls[1]["messages"])
    assert "[A]" in round2 and "[B]" in round2
    assert "saved reference by @visa" in round2
    # Unread reference: no read mark in the preview.
    assert "marked read by the user" not in round2
    # The FINAL node carries the canonical marker; no continuation node.
    interim = _fresh(llm_node.id)
    final = (Node.query.get(interim.continuation_node_id)
             if interim.continuation_node_id else interim)
    assert final.continuation_node_id is None
    all_text = "\n".join(
        n.get_content() or "" for n in [interim, final])
    assert ("{quote_ext:%d}" % item_id) in all_text
    assert "{quote:B}" not in all_text
    # Surfacing history bumped exactly once.
    _db.session.expire_all()
    fresh_item = ExternalItem.query.get(item_id)
    assert fresh_item.surfaced_count == 1
    assert fresh_item.last_surfaced_at is not None
    assert result["status"] == "completed"


def test_search_preview_shows_users_read_mark(app, monkeypatch):
    """A reference the user marked read (ExternalItem.read_at — set only
    by the user's own hand, never by surfacing) carries that mark in the
    search preview the model sees, dated."""
    from datetime import datetime
    import backend.utils.embeddings as emb_mod
    monkeypatch.setattr(
        emb_mod, "embed_texts", lambda texts, key, **kw: [[1.0, 0.0]])

    alice, system, user_node, llm_node = _build_chain("textmode")
    item = _mk_external_item(
        alice.id, "the perfect saved tweet about zen", [0.9, 0.1])
    item.read_at = datetime(2026, 9, 10, 8, 0, 0)
    item.feedback = "good"
    _db.session.commit()

    _ScriptedProvider.reset([
        _resp("Checking your archive.",
              tool_calls=[{"id": "t1", "name": "semantic_search",
                           "input": {"query": "zen"}}]),
        _resp("You already read that one — building on it."),
    ])

    result = generate_llm_response(
        _FakeSelf(), user_node.id, llm_node.id, "gpt-5", alice.id,
        source_mode="textmode",
    )

    assert result["status"] == "completed"
    assert len(_ScriptedProvider.calls) == 2
    round2 = "\n".join(
        m["text"] for m in _ScriptedProvider.calls[1]["messages"])
    assert "saved reference by @visa" in round2
    assert "marked read by the user (2026-09-10)" in round2
    assert "the user rated this a good quote" in round2


def test_continued_thread_sees_read_mark_and_verdict_on_earlier_quote(app):
    """After the assistant quoted a reference, the user marked it read and
    rated it. The assistant turn keeps its raw {quote_ext:ID} marker, so
    the next call carries the user's marks as a note in the synthetic
    final user message; an unmarked quote adds no note."""
    from datetime import datetime
    alice, system, user_node, llm_node = _build_chain("textmode")
    item = _mk_external_item(alice.id, "a tweet the model picked", [0.9, 0.1])
    other = _mk_external_item(alice.id, "another pick, untouched", [0.1, 0.9])
    # Turn the placeholder into the completed quoting reply, then continue.
    llm_node.set_content(
        "I'd pick this one. {quote_ext:%d} And this. {quote_ext:%d}"
        % (item.id, other.id))
    llm_node.llm_task_status = "completed"
    user2 = Node(user_id=alice.id, human_owner_id=alice.id,
                 parent_id=llm_node.id, node_type="user",
                 privacy_level="private", ai_usage="chat")
    user2.set_content("why that one?")
    _db.session.add(user2)
    _db.session.flush()
    llm2 = Node(user_id=llm_node.user_id, human_owner_id=alice.id,
                parent_id=user2.id, node_type="llm", llm_model="gpt-5",
                llm_task_status="pending", privacy_level="private",
                ai_usage="chat")
    llm2.set_content("[LLM response generation pending...]")
    _db.session.add(llm2)
    _db.session.commit()

    _ScriptedProvider.reset([_resp("Because it fit.")])
    result = generate_llm_response(
        _FakeSelf(), user2.id, llm2.id, "gpt-5", alice.id,
        source_mode="textmode")
    assert result["status"] == "completed"
    texts = [m["text"] for m in _ScriptedProvider.calls[0]["messages"]]
    assert not any("marks on references" in t for t in texts)
    # The assistant turn is sent with its own marker, not a resolved block.
    assert any(("{quote_ext:%d}" % item.id) in t for t in texts)

    item.read_at = datetime(2026, 9, 13, 7, 45)
    item.feedback = "bad"
    _db.session.commit()
    llm3_parent = _fresh(llm2.id)
    user3 = Node(user_id=alice.id, human_owner_id=alice.id,
                 parent_id=llm3_parent.id, node_type="user",
                 privacy_level="private", ai_usage="chat")
    user3.set_content("and now?")
    _db.session.add(user3)
    _db.session.flush()
    llm3 = Node(user_id=llm_node.user_id, human_owner_id=alice.id,
                parent_id=user3.id, node_type="llm", llm_model="gpt-5",
                llm_task_status="pending", privacy_level="private",
                ai_usage="chat")
    llm3.set_content("[LLM response generation pending...]")
    _db.session.add(llm3)
    _db.session.commit()

    _ScriptedProvider.reset([_resp("Noted.")])
    result = generate_llm_response(
        _FakeSelf(), user3.id, llm3.id, "gpt-5", alice.id,
        source_mode="textmode")
    assert result["status"] == "completed"
    msgs = _ScriptedProvider.calls[0]["messages"]
    last = msgs[-1]
    assert last["role"] == "user"
    assert ("reference %d (@visa) — read 2026-09-13, rated a bad quote"
            % item.id) in last["text"]
    assert "not listed here is unread and unrated" in last["text"]


def test_label_canonicalization_in_final_answer(app, monkeypatch):
    """A label quoted in the FINAL answer (no extra pull round left) is
    still canonicalized by _finalize."""
    import backend.utils.embeddings as emb_mod
    monkeypatch.setattr(
        emb_mod, "embed_texts", lambda texts, key, **kw: [[1.0, 0.0]])

    alice, system, user_node, llm_node = _build_chain("textmode")
    item = _mk_external_item(alice.id, "saved wisdom", [1.0, 0.0])
    _db.session.commit()
    item_id = item.id

    # Round 1: search; round 2: model answers WITH the quote — final
    # (reference quotes never trigger a pull round).
    _ScriptedProvider.reset([
        _resp("Searching.",
              tool_calls=[{"id": "t1", "name": "semantic_search",
                           "input": {"query": "wisdom"}}]),
        _resp("Final thought with {quote:A} inline."),
    ])

    generate_llm_response(
        _FakeSelf(), user_node.id, llm_node.id, "gpt-5", alice.id,
        source_mode="textmode",
    )

    _db.session.expire_all()
    nodes = Node.query.filter(Node.human_owner_id == alice.id).all()
    all_text = "\n".join(n.get_content() or "" for n in nodes)
    assert ("{quote_ext:%d}" % item_id) in all_text
    assert "{quote:A}" not in all_text


# ── Voice per-node TTS dispatch ──────────────────────────────────────────
# Voice turns dispatch TTS at each node's OWN finalization: the interim
# step's audio must be playable while the continuation call is still
# generating (it can run for minutes after a long sharing). The old design
# chained TTS after the whole task, blocking interim audio behind the turn.


def test_voice_tts_dispatched_per_node_at_finalization(app, monkeypatch):
    alice, system, user_node, llm_node = _build_chain("voice")
    _mk_artifact(alice.id, "memory", "remembered things", title="Memory")

    dispatched = []

    def _record(node_id, user_id):
        # Capture how many model calls had happened at dispatch time so we
        # can assert the interim dispatch preceded the continuation call.
        dispatched.append((node_id, user_id, len(_ScriptedProvider.calls)))

    monkeypatch.setattr(_llm_task_mod, "_dispatch_voice_tts", _record)

    _ScriptedProvider.reset([
        _resp("(on it…)", tool_calls=[{
            "id": "t1", "name": "update_artifact",
            "input": {"kind": "memory", "content": "updated"},
        }]),
        _resp("All noted — here's my full reply."),
    ])

    generate_llm_response(
        _FakeSelf(), user_node.id, llm_node.id, "gpt-5", alice.id,
        source_mode="voice",
    )

    interim = _fresh(llm_node.id)
    final = Node.query.get(interim.continuation_node_id)

    # One dispatch per node, in chain order.
    assert [d[0] for d in dispatched] == [interim.id, final.id]
    assert all(d[1] == alice.id for d in dispatched)
    # The interim's TTS was dispatched after ONE model call — i.e. before
    # the continuation call ran (that's the whole point).
    assert dispatched[0][2] == 1
    assert dispatched[1][2] == 2
    # Status marked pending in the same commit as completion, so the
    # frontend's POST /tts sees the in-flight promise and won't
    # double-enqueue.
    assert interim.tts_task_status == "pending"
    assert final.tts_task_status == "pending"


def test_voice_single_node_turn_dispatches_tts_once(app, monkeypatch):
    alice, system, user_node, llm_node = _build_chain("voice")

    dispatched = []
    monkeypatch.setattr(
        _llm_task_mod, "_dispatch_voice_tts",
        lambda node_id, user_id: dispatched.append(node_id))

    _ScriptedProvider.reset([_resp("Just a plain answer.")])

    generate_llm_response(
        _FakeSelf(), user_node.id, llm_node.id, "gpt-5", alice.id,
        source_mode="voice",
    )

    assert dispatched == [llm_node.id]
    assert _fresh(llm_node.id).tts_task_status == "pending"


def test_textmode_never_dispatches_tts(app, monkeypatch):
    alice, system, user_node, llm_node = _build_chain("textmode")
    _mk_artifact(alice.id, "memory", "remembered things", title="Memory")

    dispatched = []
    monkeypatch.setattr(
        _llm_task_mod, "_dispatch_voice_tts",
        lambda node_id, user_id: dispatched.append(node_id))

    _ScriptedProvider.reset([
        _resp("(on it…)", tool_calls=[{
            "id": "t1", "name": "update_artifact",
            "input": {"kind": "memory", "content": "updated"},
        }]),
        _resp("Done."),
    ])

    generate_llm_response(
        _FakeSelf(), user_node.id, llm_node.id, "gpt-5", alice.id,
        source_mode="textmode",
    )

    assert dispatched == []
    interim = _fresh(llm_node.id)
    assert interim.tts_task_status is None


# ── Artifact write echo + interim fallback text ──────────────────────────
# The continuation call must see WHAT an update_artifact call wrote (the
# tool arguments are dropped from the injected assistant turn and the
# inline copy in the system context predates the update) — otherwise the
# model can't build on its own edit (e.g. a question it noted in the
# artifact never gets asked). Content is re-resolved from the encrypted
# rows; only ids live in tool_calls_meta.


def test_artifact_update_echo_injects_diff(app):
    alice, system, user_node, llm_node = _build_chain("textmode")
    _mk_artifact(alice.id, "intentions",
                 "## Active\n- become a better writer", title="Intentions")
    _db.session.commit()

    new_content = ("## Active\n- become a better writer\n"
                   "- bodhisattva acceptance — asked him whether to endorse")
    _ScriptedProvider.reset([
        _resp("Updating your intentions.", tool_calls=[{
            "id": "t1", "name": "update_artifact",
            "input": {"kind": "intentions", "updated_content": new_content},
        }]),
        _resp("Want me to mark that intention endorsed?"),
    ])

    generate_llm_response(
        _FakeSelf(), user_node.id, llm_node.id, "gpt-5", alice.id,
        source_mode="textmode",
    )

    # The continuation saw a diff of the write: marker + the added line.
    second_texts = "\n".join(
        m["text"] for m in _ScriptedProvider.calls[1]["messages"])
    assert "Your changes to 'intentions'" in second_texts
    assert ("+- bodhisattva acceptance — asked him whether to endorse"
            in second_texts)
    # Unchanged lines aren't echoed wholesale (diff, not full text).
    assert "+- become a better writer" not in second_texts

    # Meta carries ids only — never the content.
    interim = _fresh(llm_node.id)
    meta_raw = interim.tool_calls_meta
    assert "bodhisattva" not in meta_raw
    entry = next(e for e in json.loads(meta_raw)
                 if e.get("name") == "update_artifact")
    assert entry["previous_artifact_id"] is not None


def test_artifact_creation_echo_injects_content(app):
    alice, system, user_node, llm_node = _build_chain("textmode")

    _ScriptedProvider.reset([
        _resp("Starting a reading list.", tool_calls=[{
            "id": "t1", "name": "update_artifact",
            "input": {"kind": "reading-list",
                      "updated_content": "1. Gravity's Rainbow"},
        }]),
        _resp("Saved — Gravity's Rainbow is on it."),
    ])

    generate_llm_response(
        _FakeSelf(), user_node.id, llm_node.id, "gpt-5", alice.id,
        source_mode="textmode",
    )

    second_texts = "\n".join(
        m["text"] for m in _ScriptedProvider.calls[1]["messages"])
    assert "You created 'reading-list' with this content:" in second_texts
    assert "1. Gravity's Rainbow" in second_texts
    interim = _fresh(llm_node.id)
    assert "Gravity" not in interim.tool_calls_meta


def test_interim_fallback_names_the_action(app):
    """A TEXT-LESS tool round stores a fallback naming what's happening
    instead of the old generic "(on it…)" — voice reads it aloud."""
    alice, system, user_node, llm_node = _build_chain("voice")

    _ScriptedProvider.reset([
        _resp("", tool_calls=[{
            "id": "t1", "name": "update_artifact",
            "input": {"kind": "memory", "updated_content": "a fact"},
        }]),
        _resp("All noted."),
    ])

    generate_llm_response(
        _FakeSelf(), user_node.id, llm_node.id, "gpt-5", alice.id,
        source_mode="voice",
    )

    interim = _fresh(llm_node.id)
    assert interim.get_content() == "(updating your memory…)"


def test_interim_fallback_composition():
    """Unit: fallback text for tool combinations."""
    fallback = _llm_task_mod._interim_fallback_text

    def up(kind):
        return {"name": "update_artifact", "input": {"kind": kind}}

    read = {"name": "read_artifact", "input": {"kind": "memory"}}
    assert fallback([read]) == "(looking that up…)"  # byte-identical to old
    assert fallback([up("memory")]) == "(updating your memory…)"
    assert (fallback([up("memory"), up("intentions")])
            == "(updating your memory and intentions…)")
    assert (fallback([up("memory"), read])
            == "(updating your memory, looking that up…)")
    assert (fallback([{"name": "apply_todo_changes", "input": {}}])
            == "(updating your todo list…)")


# ── update_artifact edits mode ───────────────────────────────────────────
# Writes come in two modes: `edits` (targeted exact-match replacements —
# cheap) or `updated_content` (full text — creations/heavy rewrites).
# Edits are all-or-nothing and fail cleanly with an error that steers the
# model to fix the anchor or fall back to full text.


def _run_update(alice, user_node, llm_node, tool_input,
                source_mode="textmode"):
    _ScriptedProvider.reset([
        _resp("Updating.", tool_calls=[{
            "id": "t1", "name": "update_artifact", "input": tool_input,
        }]),
        _resp("Done."),
    ])
    generate_llm_response(
        _FakeSelf(), user_node.id, llm_node.id, "gpt-5", alice.id,
        source_mode=source_mode,
    )
    second_texts = "\n".join(
        m["text"] for m in _ScriptedProvider.calls[1]["messages"])
    interim = _fresh(llm_node.id)
    entry = next(e for e in json.loads(interim.tool_calls_meta)
                 if e.get("name") == "update_artifact")
    return second_texts, entry


def test_artifact_edits_mode_applies_and_echoes_diff(app):
    alice, system, user_node, llm_node = _build_chain("textmode")
    _mk_artifact(alice.id, "memory",
                 "## Facts\n- has a dog\n- lives in Prague", title="Memory")
    _db.session.commit()

    second_texts, entry = _run_update(alice, user_node, llm_node, {
        "kind": "memory",
        "edits": [
            {"old_text": "- has a dog", "new_text": "- has two dogs"},
            {"old_text": "- lives in Prague",
             "new_text": "- lives in Prague\n- started a garden"},
        ],
    })

    assert entry["status"] == "success"
    art = UserArtifact.latest_for(alice.id, "memory")
    assert art.get_content() == ("## Facts\n- has two dogs\n"
                                 "- lives in Prague\n- started a garden")
    # Echo shows the diff of what the edits produced.
    assert "Your changes to 'memory'" in second_texts
    assert "+- has two dogs" in second_texts
    assert "+- started a garden" in second_texts
    # Edits are free text — redacted from plaintext meta.
    assert entry["input"]["edits"] == "[redacted]"
    assert "garden" not in _fresh(llm_node.id).tool_calls_meta


def test_artifact_edits_anchor_not_found_fails_cleanly(app):
    alice, system, user_node, llm_node = _build_chain("textmode")
    art = _mk_artifact(alice.id, "memory", "- has a dog", title="Memory")
    _db.session.commit()
    original_id = art.id

    second_texts, entry = _run_update(alice, user_node, llm_node, {
        "kind": "memory",
        "edits": [{"old_text": "- has a cat", "new_text": "- has two"}],
    })

    assert entry["status"] == "error"
    assert "was not found" in entry["error"]
    # Error round steers to a fix; no new version was written.
    assert "update_artifact failed" in second_texts
    assert "updated_content" in second_texts
    assert UserArtifact.latest_for(alice.id, "memory").id == original_id


def test_artifact_edits_ambiguous_anchor_fails(app):
    alice, system, user_node, llm_node = _build_chain("textmode")
    _mk_artifact(alice.id, "memory", "note\nnote", title="Memory")
    _db.session.commit()

    _, entry = _run_update(alice, user_node, llm_node, {
        "kind": "memory",
        "edits": [{"old_text": "note", "new_text": "notes"}],
    })
    assert entry["status"] == "error"
    assert "matches 2 places" in entry["error"]


def test_artifact_edits_on_missing_artifact_fails(app):
    alice, system, user_node, llm_node = _build_chain("textmode")

    _, entry = _run_update(alice, user_node, llm_node, {
        "kind": "memory",
        "edits": [{"old_text": "x", "new_text": "y"}],
    })
    assert entry["status"] == "error"
    assert "No existing artifact to edit" in entry["error"]


def test_artifact_full_text_wins_over_edits(app):
    alice, system, user_node, llm_node = _build_chain("textmode")
    _mk_artifact(alice.id, "memory", "old", title="Memory")
    _db.session.commit()

    _, entry = _run_update(alice, user_node, llm_node, {
        "kind": "memory",
        "updated_content": "brand new full text",
        "edits": [{"old_text": "nonexistent", "new_text": "ignored"}],
    })
    assert entry["status"] == "success"
    assert (UserArtifact.latest_for(alice.id, "memory").get_content()
            == "brand new full text")


def test_artifact_substantial_rewrite_echoes_full_text(app):
    """A diff larger than the threshold switches the echo to the full new
    content — clearer and usually shorter for near-total rewrites."""
    alice, system, user_node, llm_node = _build_chain("textmode")
    old = "\n".join(f"- old fact {i}" for i in range(200))
    _mk_artifact(alice.id, "memory", old, title="Memory")
    _db.session.commit()

    new = "\n".join(f"- new fact {i}" for i in range(200))
    second_texts, entry = _run_update(alice, user_node, llm_node, {
        "kind": "memory", "updated_content": new,
    })

    assert entry["status"] == "success"
    assert "Your rewrite of 'memory' was substantial" in second_texts
    # Full new text, not a diff: every line present, un-prefixed.
    assert "- new fact 199" in second_texts
    assert "+- new fact 199" not in second_texts


# ── #222: the prompt always ends on a user turn ──────────────────────────

def test_reply_under_llm_node_ends_with_user_turn(app):
    """A non-agentic reply whose parent is itself an LLM node would send a
    prompt ending on an assistant turn (the chain ends at parent_node).
    Prefill-unsupported models 400 on that; the task now closes the prompt
    with a neutral user turn."""
    alice = _mk_user("alice", approved=True, plan="alpha")
    llm_user = _mk_user("gpt-5", twitter_id="llm-gpt-5")
    root = Node(user_id=alice.id, human_owner_id=alice.id, node_type="user",
                privacy_level="private", ai_usage="chat")
    root.set_content("a thought")
    _db.session.add(root)
    _db.session.flush()
    first_reply = Node(user_id=llm_user.id, human_owner_id=alice.id,
                       parent_id=root.id, node_type="llm", llm_model="gpt-5",
                       llm_task_status="completed", privacy_level="private",
                       ai_usage="chat")
    first_reply.set_content("a first answer")
    _db.session.add(first_reply)
    _db.session.flush()
    placeholder = Node(user_id=llm_user.id, human_owner_id=alice.id,
                       parent_id=first_reply.id, node_type="llm",
                       llm_model="gpt-5", llm_task_status="pending",
                       privacy_level="private", ai_usage="chat")
    placeholder.set_content("[LLM response generation pending...]")
    _db.session.add(placeholder)
    _db.session.commit()

    _ScriptedProvider.reset([_resp("continuing…")])
    generate_llm_response(
        _FakeSelf(), first_reply.id, placeholder.id, "gpt-5", alice.id)

    msgs = _ScriptedProvider.calls[0]["messages"]
    assert msgs[-2]["role"] == "assistant"
    assert msgs[-2]["text"].endswith("a first answer")
    assert msgs[-1] == {"role": "user", "text": "[continue]"}


def test_agentic_prompt_already_ending_on_user_turn_is_untouched(app):
    """Agentic turns end on the injected notes (a user turn); nothing extra
    is appended — the guard is a no-op there."""
    alice, system, user_node, llm_node = _build_chain("textmode")
    _ScriptedProvider.reset([_resp("hi")])
    generate_llm_response(
        _FakeSelf(), user_node.id, llm_node.id, "gpt-5", alice.id,
        source_mode="textmode")
    msgs = _ScriptedProvider.calls[0]["messages"]
    assert msgs[-1]["role"] == "user"
    assert "[continue]" not in msgs[-1]["text"]
    assert sum(1 for m in msgs if m["text"] == "[continue]") == 0


def test_render_variant_carries_the_killswitch(app):
    """The #192 render-cache key must change when the env killswitch
    flips, not only on the per-user toggles: with the toggle off, both
    killswitch states read e0 while rendering different text (archive
    guidance vs nothing), so without the `a` bit an emergency flip would
    keep serving the cached archive-search guidance for the TTL."""
    alice, *_ = _build_chain("textmode")
    alice.external_content_enabled = False
    _db.session.commit()
    on = _llm_task_mod._render_variant(alice.id)
    assert on.startswith("a1") and on.endswith("e0")
    app.config["SEMANTIC_SEARCH_AGENTIC"] = False
    try:
        off = _llm_task_mod._render_variant(alice.id)
    finally:
        app.config["SEMANTIC_SEARCH_AGENTIC"] = True
    assert off.startswith("a0") and off.endswith("e0")
    assert on != off
    # And the toggle itself still moves the key.
    alice.external_content_enabled = True
    _db.session.commit()
    assert _llm_task_mod._render_variant(alice.id).endswith("e1")


# ── OpenAI Prompt Cache Diagnostics (#348) ───────────────────────────────

@pytest.fixture
def diag_model(app):
    """gpt-5 as a model with cache_diagnostics, restored afterwards."""
    cfg = _app.config["SUPPORTED_MODELS"]["gpt-5"]
    cfg["cache_diagnostics"] = True
    yield
    cfg.pop("cache_diagnostics", None)


def _llm_reply_under(parent, alice, text="next question"):
    """A user message under *parent* and a pending LLM placeholder under
    it, as a follow-up turn would create."""
    llm_user = User.query.filter_by(username="gpt-5").first()
    user_node = Node(user_id=alice.id, human_owner_id=alice.id,
                     parent_id=parent.id, node_type="user",
                     privacy_level="private", ai_usage="chat")
    user_node.set_content(text)
    _db.session.add(user_node)
    _db.session.flush()
    llm_node = Node(user_id=llm_user.id, human_owner_id=alice.id,
                    parent_id=user_node.id, node_type="llm",
                    llm_model="gpt-5", llm_task_status="pending",
                    privacy_level="private", ai_usage="chat")
    llm_node.set_content("[LLM response generation pending...]")
    _db.session.add(llm_node)
    _db.session.commit()
    return user_node, llm_node


def test_cache_diagnostics_tool_round_then_next_turn(app, diag_model):
    alice, system, user_node, llm_node = _build_chain("textmode")
    _mk_artifact(alice.id, "reading-list", "1. Dune", title="Reading List")
    miss = {"type": "cache_miss", "reason": "input_changed",
            "comparison_reusable_tokens": 800, "cache_missed_tokens": 120}
    _ScriptedProvider.reset([
        dict(_resp("Let me look.", tool_calls=[{
            "id": "t1", "name": "read_artifact",
            "input": {"kind": "reading-list"}}]), response_id="resp_1"),
        dict(_resp("Start with Dune."), response_id="resp_2",
             cache_diagnostics=miss),
    ])
    generate_llm_response(_FakeSelf(), user_node.id, llm_node.id, "gpt-5",
                          alice.id, source_mode="textmode")

    # The thread's first call has no baseline; the continuation compares
    # itself to the tool round before it.
    sent = [c["cache_comparison_response_id"]
            for c in _ScriptedProvider.calls]
    assert sent == [None, "resp_1"]

    interim = _fresh(llm_node.id)
    final_id = interim.continuation_node_id
    rows = APICostLog.query.order_by(APICostLog.id).all()
    assert [r.request_ref for r in rows] == [
        f"node:{interim.id}", f"node:{final_id}"]
    assert [r.provider_response_id for r in rows] == ["resp_1", "resp_2"]
    assert rows[0].cache_diag_baseline is None
    assert rows[0].cache_diag_type is None
    assert rows[1].cache_diag_baseline == "tool_round"
    assert rows[1].cache_diag_type == "cache_miss"
    assert rows[1].cache_diag_reason == "input_changed"
    assert rows[1].cache_diag_reusable_tokens == 800
    assert rows[1].cache_diag_missed_tokens == 120
    assert rows[1].cache_diag_gap_s is not None
    # Same system prompt on both calls of the turn.
    assert rows[0].system_prefix_hash
    assert rows[0].system_prefix_hash == rows[1].system_prefix_hash

    # Next turn, under the final answer: compares to the last call on the
    # path, which is the continuation's.
    final = Node.query.get(final_id)
    _u2, llm2 = _llm_reply_under(final, alice)
    _ScriptedProvider.reset([dict(_resp("Sure."), response_id="resp_3")])
    generate_llm_response(_FakeSelf(), _u2.id, llm2.id, "gpt-5", alice.id,
                          source_mode="textmode")
    assert _ScriptedProvider.calls[0]["cache_comparison_response_id"] == (
        "resp_2")
    row = APICostLog.query.order_by(APICostLog.id.desc()).first()
    assert row.cache_diag_baseline == "prev_turn"
    assert row.request_ref == f"node:{llm2.id}"

    # A branch off the first user message sees none of those calls: its
    # ancestor path holds no LLM node.
    _u3, llm3 = _llm_reply_under(system, alice, text="another branch")
    _ScriptedProvider.reset([dict(_resp("Hi."), response_id="resp_4")])
    generate_llm_response(_FakeSelf(), _u3.id, llm3.id, "gpt-5", alice.id,
                          source_mode="textmode")
    assert _ScriptedProvider.calls[0]["cache_comparison_response_id"] is None


def test_cache_diagnostics_off_for_models_without_the_flag(app):
    alice, system, user_node, llm_node = _build_chain("textmode")
    _ScriptedProvider.reset([dict(_resp("Hello."), response_id="resp_1")])
    generate_llm_response(_FakeSelf(), user_node.id, llm_node.id, "gpt-5",
                          alice.id, source_mode="textmode")
    _u2, llm2 = _llm_reply_under(_fresh(llm_node.id), alice)
    _ScriptedProvider.reset([dict(_resp("Again."), response_id="resp_2")])
    generate_llm_response(_FakeSelf(), _u2.id, llm2.id, "gpt-5", alice.id,
                          source_mode="textmode")
    assert _ScriptedProvider.calls[0]["cache_comparison_response_id"] is None
    rows = APICostLog.query.order_by(APICostLog.id).all()
    # The id and the node are still recorded, so enabling the flag later
    # has a baseline from the first turn on.
    assert [r.provider_response_id for r in rows] == ["resp_1", "resp_2"]
    assert rows[1].request_ref == f"node:{llm2.id}"
    assert all(r.cache_diag_baseline is None for r in rows)

"""Tests for the within-turn retrieval loop in generate_llm_response (#158).

Text mode only: when the model calls a retrieval tool (read_artifact), the
task executes it, injects the retrieved content back into `messages` as a
plain user message, finalizes the calling node as an INTERIM step, creates a
CONTINUATION node, and re-calls the model so it answers WITH the content in
the SAME turn. Voice / non-textmode keep their single-shot behavior.

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
        })
        nxt = cls.responses.pop(0)
        # A queued Exception is raised (e.g. to drive a PromptTooLong on the
        # continuation call); a dict is returned as a normal response.
        if isinstance(nxt, Exception):
            raise nxt
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
    # Agentic semantic_search ships dark behind this flag (#155); enable it so
    # the loop's semantic_search / quote-pull behavior is exercised. The dark
    # (flag-off) path has its own dedicated test that toggles it back off.
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
    alice = _mk_user("alice", approved=True, plan="alpha")
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


def test_quote_pull_resolves_full_content_and_continues(app):
    """A {quote:ID} the model emits for an out-of-context node triggers a
    within-turn resolution + continuation (no retrieval tool call needed):
    the full node content is injected and the model answers with it, while the
    {quote:ID} stays in the interim node's text (renders for the user +
    flows to exports)."""
    alice, system, user_node, llm_node = _build_chain("textmode")
    # An archive node that is NOT part of the conversation chain.
    archive = Node(user_id=alice.id, human_owner_id=alice.id,
                   node_type="text", privacy_level="private", ai_usage="chat")
    archive.set_content("THE FULL ARCHIVE ENTRY about leaving my job.")
    _db.session.add(archive)
    _db.session.commit()
    aid = archive.id

    _ScriptedProvider.reset([
        _resp("Let me pull that up. {quote:%d}" % aid),
        _resp("Based on that entry, here's my read."),
    ])

    result = generate_llm_response(
        _FakeSelf(), user_node.id, llm_node.id, "gpt-5", alice.id,
        source_mode="textmode",
    )

    # Quote pull (interim) + continuation = 2 calls.
    assert len(_ScriptedProvider.calls) == 2
    interim = _fresh(llm_node.id)
    assert interim.continuation_node_id is not None
    # The {quote:ID} reference stays in the interim node's content.
    assert ("{quote:%d}" % aid) in interim.get_content()
    # The full node content was injected into the continuation call.
    cont_msgs = "\n".join(
        m["text"] for m in _ScriptedProvider.calls[1]["messages"])
    assert "THE FULL ARCHIVE ENTRY" in cont_msgs
    final = Node.query.get(interim.continuation_node_id)
    assert final.get_content() == "Based on that entry, here's my read."
    assert result["llm_node_id"] == final.id


def test_quote_pull_is_depth_1_no_recursive_expansion(app):
    """A loop quote-pull resolves the pulled node's OWN content only; a
    {quote:ID} nested INSIDE it stays as a placeholder rather than being
    recursively inlined. This is the guard against the combinatorial blowup
    that produced a 2.77M-token continuation prompt — depth-3 resolution
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
        _resp("Let me pull that up. {quote:%d}" % outer_id),
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
        _resp("Let me pull that up. {quote:%d}" % aid),
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


def test_semantic_search_gated_dark_by_default(app):
    """With SEMANTIC_SEARCH_AGENTIC off (#155 ships dark), the semantic_search
    tool is dropped from the exposed tool list — the model never sees it — and
    a model-emitted {quote:ID} for an out-of-chain node is NOT pulled mid-turn.
    The other retrieval tools (read_artifact/read_todo) are unaffected, and
    manual Cmd+K search is a separate endpoint, also unaffected."""
    alice, system, user_node, llm_node = _build_chain("textmode")
    archive = Node(user_id=alice.id, human_owner_id=alice.id,
                   node_type="text", privacy_level="private", ai_usage="chat")
    archive.set_content("AN OUT-OF-CHAIN ENTRY.")
    _db.session.add(archive)
    _db.session.commit()
    aid = archive.id

    saved = _app.config.get("SEMANTIC_SEARCH_AGENTIC")
    _app.config["SEMANTIC_SEARCH_AGENTIC"] = False
    try:
        _ScriptedProvider.reset([
            _resp("Here's my take. {quote:%d}" % aid),
        ])
        generate_llm_response(
            _FakeSelf(), user_node.id, llm_node.id, "gpt-5", alice.id,
            source_mode="textmode",
        )
    finally:
        _app.config["SEMANTIC_SEARCH_AGENTIC"] = saved

    # Tool list excludes semantic_search but keeps the other retrieval tools.
    tool_names = [t["name"] for t in (_ScriptedProvider.calls[0]["tools"] or [])]
    assert "semantic_search" not in tool_names
    assert "read_artifact" in tool_names
    # The {quote:ID} did NOT trigger a within-turn pull: one call, no
    # continuation node, the placeholder stays in the answer for the frontend
    # to resolve at render time (matches main's behavior).
    assert len(_ScriptedProvider.calls) == 1
    node = _fresh(llm_node.id)
    assert node.continuation_node_id is None


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

    alice = _mk_user("alice", approved=True, plan="alpha")
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
        _resp("Someone you saved said it better: {quote:B}"),
        _resp("So, as that tweet shows, here's my answer."),
    ])

    result = generate_llm_response(
        _FakeSelf(), user_node.id, llm_node.id, "gpt-5", alice.id,
        source_mode="textmode",
    )

    assert len(_ScriptedProvider.calls) == 3
    # Round 2 saw labeled previews with surfacing metadata semantics.
    round2 = "\n".join(
        m["text"] for m in _ScriptedProvider.calls[1]["messages"])
    assert "[A]" in round2 and "[B]" in round2
    assert "saved reference by @visa" in round2
    # The interim node holding the quote has the CANONICAL marker.
    interim = _fresh(llm_node.id)
    chain_texts = []
    node = interim
    while node is not None:
        chain_texts.append(node.get_content())
        node = (Node.query.get(node.continuation_node_id)
                if node.continuation_node_id else None)
    all_text = "\n".join(chain_texts)
    assert ("{quote_ext:%d}" % item_id) in all_text
    assert "{quote:B}" not in all_text
    # Round 3 (continuation after the quote) got the full reference text.
    round3 = "\n".join(
        m["text"] for m in _ScriptedProvider.calls[2]["messages"])
    assert "the perfect saved tweet about zen" in round3
    # Surfacing history bumped exactly once.
    _db.session.expire_all()
    fresh_item = ExternalItem.query.get(item_id)
    assert fresh_item.surfaced_count == 1
    assert fresh_item.last_surfaced_at is not None
    assert result["status"] == "completed"


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

    # Round 1: search; round 2: model answers WITH the quote and the loop
    # pulls the full reference (interim); round 3: final.
    _ScriptedProvider.reset([
        _resp("Searching.",
              tool_calls=[{"id": "t1", "name": "semantic_search",
                           "input": {"query": "wisdom"}}]),
        _resp("Final thought with {quote:A} inline."),
        _resp("Done."),
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

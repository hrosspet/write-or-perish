"""Tests for OpenAI Prompt Cache Diagnostics (#348).

Covers the provider call (the option goes out only with a baseline and
only on flagged models, the verdict comes back, a rejected option never
costs the turn) and the baseline bookkeeping in
backend.utils.cache_diagnostics. The end-to-end conversation path is in
test_retrieval_loop.py (test_cache_diagnostics_*).
"""
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import MagicMock

os.environ["ENCRYPTION_DISABLED"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("TWITTER_API_KEY", "fake")
os.environ.setdefault("TWITTER_API_SECRET", "fake")

sys.modules.setdefault("celery", MagicMock())
sys.modules.setdefault("celery.utils", MagicMock())
sys.modules.setdefault("celery.utils.log", MagicMock())
sys.modules.setdefault("celery.result", MagicMock())

import httpx  # noqa: E402
import openai  # noqa: E402
import pytest  # noqa: E402
from flask import Flask  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

from backend.extensions import db as _db  # noqa: E402
from backend.models import User, Node, APICostLog  # noqa: E402
from backend.utils.cache_diagnostics import (  # noqa: E402
    ConversationCacheDiagnostics, find_prev_turn_baseline,
    system_prefix_hash,
)

SUPPORTED_MODELS = {
    "gpt-6-astra": {"provider": "openai", "api_model": "gpt-6-astra",
                    "cache_diagnostics": True},
    "gpt-5.5": {"provider": "openai", "api_model": "gpt-5.5"},
}

DIAG = {"type": "cache_miss", "reason": "tools_changed",
        "comparison_reusable_tokens": 5629, "cache_missed_tokens": 5629}


@pytest.fixture
def app():
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["TESTING"] = True
    app.config["SUPPORTED_MODELS"] = SUPPORTED_MODELS
    _db.init_app(app)
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()


# ── Provider call ────────────────────────────────────────────────────────

def _load_providers():
    """A private copy of backend/llm_providers.py. Sibling test modules
    stub or re-import backend.llm_providers in sys.modules; loading the
    source under another name leaves their module objects untouched,
    whatever order the files run in."""
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "llm_providers.py")
    spec = importlib.util.spec_from_file_location(
        "_llm_providers_under_test_348", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_openai(monkeypatch, diagnostics=None, reject_option=False):
    """The real provider code with a fake OpenAI client. Returns
    (providers module, list of kwargs each create() call received)."""
    providers = _load_providers()
    calls = []

    class FakeUsage:
        input_tokens = 100
        output_tokens = 5
        total_tokens = 105
        input_tokens_details = None

    class FakeResponse:
        id = "resp_new"
        output = []
        usage = FakeUsage()
        status = "completed"
        incomplete_details = None

    if diagnostics is not None:
        FakeResponse.prompt_cache_diagnostics = diagnostics

    class FakeResponses:
        def create(self, **kwargs):
            calls.append(kwargs)
            if reject_option and "extra_body" in kwargs:
                raise openai.BadRequestError(
                    "Unknown parameter: 'prompt_cache_options'.",
                    response=httpx.Response(400, request=httpx.Request(
                        "POST", "https://api.openai.com/v1/responses")),
                    body=None)
            return FakeResponse()

    class FakeClient:
        def __init__(self, api_key=None):
            self.responses = FakeResponses()

    monkeypatch.setattr(providers, "OpenAI", FakeClient)
    return providers, calls


def _complete(providers, model_id, baseline):
    return providers.LLMProvider.get_completion(
        model_id, [{"role": "user", "content": "x"}], {"openai": "k"},
        cache_comparison_response_id=baseline)


def test_baseline_goes_out_and_the_verdict_comes_back(app, monkeypatch):
    providers, calls = _fake_openai(monkeypatch, diagnostics=dict(DIAG))
    result = _complete(providers, "gpt-6-astra", "resp_old")
    assert calls[0]["extra_body"] == {
        "prompt_cache_options": {"comparison_response_id": "resp_old"}}
    assert result["response_id"] == "resp_new"
    assert result["cache_diagnostics"] == DIAG


def test_no_baseline_sends_no_option(app, monkeypatch):
    providers, calls = _fake_openai(monkeypatch)
    result = _complete(providers, "gpt-6-astra", None)
    assert "extra_body" not in calls[0]
    assert result["response_id"] == "resp_new"
    assert "cache_diagnostics" not in result


def test_model_without_the_flag_never_gets_the_option(app, monkeypatch):
    providers, calls = _fake_openai(monkeypatch)
    result = _complete(providers, "gpt-5.5", "resp_old")
    assert "extra_body" not in calls[0]
    # Its id is still recorded: a later GPT-5.6+ turn can compare to it.
    assert result["response_id"] == "resp_new"


def test_rejected_option_retries_without_it(app, monkeypatch):
    providers, calls = _fake_openai(monkeypatch, reject_option=True)
    result = _complete(providers, "gpt-6-astra", "resp_old")
    assert len(calls) == 2
    assert "extra_body" in calls[0] and "extra_body" not in calls[1]
    assert result["content"] == "" and "cache_diagnostics" not in result
    # The row must not claim a baseline that did not go out.
    assert result["cache_comparison_sent"] is False


def test_rejected_baseline_id_named_only_by_param_retries(app, monkeypatch):
    """A 400 naming only the id (say, from another OpenAI project after a
    key switch) still falls back to a call without the option."""
    providers, calls = _fake_openai(monkeypatch)
    real_create = None

    class Rejecting:
        def __init__(self, api_key=None):
            self.responses = self

        def create(self, **kwargs):
            calls.append(kwargs)
            if "extra_body" in kwargs:
                err = openai.BadRequestError(
                    "Invalid value.",
                    response=httpx.Response(400, request=httpx.Request(
                        "POST", "https://api.openai.com/v1/responses")),
                    body={"param": "comparison_response_id"})
                raise err
            return real_create(**kwargs)

    real_create = providers.OpenAI().responses.create
    monkeypatch.setattr(providers, "OpenAI", Rejecting)
    result = _complete(providers, "gpt-6-astra", "resp_other_project")
    assert len(calls) == 3  # the probe above, then the rejected + retry
    assert result["response_id"] == "resp_new"


def test_other_bad_requests_are_not_retried(app, monkeypatch):
    providers, calls = _fake_openai(monkeypatch)

    class Broken:
        def __init__(self, api_key=None):
            self.responses = self

        def create(self, **kwargs):
            calls.append(kwargs)
            raise openai.BadRequestError(
                "Invalid schema for function 'x'.",
                response=httpx.Response(400, request=httpx.Request(
                    "POST", "https://api.openai.com/v1/responses")),
                body=None)

    monkeypatch.setattr(providers, "OpenAI", Broken)
    with pytest.raises(openai.BadRequestError):
        _complete(providers, "gpt-6-astra", "resp_old")
    assert len(calls) == 1


def test_typed_diagnostics_object_is_read_too(app, monkeypatch):
    """A later SDK may type the field; the reader takes an object."""
    class Typed:
        type = "cache_hit"
        reason = None
        comparison_reusable_tokens = 900
        cache_missed_tokens = 0

    providers, _ = _fake_openai(monkeypatch, diagnostics=Typed())
    result = _complete(providers, "gpt-6-astra", "resp_old")
    assert result["cache_diagnostics"] == {
        "type": "cache_hit", "reason": None,
        "comparison_reusable_tokens": 900, "cache_missed_tokens": 0}


# ── Baseline bookkeeping ─────────────────────────────────────────────────

def _node(user, parent=None):
    n = Node(user_id=user.id, human_owner_id=user.id,
             parent_id=parent.id if parent else None, node_type="user",
             privacy_level="private", ai_usage="chat")
    n.set_content("x")
    _db.session.add(n)
    _db.session.flush()
    return n


def _row(user, node, response_id, created_at=None, request_type="conversation"):
    row = APICostLog(user_id=user.id, model_id="gpt-6-astra",
                     request_type=request_type, cost_microdollars=1,
                     request_ref=f"node:{node.id}",
                     provider_response_id=response_id,
                     created_at=created_at or datetime.utcnow())
    _db.session.add(row)
    _db.session.flush()
    return row


def test_prev_turn_baseline_follows_the_ancestor_path(app):
    alice = User(username="alice")
    bob = User(username="bob")
    _db.session.add_all([alice, bob])
    _db.session.flush()
    root = _node(alice)
    a1 = _node(alice, root)
    a2 = _node(alice, a1)
    sibling = _node(alice, root)
    _row(alice, a1, "resp_a1")
    _row(alice, a2, "resp_a2")
    # Newer, but on a sibling branch: must not be picked.
    _row(alice, sibling, "resp_sibling")
    # Anthropic rows have no response id; profile rows are another type.
    _row(alice, a2, None)
    _row(alice, a2, "resp_profile", request_type="profile")
    # Another user's row pointing at the same node id never counts.
    _row(bob, a2, "resp_bob")

    baseline = find_prev_turn_baseline(alice.id, [root, a1, a2])
    assert baseline.response_id == "resp_a2"
    assert baseline.kind == "prev_turn"
    assert find_prev_turn_baseline(alice.id, [root]) is None
    assert find_prev_turn_baseline(alice.id, []) is None


def test_call_annotates_and_advances_the_baseline(app):
    alice = User(username="alice")
    _db.session.add(alice)
    _db.session.flush()
    root = _node(alice)
    _row(alice, root, "resp_prev",
         created_at=datetime.utcnow() - timedelta(minutes=40))
    diag = ConversationCacheDiagnostics(True, alice.id, [root],
                                        system_hash="abc")
    sent = []

    def completion(resp_id):
        def fn(baseline_id):
            sent.append(baseline_id)
            return {"response_id": resp_id,
                    "cache_comparison_sent": baseline_id is not None}
        return fn

    first = diag.call(completion("resp_1"))
    second = diag.call(completion("resp_2"))
    assert sent == ["resp_prev", "resp_1"]
    assert first["cache_diag_baseline"] == "prev_turn"
    assert first["cache_diag_gap_s"] >= 40 * 60
    assert second["cache_diag_baseline"] == "tool_round"
    assert 0 <= second["cache_diag_gap_s"] < 60


def test_disabled_tracker_sends_nothing(app):
    alice = User(username="alice")
    _db.session.add(alice)
    _db.session.flush()
    root = _node(alice)
    _row(alice, root, "resp_prev")
    diag = ConversationCacheDiagnostics(False, alice.id, [root])
    sent = []
    resp = diag.call(lambda b: sent.append(b) or {"response_id": "r"})
    resp = diag.call(lambda b: sent.append(b) or {"response_id": "r2"})
    assert sent == [None, None]
    assert "cache_diag_baseline" not in resp


def test_log_fields_map_the_verdict(app, caplog):
    diag = ConversationCacheDiagnostics(False, None, [], system_hash="h")
    fields = diag.log_fields({
        "response_id": "resp_1", "cache_diag_baseline": "tool_round",
        "cache_diag_gap_s": 3, "cache_diagnostics": dict(DIAG)}, 42)
    assert fields == {
        "request_ref": "node:42", "provider_response_id": "resp_1",
        "system_prefix_hash": "h", "cache_diag_baseline": "tool_round",
        "cache_diag_gap_s": 3, "cache_diag_type": "cache_miss",
        "cache_diag_reason": "tools_changed",
        "cache_diag_reusable_tokens": 5629, "cache_diag_missed_tokens": 5629}
    # Every field is a real column.
    APICostLog(user_id=1, model_id="m", request_type="conversation",
               cost_microdollars=0, **fields)
    # A reason our code should never cause is logged as a warning.
    with caplog.at_level("WARNING"):
        diag.log_fields({"cache_diagnostics": {
            "type": "cache_miss", "reason": "reasoning_effort_changed"}}, 1)
    assert "reasoning_effort_changed" in caplog.text


def test_system_prefix_hash():
    msgs = [{"role": "system", "content": [{"type": "text", "text": "abc",
                                            "cache_control": {}}]},
            {"role": "user", "content": "hi"}]
    h = system_prefix_hash(msgs, 0)
    assert len(h) == 16
    # cache_control markers do not change the hash, the text does.
    assert h == system_prefix_hash(
        [{"role": "system", "content": [{"type": "text", "text": "abc"}]}], 0)
    assert h != system_prefix_hash(
        [{"role": "system", "content": [{"type": "text", "text": "abd"}]}], 0)
    assert system_prefix_hash(msgs, None) is None


def test_log_fields_drop_what_would_not_fit(app):
    """An unexpected verdict never fails the commit that completes the
    node: over-long or non-string text and non-integer counts become
    None."""
    diag = ConversationCacheDiagnostics(False, None, [])
    fields = diag.log_fields({"response_id": "r" * 300, "cache_diagnostics": {
        "type": "cache_miss", "reason": "x" * 90,
        "comparison_reusable_tokens": {"n": 1},
        "cache_missed_tokens": "12"}}, 7)
    assert len(fields["provider_response_id"]) == 128
    assert fields["cache_diag_reason"] == "x" * 40
    assert fields["cache_diag_reusable_tokens"] is None
    assert fields["cache_diag_missed_tokens"] == 12
    fields = diag.log_fields({"cache_diagnostics": {
        "type": {"nested": True}, "cache_missed_tokens": 2**40}}, 7)
    assert fields["cache_diag_type"] is None
    assert fields["cache_diag_missed_tokens"] is None


def test_failed_lookup_leaves_the_session_usable(app, monkeypatch):
    import backend.utils.cache_diagnostics as cd
    from sqlalchemy import text

    def broken(user_id, node_chain):
        _db.session.execute(text("SELECT no_such_column FROM api_cost_log"))

    monkeypatch.setattr(cd, "find_prev_turn_baseline", broken)
    alice = User(username="alice")
    _db.session.add(alice)
    _db.session.flush()
    diag = ConversationCacheDiagnostics(True, alice.id, [])
    assert diag.call(lambda b: {"response_id": "r"})["response_id"] == "r"
    # The task's transaction goes on: the user row is still there to commit.
    _db.session.commit()
    assert User.query.filter_by(username="alice").count() == 1


def test_gap_is_measured_to_the_request_not_the_reply(app, monkeypatch):
    import backend.utils.cache_diagnostics as cd
    t0 = datetime(2026, 9, 24, 12, 0, 0)
    clock = iter([t0 + timedelta(seconds=10),   # request of call 1
                  t0 + timedelta(seconds=100),  # reply of call 1
                  t0 + timedelta(seconds=110),  # request of call 2
                  t0 + timedelta(seconds=500)])  # reply of call 2

    class FakeDatetime:
        @staticmethod
        def utcnow():
            return next(clock)

    monkeypatch.setattr(cd, "datetime", FakeDatetime)
    diag = ConversationCacheDiagnostics(False, None, [])
    diag.enabled = True
    diag._baseline = cd.Baseline("resp_prev", "prev_turn", t0)
    first = diag.call(lambda b: {"response_id": "r1",
                                 "cache_comparison_sent": True})
    second = diag.call(lambda b: {"response_id": "r2",
                                  "cache_comparison_sent": True})
    assert first["cache_diag_gap_s"] == 10
    # From call 1's reply to call 2's request, not to call 2's reply.
    assert second["cache_diag_gap_s"] == 10

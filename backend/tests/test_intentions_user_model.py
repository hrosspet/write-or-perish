"""Intentions run on the user's own model, never another provider's.

They were pinned to Opus 4.8 for every user, so OpenAI users' intentions
ran on Anthropic. Now every way in (the admin button's batch and sync
runs, the poller's collect and resubmit, the backfill script) uses the
model the user's other background work runs on (default_model_for),
priced at its provider's rates. "A user's model is theirs" (Peter,
2026-10-02): no provider switch, not even as a fallback. No real LLM is
called: the `wired` fixture mocks the export, the count, the batch
submit and the completion."""
import importlib
import sys
from unittest.mock import MagicMock

import pytest

from backend.tests.test_twitter_import import app, _make_user, _db  # noqa: F401
from backend.tests.test_intentions_task import wired  # noqa: F401
from backend.models import UserArtifact, APICostLog, ProfileBatchJob


@pytest.fixture
def it():
    import backend.tasks.intentions as module
    return module


def _openai_user(name, model="gpt-6-astra"):
    u = _make_user(name)
    u.preferred_model = model
    _db.session.commit()
    return u


def _item(u, **extra):
    return {"custom_id": f"int-u{u.id}", "user_id": u.id, "kind": "intentions",
            "budget": 1_000_000, "resubmitted": False, **extra}


# ── Which model ────────────────────────────────────────────────────────────

def test_an_openai_user_is_submitted_to_openai_on_their_model(app, wired, it):  # noqa: F811
    u = _openai_user("oa_batch")
    kind, ref = it.start_infer_intentions_impl(u.id)
    assert kind == "batch"
    assert list(wired[0]) == ["openai"]
    req = wired[0]["openai"][0]
    assert req["model_id"] == "gpt-6-astra" and req["api_model"] == "gpt-6-astra"
    # Room for reasoning and the answer, as the other background jobs.
    assert req["max_tokens"] == 32000
    assert ref["provider_key"] == "openai:gpt-6-astra"
    job = ProfileBatchJob.query.filter_by(batch_id=ref["batch_id"]).one()
    assert job.provider_key == "openai:gpt-6-astra"
    assert job.items[0]["model_id"] == "gpt-6-astra"
    # A 500k-token prompt is sent whole: the whole archive is read, over
    # the 272k price tier (no shrink, the full cap).
    assert ref["item"]["budget"] == 1_000_000


def test_an_openai_prompt_is_sized_to_openais_input_ceiling(app, wired, it, monkeypatch):  # noqa: F811
    """950k counted tokens fit Opus 5.5's window but not GPT-6 Astra's
    922k input ceiling, so only the OpenAI run shrinks."""
    import backend.llm_providers as lp
    import backend.tasks.recent_context as rc
    monkeypatch.setattr(rc, "_count_total_eligible_tokens", lambda uid: 900_000)
    counts = []

    def count(model_id, messages, keys):
        counts.append(model_id)
        return 950_000 if len(counts) == 1 else 800_000
    monkeypatch.setattr(lp.LLMProvider, "count_tokens", staticmethod(count))
    u = _openai_user("oa_ceiling")
    _kind, ref = it.start_infer_intentions_impl(u.id)
    assert counts == ["gpt-6-astra", "gpt-6-astra"]
    # min(1M, 900k corpus) * 922k / 950k * 0.99 ≈ 865k
    assert 850_000 < ref["item"]["budget"] < 880_000

    counts.clear()
    claude = _make_user("claude_ceiling")
    _db.session.commit()
    _kind, ref = it.start_infer_intentions_impl(claude.id)
    assert counts == ["claude-opus-5.5"]          # 950k < 968k: fits at once
    assert ref["item"]["budget"] == 1_000_000


def test_no_saved_model_runs_on_llm_name(app, wired, it):  # noqa: F811
    u = _make_user("no_pref")
    _db.session.commit()
    it.start_infer_intentions_impl(u.id)
    assert wired[0]["anthropic"][0]["model_id"] == "claude-opus-5.5"


def test_a_retired_anthropic_model_runs_on_llm_name(app, wired, it):  # noqa: F811
    """Opus 4.8 (the old pin) saved as the preference, now deprecated: the
    same provider's LLM_NAME, as for the user's other background work."""
    u = _openai_user("old_claude", model="claude-opus-4.8")
    it.start_infer_intentions_impl(u.id)
    assert wired[0]["anthropic"][0]["model_id"] == "claude-opus-5.5"


# Provider of each test model (test_intentions_task.MODELS).
MODEL_PROVIDER = {"claude-opus-5.5": "anthropic", "claude-opus-4.8": "anthropic",
                  "gpt-6-astra": "openai", "gpt-5.5": "openai"}


def test_a_retired_openai_model_never_moves_the_run_to_anthropic(app, wired, it, monkeypatch):  # noqa: F811
    """GPT-5.5 saved, now deprecated, while LLM_NAME is Anthropic's. Where
    default_model_for falls back to LLM_NAME, the run is refused before
    anything is built or sent; where it gives the provider's own
    replacement (#555), the run goes there. Never to Anthropic, in batch
    or sync mode."""
    import backend.llm_providers as lp
    calls = []
    monkeypatch.setattr(lp.LLMProvider, "get_completion", staticmethod(
        lambda model_id, messages, keys, **kw: calls.append(model_id) or {
            "content": "# Endorsed\n- x", "input_tokens": 10,
            "output_tokens": 1, "total_tokens": 11}))
    u = _openai_user("old_gpt", model="gpt-5.5")
    refused = 0
    for run in (it.start_infer_intentions_impl, it.run_infer_intentions_sync_impl):
        try:
            run(u.id)
        except RuntimeError as e:
            assert "another provider's model" in str(e)
            refused += 1
    sent = [r["model_id"] for by in wired for reqs in by.values() for r in reqs]
    sent += calls
    assert all(MODEL_PROVIDER[m] == "openai" for m in sent), sent
    assert len(sent) + refused == 2
    assert all(log.model_id != "claude-opus-5.5" for log in APICostLog.query.all())


# ── Sync runs and collected batches are saved and priced as what ran ──────

def test_sync_run_calls_and_prices_the_users_openai_model(app, wired, it, monkeypatch):  # noqa: F811
    import backend.llm_providers as lp
    calls = []
    monkeypatch.setattr(lp.LLMProvider, "get_completion", staticmethod(
        lambda model_id, messages, keys, **kw: calls.append((model_id, kw)) or {
            "content": "# Endorsed\n- Y", "total_tokens": 502_000,
            "input_tokens": 500_000, "output_tokens": 2_000}))
    u = _openai_user("oa_sync")
    stages = []
    saved = it.run_infer_intentions_sync_impl(u.id, progress=stages.append)
    assert calls == [("gpt-6-astra", {"max_tokens": 32000})]
    assert "gpt-6-astra" in stages[0]
    assert saved["model_id"] == "gpt-6-astra" and saved["version"] == 1
    log = APICostLog.query.filter_by(user_id=u.id).one()
    assert log.model_id == "gpt-6-astra"
    # Over 272k input: the whole request at 2x input and 1.5x output.
    assert log.cost_microdollars == int(500_000 * 20.0 + 2_000 * 75.0)
    art = UserArtifact.query.filter_by(user_id=u.id, kind="intentions").one()
    assert art.generated_by == "gpt-6-astra"
    assert wired == []


def test_collected_batch_is_priced_as_the_model_it_ran_on(app, wired, it):  # noqa: F811
    """The item's model, not the user's model at collect time: the user
    moved to Opus 5.5 while the GPT-6 Astra batch was out."""
    u = _openai_user("oa_collect")
    u.preferred_model = "claude-opus-5.5"
    _db.session.commit()
    saved = it.apply_intentions_item(u, _item(u, model_id="gpt-6-astra"), {
        "content": "# Endorsed\n- Z", "input_tokens": 500_000,
        "output_tokens": 2_000})
    assert saved["model_id"] == "gpt-6-astra"
    log = APICostLog.query.filter_by(user_id=u.id).one()
    assert log.model_id == "gpt-6-astra"
    assert log.cost_microdollars == int((500_000 * 20.0 + 2_000 * 75.0) * 0.5)
    assert UserArtifact.query.filter_by(
        user_id=u.id, kind="intentions").one().generated_by == "gpt-6-astra"


def test_a_refused_openai_result_logs_the_openai_model(app, wired, it):  # noqa: F811
    from backend.llm_providers import EmptyTruncatedOutputError
    u = _openai_user("oa_refused")
    with pytest.raises(EmptyTruncatedOutputError) as ei:
        it.apply_intentions_item(u, _item(u, model_id="gpt-6-astra"), {
            "content": "", "truncated": True,
            "input_tokens": 100_000, "output_tokens": 32_000})
    assert "model=gpt-6-astra" in str(ei.value)
    assert APICostLog.query.filter_by(user_id=u.id).one().model_id == "gpt-6-astra"


def test_a_resubmit_runs_on_the_users_model_now(app, wired, it, monkeypatch):  # noqa: F811
    """A failed item from the pinned days (no model_id, Opus 4.8) of a
    user on OpenAI: the one resubmit goes to their OpenAI model."""
    from datetime import datetime
    u = _openai_user("oa_resubmit")
    job = ProfileBatchJob(provider_key="anthropic", batch_id="b-old",
                          status="pending", items=[_item(u)],
                          submitted_at=datetime.utcnow())
    _db.session.add(job)
    _db.session.commit()
    monkeypatch.setattr(it, "_failed_item_tokens", lambda pk, bid, cid, keys: (None, None))
    it.handle_failed_intentions_item(u, job.items[0], job, {})
    assert list(wired[0]) == ["openai"]
    new = ProfileBatchJob.query.filter(ProfileBatchJob.batch_id != "b-old").one()
    assert new.provider_key == "openai:gpt-6-astra"
    assert new.items[0]["model_id"] == "gpt-6-astra"
    assert new.items[0]["resubmitted"] is True


# ── Limits ──────────────────────────────────────────────────────────────────

def test_token_limits(app, it):  # noqa: F811
    from backend.tests.test_intentions_task import MODELS
    app.config["SUPPORTED_MODELS"] = {
        **MODELS,
        "claude-opus-3": {"provider": "anthropic", "api_model": "claude-3-opus",
                          "context_window": 200000, "max_output_tokens": 4096}}
    assert it.token_limits("claude-opus-5.5") == (968_000, 32_000)
    # The provider's input ceiling, not the 272k price tier.
    assert it.token_limits("gpt-6-astra") == (922_000, 32_000)
    # A model with a smaller output cap keeps it.
    assert it.token_limits("claude-opus-3") == (195_904, 4096)


def test_model_input_cap_keeps_the_price_tier_by_default(app):  # noqa: F811
    from backend.llm_providers import model_input_cap
    cfg = {"context_window": 1050000, "max_input_tokens": 922000,
           "long_context_threshold": 272000}
    assert model_input_cap(cfg, 32000) == 272000
    assert model_input_cap(cfg, 32000, price_tier=False) == 922000


# ── backfill_intentions.py ──────────────────────────────────────────────────

@pytest.fixture
def script(monkeypatch):
    # backend.app builds the whole app at import; these checks need none of it.
    monkeypatch.setitem(sys.modules, "backend.app", MagicMock())
    monkeypatch.delitem(sys.modules, "backend.scripts.backfill_intentions",
                        raising=False)
    module = importlib.import_module("backend.scripts.backfill_intentions")
    monkeypatch.delitem(sys.modules, "backend.scripts.backfill_intentions")
    return module


def test_backfill_resolves_the_users_model(app, wired, script, capsys):  # noqa: F811
    oa = _openai_user("bf_openai")
    plain = _make_user("bf_plain")
    old_gpt = _openai_user("bf_old_gpt", model="gpt-5.5")
    assert script._resolve_model(app, oa, None) == "gpt-6-astra"
    assert script._resolve_model(app, plain, None) == "claude-opus-5.5"
    # --model of the same provider replaces it; another provider's skips.
    assert script._resolve_model(app, plain, "claude-opus-4.8") == "claude-opus-4.8"
    assert script._resolve_model(app, oa, "claude-opus-5.5") is None
    assert "another provider's" in capsys.readouterr().out
    # A retired OpenAI model: its own provider's replacement, or skipped.
    resolved = script._resolve_model(app, old_gpt, None)
    assert resolved is None or MODEL_PROVIDER[resolved] == "openai"


def test_backfill_sync_runs_and_prices_the_users_model(app, wired, script, monkeypatch):  # noqa: F811
    import backend.llm_providers as lp
    monkeypatch.setattr(script, "build_user_export_content",
                        lambda user, **kw: f"EXPORT[{kw.get('max_tokens')}]")
    monkeypatch.setattr(script, "_count_total_eligible_tokens", lambda uid: 1000)
    calls = []
    monkeypatch.setattr(lp.LLMProvider, "get_completion", staticmethod(
        lambda model_id, messages, keys, **kw: calls.append((model_id, kw)) or {
            "content": "# Endorsed\n- B", "total_tokens": 101_000,
            "input_tokens": 100_000, "output_tokens": 1_000}))
    u = _openai_user("bf_sync")
    template = "Read this.\n{user_export?keep=newest&max_export_tokens=1000000}"
    script.backfill_user(app, u, template, script._resolve_model(app, u, None))
    assert calls == [("gpt-6-astra", {"max_tokens": 32000})]
    log = APICostLog.query.filter_by(user_id=u.id).one()
    assert log.model_id == "gpt-6-astra" and log.request_type == "intentions_backfill"
    assert log.cost_microdollars == int(100_000 * 10.0 + 1_000 * 50.0)


def test_backfill_batch_request_carries_the_output_cap(app, wired, script, monkeypatch):  # noqa: F811
    sent = []
    monkeypatch.setattr(script, "batch_submit", lambda by_provider, keys, kind: (
        sent.append(by_provider) or {"openai:gpt-6-astra": "b-1"}))
    monkeypatch.setattr(script, "build_user_export_content",
                        lambda user, **kw: "EXPORT")
    u = _openai_user("bf_batch")
    template = "Read this.\n{user_export?keep=newest&max_export_tokens=1000000}"
    pending = {}
    script._submit_batch_for(app, u, template, "gpt-6-astra", 1000, False,
                             {}, 1000, pending, "full-cap")
    req = sent[0]["openai"][0]
    assert req["model_id"] == "gpt-6-astra" and req["max_tokens"] == 32000
    assert pending[u.id]["model_id"] == "gpt-6-astra"

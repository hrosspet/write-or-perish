"""Admin "Infer intentions" (batch-first): count-based sizing/submit,
collect, saving, and the pinned-model / opt-out guards."""


import pytest

from backend.tests.test_twitter_import import app, _make_user, _db  # noqa: F401
from backend.models import UserArtifact, APICostLog, ProfileBatchJob

# Imported lazily in the `wired` fixture: an eager import at collection time
# trips over cross-file celery-mock ordering in the full suite (same pattern
# as test_profile_batch).
it = None


MODELS = {"claude-opus-4.8": {"provider": "anthropic", "api_model": "claude-opus-4-8",
                              "context_window": 1000000,
                              "input_price_per_mtok": 5.0, "output_price_per_mtok": 25.0}}


@pytest.fixture
def wired(app, monkeypatch, tmp_path):  # noqa: F811
    global it
    import backend.tasks.intentions as _it
    it = _it
    app.config["SUPPORTED_MODELS"] = MODELS
    import backend.routes.export_data as ed
    import backend.utils.llm_batch as lb
    import backend.tasks.recent_context as rc
    monkeypatch.setattr(ed, "build_user_export_content",
                        lambda user, **kw: f"EXPORT[{kw.get('max_tokens')}]")
    submitted = []
    monkeypatch.setattr(lb, "batch_submit", lambda by_provider, keys, kind: (
        submitted.append(by_provider) or {"anthropic": f"b-{len(submitted)}"}))
    monkeypatch.setattr(lb, "apply_batch_key_override", lambda keys, cfg: keys)
    monkeypatch.setattr(rc, "_count_total_eligible_tokens", lambda uid: 1000)
    # Default: the counted prompt fits the context comfortably.
    import backend.llm_providers as lp
    monkeypatch.setattr(lp.LLMProvider, "count_tokens",
                        staticmethod(lambda model_id, messages, keys: 500_000))
    # The sizing loop must never fall back to a billed completion.
    monkeypatch.setattr(lp.LLMProvider, "get_completion", staticmethod(
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("sync completion must not be called"))))
    return submitted


def test_small_corpus_submits_batch_directly_and_persists_job(app, wired, monkeypatch):  # noqa: F811
    u = _make_user("alice")
    _db.session.commit()
    kind, ref = it.start_infer_intentions_impl(u.id)
    assert kind == "batch" and ref["batch_id"] == "b-1"
    req = wired[0]["anthropic"][0]
    assert req["model_id"] == "claude-opus-4.8" and req["api_model"] == "claude-opus-4-8"
    assert "EXPORT[" in req["messages"][0]["content"][0]["text"]
    # The flight is persisted — a worker restart loses nothing.
    job = ProfileBatchJob.query.filter_by(batch_id="b-1", status="pending").one()
    assert job.items == [{"custom_id": f"int-u{u.id}", "user_id": u.id,
                          "kind": "intentions", "budget": 1000000, "resubmitted": False}]


def test_oversized_count_shrinks_budget_then_submits(app, wired, monkeypatch):  # noqa: F811
    """Sizing via the free count endpoint: an over-limit count shrinks the
    export budget by the real ratio (against min(budget, corpus)) and the
    rebuilt prompt is submitted — no billed probe, no batch round-trip."""
    import backend.tasks.recent_context as rc
    import backend.llm_providers as lp
    monkeypatch.setattr(rc, "_count_total_eligible_tokens", lambda uid: 900_000)
    counts = iter([1_400_000, 800_000])
    monkeypatch.setattr(lp.LLMProvider, "count_tokens",
                        staticmethod(lambda model_id, messages, keys: next(counts)))
    u = _make_user("bob")
    _db.session.commit()
    kind, ref = it.start_infer_intentions_impl(u.id)
    assert kind == "batch"
    budget = ref["item"]["budget"]
    # min(1M, 900k corpus) * (1M - 8192)/1.4M * 0.99 ≈ 631k
    assert budget is not None and 600_000 < budget < 700_000
    assert APICostLog.query.count() == 0  # counting is free — nothing billed


def test_count_unavailable_falls_back_to_full_cap_batch(app, wired, monkeypatch):  # noqa: F811
    """A None count (endpoint down, tiktoken missing) submits at the full
    cap — the poller's overflow-resubmit backstop handles any overflow."""
    import backend.llm_providers as lp
    monkeypatch.setattr(lp.LLMProvider, "count_tokens",
                        staticmethod(lambda model_id, messages, keys: None))
    u = _make_user("carol")
    _db.session.commit()
    kind, ref = it.start_infer_intentions_impl(u.id)
    assert kind == "batch" and ref["item"]["budget"] == 1_000_000


def test_sizing_that_never_converges_raises(app, wired, monkeypatch):  # noqa: F811
    import backend.llm_providers as lp
    monkeypatch.setattr(lp.LLMProvider, "count_tokens",
                        staticmethod(lambda model_id, messages, keys: 2_000_000))
    u = _make_user("noel")
    _db.session.commit()
    with pytest.raises(lp.PromptTooLongError):
        it.start_infer_intentions_impl(u.id)
    assert wired == []  # nothing submitted


def test_fit_by_count_shrinks_and_reuses_first_built(app, monkeypatch):  # noqa: F811
    """The shared sizing helper: reuses first_built (no duplicate build),
    shrinks against min(budget, corpus), returns the rebuilt result."""
    import backend.llm_providers as lp
    counts = iter([400_000, 100_000])
    monkeypatch.setattr(lp.LLMProvider, "count_tokens",
                        staticmethod(lambda m, msgs, k: next(counts)))
    builds = []

    def build(budget):
        builds.append(budget)
        return ([{"role": "user", "content": [
            {"type": "text", "text": f"P[{budget}]"}]}], budget)

    built, budget, real = lp.fit_by_count(
        "claude-opus-4.8", {}, 192_000, 90_000, build,
        first_built=([{"role": "user", "content": [
            {"type": "text", "text": "P[first]"}]}], "first"),
        max_rounds=3, min_budget=5000)
    # first_built counted (over) -> one rebuild at the shrunk budget
    assert builds == [pytest.approx(90_000 * 192_000 / 400_000 * 0.99, abs=2)]
    assert built[1] == builds[0] and budget == builds[0]
    assert real == 100_000


def test_fit_by_count_none_count_returns_first(app, monkeypatch):  # noqa: F811
    import backend.llm_providers as lp
    monkeypatch.setattr(lp.LLMProvider, "count_tokens",
                        staticmethod(lambda m, msgs, k: None))
    built, budget, real = lp.fit_by_count(
        "claude-opus-4.8", {}, 192_000, 90_000,
        lambda b: ([{"role": "user", "content": [
            {"type": "text", "text": "P"}]}], b))
    assert built is not None and budget == 90_000 and real is None


def test_apply_intentions_item_saves_at_batch_price(app, wired):  # noqa: F811
    u = _make_user("dave")
    _db.session.commit()
    item = {"custom_id": f"int-u{u.id}", "user_id": u.id, "kind": "intentions",
            "budget": 1_000_000, "resubmitted": False}
    saved = it.apply_intentions_item(u, item, {"content": "# Endorsed\nX",
                                               "input_tokens": 100_000, "output_tokens": 1_000})
    assert saved["batch"] is True and saved["version"] == 1
    log = APICostLog.query.filter_by(user_id=u.id, request_type="intentions_infer").one()
    assert log.cost_microdollars == int((100_000 * 5.0 + 1_000 * 25.0) * 0.5)  # batch = 50%
    assert UserArtifact.query.filter_by(user_id=u.id, kind="intentions").count() == 1


def _job_for(item):
    from datetime import datetime
    j = ProfileBatchJob(provider_key="anthropic", batch_id="b-old",
                        status="pending", items=[item], submitted_at=datetime.utcnow())
    _db.session.add(j)
    _db.session.commit()
    return j


def test_failed_item_resubmits_calibrated_from_corpus_not_budget(app, wired, monkeypatch):  # noqa: F811
    """User-110 regression: the corpus (560k DB units) was smaller than the
    1M budget, so scaling the budget re-rendered the identical export. The
    calibration must scale min(budget, corpus) by the real ratio."""
    import backend.tasks.recent_context as rc
    monkeypatch.setattr(rc, "_count_total_eligible_tokens", lambda uid: 560_000)
    u = _make_user("erin")
    _db.session.commit()
    item = {"custom_id": f"int-u{u.id}", "user_id": u.id, "kind": "intentions",
            "budget": 1_000_000, "resubmitted": False}
    job = _job_for(item)
    monkeypatch.setattr(it, "_failed_item_tokens", lambda pk, bid, cid, keys: (1_496_460, 1_000_000))
    it.handle_failed_intentions_item(u, item, job, {})
    new = ProfileBatchJob.query.filter(ProfileBatchJob.batch_id != "b-old").one()
    assert new.items[0]["resubmitted"] is True
    # min(1M, 560k) * (1M / 1.49646M) * 0.99 ≈ 370k — STRICTLY below the corpus,
    # so the export actually shrinks this time.
    assert 350_000 < new.items[0]["budget"] < 560_000
    # A failure of the resubmitted item gives up (no third job) and the
    # give-up is persisted on the item for the admin column.
    it.handle_failed_intentions_item(u, new.items[0], new, {})
    assert ProfileBatchJob.query.count() == 2
    assert ProfileBatchJob.query.get(new.id).items[0]["gave_up"] is True


def test_failed_item_falls_back_to_70pct_of_corpus_without_counts(app, wired, monkeypatch):  # noqa: F811
    import backend.tasks.recent_context as rc
    monkeypatch.setattr(rc, "_count_total_eligible_tokens", lambda uid: 560_000)
    u = _make_user("gita")
    _db.session.commit()
    item = {"custom_id": f"int-u{u.id}", "user_id": u.id, "kind": "intentions",
            "budget": 1_000_000, "resubmitted": False}
    job = _job_for(item)
    monkeypatch.setattr(it, "_failed_item_tokens", lambda pk, bid, cid, keys: (None, None))
    it.handle_failed_intentions_item(u, item, job, {})
    new = ProfileBatchJob.query.filter(ProfileBatchJob.batch_id != "b-old").one()
    assert new.items[0]["budget"] == int(560_000 * 0.7)


def test_opted_out_user_refused(app, wired):  # noqa: F811
    u = _make_user("frank")
    u.default_ai_usage = "none"
    _db.session.commit()
    with pytest.raises(RuntimeError, match="opted out"):
        it.start_infer_intentions_impl(u.id)


# ── Sync mode ("run now") + cancel ──────────────────────────────────────────

def test_sync_mode_calls_model_once_and_saves_at_full_price(app, wired, monkeypatch):  # noqa: F811
    import backend.llm_providers as lp
    calls = []
    monkeypatch.setattr(lp.LLMProvider, "get_completion", staticmethod(
        lambda model_id, messages, keys, **kw: (calls.append((model_id, kw)) or {
            "content": "# Endorsed\nY", "total_tokens": 101_000,
            "input_tokens": 100_000, "output_tokens": 1_000})))
    u = _make_user("hana")
    _db.session.commit()
    stages = []
    saved = it.run_infer_intentions_sync_impl(u.id, progress=stages.append)
    assert saved["batch"] is False and saved["version"] == 1
    assert calls == [("claude-opus-4.8", {"max_tokens": it.BATCH_OUTPUT_TOKENS})]
    assert stages and "synchronously" in stages[0]
    log = APICostLog.query.filter_by(user_id=u.id, request_type="intentions_infer").one()
    assert log.cost_microdollars == int(100_000 * 5.0 + 1_000 * 25.0)  # full price
    assert UserArtifact.query.filter_by(user_id=u.id, kind="intentions").count() == 1
    assert wired == []                                # nothing submitted
    assert ProfileBatchJob.query.count() == 0         # nothing persisted


def test_sync_mode_retries_smaller_on_prompt_too_long(app, wired, monkeypatch):  # noqa: F811
    import backend.llm_providers as lp
    budgets = []

    def completion(model_id, messages, keys, **kw):
        text = messages[0]["content"][0]["text"]
        budgets.append(text)
        if len(budgets) == 1:
            raise lp.PromptTooLongError(1_200_000, 1_000_000)
        return {"content": "ok", "total_tokens": 10, "input_tokens": 9, "output_tokens": 1}
    monkeypatch.setattr(lp.LLMProvider, "get_completion", staticmethod(completion))
    u = _make_user("ivan")
    _db.session.commit()
    saved = it.run_infer_intentions_sync_impl(u.id)
    assert saved["version"] == 1 and len(budgets) == 2
    # The retry rebuilt the export at a smaller budget than the first call.
    first = int(budgets[0].split("EXPORT[")[1].split("]")[0])
    second = int(budgets[1].split("EXPORT[")[1].split("]")[0])
    assert second < first


def test_sync_mode_refuses_opted_out_user(app, wired):  # noqa: F811
    u = _make_user("jo")
    u.default_ai_usage = "none"
    _db.session.commit()
    with pytest.raises(RuntimeError, match="opted out"):
        it.run_infer_intentions_sync_impl(u.id)


def test_cancel_marks_job_and_calls_provider(app, wired, monkeypatch):  # noqa: F811
    u = _make_user("kim")
    _db.session.commit()
    item = {"custom_id": f"int-u{u.id}", "user_id": u.id, "kind": "intentions",
            "budget": 1_000_000, "resubmitted": False}
    job = _job_for(item)
    cancelled = []
    monkeypatch.setattr(it, "_provider_cancel",
                        lambda pk, bid, keys: cancelled.append((pk, bid)) or None)
    out = it.cancel_pending_intentions(u.id)
    assert out == {"cancelled": 1, "errors": []}
    assert cancelled == [("anthropic", "b-old")]
    job = ProfileBatchJob.query.get(job.id)
    assert job.status == "cancelled" and job.collected_at is not None
    assert job.items[0]["cancelled"] is True
    # Idempotent: nothing left to cancel.
    assert it.cancel_pending_intentions(u.id) == {"cancelled": 0, "errors": []}
    assert it.pending_intentions_jobs(u.id) == []


def test_cancel_marks_job_even_when_provider_cancel_fails(app, wired, monkeypatch):  # noqa: F811
    u = _make_user("lea")
    _db.session.commit()
    job = _job_for({"custom_id": f"int-u{u.id}", "user_id": u.id, "kind": "intentions",
                    "budget": 1_000_000, "resubmitted": False})
    monkeypatch.setattr(it, "_provider_cancel", lambda pk, bid, keys: "boom")
    out = it.cancel_pending_intentions(u.id)
    assert out["cancelled"] == 1 and out["errors"] == ["b-old: boom"]
    assert ProfileBatchJob.query.get(job.id).status == "cancelled"


def test_cancel_never_touches_profile_jobs_or_other_users(app, wired, monkeypatch):  # noqa: F811
    u = _make_user("mia")
    other = _make_user("noa")
    _db.session.commit()
    from datetime import datetime
    _db.session.add(ProfileBatchJob(
        provider_key="anthropic", batch_id="b-profile", status="pending",
        items=[{"custom_id": f"u{u.id}-c1", "user_id": u.id, "kind": "chunk"}],
        submitted_at=datetime.utcnow()))
    _db.session.add(ProfileBatchJob(
        provider_key="anthropic", batch_id="b-other", status="pending",
        items=[{"custom_id": f"int-u{other.id}", "user_id": other.id, "kind": "intentions"}],
        submitted_at=datetime.utcnow()))
    _db.session.commit()
    monkeypatch.setattr(it, "_provider_cancel", lambda pk, bid, keys: None)
    assert it.cancel_pending_intentions(u.id) == {"cancelled": 0, "errors": []}
    assert ProfileBatchJob.query.filter_by(status="pending").count() == 2


def test_poller_skips_job_cancelled_mid_poll(app, wired, monkeypatch):  # noqa: F811
    """The provider round-trip in the poller can overlap an admin cancel:
    the refreshed row must be skipped — no collect, no overflow-resubmit."""
    import backend.tasks.profile_batch as pb
    u = _make_user("oli")
    _db.session.commit()
    job = _job_for({"custom_id": f"int-u{u.id}", "user_id": u.id, "kind": "intentions",
                    "budget": 1_000_000, "resubmitted": False})

    def check_and_cancel_meanwhile(batch_ids, keys):
        # Simulate the admin clicking Cancel while the provider call runs.
        from backend.extensions import db
        row = ProfileBatchJob.query.get(job.id)
        row.status = "cancelled"
        db.session.commit()
        return {}, {}, {}  # ended, item failed → would otherwise resubmit
    monkeypatch.setattr(pb, "batch_check_and_collect", check_and_cancel_meanwhile)
    monkeypatch.setattr(pb, "get_api_keys_for_usage", lambda cfg, usage: {})
    monkeypatch.setattr(pb, "apply_batch_key_override", lambda keys, cfg: keys)
    resubmits = []
    monkeypatch.setattr(it, "handle_failed_intentions_item",
                        lambda *a, **k: resubmits.append(a))
    pb._poll_profile_batches()
    assert resubmits == []
    assert ProfileBatchJob.query.get(job.id).status == "cancelled"
    assert wired == []

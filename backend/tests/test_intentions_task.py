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
    with pytest.raises(RuntimeError, match="sizing rounds"):
        it.start_infer_intentions_impl(u.id)
    assert wired == []  # nothing submitted


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

"""Admin "Infer intentions" (batch-first): sizing/probe/submit, collect,
saving, and the pinned-model / opt-out guards."""


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


def test_large_corpus_probes_then_calibrates(app, wired, monkeypatch):  # noqa: F811
    import backend.tasks.recent_context as rc
    import backend.llm_providers as lp
    monkeypatch.setattr(rc, "_count_total_eligible_tokens", lambda uid: 900_000)

    def too_long(model_id, messages, keys):
        raise lp.PromptTooLongError(actual_tokens=1_400_000, max_tokens=1_000_000)

    monkeypatch.setattr(lp.LLMProvider, "get_completion", staticmethod(too_long))
    u = _make_user("bob")
    _db.session.commit()
    kind, ref = it.start_infer_intentions_impl(u.id)
    assert kind == "batch"
    budget = ref["item"]["budget"]
    assert budget is not None and budget < 1_000_000  # calibrated below the cap


def test_probe_that_fits_saves_sync_full_price(app, wired, monkeypatch):  # noqa: F811
    import backend.tasks.recent_context as rc
    import backend.llm_providers as lp
    monkeypatch.setattr(rc, "_count_total_eligible_tokens", lambda uid: 900_000)
    monkeypatch.setattr(lp.LLMProvider, "get_completion", staticmethod(
        lambda model_id, messages, keys: {"content": "# Endorsed\\n...",
                                          "input_tokens": 500_000, "output_tokens": 2_000,
                                          "total_tokens": 502_000}))
    u = _make_user("carol")
    _db.session.commit()
    kind, result = it.start_infer_intentions_impl(u.id)
    assert kind == "done" and result["version"] == 1 and result["batch"] is False
    log = APICostLog.query.filter_by(user_id=u.id, request_type="intentions_infer").one()
    assert log.model_id == "claude-opus-4.8"
    assert log.cost_microdollars == int(500_000 * 5.0 + 2_000 * 25.0)  # full price, no long-context tier
    art = UserArtifact.query.filter_by(user_id=u.id, kind="intentions").one()
    assert art.generated_by == "claude-opus-4.8" and art.get_content().startswith("# Endorsed")
    assert wired == []  # no batch submitted


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


def test_failed_item_resubmits_calibrated_from_real_count(app, wired, monkeypatch):  # noqa: F811
    u = _make_user("erin")
    _db.session.commit()
    item = {"custom_id": f"int-u{u.id}", "user_id": u.id, "kind": "intentions",
            "budget": 1_000_000, "resubmitted": False}
    job = _job_for(item)
    monkeypatch.setattr(it, "_failed_item_tokens", lambda pk, bid, cid, keys: (2_300_000, 1_000_000))
    it.handle_failed_intentions_item(u, item, job, {})
    new = ProfileBatchJob.query.filter(ProfileBatchJob.batch_id != "b-old").one()
    assert new.items[0]["resubmitted"] is True
    assert new.items[0]["budget"] < 500_000  # scaled by the real overflow ratio, not 70%
    # A failure of the resubmitted item gives up (no third job).
    it.handle_failed_intentions_item(u, new.items[0], new, {})
    assert ProfileBatchJob.query.count() == 2


def test_failed_item_falls_back_to_70pct_without_counts(app, wired, monkeypatch):  # noqa: F811
    u = _make_user("gita")
    _db.session.commit()
    item = {"custom_id": f"int-u{u.id}", "user_id": u.id, "kind": "intentions",
            "budget": 1_000_000, "resubmitted": False}
    job = _job_for(item)
    monkeypatch.setattr(it, "_failed_item_tokens", lambda pk, bid, cid, keys: (None, None))
    it.handle_failed_intentions_item(u, item, job, {})
    new = ProfileBatchJob.query.filter(ProfileBatchJob.batch_id != "b-old").one()
    assert new.items[0]["budget"] == 700_000


def test_prefilled_account_estimate_scaled_into_probe(app, wired, monkeypatch):  # noqa: F811
    """A 20k-tweet CA pre-fill: DB estimate ~200k (chars/4) is really ~600k+
    — the x3 tweet scaling must push it over the probe threshold instead of
    submitting a full-cap batch that overflows (user 110, 2026-08-31)."""
    import backend.tasks.recent_context as rc
    import backend.llm_providers as lp
    monkeypatch.setattr(rc, "_count_total_eligible_tokens", lambda uid: 200_000)
    monkeypatch.setattr(lp.LLMProvider, "get_completion", staticmethod(
        lambda model_id, messages, keys: (_ for _ in ()).throw(
            lp.PromptTooLongError(actual_tokens=1_400_000, max_tokens=1_000_000))))
    u = _make_user("hana")
    u.prefilled_handle = "hana"
    _db.session.commit()
    kind, ref = it.start_infer_intentions_impl(u.id)
    assert kind == "batch" and ref["item"]["budget"] < 1_000_000  # probed + calibrated
    o = _make_user("iris")  # organic account, same estimate: no scaling, direct batch
    _db.session.commit()
    kind, ref = it.start_infer_intentions_impl(o.id)
    assert kind == "batch" and ref["item"]["budget"] == 1_000_000


def test_opted_out_user_refused(app, wired):  # noqa: F811
    u = _make_user("frank")
    u.default_ai_usage = "none"
    _db.session.commit()
    with pytest.raises(RuntimeError, match="opted out"):
        it.start_infer_intentions_impl(u.id)

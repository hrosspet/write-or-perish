"""Admin "Infer intentions" (batch-first): sizing/probe/submit, collect,
saving, and the pinned-model / opt-out guards."""


import pytest

from backend.tests.test_twitter_import import app, _make_user, _db  # noqa: F401
from backend.models import UserArtifact, APICostLog

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


def test_small_corpus_submits_batch_directly(app, wired, monkeypatch):  # noqa: F811
    u = _make_user("alice")
    _db.session.commit()
    kind, ref = it.start_infer_intentions_impl(u.id)
    assert kind == "batch" and ref["batch_id"] == "b-1"
    assert ref["custom_id"] == f"int-u{u.id}" and ref["resubmitted"] is False
    req = wired[0]["anthropic"][0]
    assert req["model_id"] == "claude-opus-4.8" and req["api_model"] == "claude-opus-4-8"
    assert "EXPORT[" in req["messages"][0]["content"][0]["text"]


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
    assert kind == "batch" and ref["budget"] is not None
    assert ref["budget"] < 1_000_000  # calibrated below the prompt cap


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


def test_collect_pending_then_saves_at_batch_price(app, wired, monkeypatch):  # noqa: F811
    import backend.utils.llm_batch as lb
    u = _make_user("dave")
    _db.session.commit()
    ref = {"provider_key": "anthropic", "batch_id": "b-9",
           "custom_id": f"int-u{u.id}", "budget": 1_000_000, "resubmitted": False}
    monkeypatch.setattr(lb, "batch_check_and_collect",
                        lambda bids, keys: ({}, {"anthropic": "b-9"}, {}))
    assert it.collect_intentions_impl(u.id, ref) is None
    monkeypatch.setattr(lb, "batch_check_and_collect", lambda bids, keys: (
        {ref["custom_id"]: {"content": "# Endorsed\\nX", "input_tokens": 100_000,
                            "output_tokens": 1_000}}, {}, {}))
    kind, result = it.collect_intentions_impl(u.id, ref)
    assert kind == "done" and result["batch"] is True
    log = APICostLog.query.filter_by(user_id=u.id, request_type="intentions_infer").one()
    assert log.cost_microdollars == int((100_000 * 5.0 + 1_000 * 25.0) * 0.5)  # batch = 50%
    assert UserArtifact.query.filter_by(user_id=u.id, kind="intentions").count() == 1


def test_collect_failed_item_raises(app, wired, monkeypatch):  # noqa: F811
    import backend.utils.llm_batch as lb
    u = _make_user("erin")
    _db.session.commit()
    ref = {"provider_key": "anthropic", "batch_id": "b-9",
           "custom_id": f"int-u{u.id}", "budget": 1_000_000, "resubmitted": False}
    monkeypatch.setattr(lb, "batch_check_and_collect", lambda bids, keys: ({}, {}, {}))
    with pytest.raises(RuntimeError, match="batch ended without a result"):
        it.collect_intentions_impl(u.id, ref)


def test_opted_out_user_refused(app, wired):  # noqa: F811
    u = _make_user("frank")
    u.default_ai_usage = "none"
    _db.session.commit()
    with pytest.raises(RuntimeError, match="opted out"):
        it.start_infer_intentions_impl(u.id)

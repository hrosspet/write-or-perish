"""Tests for the nightly external-references digest rebuild.

The digest is ONE LLM call over the user's whole saved corpus, so it must
fire once a night per user — not once per saved reference. Riding
individual saves cost $26 in a single day of clipping (30 rebuilds x
~77k input tokens on a flagship model), which is what this gate prevents.

Covers: the staleness definition, the direct path's not-stale skip and
corpus-state timestamp, the nightly batch submit (own-night gate, one
request however many references arrived, no double-submit while a batch
is pending) and the collector (batch pricing, corpus-state timestamp,
failed items left stale, stuck batches abandoned). LLM/batch/celery glue
stubbed.
"""
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import MagicMock

os.environ["ENCRYPTION_DISABLED"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("SECRET_KEY", "test-secret")

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
    APICostLog, ExternalDigestBatchJob, ExternalItem, User, UserArtifact,
)


def _make_app():
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["ANTHROPIC_API_KEY"] = "fake-key"
    app.config["DEFAULT_LLM_MODEL"] = "claude-opus-5"
    app.config["SUPPORTED_MODELS"] = {
        "claude-opus-5": {
            "provider": "anthropic",
            "api_model": "claude-opus-5-20260101",
            "input_price_per_mtok": 5.0,
            "output_price_per_mtok": 25.0,
        },
        "gpt-6-astra": {
            "provider": "openai",
            "api_model": "gpt-6-astra",
            "input_price_per_mtok": 10.0,
            "output_price_per_mtok": 50.0,
        },
    }
    _db.init_app(app)
    return app


# Import the REAL digest module against an identity-decorator celery stub
# and our test flask_app (mirrors test_bookmark_nightly_sync.py).
_app = _make_app()
_celery_stub = MagicMock()
_celery_stub.celery.task = lambda *a, **k: (lambda fn: fn)
_celery_stub.flask_app = _app


def _import_real_digest_module():
    import importlib
    glue = ("backend.celery_app", "backend.tasks.external_digest")
    saved = {k: sys.modules.get(k) for k in glue}
    sys.modules["backend.celery_app"] = _celery_stub
    sys.modules.pop("backend.tasks.external_digest", None)
    try:
        mod = importlib.import_module("backend.tasks.external_digest")
        if isinstance(mod, MagicMock):
            sys.modules.pop("backend.tasks.external_digest", None)
            mod = importlib.import_module("backend.tasks.external_digest")
        return mod
    finally:
        for _k, _v in saved.items():
            if _v is None:
                sys.modules.pop(_k, None)
            else:
                sys.modules[_k] = _v


_digest = _import_real_digest_module()
assert not isinstance(_digest, MagicMock)


class _FakeSelf:
    def update_state(self, *args, **kwargs):
        pass

    def retry(self, exc=None, **kwargs):
        raise exc or AssertionError("retry")


@pytest.fixture
def app():
    saved_flask_app = _digest.flask_app
    _digest.flask_app = _app
    with _app.app_context():
        _db.create_all()
        user = User(username="tester")
        _db.session.add(user)
        _db.session.commit()
        yield _app
        _db.session.remove()
        _db.drop_all()
    _digest.flask_app = saved_flask_app


def _tz_at_hour(target_hour):
    """An IANA zone whose CURRENT local hour == target_hour, built from the
    fixed-offset Etc/GMT zones (POSIX-inverted signs: Etc/GMT+5 = UTC-5)."""
    x = (datetime.utcnow().hour - target_hour) % 24
    return f"Etc/GMT+{x}" if x <= 12 else f"Etc/GMT-{24 - x}"


def _mk_item(user_id, external_id="1", fetched_at=None):
    item = ExternalItem(
        user_id=user_id, source="web_clip", external_id=external_id,
        url=f"https://example.com/{external_id}", title="A page",
        fetched_at=fetched_at or datetime.utcnow(),
    )
    item.set_content("something worth returning to")
    _db.session.add(item)
    _db.session.commit()
    return item


def _mk_digest(user_id, created_at=None):
    artifact = UserArtifact(
        user_id=user_id, kind=_digest.DIGEST_KIND,
        title=_digest.DIGEST_TITLE, generated_by="claude-opus-5",
        created_at=created_at or datetime.utcnow(),
    )
    artifact.set_content("# Topics")
    _db.session.add(artifact)
    _db.session.commit()
    return artifact


def _stub_llm(monkeypatch, on_call=None):
    calls = []

    def fake_completion(model_id, messages, api_keys, **kwargs):
        calls.append({"model_id": model_id, "messages": messages})
        if on_call is not None:
            on_call()
        return {"content": "# Topics\n- one", "input_tokens": 100,
                "output_tokens": 20}
    monkeypatch.setattr(_digest.LLMProvider, "get_completion",
                        staticmethod(fake_completion))
    return calls


def test_digest_is_stale_only_while_the_corpus_is_ahead(app):
    uid = User.query.first().id
    assert _digest.digest_is_stale(uid) is False  # nothing saved yet

    _mk_item(uid, "a", fetched_at=datetime.utcnow() - timedelta(hours=2))
    assert _digest.digest_is_stale(uid) is True  # never digested

    _mk_digest(uid, created_at=datetime.utcnow() - timedelta(hours=1))
    assert _digest.digest_is_stale(uid) is False

    _mk_item(uid, "b")  # a fresh clip
    assert _digest.digest_is_stale(uid) is True


def test_rebuild_skips_a_current_digest_without_calling_the_model(
        app, monkeypatch):
    """The expensive call is the whole point of the gate: a re-dispatch
    for an unchanged corpus must cost nothing."""
    uid = User.query.first().id
    _mk_item(uid, "a", fetched_at=datetime.utcnow() - timedelta(hours=2))
    _mk_digest(uid, created_at=datetime.utcnow() - timedelta(hours=1))
    calls = _stub_llm(monkeypatch)

    assert _digest.rebuild_external_digest(
        _FakeSelf(), uid) == {"status": "not_stale"}
    assert calls == []
    assert APICostLog.query.count() == 0

    # force=True is the escape hatch for a manual rebuild.
    result = _digest.rebuild_external_digest(_FakeSelf(), uid, force=True)
    assert result["status"] == "ok"
    assert len(calls) == 1


def test_rebuild_uses_the_configured_default_model(app, monkeypatch):
    """Users without a preferred_model fall back to DEFAULT_LLM_MODEL —
    the config key. (LLM_NAME is only the env var it reads from, and
    asking config for it yielded None, i.e. no model at all.)"""
    uid = User.query.first().id
    _mk_item(uid, "a")
    calls = _stub_llm(monkeypatch)

    result = _digest.rebuild_external_digest(_FakeSelf(), uid)
    assert result["status"] == "ok"
    assert calls[0]["model_id"] == "claude-opus-5"
    artifact = UserArtifact.latest_for(uid, _digest.DIGEST_KIND)
    assert artifact.generated_by == "claude-opus-5"
    assert APICostLog.query.one().request_type == "external_digest"


def test_rebuild_stamps_the_corpus_state_not_the_finish_time(
        app, monkeypatch):
    """An item saved DURING a rebuild isn't in that digest, so it must
    still read as stale afterwards — otherwise it is never digested."""
    uid = User.query.first().id
    _mk_item(uid, "a")
    _stub_llm(monkeypatch, on_call=lambda: _mk_item(uid, "mid-flight"))

    assert _digest.rebuild_external_digest(_FakeSelf(), uid)["status"] == "ok"
    assert _digest.digest_is_stale(uid) is True


def _stub_batch_submit(monkeypatch, batch_id="batch_1"):
    """batch_submit stand-in: records what was submitted and answers with
    one batch id per provider key, the way llm_batch keys them."""
    submitted = []

    def fake_submit(requests_by_provider, api_keys, phase=None):
        submitted.append(requests_by_provider)
        ids = {}
        for provider, reqs in requests_by_provider.items():
            if provider == "anthropic":
                ids["anthropic"] = f"{batch_id}-anthropic"
            else:
                for req in reqs:
                    key = f"openai:{req['api_model']}"
                    ids[key] = f"{batch_id}-{req['api_model']}"
        return ids
    monkeypatch.setattr(_digest, "batch_submit", fake_submit)
    return submitted


def test_nightly_sweep_submits_one_batch_request_per_stale_user_in_their_night(
        app, monkeypatch):
    """However many references arrived during the day, the user's corpus
    is one batch request — only while it is night where they are, and
    never while a batch of theirs is still pending."""
    night_tz = _tz_at_hour(_digest.NIGHTLY_DIGEST_LOCAL_HOUR)
    day_tz = _tz_at_hour((_digest.NIGHTLY_DIGEST_LOCAL_HOUR + 12) % 24)

    night_user = User.query.first()
    night_user.timezone = night_tz
    day_user = User(username="daytime", timezone=day_tz)
    current_user_ = User(username="unchanged", timezone=night_tz)
    quiet_user = User(username="no-references", timezone=night_tz)
    in_flight_user = User(username="already-submitted", timezone=night_tz)
    oai_user = User(username="openai-user", timezone=night_tz,
                    preferred_model="gpt-6-astra")
    _db.session.add_all([day_user, current_user_, quiet_user,
                         in_flight_user, oai_user])
    _db.session.commit()

    for i in range(20):  # a day of clipping
        _mk_item(night_user.id, f"clip-{i}")
    _mk_item(day_user.id, "d1")
    _mk_item(current_user_.id, "c1",
             fetched_at=datetime.utcnow() - timedelta(hours=3))
    _mk_digest(current_user_.id,
               created_at=datetime.utcnow() - timedelta(hours=2))
    _mk_item(in_flight_user.id, "f1")
    _db.session.add(ExternalDigestBatchJob(
        provider_key="anthropic", batch_id="older", items=[{
            "custom_id": f"external-digest-{in_flight_user.id}",
            "user_id": in_flight_user.id, "model_id": "claude-opus-5",
            "corpus_at": datetime.utcnow().isoformat(), "total_items": 1,
        }]))
    _mk_item(oai_user.id, "o1")
    _db.session.commit()

    submitted = _stub_batch_submit(monkeypatch)
    result = _digest.sweep_external_digests()
    assert result == {"status": "ok", "submitted": 2}

    # One submit call, grouped by provider, one request per user.
    assert len(submitted) == 1
    by_provider = submitted[0]
    assert [r["custom_id"] for r in by_provider["anthropic"]] == [
        f"external-digest-{night_user.id}"]
    assert [r["custom_id"] for r in by_provider["openai"]] == [
        f"external-digest-{oai_user.id}"]
    prompt = by_provider["anthropic"][0]["messages"][0]["content"]
    assert "The corpus holds 20 saved items" in prompt
    assert by_provider["openai"][0]["api_model"] == "gpt-6-astra"

    # Each provider batch persisted with its own routing metadata.
    jobs = {j.provider_key: j for j in ExternalDigestBatchJob.query.filter_by(
        status="pending").all() if j.batch_id != "older"}
    assert set(jobs) == {"anthropic", "openai:gpt-6-astra"}
    assert jobs["anthropic"].items[0]["user_id"] == night_user.id
    assert jobs["openai:gpt-6-astra"].items[0]["model_id"] == "gpt-6-astra"

    # Nothing is billed at submit time, and the sync path was not used.
    assert APICostLog.query.count() == 0

    # A second sweep in the same hour submits nothing: everyone stale is
    # now in a pending batch.
    assert _digest.sweep_external_digests() == {"status": "ok",
                                                "submitted": 0}
    assert len(submitted) == 1


def _pending_job(user, corpus_at, provider_key="anthropic",
                 batch_id="batch_x", submitted_at=None):
    job = ExternalDigestBatchJob(
        provider_key=provider_key, batch_id=batch_id, items=[{
            "custom_id": f"external-digest-{user.id}",
            "user_id": user.id, "model_id": "claude-opus-5",
            "corpus_at": corpus_at.isoformat(), "total_items": 1,
        }])
    if submitted_at is not None:
        job.submitted_at = submitted_at
    _db.session.add(job)
    _db.session.commit()
    return job


def test_collector_saves_batch_results_at_batch_price_and_corpus_time(
        app, monkeypatch):
    user = User.query.first()
    _mk_item(user.id, "a")
    corpus_at = datetime.utcnow()
    job = _pending_job(user, corpus_at)
    _mk_item(user.id, "saved-while-batch-ran")  # newer than corpus_at

    def fake_collect(batch_ids, api_keys):
        assert batch_ids == {"anthropic": "batch_x"}
        return ({f"external-digest-{user.id}": {
            "content": "# Topics\n- batch built", "input_tokens": 1000,
            "output_tokens": 100}}, {}, {})
    monkeypatch.setattr(_digest, "batch_check_and_collect", fake_collect)

    assert _digest._collect_digest_batches() == {"collected": 1,
                                                 "abandoned": 0}
    artifact = UserArtifact.latest_for(user.id, _digest.DIGEST_KIND)
    assert artifact.get_content() == "# Topics\n- batch built"
    assert artifact.created_at == corpus_at
    log = APICostLog.query.one()
    assert log.request_type == "external_digest"
    # 1000 in x $5 + 100 out x $25 = 7500 microdollars, halved for batch.
    assert log.cost_microdollars == 3750
    _db.session.expire_all()
    assert ExternalDigestBatchJob.query.get(job.id).status == "collected"
    # The item saved mid-batch is not in this digest: still stale.
    assert _digest.digest_is_stale(user.id) is True


def test_collector_leaves_a_failed_item_stale_and_waits_on_pending(
        app, monkeypatch):
    user = User.query.first()
    _mk_item(user.id, "a")
    job = _pending_job(user, datetime.utcnow())

    # Still processing: nothing changes.
    monkeypatch.setattr(_digest, "batch_check_and_collect",
                        lambda ids, keys: ({}, dict(ids), {}))
    assert _digest._collect_digest_batches() == {"collected": 0,
                                                 "abandoned": 0}
    assert ExternalDigestBatchJob.query.get(job.id).status == "pending"

    # Ended without a result for our item (errored/expired at the
    # provider): the job closes, the user stays stale for the next sweep.
    monkeypatch.setattr(_digest, "batch_check_and_collect",
                        lambda ids, keys: ({}, {}, {}))
    assert _digest._collect_digest_batches() == {"collected": 1,
                                                 "abandoned": 0}
    _db.session.expire_all()
    assert ExternalDigestBatchJob.query.get(job.id).status == "collected"
    assert UserArtifact.latest_for(user.id, _digest.DIGEST_KIND) is None
    assert _digest.digest_is_stale(user.id) is True
    assert APICostLog.query.count() == 0


def _raise_not_found(ids, keys):
    raise RuntimeError("404: no such batch")


@pytest.mark.parametrize("check", [
    lambda ids, keys: ({}, dict(ids), {}),  # provider still says pending
    _raise_not_found,                       # we can't read it at all
])
def test_collector_abandons_a_batch_it_cannot_close_past_max_age(
        app, monkeypatch, check):
    """The providers end a batch themselves at 24h, so the age backstop
    is for a batch we can no longer read — the raising case must reach
    it too, or a lost batch id blocks the user's resubmission forever."""
    user = User.query.first()
    _mk_item(user.id, "a")
    stuck = _pending_job(
        user, datetime.utcnow(), batch_id="stuck",
        submitted_at=datetime.utcnow() - _digest.BATCH_JOB_MAX_AGE
        - timedelta(minutes=1))
    monkeypatch.setattr(_digest, "batch_check_and_collect", check)

    assert _digest._collect_digest_batches() == {"collected": 0,
                                                 "abandoned": 1}
    _db.session.expire_all()
    assert ExternalDigestBatchJob.query.get(stuck.id).status == "abandoned"
    # ...which frees the user for the next nightly sweep.
    assert user.id not in _digest._users_in_pending_batches()
    assert _digest.digest_is_stale(user.id) is True


def test_collector_keeps_a_young_unreadable_batch_pending(app, monkeypatch):
    """A transient read failure on a fresh batch is retried next tick,
    not written off."""
    user = User.query.first()
    _mk_item(user.id, "a")
    job = _pending_job(user, datetime.utcnow())
    monkeypatch.setattr(_digest, "batch_check_and_collect", _raise_not_found)

    assert _digest._collect_digest_batches() == {"collected": 0,
                                                 "abandoned": 0}
    assert ExternalDigestBatchJob.query.get(job.id).status == "pending"

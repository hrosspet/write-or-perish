"""State-machine tests for the profile-batch pipeline (issue #173, Part A).

The network boundary (batch_submit / batch_check_and_collect) and the export
builder are mocked; _save_profile and the DB run for real so we assert the
chain actually advances. The @celery.task wrappers aren't called directly
(they're mocks under the celery stub) — we exercise the _impl functions.
"""
import os
import re
import sys
from datetime import datetime, timedelta
from unittest.mock import MagicMock

# Anthropic requires batch custom_id to match this; OpenAI is no stricter.
CUSTOM_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

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

for _mod in ["backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

from backend.extensions import db  # noqa: E402
from backend.models import (  # noqa: E402
    User, UserProfile, ProfileBatchJob, APICostLog, Node)

# backend.tasks.profile_batch is imported lazily in the `app` fixture: an
# eager import at collection time trips over cross-file celery-mock ordering
# in the full suite. Matches how the other task-module tests import.
pb = None

MODELS = {"test-model": {
    "provider": "anthropic", "api_model": "claude-x",
    "input_price_per_mtok": 5.0, "output_price_per_mtok": 30.0}}


@pytest.fixture
def app(monkeypatch):
    global pb
    import backend.tasks.profile_batch as _pb
    pb = _pb
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["DEFAULT_LLM_MODEL"] = "test-model"
    app.config["SUPPORTED_MODELS"] = MODELS
    app.config["PROFILE_USE_BATCH"] = False
    app.config["PROFILE_BATCH_USER_IDS"] = set()
    app.config["OPENAI_API_KEY_BATCH"] = None
    db.init_app(app)
    # No real keys in the test app — the network boundary is mocked anyway.
    monkeypatch.setattr(pb, "get_api_keys_for_usage",
                        lambda *a, **k: {"anthropic": "k", "openai": "k"})
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


_N = [0]


def _user(**kw):
    _N[0] += 1
    u = User(username=f"u{_N[0]}", plan="alpha", twitter_id=None,
             approved=True, preferred_model="test-model", **kw)
    db.session.add(u)
    db.session.flush()
    return u


def _prev_profile(user, cutoff, source_tokens=1000, gen_type="update"):
    p = UserProfile(
        user_id=user.id, generated_by="test-model", tokens_used=0,
        generation_type=gen_type, source_tokens_used=source_tokens,
        source_data_cutoff=cutoff)
    p.set_content("PREVIOUS PROFILE")
    db.session.add(p)
    db.session.flush()
    return p


# ── gate ────────────────────────────────────────────────────────────────

def test_use_batch_for_user_gate(app):
    u = _user()
    db.session.commit()
    assert not pb.use_batch_for_user(
        u, {"PROFILE_USE_BATCH": False, "PROFILE_BATCH_USER_IDS": set()})
    assert pb.use_batch_for_user(
        u, {"PROFILE_USE_BATCH": True, "PROFILE_BATCH_USER_IDS": set()})
    assert pb.use_batch_for_user(
        u, {"PROFILE_USE_BATCH": False, "PROFILE_BATCH_USER_IDS": {u.id}})


# ── request builder ───────────────────────────────────────────────────────

def test_build_next_request_chunk(app, monkeypatch):
    u = _user()
    prev = _prev_profile(u, datetime(2026, 5, 1))
    db.session.commit()
    monkeypatch.setattr(pb, "build_user_export_content", MagicMock(
        return_value={"content": "NEW DATA", "token_count": 90000,
                      "latest_node_created_at": datetime(2026, 6, 1)}))
    monkeypatch.setattr(pb, "build_update_template", lambda uid: (
        "T {existing_profile}|{new_data}|{source_tokens_past}"
        "|{source_tokens_new}|{ratio_percent}"))

    req = pb._build_next_profile_request(u)

    assert req["provider"] == "anthropic"
    assert req["meta"]["kind"] == "chunk"
    assert req["meta"]["generation_type"] == "update"
    assert req["meta"]["prev_profile_id"] == prev.id
    assert req["meta"]["prev_cumulative"] == 1000
    assert req["meta"]["source_data_cutoff"] == "2026-06-01T00:00:00"
    text = req["request"]["messages"][0]["content"][0]["text"]
    assert "NEW DATA" in text and "PREVIOUS PROFILE" in text
    # Regression: Anthropic rejects custom_id with colons (must match pattern)
    assert CUSTOM_ID_RE.match(req["request"]["custom_id"])


def test_build_next_request_none_when_no_data(app, monkeypatch):
    u = _user()
    _prev_profile(u, datetime(2026, 5, 1))   # single version → no integration
    db.session.commit()
    monkeypatch.setattr(pb, "build_user_export_content",
                        MagicMock(return_value=None))
    assert pb._build_next_profile_request(u) is None


# ── seed gate: null-cutoff (user-written) profiles ────────────────────────

def _age(obj, days):
    """Backdate a row so the inactivity/interval gates pass, isolating the
    token-threshold decision."""
    obj.created_at = datetime.utcnow() - timedelta(days=days)


def _seed_node(user, tokens):
    n = Node(user_id=user.id, node_type="user", ai_usage="chat",
             token_count=tokens)
    n.set_content("writing")
    db.session.add(n)
    db.session.flush()
    _age(n, 25)
    return n


def test_should_seed_null_cutoff_low_data_false(app):
    """A null-cutoff (user-written) profile with <80k tokens must NOT seed —
    the sentinel that used to force-seed it is gone."""
    u = _user()
    _age(_prev_profile(u, None, gen_type="initial"), 30)
    _seed_node(u, 2884)
    db.session.commit()
    assert pb._should_seed(u) is False


def test_should_seed_null_cutoff_high_data_true(app):
    """A null-cutoff profile WITH >=80k tokens still seeds, so the base gets
    folded into a data-grounded profile."""
    u = _user()
    _age(_prev_profile(u, None, gen_type="initial"), 30)
    _seed_node(u, 90000)
    db.session.commit()
    assert pb._should_seed(u) is True


# ── full-regen flag (profile_needs_full_regen) ────────────────────────────
# The batch pipeline used to be blind to the flag: the seeder's gates
# measure "new tokens since cutoff" (a cutoff the flag often exists to
# disavow) and the builder always resumed from the latest profile, so a
# requested full rebuild was silently downgraded to an incremental
# update and the flag swallowed.

def test_should_seed_full_regen_flag_overrides_gates(app):
    """Flag set → seed, even when interval/token gates would refuse."""
    u = _user()
    _prev_profile(u, datetime.utcnow())   # fresh profile → MIN_INTERVAL fails
    u.profile_needs_full_regen = True
    db.session.commit()
    assert pb._should_seed(u) is True


def test_build_next_request_full_regen_starts_from_scratch(app, monkeypatch):
    """Flag set → builder ignores the existing chain: from-scratch chunk 1
    (initial-generation prompt, no parent, cumulative from zero)."""
    u = _user()
    _prev_profile(u, datetime(2026, 5, 1))
    u.profile_needs_full_regen = True
    db.session.commit()
    export = MagicMock(
        return_value={"content": "ALL DATA", "token_count": 90000,
                      "latest_node_created_at": datetime(2026, 6, 1)})
    monkeypatch.setattr(pb, "build_user_export_content", export)
    monkeypatch.setattr(pb, "_load_prompt",
                        lambda *a, **k: "GEN {user_export}")

    req = pb._build_next_profile_request(u)

    # Export builds from the beginning of time, not from prev's cutoff.
    assert export.call_args.kwargs["created_after"] is None
    assert req["meta"]["prev_profile_id"] is None
    assert req["meta"]["generation_type"] == "iterative"
    assert req["meta"]["prev_cumulative"] == 0
    text = req["request"]["messages"][0]["content"][0]["text"]
    assert "ALL DATA" in text and "PREVIOUS PROFILE" not in text


def test_poll_clears_flag_only_for_from_scratch_chunk(app, monkeypatch):
    """A flag set while an incremental chunk is in flight survives that
    chunk (so the next build honors it); a from-scratch chunk commits the
    rebuild and clears it."""
    monkeypatch.setattr(pb, "build_user_export_content",
                        MagicMock(return_value=None))
    monkeypatch.setattr(pb, "build_integration_messages",
                        lambda uid, pid: (None, None))
    monkeypatch.setattr(pb, "batch_submit", MagicMock(return_value={}))

    # 1. incremental chunk (prev_profile_id set): flag survives
    u = _user()
    prev = _prev_profile(u, datetime(2026, 5, 1))
    job, item = _chunk_job(u, prev)
    u.profile_needs_full_regen = True
    db.session.commit()
    monkeypatch.setattr(pb, "batch_check_and_collect", lambda bids, keys: (
        {item["custom_id"]: {"content": "P", "input_tokens": 100,
                             "output_tokens": 50}}, {}, {}))
    pb._poll_profile_batches()
    assert User.query.get(u.id).profile_needs_full_regen is True

    # 2. from-scratch chunk (prev_profile_id None): flag cleared
    u2 = _user()
    u2.profile_needs_full_regen = True
    item2 = {
        "custom_id": f"profile_{u2.id}_0_chunk", "user_id": u2.id,
        "kind": "chunk", "prev_profile_id": None,
        "generation_type": "iterative", "prev_cumulative": 0,
        "source_data_cutoff": "2026-06-01T00:00:00",
        "model_id": "test-model",
    }
    db.session.add(ProfileBatchJob(
        provider_key="anthropic", batch_id="b9", status="pending",
        items=[item2], submitted_at=datetime.utcnow()))
    u2.profile_batch_pending = True
    db.session.commit()
    monkeypatch.setattr(pb, "batch_check_and_collect", lambda bids, keys: (
        {item2["custom_id"]: {"content": "P2", "input_tokens": 100,
                              "output_tokens": 50}}, {}, {}))
    pb._poll_profile_batches()
    u2_fresh = User.query.get(u2.id)
    assert u2_fresh.profile_needs_full_regen is False
    saved = UserProfile.query.filter_by(
        user_id=u2.id, generation_type="iterative").first()
    assert saved is not None and saved.parent_profile_id is None


# ── poll cycle ────────────────────────────────────────────────────────────

def _chunk_job(user, prev):
    item = {
        "custom_id": f"profile:{user.id}:{prev.id}:chunk",
        "user_id": user.id, "kind": "chunk", "prev_profile_id": prev.id,
        "generation_type": "update", "prev_cumulative": 1000,
        "source_data_cutoff": "2026-06-01T00:00:00", "model_id": "test-model",
    }
    job = ProfileBatchJob(
        provider_key="anthropic", batch_id="b1", status="pending",
        items=[item], submitted_at=datetime.utcnow())
    db.session.add(job)
    user.profile_batch_pending = True
    db.session.commit()
    return job, item


def test_poll_saves_chunk_then_enqueues_integration(app, monkeypatch):
    u = _user()
    prev = _prev_profile(u, datetime(2026, 5, 1))
    job, item = _chunk_job(u, prev)

    monkeypatch.setattr(pb, "batch_check_and_collect", lambda bids, keys: (
        {item["custom_id"]: {"content": "NEW PROFILE",
                             "input_tokens": 2000, "output_tokens": 500}},
        {}, {}))
    # After the chunk, no more raw data → the chain (prev + new) integrates.
    monkeypatch.setattr(pb, "build_user_export_content",
                        MagicMock(return_value=None))
    monkeypatch.setattr(pb, "build_integration_messages", lambda uid, pid: (
        [{"role": "user", "content": [{"type": "text", "text": "INTEG"}]}],
        [prev]))
    monkeypatch.setattr(pb, "batch_submit",
                        MagicMock(return_value={"anthropic": "b2"}))

    pb._poll_profile_batches()

    # chunk profile saved, cutoff advanced, batch-cost tagged
    newp = UserProfile.query.filter_by(
        user_id=u.id, parent_profile_id=prev.id,
        generation_type="update").first()
    assert newp is not None
    assert newp.source_data_cutoff == datetime(2026, 6, 1)
    assert newp.source_tokens_used == 1000 + 2000   # prev_cumulative + input
    log = (APICostLog.query.filter_by(user_id=u.id)
           .order_by(APICostLog.id.desc()).first())
    assert log.request_type == "profile_batch"

    # first job collected; an integration batch was submitted; user still busy
    assert ProfileBatchJob.query.get(job.id).status == "collected"
    integ = ProfileBatchJob.query.filter_by(batch_id="b2").first()
    assert integ is not None and integ.items[0]["kind"] == "integration"
    assert CUSTOM_ID_RE.match(integ.items[0]["custom_id"])   # no colons
    assert User.query.get(u.id).profile_batch_pending is True


def test_poll_failed_item_bumps_attempts_and_clears_pending(app, monkeypatch):
    u = _user()
    prev = _prev_profile(u, datetime(2026, 5, 1))
    job, item = _chunk_job(u, prev)

    # batch ended but this item is absent from results (failed/errored)
    monkeypatch.setattr(pb, "batch_check_and_collect",
                        lambda bids, keys: ({}, {}, {}))
    monkeypatch.setattr(pb, "batch_submit", MagicMock(return_value={}))

    pb._poll_profile_batches()

    u2 = User.query.get(u.id)
    assert u2.profile_batch_attempts == 1
    assert u2.profile_batch_pending is False
    assert ProfileBatchJob.query.get(job.id).status == "collected"
    # nothing saved
    assert UserProfile.query.filter_by(
        parent_profile_id=prev.id).first() is None


def test_poll_leaves_pending_job_untouched(app, monkeypatch):
    u = _user()
    prev = _prev_profile(u, datetime(2026, 5, 1))
    job, item = _chunk_job(u, prev)

    # batch still processing
    monkeypatch.setattr(pb, "batch_check_and_collect",
                        lambda bids, keys: ({}, {"anthropic": "b1"}, {}))
    submit = MagicMock(return_value={})
    monkeypatch.setattr(pb, "batch_submit", submit)

    pb._poll_profile_batches()

    assert ProfileBatchJob.query.get(job.id).status == "pending"
    assert User.query.get(u.id).profile_batch_pending is True
    submit.assert_not_called()


def test_seed_paused_is_noop(app, monkeypatch):
    app.config["PROFILE_UPDATES_PAUSED"] = True
    submit = MagicMock()
    monkeypatch.setattr(pb, "batch_submit", submit)
    pb._seed_profile_batches()
    submit.assert_not_called()
    assert ProfileBatchJob.query.count() == 0


def test_poll_is_not_paused(app, monkeypatch):
    # The pause kill-switch must NOT stop the poller — an in-flight batch
    # still gets collected so it can finish on its own.
    app.config["PROFILE_UPDATES_PAUSED"] = True
    u = _user()
    prev = _prev_profile(u, datetime(2026, 5, 1))
    job, item = _chunk_job(u, prev)
    monkeypatch.setattr(pb, "batch_check_and_collect", lambda bids, keys: (
        {item["custom_id"]: {"content": "P", "input_tokens": 100,
                             "output_tokens": 50}}, {}, {}))
    monkeypatch.setattr(pb, "build_user_export_content",
                        MagicMock(return_value=None))
    monkeypatch.setattr(pb, "build_integration_messages",
                        lambda uid, pid: (None, None))
    monkeypatch.setattr(pb, "batch_submit", MagicMock(return_value={}))

    pb._poll_profile_batches()

    assert ProfileBatchJob.query.get(job.id).status == "collected"
    assert UserProfile.query.filter_by(
        user_id=u.id, parent_profile_id=prev.id).first() is not None


def test_poll_fails_stale_job(app, monkeypatch):
    u = _user()
    prev = _prev_profile(u, datetime(2026, 5, 1))
    job, item = _chunk_job(u, prev)
    job.submitted_at = datetime.utcnow() - timedelta(hours=30)  # past SLA
    db.session.commit()
    monkeypatch.setattr(pb, "batch_submit", MagicMock(return_value={}))

    pb._poll_profile_batches()

    assert ProfileBatchJob.query.get(job.id).status == "failed"
    u2 = User.query.get(u.id)
    assert u2.profile_batch_pending is False
    assert u2.profile_batch_attempts == 1

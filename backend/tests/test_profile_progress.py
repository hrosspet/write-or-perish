"""GET /export/profile-progress (#258): one source of truth for "is my
profile being built, and how far" across both pipelines.

- Batch API chain: profile_batch_pending, no task id — chunk n of ~N from
  the saved chain plus the planner's count for the remainder.
- Synchronous Celery task: the guard's task state, with the dispatchers'
  staleness rule applied (and the stale guard cleared).
- Nothing in flight: `idle`, or the outcome of the sync task the client
  last saw (its own `finally` clears the guard before the client can
  observe the terminal state).

Real-app + sqlite pattern from test_node_deletion.py; the Celery result
backend is faked per test.
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
sys.modules.setdefault("ffmpeg", MagicMock())

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login  # noqa: E402
from backend.extensions import db as _db  # noqa: E402
from backend.models import User, UserProfile, ProfileBatchJob  # noqa: E402
import backend.models as _real_backend_models  # noqa: E402
from backend.utils.chunk_plan import CHUNK_TARGET_UNITS  # noqa: E402

MODELS = {"test-model": {
    "provider": "anthropic", "api_model": "claude-x",
    "input_price_per_mtok": 5.0, "output_price_per_mtok": 30.0,
    "context_window": 1_000_000}}


def _make_app():
    from flask_login import LoginManager

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["DEFAULT_LLM_MODEL"] = "test-model"
    app.config["SUPPORTED_MODELS"] = MODELS
    _db.init_app(app)

    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from backend.routes.export_data import export_bp
    app.register_blueprint(export_bp, url_prefix="/api")
    return app


@pytest.fixture
def app():
    _affected = lambda k: (  # noqa: E731
        k == "flask_login"
        or k.startswith("backend.routes")
        or k == "backend.models"
    )
    saved = {k: sys.modules[k] for k in list(sys.modules) if _affected(k)}
    sys.modules["flask_login"] = _real_flask_login
    sys.modules["backend.models"] = _real_backend_models
    for _k in [k for k in list(sys.modules) if k.startswith("backend.routes")]:
        del sys.modules[_k]

    app = _make_app()
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()

    for k in [k for k in list(sys.modules) if _affected(k)]:
        if k not in saved:
            del sys.modules[k]
    for k, mod in saved.items():
        sys.modules[k] = mod


@pytest.fixture
def user(app):
    u = User(username="alice", twitter_id="alice-twitter-id", plan="alpha",
             approved=True, preferred_model="test-model")
    _db.session.add(u)
    _db.session.commit()
    return u


@pytest.fixture
def client(app, user):
    c = app.test_client()
    with c.session_transaction() as session:
        session["_user_id"] = str(user.id)
        session["_fresh"] = True
    return c


class _FakeResult:
    def __init__(self, state, info=None):
        self.state, self.info = state, info


def _fake_celery(monkeypatch, states):
    """Celery result backend answering `states[task_id]` (default PENDING,
    which is also what Celery says for an id it never heard of)."""
    import backend.celery_app as ca

    def _async_result(task_id):
        st = states.get(task_id, ("PENDING", None))
        return _FakeResult(*st)
    monkeypatch.setattr(ca.celery, "AsyncResult", _async_result)


def _version(user, gen_type="update", parent=None, cutoff=None,
             created_at=None):
    p = UserProfile(
        user_id=user.id, generated_by="test-model", tokens_used=0,
        generation_type=gen_type, source_tokens_used=CHUNK_TARGET_UNITS,
        source_data_cutoff=cutoff,
        parent_profile_id=parent.id if parent else None)
    p.set_content("PROFILE")
    if created_at is not None:
        p.created_at = created_at
    _db.session.add(p)
    _db.session.commit()
    return p


# ── nothing in flight ────────────────────────────────────────────────────

def test_idle_when_nothing_is_running(client):
    r = client.get("/api/export/profile-progress")
    assert r.status_code == 200
    body = r.get_json()
    assert body["running"] is False
    assert body["status"] == "idle"
    assert body["source"] is None
    assert body["latest_profile"] is None


def test_idle_reports_latest_version(client, user):
    p = _version(user)
    body = client.get("/api/export/profile-progress").get_json()
    assert body["latest_profile"]["id"] == p.id


@pytest.mark.parametrize("state,expected", [
    ("SUCCESS", "completed"),
    ("FAILURE", "failed"),
    ("REVOKED", "failed"),
    ("PENDING", "completed"),   # id Celery no longer knows: it finished
])
def test_outcome_of_last_seen_sync_task(client, monkeypatch, state, expected):
    _fake_celery(monkeypatch, {"t-1": (state, RuntimeError("boom"))})
    body = client.get("/api/export/profile-progress?task_id=t-1").get_json()
    assert body["running"] is False
    assert body["status"] == expected
    if expected == "failed":
        assert body["error"] == "boom"


# ── synchronous task ─────────────────────────────────────────────────────

def test_sync_task_in_progress(client, user, monkeypatch):
    user.profile_generation_task_id = "t-run"
    user.profile_generation_task_dispatched_at = datetime.utcnow()
    _db.session.commit()
    _fake_celery(monkeypatch, {"t-run": (
        "PROGRESS", {"progress": 35, "status": "Generating profile: Chunk 1"})})
    body = client.get("/api/export/profile-progress").get_json()
    assert body["running"] is True
    assert body["source"] == "sync"
    assert body["status"] == "progress"
    assert body["progress"] == 35
    assert body["message"] == "Generating profile: Chunk 1"
    assert body["task_id"] == "t-run"
    # Guard untouched while the task is legitimately running.
    assert User.query.get(user.id).profile_generation_task_id == "t-run"


def test_sync_task_pending_too_long_is_stalled_and_guard_cleared(
        client, user, monkeypatch):
    user.profile_generation_task_id = "t-lost"
    user.profile_generation_task_dispatched_at = (
        datetime.utcnow() - timedelta(minutes=20))
    _db.session.commit()
    _fake_celery(monkeypatch, {})   # PENDING
    body = client.get("/api/export/profile-progress").get_json()
    assert body["running"] is False
    assert body["status"] == "stalled"
    assert body["task_id"] == "t-lost"
    u = User.query.get(user.id)
    assert u.profile_generation_task_id is None
    assert u.profile_generation_task_dispatched_at is None
    # The next poll is plain idle — no repeated "stalled" on every reload.
    assert client.get("/api/export/profile-progress").get_json()["status"] == "idle"


def test_sync_task_running_over_an_hour_is_stalled(client, user, monkeypatch):
    # Celery kills every task at 1 h; a PROGRESS payload older than that
    # is a dead task whose result Redis still serves.
    user.profile_generation_task_id = "t-old"
    user.profile_generation_task_dispatched_at = (
        datetime.utcnow() - timedelta(hours=1, minutes=5))
    _db.session.commit()
    _fake_celery(monkeypatch, {"t-old": (
        "PROGRESS", {"progress": 35, "status": "Chunk 1"})})
    body = client.get("/api/export/profile-progress").get_json()
    assert body["running"] is False
    assert body["status"] == "stalled"
    assert User.query.get(user.id).profile_generation_task_id is None


def test_sync_task_failed_with_guard_still_set(client, user, monkeypatch):
    # Worker died without running the task's finally: FAILURE recorded,
    # guard left behind.
    user.profile_generation_task_id = "t-fail"
    user.profile_generation_task_dispatched_at = datetime.utcnow()
    _db.session.commit()
    _fake_celery(monkeypatch, {"t-fail": ("FAILURE", RuntimeError("kaboom"))})
    body = client.get("/api/export/profile-progress").get_json()
    assert body["running"] is False
    assert body["status"] == "failed"
    assert body["error"] == "kaboom"
    assert User.query.get(user.id).profile_generation_task_id is None


# ── batch chain ──────────────────────────────────────────────────────────

def _pending_job(user, item, status="pending", batch_id="b-1"):
    """A submitted batch carrying one item for `user` (meta shape of
    profile_batch._build_next_profile_request)."""
    job = ProfileBatchJob(
        provider_key="anthropic", batch_id=batch_id, status=status,
        items=[{"custom_id": f"profile_{user.id}_0_chunk", "user_id": user.id,
                "model_id": "test-model", **item}])
    _db.session.add(job)
    user.profile_batch_pending = (status == "pending")
    _db.session.commit()
    return job


def test_batch_chunk_label_comes_from_the_pending_item(client, user):
    # The builder recorded the step's ordinal and planned total at submit
    # time; the endpoint re-plans nothing.
    _pending_job(user, {"kind": "chunk", "chunk_num": 2, "chunk_total": 4})
    body = client.get("/api/export/profile-progress").get_json()
    assert body["running"] is True
    assert body["source"] == "batch"
    assert body["task_id"] is None
    assert body["message"] == "Generating profile: Chunk 2 of ~4"
    assert body["progress"] == 25


def test_batch_integration_step(client, user):
    v1 = _version(user, "iterative", cutoff=datetime(2026, 1, 1))
    v2 = _version(user, "iterative", parent=v1, cutoff=datetime(2026, 3, 1))
    _pending_job(user, {"kind": "integration", "prev_profile_id": v2.id})
    body = client.get("/api/export/profile-progress").get_json()
    assert body["running"] is True
    assert body["message"] == "Integrating profile versions"
    assert body["latest_profile"]["id"] == v2.id


def test_batch_does_not_replan(client, user, monkeypatch):
    import backend.routes.export_data as ed

    def _boom(*a, **k):
        raise AssertionError("count_remaining_units called from the progress endpoint")
    monkeypatch.setattr(ed, "count_remaining_units", _boom)
    _pending_job(user, {"kind": "chunk", "chunk_num": 1, "chunk_total": 3})
    assert client.get("/api/export/profile-progress").status_code == 200


def test_batch_flag_without_a_pending_item_is_generic(client, user):
    # Flag set, no job carries the user (submit failed after the flag,
    # admin cancel): still running, no invented numbers.
    user.profile_batch_pending = True
    _db.session.commit()
    body = client.get("/api/export/profile-progress").get_json()
    assert body["running"] is True
    assert body["message"] == "Generating profile"
    assert body["progress"] == 0


def test_batch_item_without_ordinal_is_generic(client, user):
    # Submitted before chunk_num was recorded.
    _pending_job(user, {"kind": "chunk"})
    body = client.get("/api/export/profile-progress").get_json()
    assert body["message"] == "Generating profile"


def test_batch_ignores_collected_jobs_and_other_kinds(client, user):
    _pending_job(user, {"kind": "chunk", "chunk_num": 1, "chunk_total": 5},
                 status="collected", batch_id="b-old")
    job = ProfileBatchJob(
        provider_key="anthropic", batch_id="b-int", status="pending",
        items=[{"custom_id": f"intentions_{user.id}", "user_id": user.id,
                "kind": "intentions"}])
    _db.session.add(job)
    user.profile_batch_pending = True
    _db.session.commit()
    body = client.get("/api/export/profile-progress").get_json()
    assert body["message"] == "Generating profile"


def test_batch_outranks_a_sync_guard(client, user, monkeypatch):
    _pending_job(user, {"kind": "chunk", "chunk_num": 1, "chunk_total": 1})
    user.profile_generation_task_id = "t-x"
    user.profile_generation_task_dispatched_at = datetime.utcnow()
    _db.session.commit()
    body = client.get("/api/export/profile-progress").get_json()
    assert body["source"] == "batch"


def test_idle_reports_a_failed_batch_step(client, user):
    # A chain that saved a chunk and then failed: attempts > 0 until the
    # seeder's retry applies a step. The client must not call it "updated".
    user.profile_batch_attempts = 1
    _db.session.commit()
    body = client.get("/api/export/profile-progress").get_json()
    assert body["status"] == "idle"
    assert body["batch_step_failed"] is True
    user.profile_batch_attempts = 0
    _db.session.commit()
    assert client.get("/api/export/profile-progress").get_json()["batch_step_failed"] is False


def test_requires_login(app):
    r = app.test_client().get("/api/export/profile-progress")
    assert r.status_code in (302, 401)

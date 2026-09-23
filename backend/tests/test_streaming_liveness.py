"""A live recording session belongs to the tab recording it (#320).

A second tab (cmd+click Back opens the previous page in a new tab) used
to load the session as a draft, auto-fire recovery, and have
GET /status auto-complete it: the recording tab's SSE then reported
all_complete and the half-spoken turn went to the LLM, while the
recorder kept uploading into a dead session (400 → error tone loop).

Covers:
- /status never auto-completes a live session; still does once stale,
  released, or never stamped (pre-deploy sessions).
- GET /drafts/ and /drafts/interrupted skip live sessions, list them
  once stale or released.
- /release clears the stamp (owner only, 'recording' only).
- chunk uploads stamp the session; dead-session rejections carry codes.
- transcribe-remaining refuses a live session.
- stamp_session_alive leaves updated_at alone and, for the SSE stream,
  never revives a released session.
"""

import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta
from unittest.mock import MagicMock

# ── Environment ──────────────────────────────────────────────────────────
os.environ["ENCRYPTION_DISABLED"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("TWITTER_API_KEY", "fake")
os.environ.setdefault("TWITTER_API_SECRET", "fake")
os.environ["AUDIO_STORAGE_PATH"] = tempfile.mkdtemp(prefix="wop-audio-test-")

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

import flask_login as _real_flask_login          # noqa: E402
from backend.extensions import db as _db         # noqa: E402
from backend.models import (                     # noqa: E402
    User, Draft, NodeTranscriptChunk,
)
import backend.models as _real_backend_models    # noqa: E402


def _make_app():
    from flask_login import LoginManager

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True

    _db.init_app(app)

    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from backend.routes.drafts import drafts_bp
    app.register_blueprint(drafts_bp, url_prefix="/api/drafts")

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
def transcribe_task():
    """Stand-in for the lazily imported Celery task module."""
    key = "backend.tasks.streaming_transcription"
    saved = sys.modules.get(key)
    mod = MagicMock()
    mod.transcribe_chunk_batch.delay.return_value = MagicMock(id="task-1")
    sys.modules[key] = mod
    yield mod
    if saved is None:
        del sys.modules[key]
    else:
        sys.modules[key] = saved


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True


def _make_user(username):
    u = User(username=username, approved=True, plan="alpha")
    _db.session.add(u)
    _db.session.flush()
    return u


LIVE = timedelta(seconds=5)       # stamped 5 s ago: live
STALE = timedelta(minutes=2)      # stamped 2 min ago: left behind


def _make_session(user, heartbeat_age=LIVE, status="recording",
                  parent_id=None, chunk_statuses=("completed",)):
    d = Draft(
        user_id=user.id,
        parent_id=parent_id,
        session_id=str(uuid.uuid4()),
        streaming_status=status,
        streaming_completed_chunks=0,
        privacy_level="private",
        ai_usage="chat",
        streaming_heartbeat_at=(
            None if heartbeat_age is None
            else datetime.utcnow() - heartbeat_age
        ),
    )
    d.set_content("words so far")
    _db.session.add(d)
    _db.session.flush()
    for i, st in enumerate(chunk_statuses):
        _db.session.add(NodeTranscriptChunk(
            session_id=d.session_id, chunk_index=i, status=st,
        ))
    _db.session.commit()
    return d


def _setup(app):
    client = app.test_client()
    alice = _make_user("alice")
    _db.session.commit()
    _login(client, alice.id)
    return client, alice


def _status(client, draft):
    return client.get(f"/api/drafts/streaming/{draft.session_id}/status")


class TestStatusDoesNotEndLiveSession:
    def test_live_session_is_not_auto_completed(self, app):
        client, alice = _setup(app)
        d = _make_session(alice, heartbeat_age=LIVE)

        resp = _status(client, d)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["streaming_status"] == "recording"
        assert data["live"] is True
        _db.session.expire_all()
        assert Draft.query.get(d.id).streaming_status == "recording"

    def test_stale_session_is_still_auto_completed(self, app):
        client, alice = _setup(app)
        d = _make_session(alice, heartbeat_age=STALE)

        data = _status(client, d).get_json()
        assert data["streaming_status"] == "completed"
        assert data["live"] is False

    def test_unstamped_session_is_still_auto_completed(self, app):
        # Sessions started before this deploy have no stamp.
        client, alice = _setup(app)
        d = _make_session(alice, heartbeat_age=None)

        assert _status(client, d).get_json()["streaming_status"] == "completed"

    def test_live_session_with_pending_chunk_reports_live(self, app):
        client, alice = _setup(app)
        d = _make_session(alice, chunk_statuses=("completed", "stored"))

        data = _status(client, d).get_json()
        assert data["streaming_status"] == "recording"
        assert data["live"] is True


class TestDraftListingsSkipLiveSessions:
    def test_get_draft_skips_live_session(self, app):
        client, alice = _setup(app)
        _make_session(alice, heartbeat_age=LIVE,
                      chunk_statuses=("completed", "stored"))

        resp = client.get("/api/drafts/")
        assert resp.status_code == 404

    def test_get_draft_returns_session_once_stale(self, app):
        client, alice = _setup(app)
        d = _make_session(alice, heartbeat_age=STALE,
                          chunk_statuses=("completed", "stored"))

        resp = client.get("/api/drafts/")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["session_id"] == d.session_id
        assert data["has_stored_chunks"] is True

    def test_get_draft_falls_back_to_other_draft_while_session_live(self, app):
        client, alice = _setup(app)
        text = Draft(user_id=alice.id,
                     updated_at=datetime.utcnow() - timedelta(hours=1))
        text.set_content("typed")
        _db.session.add(text)
        _db.session.commit()
        # The session is the most recent draft for this context.
        _make_session(alice, heartbeat_age=LIVE)

        data = client.get("/api/drafts/").get_json()
        assert data["id"] == text.id
        assert "session_id" not in data

    def test_get_draft_keeps_completed_session(self, app):
        # A finished session is an ordinary draft whatever its stamp.
        client, alice = _setup(app)
        d = _make_session(alice, heartbeat_age=LIVE, status="completed")

        assert client.get("/api/drafts/").get_json()["id"] == d.id

    def test_interrupted_skips_live_lists_stale(self, app):
        client, alice = _setup(app)
        live = _make_session(alice, heartbeat_age=LIVE)
        stale = _make_session(alice, heartbeat_age=STALE)

        ids = [e["session_id"] for e in
               client.get("/api/drafts/interrupted").get_json()]
        assert ids == [stale.session_id]
        assert live.session_id not in ids


class TestRelease:
    def test_release_makes_session_recoverable_at_once(self, app):
        client, alice = _setup(app)
        d = _make_session(alice, heartbeat_age=LIVE)
        assert client.get("/api/drafts/interrupted").get_json() == []

        resp = client.post(f"/api/drafts/streaming/{d.session_id}/release")
        assert resp.status_code == 200
        assert resp.get_json()["released"] is True

        ids = [e["session_id"] for e in
               client.get("/api/drafts/interrupted").get_json()]
        assert ids == [d.session_id]

    def test_release_ignores_finished_session(self, app):
        client, alice = _setup(app)
        d = _make_session(alice, heartbeat_age=LIVE, status="finalizing")

        resp = client.post(f"/api/drafts/streaming/{d.session_id}/release")
        assert resp.get_json()["released"] is False
        _db.session.expire_all()
        assert Draft.query.get(d.id).streaming_heartbeat_at is not None

    def test_release_is_owner_only(self, app):
        client, alice = _setup(app)
        bob = _make_user("bob")
        _db.session.commit()
        d = _make_session(bob, heartbeat_age=LIVE)

        resp = client.post(f"/api/drafts/streaming/{d.session_id}/release")
        assert resp.get_json()["released"] is False
        _db.session.expire_all()
        assert Draft.query.get(d.id).streaming_heartbeat_at is not None


class TestChunkUpload:
    def _chunk_dir(self, user, draft):
        from backend.routes import drafts as drafts_module
        path = (drafts_module.AUDIO_STORAGE_ROOT
                / f"drafts/{user.id}/{draft.session_id}")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _upload(self, client, draft, index=1):
        import io
        return client.post(
            f"/api/drafts/streaming/{draft.session_id}/audio-chunk",
            data={
                "chunk": (io.BytesIO(b"\x00" * 64), f"chunk_{index}.webm"),
                "chunk_index": str(index),
                "mime_type": "audio/webm",
            },
            content_type="multipart/form-data",
        )

    def test_chunk_stamps_session_and_keeps_updated_at(self, app):
        client, alice = _setup(app)
        d = _make_session(alice, heartbeat_age=STALE)
        self._chunk_dir(alice, d)
        updated_before = Draft.query.get(d.id).updated_at

        resp = self._upload(client, d)
        assert resp.status_code == 202

        _db.session.expire_all()
        fresh = Draft.query.get(d.id)
        assert fresh.streaming_heartbeat_at > datetime.utcnow() - LIVE
        assert fresh.updated_at == updated_before
        # Live again: the second tab no longer gets it.
        assert client.get("/api/drafts/interrupted").get_json() == []

    def test_upload_into_ended_session_is_coded(self, app):
        client, alice = _setup(app)
        d = _make_session(alice, status="completed")

        resp = self._upload(client, d)
        assert resp.status_code == 400
        assert resp.get_json()["code"] == "session_not_active"

    def test_upload_into_missing_session_is_coded(self, app):
        client, alice = _setup(app)
        ghost = Draft(session_id=str(uuid.uuid4()))

        resp = self._upload(client, ghost)
        assert resp.status_code == 404
        assert resp.get_json()["code"] == "session_not_found"


class TestTranscribeRemaining:
    def test_refuses_live_session(self, app, transcribe_task):
        client, alice = _setup(app)
        d = _make_session(alice, heartbeat_age=LIVE,
                          chunk_statuses=("stored",))

        resp = client.post(
            f"/api/drafts/streaming/{d.session_id}/transcribe-remaining")
        assert resp.status_code == 409
        assert resp.get_json()["code"] == "session_live"
        transcribe_task.transcribe_chunk_batch.delay.assert_not_called()

    def test_recovers_stale_session(self, app, transcribe_task):
        client, alice = _setup(app)
        d = _make_session(alice, heartbeat_age=STALE,
                          chunk_statuses=("stored",))

        resp = client.post(
            f"/api/drafts/streaming/{d.session_id}/transcribe-remaining")
        assert resp.status_code == 202
        transcribe_task.transcribe_chunk_batch.delay.assert_called_once()


class TestStampHelper:
    def test_sse_stamp_does_not_revive_released_session(self, app):
        from backend.utils.streaming_session import stamp_session_alive
        _, alice = _setup(app)
        d = _make_session(alice, heartbeat_age=None)

        stamp_session_alive(d.session_id, only_if_unreleased=True)
        _db.session.commit()
        _db.session.expire_all()
        assert Draft.query.get(d.id).streaming_heartbeat_at is None

    def test_sse_stamp_refreshes_set_stamp(self, app):
        from backend.utils.streaming_session import stamp_session_alive
        _, alice = _setup(app)
        d = _make_session(alice, heartbeat_age=STALE)
        updated_before = Draft.query.get(d.id).updated_at

        stamp_session_alive(d.session_id, only_if_unreleased=True)
        _db.session.commit()
        _db.session.expire_all()
        fresh = Draft.query.get(d.id)
        assert fresh.streaming_heartbeat_at > datetime.utcnow() - LIVE
        assert fresh.updated_at == updated_before

    def test_stamp_ignores_finished_session(self, app):
        from backend.utils.streaming_session import stamp_session_alive
        _, alice = _setup(app)
        d = _make_session(alice, heartbeat_age=STALE, status="completed")
        before = Draft.query.get(d.id).streaming_heartbeat_at

        stamp_session_alive(d.session_id)
        _db.session.commit()
        _db.session.expire_all()
        assert Draft.query.get(d.id).streaming_heartbeat_at == before

"""The first audio of a recording, counted under the account's lock, on
PostgreSQL with requests at the same moment (see
test_recording_first_audio_under_lock.py for the rule).

sqlite takes no lock, so this runs only against a throwaway Postgres
database named by LOORE_TEST_POSTGRES_URL (it creates and drops every
table in it); skipped otherwise, like test_payments_postgres.py. Each
thread sends the first chunk of its own recording; all of them wait at the
encrypt step until every one has passed the early check, then go on
together.
"""
import io
import os
import sys
import threading

import pytest

PG_URL = os.environ.get("LOORE_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not PG_URL, reason="needs LOORE_TEST_POSTGRES_URL (a throwaway database)")

from flask import Flask  # noqa: E402

from backend.tests import test_recording_limits as trl  # noqa: E402
from backend.tests.test_recording_limits import (  # noqa: E402,F401
    tasks, _payload, _cost, WEB_CHUNK_BYTES, limits, _db,
    NodeTranscriptChunk, User)

N = 12


@pytest.fixture
def pg_app(tasks, tmp_path, monkeypatch):  # noqa: F811
    from flask_login import LoginManager
    affected = lambda k: (  # noqa: E731
        k == "flask_login" or k.startswith("backend.routes")
        or k == "backend.models")
    saved = {k: sys.modules[k] for k in list(sys.modules) if affected(k)}
    sys.modules["flask_login"] = trl._real_flask_login
    sys.modules["backend.models"] = trl._real_backend_models
    for k in [k for k in list(sys.modules) if k.startswith("backend.routes")]:
        del sys.modules[k]

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = PG_URL
    # One connection for each request that waits at the same moment.
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_size": N + 4,
                                              "max_overflow": 4}
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    _db.init_app(app)
    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return _db.session.get(User, int(user_id))

    from backend.routes.drafts import drafts_bp
    app.register_blueprint(drafts_bp, url_prefix="/api/drafts")
    import backend.routes.drafts as drafts_mod
    monkeypatch.setattr(drafts_mod, "AUDIO_STORAGE_ROOT", tmp_path)
    with app.app_context():
        _db.drop_all()
        _db.create_all()
    yield app
    with app.app_context():
        _db.session.remove()
        _db.drop_all()
        _db.engine.dispose()
    for k in [k for k in list(sys.modules) if affected(k)]:
        if k not in saved:
            del sys.modules[k]
    for k, mod in saved.items():
        sys.modules[k] = mod


def _client(app, user_id):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
    return c


def _account(app, limit_usd=None):
    with app.app_context():
        u = User(username="alice", twitter_id="alice-tw", approved=True,
                 plan="alpha", default_ai_usage="chat",
                 monthly_spend_limit_usd=limit_usd)
        _db.session.add(u)
        _db.session.commit()
        return u.id


def _starts(app, user_id, n):
    client = _client(app, user_id)
    sids = []
    for _ in range(n):
        r = client.post("/api/drafts/streaming/init",
                        json={"ai_usage": "chat"})
        assert r.status_code == 201, r.get_data(as_text=True)
        sids.append(r.get_json()["session_id"])
    return sids


def _together(app, user_id, sids, monkeypatch):
    """Chunk 1 of every session in *sids*, sent in parallel; each waits at
    the encrypt step until all have passed the early check."""
    import backend.routes.drafts as drafts_mod
    real_stage = drafts_mod.stage_encrypted_file
    barrier = threading.Barrier(len(sids), timeout=30)

    def stage(upload_path, chunk_path):
        barrier.wait()
        return real_stage(upload_path, chunk_path)
    monkeypatch.setattr(drafts_mod, "stage_encrypted_file", stage)

    codes = []

    def send(sid):
        resp = _client(app, user_id).post(
            f"/api/drafts/streaming/{sid}/audio-chunk",
            data={"chunk": (io.BytesIO(_payload(WEB_CHUNK_BYTES)),
                            "chunk_1.webm"),
                  "chunk_index": "1", "mime_type": "audio/webm"},
            content_type="multipart/form-data")
        codes.append(resp.status_code)

    threads = [threading.Thread(target=send, args=(sid,)) for sid in sids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    monkeypatch.setattr(drafts_mod, "stage_encrypted_file", real_stage)
    return sorted(codes)


def test_first_chunks_at_the_same_moment_stop_at_the_count(
        pg_app, monkeypatch):
    user_id = _account(pg_app)
    sids = _starts(pg_app, user_id, N)

    codes = _together(pg_app, user_id, sids, monkeypatch)

    assert codes == [202] * limits.MAX_OPEN_RECORDINGS \
        + [429] * (N - limits.MAX_OPEN_RECORDINGS)
    with pg_app.app_context():
        assert limits.open_recordings(user_id) == limits.MAX_OPEN_RECORDINGS
        assert NodeTranscriptChunk.query.count() \
            == limits.MAX_OPEN_RECORDINGS


def test_first_chunks_at_the_same_moment_stop_at_the_usage_limit(
        pg_app, monkeypatch):
    pg_app.config["AUDIO_PRICING"] = trl.PRICE
    # A usage limit that three waiting chunks reach (15 s each).
    user_id = _account(pg_app, limit_usd=0.004)
    sids = _starts(pg_app, user_id, 6)[:5]    # the newest is left out: its
    #                                           first audio skips the limit

    codes = _together(pg_app, user_id, sids, monkeypatch)

    assert codes == [202] * 3 + [503] * 2
    with pg_app.app_context():
        assert _cost(limits.pending_audio_seconds(user_id)) \
            == 3 * _cost(limits.STREAMING_CHUNK_SECONDS)

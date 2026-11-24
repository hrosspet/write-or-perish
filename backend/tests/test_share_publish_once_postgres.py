"""Publishes of one share at the same moment, on PostgreSQL (see
test_share_publish_once.py for the rule).

Runs only against a throwaway Postgres database named by
LOORE_TEST_POSTGRES_URL (it creates and drops every table in it); skipped
otherwise, like test_payments_postgres.py. Each request waits right after
it found the share until all have found it, then they go on together.
"""
import os
import threading

import pytest

PG_URL = os.environ.get("LOORE_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not PG_URL, reason="needs LOORE_TEST_POSTGRES_URL (a throwaway database)")

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ENCRYPTION_DISABLED", "true")

from flask import Flask  # noqa: E402

from backend.extensions import db as _db  # noqa: E402
from backend.models import Node, ShareDraft, User  # noqa: E402

N = 4
TEXT = "private thought I share"


@pytest.fixture
def pg_app():
    from flask_login import LoginManager
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = PG_URL
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_size": N + 4,
                                              "max_overflow": 4}
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["SHARE_V1"] = True
    _db.init_app(app)
    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return _db.session.get(User, int(user_id))

    from backend.routes.share import share_bp
    app.register_blueprint(share_bp, url_prefix="/api/share")
    with app.app_context():
        _db.drop_all()
        _db.create_all()
        user = User(username="tester", public_sharing_enabled=True)
        _db.session.add(user)
        _db.session.commit()
        app.user_id = user.id
    yield app
    with app.app_context():
        _db.session.remove()
        _db.drop_all()
        _db.engine.dispose()


def _client(app):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(app.user_id)
        sess["_fresh"] = True
    return c


def _alive_public(app):
    with app.app_context():
        return Node.query.filter_by(human_owner_id=app.user_id,
                                    privacy_level="public",
                                    deleted_at=None).all()


def test_publishes_at_the_same_moment_make_one_public_node(
        pg_app, monkeypatch):
    r = _client(pg_app).post("/api/share", json={"content": TEXT,
                                                 "share_type": "offering"})
    assert r.status_code == 201, r.get_data(as_text=True)
    sid = r.get_json()["id"]

    import backend.routes.share as share_routes
    real = share_routes._get_own_share_or_404
    barrier = threading.Barrier(N, timeout=30)

    def found(share_id):
        share = real(share_id)
        barrier.wait()
        return share
    monkeypatch.setattr(share_routes, "_get_own_share_or_404", found)
    codes = []

    def publish():
        codes.append(_client(pg_app).post(
            f"/api/share/{sid}/publish").status_code)

    threads = [threading.Thread(target=publish) for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    monkeypatch.setattr(share_routes, "_get_own_share_or_404", real)

    assert codes == [200] * N
    public = _alive_public(pg_app)
    assert len(public) == 1
    with pg_app.app_context():
        assert _db.session.get(ShareDraft, sid).public_node_id \
            == public[0].id

    assert _client(pg_app).post(
        f"/api/share/{sid}/revoke").status_code == 200
    assert _alive_public(pg_app) == []


def test_a_share_deleted_at_the_moment_of_its_publish_answers_not_found(
        pg_app, monkeypatch):
    r = _client(pg_app).post("/api/share", json={"content": TEXT,
                                                 "share_type": "offering"})
    sid = r.get_json()["id"]

    import backend.routes.share as share_routes
    real = share_routes._get_own_share_or_404
    first = []

    def found_then_deleted_elsewhere(share_id):
        share = real(share_id)
        if first:           # the DELETE's own lookup
            return share
        first.append(share_id)
        # The user's own DELETE, in another request, commits now.
        done = threading.Event()

        def delete():
            assert _client(pg_app).delete(
                f"/api/share/{sid}").status_code == 200
            done.set()
        threading.Thread(target=delete).start()
        assert done.wait(10)
        return share
    monkeypatch.setattr(share_routes, "_get_own_share_or_404",
                        found_then_deleted_elsewhere)

    resp = _client(pg_app).post(f"/api/share/{sid}/publish")

    assert resp.status_code == 404
    assert resp.get_json() == {"error": "Not found"}
    assert _alive_public(pg_app) == []

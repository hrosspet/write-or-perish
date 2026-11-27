"""AI drafts of a poll answer on PostgreSQL, with requests at the same
moment (see test_poll_draft_requests.py for the rules).

sqlite takes no row lock, so this runs only against a throwaway Postgres
database named by LOORE_TEST_POSTGRES_URL (it creates and drops every
table in it); skipped otherwise, like test_payments_postgres.py. The
requests wait for each other right after they read the answer, then go
on together.
"""
import os
import sys
import threading
import time
from datetime import datetime
from unittest.mock import MagicMock

import pytest

PG_URL = os.environ.get("LOORE_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not PG_URL, reason="needs LOORE_TEST_POSTGRES_URL (a throwaway database)")

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ENCRYPTION_DISABLED", "true")

from flask import Flask  # noqa: E402

from backend.extensions import db as _db  # noqa: E402
from backend.models import Poll, PollResponse, User  # noqa: E402

N = 8


@pytest.fixture
def pg_app(monkeypatch):
    from flask_login import LoginManager
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = PG_URL
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_size": N + 4,
                                              "max_overflow": 4}
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["DEFAULT_LLM_MODEL"] = "test-model"
    app.config["SUPPORTED_MODELS"] = {"test-model": {
        "provider": "anthropic", "api_model": "test-model-api",
        "display_name": "Test Model", "context_window": 200000}}
    _db.init_app(app)
    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return _db.session.get(User, int(user_id))

    from backend.routes.updates import updates_bp
    app.register_blueprint(updates_bp, url_prefix="/api/updates")
    task = MagicMock()
    task.submit_poll_draft.delay.return_value = MagicMock(id="t")
    monkeypatch.setitem(sys.modules, "backend.tasks.poll_draft", task)
    with app.app_context():
        _db.drop_all()
        _db.create_all()
        user = User(username="tester", approved=True,
                    created_at=datetime(2026, 1, 1),
                    default_ai_usage="chat")
        poll = Poll(question="What's missing?", model_id="test-model")
        _db.session.add_all([user, poll])
        _db.session.commit()
        _db.session.add(PollResponse(poll_id=poll.id, user_id=user.id,
                                     status="draft_failed"))
        _db.session.commit()
        ids = (user.id, poll.id)
    yield app, task, ids
    with app.app_context():
        _db.session.remove()
        _db.drop_all()
        _db.engine.dispose()


def _client(app, user_id):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
    return c


def _together(app, monkeypatch, calls):
    """Run *calls* (functions of a test client) in parallel; each waits
    right after it read the answer until all have read it. A save then
    waits a moment more, so a draft request moves first."""
    from flask import request
    import backend.routes.updates as updates_routes
    real = updates_routes._get_or_create_response
    barrier = threading.Barrier(len(calls), timeout=30)

    def get_or_create(poll):
        resp = real(poll)
        barrier.wait()
        if request.method == "PUT":
            time.sleep(0.3)
        return resp
    monkeypatch.setattr(updates_routes, "_get_or_create_response",
                        get_or_create)
    results = [None] * len(calls)

    def run(i, call):
        with app.app_context():
            user_id = User.query.filter_by(username="tester").one().id
        results[i] = call(_client(app, user_id))

    threads = [threading.Thread(target=run, args=(i, c))
               for i, c in enumerate(calls)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_draft_requests_at_the_same_moment_queue_one_draft(
        pg_app, monkeypatch):
    app, task, (user_id, poll_id) = pg_app

    def draft(c):
        return c.post(f"/api/updates/polls/{poll_id}/draft").status_code

    codes = _together(app, monkeypatch, [draft] * N)

    assert sorted(codes) == [200] * (N - 1) + [202]
    assert task.submit_poll_draft.delay.call_count == 1
    with app.app_context():
        resp = PollResponse.query.one()
        assert (resp.status, resp.draft_count) == ("drafting", 1)


def test_a_save_at_the_moment_of_a_draft_request_leaves_it_drafting(
        pg_app, monkeypatch):
    app, task, (user_id, poll_id) = pg_app

    def draft(c):
        return c.post(f"/api/updates/polls/{poll_id}/draft").status_code

    def save(c):
        return c.put(f"/api/updates/polls/{poll_id}/response",
                     json={"content": "My words."}).status_code

    codes = _together(app, monkeypatch, [draft, save])

    assert codes == [202, 409]
    assert task.submit_poll_draft.delay.call_count == 1
    with app.app_context():
        assert PollResponse.query.one().status == "drafting"

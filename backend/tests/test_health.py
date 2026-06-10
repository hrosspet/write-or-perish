"""Tests for /health and /ready endpoints (roadmap Phase 0).

Minimal Flask app + sqlite in-memory; redis reachability is monkeypatched
(no real Redis in the test environment).
"""
import os
import sys
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

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

for _mod in ["backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

from backend.extensions import db as _db  # noqa: E402
import backend.routes.health as health_module  # noqa: E402
from backend.routes.health import health_bp  # noqa: E402


@pytest.fixture
def client():
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["TESTING"] = True
    _db.init_app(app)
    app.register_blueprint(health_bp)
    app.register_blueprint(health_bp, url_prefix="/api", name="health_api_bp")
    with app.app_context():
        _db.create_all()
        yield app.test_client()


def test_health_always_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}


def test_health_under_api_prefix(client):
    assert client.get("/api/health").status_code == 200


def test_ready_ok_when_deps_up(client, monkeypatch):
    monkeypatch.setattr(health_module, "_check_redis", lambda: True)
    resp = client.get("/ready")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ready"
    assert body["database"] is True
    assert body["redis"] is True


def test_ready_503_when_redis_down(client, monkeypatch):
    monkeypatch.setattr(health_module, "_check_redis", lambda: False)
    resp = client.get("/ready")
    assert resp.status_code == 503
    body = resp.get_json()
    assert body["status"] == "not_ready"
    assert body["redis"] is False


def test_ready_503_when_db_down(client, monkeypatch):
    monkeypatch.setattr(health_module, "_check_redis", lambda: True)
    monkeypatch.setattr(health_module, "_check_db", lambda: False)
    resp = client.get("/ready")
    assert resp.status_code == 503
    assert resp.get_json()["database"] is False

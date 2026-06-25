"""Tests for admin endpoint access control.

admin_required must be keyed on the is_admin column, NOT the username:
usernames are renamable, and #91 made 'hrosspet' reserved, so the old
username-keyed placeholder check turned an admin rename into a permanent
lockout (rename away allowed, rename back rejected as reserved).

Follows the real-app + sqlite pattern from test_audio_access.py.
"""

import os
import sys
from unittest.mock import MagicMock

# ── Environment ──────────────────────────────────────────────────────────
os.environ["ENCRYPTION_DISABLED"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("TWITTER_API_KEY", "fake")
os.environ.setdefault("TWITTER_API_SECRET", "fake")

# Mock optional heavy deps that may not be installed locally
sys.modules.setdefault("celery", MagicMock())
sys.modules.setdefault("celery.utils", MagicMock())
sys.modules.setdefault("celery.utils.log", MagicMock())
sys.modules.setdefault("celery.result", MagicMock())

import pytest
from flask import Flask

# ── Force-import real modules ────────────────────────────────────────────
# Only evict specific mocks that other test files may have installed.
for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login  # noqa: E402
from backend.extensions import db as _db  # noqa: E402
from backend.models import User, APICostLog  # noqa: E402
import backend.models as _real_backend_models  # noqa: E402


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

    from backend.routes.admin import admin_bp
    app.register_blueprint(admin_bp, url_prefix="/api/admin")

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
def users(app):
    """Two users covering both sides of the is_admin/username matrix:

    - renamed_admin: is_admin=True with a non-founder username (an admin
      who renamed away from 'hrosspet' must keep admin access)
    - impostor: username 'hrosspet' but is_admin=False (the username alone
      must no longer grant admin)
    """
    renamed_admin = User(username="explore", approved=True, is_admin=True)
    impostor = User(username="hrosspet", approved=True, is_admin=False)
    _db.session.add_all([renamed_admin, impostor])
    _db.session.commit()
    return {"renamed_admin": renamed_admin, "impostor": impostor}


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)


class TestAdminRequired:
    def test_is_admin_user_allowed_regardless_of_username(self, app, users):
        client = app.test_client()
        _login(client, users["renamed_admin"].id)
        resp = client.get("/api/admin/users")
        assert resp.status_code == 200

    def test_username_hrosspet_without_is_admin_forbidden(self, app, users):
        client = app.test_client()
        _login(client, users["impostor"].id)
        resp = client.get("/api/admin/users")
        assert resp.status_code == 403

    def test_unauthenticated_rejected(self, app, users):
        client = app.test_client()
        resp = client.get("/api/admin/users")
        # login_required fires first; unauthenticated is 401 by default
        assert resp.status_code in (401, 403)


class TestCacheHitRate:
    """#187/#189: /admin/users reports a per-user prompt-cache hit-rate over
    conversation turns — input served from cache / total prompt input —
    unified across Anthropic (cache reads) and OpenAI (cached_tokens)."""

    def test_hit_rate_served_over_prompt_input(self, app, users):
        admin = users["renamed_admin"]
        other = users["impostor"]
        with app.app_context():
            _db.session.add_all([
                # Anthropic: input_tokens is the full prompt; cache_read_tokens
                # holds the served portion. 900k of 1M served → 90%.
                APICostLog(user_id=admin.id, model_id="claude-opus-4.6",
                           request_type="conversation", input_tokens=1_000_000,
                           cache_read_tokens=900_000, cache_write_tokens=100_000,
                           cost_microdollars=1),
                # Non-conversation rows must NOT dilute the denominator.
                APICostLog(user_id=admin.id, model_id="gpt-4o-transcribe",
                           request_type="transcription", input_tokens=500_000,
                           cost_microdollars=1),
                APICostLog(user_id=admin.id, model_id="text-embedding-3-small",
                           request_type="embedding", input_tokens=2_000_000,
                           cost_microdollars=1),
            ])
            _db.session.commit()

        client = app.test_client()
        _login(client, admin.id)
        rows = {u["id"]: u for u in client.get("/api/admin/users").get_json()["users"]}

        assert rows[admin.id]["cache_hit_rate"] == 0.9   # 900k / 1M, not diluted
        assert rows[admin.id]["cache_served_tokens"] == 900_000
        assert rows[admin.id]["cache_input_tokens"] == 1_000_000
        # No conversation prompt input → null (UI renders "—").
        assert rows[other.id]["cache_hit_rate"] is None

    def test_openai_cached_counts_as_served(self, app, users):
        # OpenAI: cached_tokens is recorded in cache_read_tokens (served), with
        # input_tokens the FULL prompt (incl. cached). 7808 of 7993 → ~97.7%.
        admin = users["renamed_admin"]
        with app.app_context():
            _db.session.add(APICostLog(
                user_id=admin.id, model_id="gpt-5.5",
                request_type="conversation", input_tokens=7_993,
                cache_read_tokens=7_808, cache_write_tokens=0,
                cost_microdollars=1))
            _db.session.commit()

        client = app.test_client()
        _login(client, admin.id)
        rows = {u["id"]: u for u in client.get("/api/admin/users").get_json()["users"]}
        assert abs(rows[admin.id]["cache_hit_rate"] - 7_808 / 7_993) < 1e-9

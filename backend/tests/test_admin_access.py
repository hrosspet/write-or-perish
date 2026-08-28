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


class TestProfileStatus:
    """Admin list shows whether a profile chain is at rest (one version, or
    the latest is an integration) or still generating, plus the
    Community Archive handle an account was pre-filled from."""

    def _profile(self, user, gen_type, parent=None):
        from backend.models import UserProfile
        p = UserProfile(user_id=user.id, generated_by="m", tokens_used=0,
                        generation_type=gen_type,
                        parent_profile_id=parent.id if parent else None)
        p.set_content("x")
        _db.session.add(p)
        _db.session.flush()
        return p

    def test_stuck_chain_is_not_complete(self, app, users, monkeypatch):
        """Latest version is a root chunk but data remains beyond its
        cutoff and nothing is in flight → chunk 2 failed → not ✓."""
        from datetime import datetime
        adm = users["renamed_admin"]
        stuck = User(username="stuck", approved=True, profile_batch_attempts=1)
        _db.session.add(stuck); _db.session.flush()
        p = self._profile(stuck, "iterative")
        p.source_data_cutoff = datetime(2026, 1, 1)
        _db.session.commit()
        import backend.tasks.profile_batch as pbm
        monkeypatch.setattr(pbm, "_remaining_token_count", lambda u, ts: 85000)
        client = app.test_client()
        _login(client, adm.id)
        row = {u["username"]: u for u in client.get("/api/admin/users").json["users"]}["stuck"]
        assert row["profile"]["state"] == "generating"
        assert row["profile"]["incomplete"] is True and row["profile"]["batch_attempts"] == 1
        monkeypatch.setattr(pbm, "_remaining_token_count", lambda u, ts: 30000)  # waiting tail
        row = {u["username"]: u for u in client.get("/api/admin/users").json["users"]}["stuck"]
        assert row["profile"]["state"] == "complete" and row["profile"]["incomplete"] is False

    def test_states(self, app, users):
        admin = users["renamed_admin"]
        none_u = User(username="fresh", approved=True)
        one = User(username="one", approved=True, prefilled_handle="corbindreams")
        chain = User(username="chain", approved=True)
        done = User(username="done", approved=True)
        pending = User(username="pending", approved=True, profile_batch_pending=True)
        _db.session.add_all([none_u, one, chain, done, pending])
        _db.session.flush()
        self._profile(one, "initial")
        c1 = self._profile(chain, "iterative"); self._profile(chain, "update", parent=c1)
        d1 = self._profile(done, "iterative"); self._profile(done, "integration", parent=d1)
        self._profile(pending, "initial")
        # From-scratch rebuild: 2 versions, latest is a root chunk → at rest
        rebuilt = User(username="rebuilt", approved=True)
        _db.session.add(rebuilt); _db.session.flush()
        self._profile(rebuilt, "iterative"); self._profile(rebuilt, "iterative")
        # Pre-filled but never activated: rebuild requested, nothing in
        # flight → the approved-only seeder will never pick it up.
        waiting0 = User(username="waiting0", approved=False, profile_force_batch=True,
                        profile_needs_full_regen=True)
        waiting1 = User(username="waiting1", approved=False, profile_force_batch=True,
                        profile_needs_full_regen=True)
        active_regen = User(username="active_regen", approved=True, profile_needs_full_regen=True)
        _db.session.add_all([waiting0, waiting1, active_regen]); _db.session.flush()
        self._profile(waiting1, "iterative")
        _db.session.commit()

        client = app.test_client()
        _login(client, admin.id)
        rows = {u["username"]: u for u in client.get("/api/admin/users").json["users"]}
        assert rows["fresh"]["profile"] == {
            "versions": 0, "last_generation_type": None,
            "last_created_at": None, "state": "none", "waiting": None}
        assert rows["one"]["profile"]["state"] == "complete"
        assert rows["one"]["profile"]["versions"] == 1
        assert rows["one"]["prefilled_handle"] == "corbindreams"
        assert rows["chain"]["profile"]["state"] == "generating"
        assert rows["done"]["profile"]["state"] == "complete"
        assert rows["done"]["profile"]["last_generation_type"] == "integration"
        # A batch job in flight overrides "one version at rest"
        assert rows["pending"]["profile"]["state"] == "generating"
        assert rows["rebuilt"]["profile"]["state"] == "complete"
        assert rows["rebuilt"]["profile"]["versions"] == 2
        assert rows["waiting0"]["profile"]["state"] == "generating"
        assert rows["waiting0"]["profile"]["waiting"] == "inactive"
        assert rows["waiting1"]["profile"]["state"] == "generating"
        assert rows["waiting1"]["profile"]["waiting"] == "inactive"
        assert rows["waiting1"]["profile"]["incomplete"] is False
        assert rows["active_regen"]["profile"]["waiting"] is None
        assert rows["pending"]["profile"]["waiting"] is None


class TestPrefillCheck:
    def test_check_reports_coverage_and_already_imported(self, app, users, monkeypatch):
        from backend.utils import community_archive as ca
        from backend.models import Node
        admin = users["renamed_admin"]
        target = User(username="cedcolas", approved=False)
        _db.session.add(target); _db.session.flush()
        n = Node(user_id=target.id, human_owner_id=target.id, origin="twitter",
                 privacy_level="private", ai_usage="chat", token_count=1)
        n.set_content("x")
        _db.session.add(n); _db.session.commit()
        monkeypatch.setattr(ca, "coverage_summary", lambda h, snapshot_dir=None: {
            "account_id": "9", "username": "cedcolas", "account_num_tweets": 571,
            "ingestion": "twitter_import", "archived": 6, "retweets": 0,
            "replies": 4, "originals": 2, "est_tokens": 221, "detail_source": "rest"})
        client = app.test_client()
        _login(client, admin.id)
        r = client.get(f"/api/admin/prefill/check?handle=@cedcolas&user_id={target.id}")
        assert r.status_code == 200, r.json
        assert r.json["archived"] == 6 and r.json["import_source"] == "rest"
        # Big by live count but not in the snapshot → parquet only if current
        monkeypatch.setattr(ca, "coverage_summary", lambda h, snapshot_dir=None: {
            "account_id": "9", "username": "big", "account_num_tweets": 13762,
            "ingestion": "twitter_import", "archived": 6000, "retweets": 0,
            "replies": 0, "originals": 6000, "est_tokens": 90000, "detail_source": "rest"})
        assert client.get("/api/admin/prefill/check?handle=big").json["import_source"] == "parquet (if snapshot is current)"
        monkeypatch.setattr(ca, "coverage_summary", lambda h, snapshot_dir=None: {
            "account_id": "9", "username": "big", "account_num_tweets": 13762,
            "ingestion": "archive", "archived": 6000, "retweets": 0, "replies": 0,
            "originals": 6000, "est_tokens": 90000, "detail_source": "parquet",
            "archived_live": 6100})
        assert client.get("/api/admin/prefill/check?handle=big").json["import_source"] == "rest"
        assert r.json["already_imported"] == 1
        assert r.json["profile_threshold_tokens"] == 10000
        monkeypatch.setattr(ca, "coverage_summary", lambda h, snapshot_dir=None: None)
        assert client.get("/api/admin/prefill/check?handle=nobody").status_code == 404
        assert client.get("/api/admin/prefill/check").status_code == 400

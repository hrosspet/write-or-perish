"""Tests for how an X (Twitter) login finds its account: by numeric X id only.

/auth/login used to fall back to the X handle: with an unknown X id it
looked up an account by username and, if one existed, stamped the X id on
it and logged the caller in, without checking whether that account already
had a login of its own. Magic-link usernames are the email's local part and
X handles are freely re-registrable, so whoever held a matching handle could
log into someone else's magic-link account (and take over an X account whose
handle had changed hands, since the existing X id was overwritten too).

Now the handle never finds an account. Accounts created ahead of their
owner's first login (admin whitelist, pre-fill) carry the X id from
creation and are found by it; anything else gets a fresh account under a
derived username. Also here: the whitelist route resolving that id, logout
dropping the stored X token, and two first logins for one X id racing.

Follows the real-app + sqlite pattern from test_admin_access.py.
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

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
from sqlalchemy.exc import IntegrityError

# ── Force-import real modules ────────────────────────────────────────────
# Only evict specific mocks that other test files may have installed.
for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login  # noqa: E402
from backend.extensions import db as _db  # noqa: E402
from backend.models import User, APICostLog  # noqa: E402
import backend.models as _real_backend_models  # noqa: E402
from backend.utils import community_archive as ca_mod  # noqa: E402
from backend.utils import x_api  # noqa: E402
from backend.utils import x_identity  # noqa: E402
from backend.utils.x_identity import LOOKUP_TIMEOUT  # noqa: E402

FRONTEND_URL = "http://frontend.test"


def _make_app():
    from flask_login import LoginManager

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["FRONTEND_URL"] = FRONTEND_URL
    app.config["TWITTER_API_KEY"] = "fake"
    app.config["TWITTER_API_SECRET"] = "fake"

    _db.init_app(app)

    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from backend.routes.auth import auth_bp
    from backend.routes.admin import admin_bp
    from backend.oauth import init_twitter_blueprint
    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(admin_bp, url_prefix="/api/admin")
    # The real flask-dance blueprint: logout must drop the token it stores.
    init_twitter_blueprint(app)

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
def real_app():
    """The real app from create_app(): every blueprint, the login manager
    and, what the bare app lacks, the approval gate (block_unapproved_users)."""
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

    from backend import create_app
    app = create_app()
    app.config["TESTING"] = True
    app.config["FRONTEND_URL"] = FRONTEND_URL
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


def _x_login(app, x_id, screen_name):
    """Hit /auth/login as an already-authorized X session for the given
    X user. Returns (response, id of the logged-in user or None)."""
    client = app.test_client()
    x = MagicMock()
    x.authorized = True
    x.get.return_value = MagicMock(
        ok=True, json=lambda: {"id": x_id, "screen_name": screen_name})
    with patch("backend.routes.auth.twitter", x):
        resp = client.get("/auth/login")
    with client.session_transaction() as sess:
        uid = sess.get("_user_id")
    return resp, (int(uid) if uid is not None else None)


def _add(**kwargs):
    user = User(**kwargs)
    _db.session.add(user)
    _db.session.commit()
    return user


def _admin_client(app):
    admin = _add(username="explore", approved=True, is_admin=True)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(admin.id)
    return client, admin


def _never(*args, **kwargs):
    raise AssertionError("lookup must not be called")


class TestXLoginMatchesByIdOnly:
    @pytest.mark.parametrize("approved", [True, False])
    def test_magic_link_account_is_never_touched(self, app, approved):
        """The reported hole: an X handle matching a magic-link username
        must not log into that account or stamp an X id on it, whether
        the account is approved yet or (the usual state right after
        signup) still waiting."""
        victim = _add(username="alice", email="alice@example.com",
                      approved=approved)

        resp, logged_in_id = _x_login(app, x_id=123, screen_name="alice")

        assert resp.status_code == 302
        assert resp.headers["Location"] == f"{FRONTEND_URL}/dashboard"
        _db.session.refresh(victim)
        assert victim.twitter_id is None
        assert logged_in_id is not None and logged_in_id != victim.id
        created = User.query.get(logged_in_id)
        assert created.twitter_id == "123"
        assert created.username == "alice2"
        assert User.query.count() == 2

    def test_blanked_email_changes_nothing(self, app):
        """Users can save an empty email through account settings; the
        handle still finds nothing."""
        victim = _add(username="alice", email="", approved=True)

        _, logged_in_id = _x_login(app, x_id=123, screen_name="alice")

        _db.session.refresh(victim)
        assert victim.twitter_id is None
        assert logged_in_id != victim.id
        assert User.query.get(logged_in_id).username == "alice2"

    def test_account_with_another_x_id_is_not_relinked(self, app):
        """An X-created account whose handle later changed hands on X
        stays with its original X id."""
        original = _add(username="alice", twitter_id="1", approved=True)

        _, logged_in_id = _x_login(app, x_id=123, screen_name="alice")

        _db.session.refresh(original)
        assert original.twitter_id == "1"
        assert logged_in_id != original.id
        assert User.query.get(logged_in_id).username == "alice2"

    def test_handle_only_account_without_x_id_is_not_claimed(self, app):
        """A placeholder that never got its X id (whitelisted before ids
        were resolved, or a dev-script account) is inert: the handle alone
        claims nothing. backfill_placeholder_x_ids.py gives such rows
        their id."""
        placeholder = _add(username="alice", approved=False)

        _, logged_in_id = _x_login(app, x_id=123, screen_name="alice")

        _db.session.refresh(placeholder)
        assert placeholder.twitter_id is None
        assert logged_in_id != placeholder.id
        assert User.query.get(logged_in_id).username == "alice2"

    def test_case_variant_of_a_taken_username_is_derived(self, app):
        """Usernames are unique case-insensitively (validate_username,
        derive_available_username); a handle differing only in case from
        a taken username gets the derived name, and touches nothing."""
        victim = _add(username="Alice", email="alice@example.com",
                      approved=True)

        _, logged_in_id = _x_login(app, x_id=123, screen_name="alice")

        _db.session.refresh(victim)
        assert victim.twitter_id is None
        assert logged_in_id != victim.id
        assert User.query.get(logged_in_id).username == "alice2"

    def test_reserved_handle_gets_derived_username(self, app):
        system = _add(username="system", approved=False)

        _, logged_in_id = _x_login(app, x_id=123, screen_name="system")

        _db.session.refresh(system)
        assert system.twitter_id is None
        created = User.query.get(logged_in_id)
        assert created.id != system.id
        assert created.username == "system2"
        assert created.twitter_id == "123"

    def test_known_x_id_logs_in_regardless_of_handle(self, app):
        existing = _add(username="someone", twitter_id="123", approved=True)
        _add(username="alice", email="alice@example.com", approved=True)

        _, logged_in_id = _x_login(app, x_id=123, screen_name="alice")

        assert logged_in_id == existing.id
        assert User.query.count() == 2

    def test_fresh_handle_creates_account(self, app):
        _, logged_in_id = _x_login(app, x_id=123, screen_name="alice")

        created = User.query.get(logged_in_id)
        assert created.username == "alice"
        assert created.twitter_id == "123"

    def test_concurrent_first_logins_share_one_account(self, app, monkeypatch):
        """Two callbacks for the same X id at once: the loser's insert
        fails on the unique X id, and it logs into the winner's row
        instead of failing the callback."""
        real_commit = _db.session.commit
        raced = {"done": False}

        def racing_commit():
            if raced["done"]:
                return real_commit()
            raced["done"] = True
            # The other callback commits its account first.
            _db.session.rollback()
            _db.session.add(User(username="alice", twitter_id="123"))
            real_commit()
            raise IntegrityError("INSERT INTO user", {}, Exception(
                "UNIQUE constraint failed: user.twitter_id"))

        monkeypatch.setattr(_db.session, "commit", racing_commit)

        resp, logged_in_id = _x_login(app, x_id=123, screen_name="alice")

        assert resp.status_code == 302
        winner = User.query.filter_by(twitter_id="123").one()
        assert logged_in_id == winner.id
        assert User.query.count() == 1


class TestWhitelistByXId:
    def test_archive_id_then_owner_login_lands_in_it(self, app, monkeypatch):
        """The workflow: whitelist a handle (unapproved, id from the
        archive), pre-fill it, the owner's X login finds it by id and
        stays behind the approval gate until approved by hand."""
        client, _admin = _admin_client(app)
        seen = {}

        def fetch_account(h, timeout=None):
            seen["timeout"] = timeout
            return {"account_id": "123", "username": "Alice",
                    "account_display_name": "Alice Doe", "num_tweets": 3}
        monkeypatch.setattr(ca_mod, "fetch_account", fetch_account)
        monkeypatch.setattr(x_api, "lookup_user", _never)

        r = client.post("/api/admin/whitelist", json={"handle": "alice"})

        assert r.status_code == 201, r.json
        assert r.json["source"] == "community-archive"
        # who was matched, for the admin to check, not just an id
        assert r.json["matched"] == {"username": "Alice", "display_name": "Alice Doe"}
        assert r.json["user"]["twitter_id"] == "123"
        assert r.json["user"]["approved"] is False
        # the lookup must give up well before nginx/axios do (60 s), or the
        # placeholder gets created after the admin already saw an error;
        # each call gets what is left of the one overall budget
        assert 0 < seen["timeout"] <= LOOKUP_TIMEOUT < 60
        placeholder = User.query.filter_by(username="alice").one()

        # The callback reports the handle in X's casing; the id decides.
        _, logged_in_id = _x_login(app, x_id=123, screen_name="Alice")

        assert logged_in_id == placeholder.id
        _db.session.refresh(placeholder)
        assert placeholder.twitter_id == "123"
        assert placeholder.approved is False
        assert User.query.count() == 2

    def test_admin_set_email_does_not_block_the_claim(self, app, monkeypatch):
        """An admin adds an email to the placeholder (Activate & Welcome
        needs one) before the owner ever logs in; the owner's X login
        still lands in it, by id."""
        client, _admin = _admin_client(app)
        monkeypatch.setattr(ca_mod, "fetch_account", lambda h, timeout=None: {
            "account_id": "123", "username": "alice", "num_tweets": 3})
        r = client.post("/api/admin/whitelist", json={"handle": "alice"})
        placeholder = User.query.get(r.json["user"]["id"])
        r = client.put(f"/api/admin/users/{placeholder.id}/update_email",
                       json={"email": "alice@example.com"})
        assert r.status_code == 200, r.json

        _, logged_in_id = _x_login(app, x_id=123, screen_name="alice")

        assert logged_in_id == placeholder.id
        _db.session.refresh(placeholder)
        assert placeholder.email == "alice@example.com"
        assert placeholder.twitter_id == "123"
        assert User.query.count() == 2

    def test_not_in_archive_is_refused_without_the_x_flag(self, app, monkeypatch):
        """No silent spend: without x_lookup the X API is never called,
        and no handle-only row is created."""
        client, _admin = _admin_client(app)
        monkeypatch.setattr(ca_mod, "fetch_account", lambda h, timeout=None: None)
        monkeypatch.setattr(x_api, "lookup_user", _never)

        r = client.post("/api/admin/whitelist", json={"handle": "alice"})

        assert r.status_code == 404, r.json
        assert "not in the Community Archive" in r.json["error"]
        assert 'Tick "Look up on X"' in r.json["error"]
        assert "enter the X id" not in r.json["error"]
        assert User.query.filter_by(username="alice").first() is None

    def test_archive_error_is_502_without_the_x_flag(self, app, monkeypatch):
        client, _admin = _admin_client(app)

        def boom(h, timeout=None):
            raise OSError("archive down")
        monkeypatch.setattr(ca_mod, "fetch_account", boom)
        monkeypatch.setattr(x_api, "lookup_user", _never)

        r = client.post("/api/admin/whitelist", json={"handle": "alice"})

        assert r.status_code == 502, r.json
        assert "archive down" in r.json["error"]
        assert User.query.filter_by(username="alice").first() is None

    def test_ambiguous_archive_match_is_refused(self, app, monkeypatch):
        """Several archive accounts with exactly that username: never
        guess which X id to key the account on."""
        client, _admin = _admin_client(app)

        def ambiguous(h, timeout=None):
            raise ca_mod.CommunityArchiveError(
                "@alice: 2 Community Archive accounts have exactly that username")
        monkeypatch.setattr(ca_mod, "fetch_account", ambiguous)
        monkeypatch.setattr(x_api, "lookup_user", _never)

        r = client.post("/api/admin/whitelist", json={"handle": "alice", "x_lookup": True})

        assert r.status_code == 409, r.json
        assert "2 Community Archive accounts" in r.json["error"]
        assert User.query.filter_by(username="alice").first() is None

    def test_x_lookup_fallback_is_explicit_and_billed_to_the_admin(self, app, monkeypatch):
        client, admin = _admin_client(app)
        monkeypatch.setattr(ca_mod, "fetch_account", lambda h, timeout=None: None)
        seen = {}

        def lookup_user(h, c, timeout=None):
            seen["timeout"] = timeout
            return {"id": "777", "username": "alice", "name": "Alice Doe",
                    "tweet_count": 10, "protected": False}
        monkeypatch.setattr(x_api, "lookup_user", lookup_user)

        r = client.post("/api/admin/whitelist",
                        json={"handle": "alice", "x_lookup": True})

        assert r.status_code == 201, r.json
        assert r.json["source"] == "x-api"
        assert r.json["matched"] == {"username": "alice", "display_name": "Alice Doe"}
        assert r.json["user"]["twitter_id"] == "777"
        assert 0 < seen["timeout"] <= LOOKUP_TIMEOUT
        log = APICostLog.query.filter_by(request_type="x_id_lookup").one()
        assert log.user_id == admin.id  # the admin pays, never the placeholder
        assert log.model_id == "x-api/user-lookup"
        assert log.cost_microdollars == x_api.cost_microdollars(0, user_reads=1)

    def test_x_lookup_unknown_handle_is_404_and_still_billed(self, app, monkeypatch):
        client, admin = _admin_client(app)
        monkeypatch.setattr(ca_mod, "fetch_account", lambda h, timeout=None: None)
        monkeypatch.setattr(x_api, "lookup_user", lambda h, c, timeout=None: None)

        r = client.post("/api/admin/whitelist",
                        json={"handle": "alice", "x_lookup": True})

        assert r.status_code == 404, r.json
        assert "not on X" in r.json["error"]
        assert APICostLog.query.filter_by(
            user_id=admin.id, request_type="x_id_lookup").count() == 1
        assert User.query.filter_by(username="alice").first() is None

    def test_x_lookup_falls_back_when_the_archive_errors(self, app, monkeypatch):
        client, _admin = _admin_client(app)

        def boom(h, timeout=None):
            raise OSError("archive down")
        monkeypatch.setattr(ca_mod, "fetch_account", boom)
        monkeypatch.setattr(x_api, "lookup_user", lambda h, c, timeout=None: {
            "id": "777", "username": "alice", "name": "A",
            "tweet_count": 10, "protected": False})

        r = client.post("/api/admin/whitelist",
                        json={"handle": "alice", "x_lookup": True})

        assert r.status_code == 201, r.json
        assert r.json["source"] == "x-api"

    def test_x_id_already_on_an_account_is_409(self, app, monkeypatch):
        client, _admin = _admin_client(app)
        holder = _add(username="someone", twitter_id="123", approved=True)
        monkeypatch.setattr(ca_mod, "fetch_account", lambda h, timeout=None: {
            "account_id": "123", "username": "alice", "num_tweets": 3})

        r = client.post("/api/admin/whitelist", json={"handle": "alice"})

        assert r.status_code == 409, r.json
        assert r.json["user_id"] == holder.id
        assert User.query.filter_by(username="alice").first() is None

    def test_concurrent_insert_is_409_without_db_text(self, app, monkeypatch):
        """A double submit or a signup racing the insert: a readable 409,
        never the driver's error text."""
        client, _admin = _admin_client(app)
        monkeypatch.setattr(ca_mod, "fetch_account", lambda h, timeout=None: {
            "account_id": "123", "username": "alice", "num_tweets": 3})
        real_commit = _db.session.commit

        def racing_commit():
            _db.session.rollback()
            raise IntegrityError("INSERT INTO user", {}, Exception(
                "UNIQUE constraint failed: user.username"))
        monkeypatch.setattr(_db.session, "commit", racing_commit)

        r = client.post("/api/admin/whitelist", json={"handle": "alice"})

        monkeypatch.setattr(_db.session, "commit", real_commit)
        assert r.status_code == 409, r.json
        assert "another request" in r.json["error"]
        assert "UNIQUE" not in r.json["error"] and "details" not in r.json
        assert User.query.filter_by(username="alice").first() is None

    def test_username_rules_still_apply(self, app, monkeypatch):
        client, _admin = _admin_client(app)
        monkeypatch.setattr(ca_mod, "fetch_account", _never)
        _add(username="taken", email="t@example.com")

        assert client.post("/api/admin/whitelist",
                           json={"handle": "admin"}).status_code == 400
        assert client.post("/api/admin/whitelist",
                           json={"handle": "Taken"}).status_code == 400
        assert client.post("/api/admin/whitelist",
                           json={"handle": ""}).status_code == 400


class TestApprovalGateOnTheRealApp:
    """create_app() carries block_unapproved_users; the bare test app
    does not. A claimed, still-unapproved owner must be able to load the
    dashboard (the thank-you page reads it) and nothing else."""

    def test_claimed_unapproved_owner_sees_only_the_dashboard(self, real_app, monkeypatch):
        client, _admin = _admin_client(real_app)
        monkeypatch.setattr(ca_mod, "fetch_account", lambda h, timeout=None: {
            "account_id": "123", "username": "alice", "num_tweets": 3})
        r = client.post("/api/admin/whitelist", json={"handle": "alice"})
        assert r.status_code == 201, r.json
        placeholder_id = r.json["user"]["id"]

        _, logged_in_id = _x_login(real_app, x_id=123, screen_name="alice")
        assert logged_in_id == placeholder_id

        owner = real_app.test_client()
        with owner.session_transaction() as sess:
            sess["_user_id"] = str(placeholder_id)
        r = owner.get("/api/dashboard/")
        assert r.status_code == 200, r.get_data(as_text=True)[:200]
        assert r.get_json()["user"]["approved"] is False
        r = owner.get("/api/nodes/1")
        assert r.status_code == 403
        assert "not approved" in r.get_json()["error"]
        r = owner.get("/api/admin/users")
        assert r.status_code == 403


class TestBackfillPlaceholderXIds:
    def _run(self, tmp_path, **kw):
        import io
        from backend.scripts.backfill_placeholder_x_ids import _run
        out = io.StringIO()
        kw.setdefault("dumps_dir", tmp_path)
        result = _run(kw.pop("apply", False), kw.pop("x_lookup", False),
                      kw.pop("admin_id", None), kw.pop("dumps_dir"), out=out)
        return result, out.getvalue()

    def test_dry_run_makes_no_paid_call_and_writes_nothing(self, app, tmp_path, monkeypatch):
        placeholder = _add(username="alice", approved=False)
        monkeypatch.setattr(ca_mod, "fetch_account", lambda h, timeout=None: None)
        monkeypatch.setattr(x_api, "lookup_user", _never)

        result, out = self._run(tmp_path, x_lookup=True, admin_id=1)

        assert result["stamped"] == 0
        assert any("would need a paid X lookup" in line for line in result["unresolved"])
        assert "UNRESOLVED" in out and "@alice" in out
        assert APICostLog.query.count() == 0
        _db.session.refresh(placeholder)
        assert placeholder.twitter_id is None

    def test_apply_stamps_from_the_archive_and_names_the_match(self, app, tmp_path, monkeypatch):
        placeholder = _add(username="alice", approved=False)
        _add(username="bob", email="bob@example.com")       # has a login: untouched
        _add(username="carol", twitter_id="9", approved=True)
        monkeypatch.setattr(ca_mod, "fetch_account", lambda h, timeout=None: {
            "account_id": "123", "username": "Alice",
            "account_display_name": "Alice Doe", "num_tweets": 3})

        result, out = self._run(tmp_path, apply=True)

        assert result["stamped"] == 1 and not result["unresolved"]
        assert "@Alice “Alice Doe” → X id 123 (community-archive)" in out
        _db.session.refresh(placeholder)
        assert placeholder.twitter_id == "123"
        assert User.query.filter_by(username="bob").one().twitter_id is None

    def test_prefilled_handle_is_the_name_looked_up(self, app, tmp_path, monkeypatch):
        placeholder = _add(username="alice", prefilled_handle="Alice_Real", approved=False)
        asked = []

        def fetch_account(h, timeout=None):
            asked.append(h)
            return {"account_id": "123", "username": "Alice_Real", "num_tweets": 3}
        monkeypatch.setattr(ca_mod, "fetch_account", fetch_account)

        result, out = self._run(tmp_path, apply=True)

        assert asked == ["Alice_Real"]
        assert "pre-filled from @Alice_Real" in out
        _db.session.refresh(placeholder)
        assert placeholder.twitter_id == "123"

    def test_x_api_dump_beats_a_new_lookup(self, app, tmp_path, monkeypatch):
        placeholder = _add(username="alice", prefilled_handle="alice", approved=False)
        other = _add(username="dave", approved=False)
        (tmp_path / "alice-2026-09-01T00-00-00Z.jsonl").write_text(json.dumps({
            "_meta": "loore x-api pre-fill", "for_user_id": placeholder.id,
            "account": {"id": "555", "username": "alice", "name": "Alice Doe"}}) + "\n"
            + json.dumps({"tweet": {}}) + "\n")
        (tmp_path / "junk.jsonl").write_text("not json\n")
        asked = []

        def fetch_account(h, timeout=None):
            asked.append(h)
            return None
        monkeypatch.setattr(ca_mod, "fetch_account", fetch_account)
        monkeypatch.setattr(x_api, "lookup_user", _never)

        result, out = self._run(tmp_path, apply=True)

        assert asked == ["dave"]  # alice came from the dump, no lookup
        assert "x-api dump alice-2026-09-01T00-00-00Z.jsonl" in out
        _db.session.refresh(placeholder)
        assert placeholder.twitter_id == "555"
        assert User.query.get(other.id).twitter_id is None
        assert any("@dave" in line for line in result["unresolved"])

    def test_paid_lookup_needs_apply_and_is_billed_to_the_admin(self, app, tmp_path, monkeypatch):
        admin = _add(username="explore", email="admin@example.com",
                     approved=True, is_admin=True)
        placeholder = _add(username="alice", approved=False)
        monkeypatch.setattr(ca_mod, "fetch_account", lambda h, timeout=None: None)
        monkeypatch.setattr(x_api, "lookup_user", lambda h, c, timeout=None: {
            "id": "777", "username": "alice", "name": "A", "tweet_count": 1, "protected": False})

        with pytest.raises(SystemExit, match="--admin-id"):
            self._run(tmp_path, apply=True, x_lookup=True)
        result, _out = self._run(tmp_path, apply=True, x_lookup=True, admin_id=admin.id)

        assert result["stamped"] == 1
        log = APICostLog.query.filter_by(request_type="x_id_lookup").one()
        assert log.user_id == admin.id
        _db.session.refresh(placeholder)
        assert placeholder.twitter_id == "777"

    def test_owner_who_logged_in_first_is_reported_as_a_conflict(self, app, tmp_path, monkeypatch):
        from datetime import datetime
        placeholder = _add(username="alice", approved=False)
        # the owner's X login, pre-backfill: a used account (last seen set)
        fresh = _add(username="alice2", twitter_id="123",
                     last_seen_at=datetime(2026, 9, 17, 9, 0))
        monkeypatch.setattr(ca_mod, "fetch_account", lambda h, timeout=None: {
            "account_id": "123", "username": "alice", "num_tweets": 3})

        result, out = self._run(tmp_path, apply=True)

        assert result["stamped"] == 0
        assert len(result["conflicts"]) == 1
        line = result["conflicts"][0]
        assert f"already signs in as user {fresh.id} @alice2" in line
        assert "logged in before the backfill" in line and "delete it" in line
        assert "rename that account" not in line
        assert "CONFLICTS" in out
        _db.session.refresh(placeholder)
        assert placeholder.twitter_id is None

    def test_second_run_words_a_stamped_placeholder_as_a_placeholder(self, app, tmp_path, monkeypatch):
        """A placeholder stamped in an earlier run (or by a pre-fill) holds
        the id and nobody has signed into it: the next run must say so,
        not tell the admin the owner logged in and to delete the account."""
        dup = _add(username="alice_old", prefilled_handle="alice", approved=False)
        stamped_before = _add(username="alice", twitter_id="123", approved=False)
        monkeypatch.setattr(ca_mod, "fetch_account", lambda h, timeout=None: {
            "account_id": "123", "username": "alice", "num_tweets": 3})

        result, out = self._run(tmp_path, apply=True)

        assert result["stamped"] == 0 and len(result["conflicts"]) == 1
        line = result["conflicts"][0]
        assert f"is already on user {stamped_before.id} @alice, a placeholder nobody has signed into" in line
        assert "two placeholders for one X account" in line
        assert "logged in before the backfill" not in line
        _db.session.refresh(dup)
        assert dup.twitter_id is None

    def test_an_account_from_before_the_record_is_not_called_a_placeholder(self, app, tmp_path, monkeypatch):
        """An early X signup who never accepted the terms has no last-seen
        (the column postdates them) and no terms date — exactly what a
        stamped placeholder looks like. The backfill must not invite the
        admin to delete it."""
        from datetime import datetime
        dup = _add(username="alice_old", prefilled_handle="alice", approved=False)
        early = _add(username="alice", twitter_id="123", approved=True,
                     created_at=datetime(2026, 8, 1))
        monkeypatch.setattr(ca_mod, "fetch_account", lambda h, timeout=None: {
            "account_id": "123", "username": "alice", "num_tweets": 3})

        result, out = self._run(tmp_path, apply=True)

        assert result["stamped"] == 0 and len(result["conflicts"]) == 1
        line = result["conflicts"][0]
        assert f"is already on user {early.id} @alice, which has no sign-in on record but predates" in line
        assert "created 2026-08-01" in line
        assert "check with the person before deleting anything" in line
        assert "placeholder nobody has signed into" not in line
        assert "keep one, delete the other" not in line
        _db.session.refresh(dup)
        assert dup.twitter_id is None

    def test_apply_does_not_overwrite_a_concurrent_stamp(self, app, tmp_path, monkeypatch):
        """A pre-fill stamps the placeholder while the backfill's lookup
        runs: the backfill's write is conditional on the id still being
        empty, so the pre-fill's id stays and the run reports it."""
        from sqlalchemy import text
        placeholder = _add(username="alice", approved=False)

        def fetch_account(h, timeout=None):
            # lands behind the running backfill's back (no ORM sync)
            _db.session.execute(text('UPDATE "user" SET twitter_id = :x WHERE id = :id'),
                                {"x": "Z9", "id": placeholder.id})
            return {"account_id": "123", "username": "alice", "num_tweets": 3}
        monkeypatch.setattr(ca_mod, "fetch_account", fetch_account)

        result, out = self._run(tmp_path, apply=True)

        assert result["stamped"] == 0
        assert any("changed by another job while this ran (now X id Z9)" in line
                   for line in result["conflicts"])
        assert User.query.get(placeholder.id).twitter_id == "Z9"


    def test_mismatched_dump_is_a_conflict_not_a_stamp(self, app, tmp_path, monkeypatch):
        """A pull of another handle saved under this user (a mistaken
        pre-fill) must not hand the placeholder that person's id."""
        placeholder = _add(username="alice", prefilled_handle="alice", approved=False)
        (tmp_path / "mallory-2026-09-01T00-00-00Z.jsonl").write_text(json.dumps({
            "_meta": "loore x-api pre-fill", "for_user_id": placeholder.id,
            "fetched_at": "2026-09-01T00:00:00+00:00",
            "account": {"id": "666", "username": "mallory", "name": "M"}}) + "\n")
        monkeypatch.setattr(ca_mod, "fetch_account", _never)
        monkeypatch.setattr(x_api, "lookup_user", _never)

        result, out = self._run(tmp_path, apply=True)

        assert result["stamped"] == 0
        assert any("is for @mallory, not @alice" in line for line in result["conflicts"])
        _db.session.refresh(placeholder)
        assert placeholder.twitter_id is None

    def test_newest_dump_by_fetch_time_wins(self, app, tmp_path):
        placeholder = _add(username="alice", prefilled_handle="alice", approved=False)
        # file names sort the other way round from the fetch times
        (tmp_path / "alice-b.jsonl").write_text(json.dumps({
            "for_user_id": placeholder.id, "fetched_at": "2026-08-01T00:00:00+00:00",
            "account": {"id": "111", "username": "alice", "name": "Old"}}) + "\n")
        (tmp_path / "alice-a.jsonl").write_text(json.dumps({
            "for_user_id": placeholder.id, "fetched_at": "2026-09-01T00:00:00+00:00",
            "account": {"id": "222", "username": "alice", "name": "New"}}) + "\n")

        result, out = self._run(tmp_path, apply=True)

        assert result["stamped"] == 1 and "x-api dump alice-a.jsonl" in out
        _db.session.refresh(placeholder)
        assert placeholder.twitter_id == "222"

    @pytest.mark.parametrize("apply", [False, True])
    def test_two_placeholders_for_one_x_id_are_a_conflict(self, app, tmp_path, monkeypatch, apply):
        """Both resolve to 123: the first is matched, the second reported as
        a duplicate placeholder — in the dry run too, and never as "the
        owner logged in before the backfill; delete that account"."""
        first = _add(username="alice", approved=False)
        second = _add(username="alice_old", prefilled_handle="alice", approved=False)
        monkeypatch.setattr(ca_mod, "fetch_account", lambda h, timeout=None: {
            "account_id": "123", "username": "alice", "num_tweets": 3})

        result, out = self._run(tmp_path, apply=apply)

        assert result["stamped"] == (1 if apply else 0)
        assert len(result["conflicts"]) == 1
        line = result["conflicts"][0]
        assert f"user {second.id} @alice_old" in line
        assert f"also resolves to user {first.id} @alice" in line
        assert "two placeholders for one X account" in line
        assert "logged in before the backfill" not in line
        _db.session.refresh(second)
        assert second.twitter_id is None

    def test_unknown_admin_id_stops_before_any_lookup(self, app, tmp_path, monkeypatch):
        _add(username="alice", approved=False)
        monkeypatch.setattr(ca_mod, "fetch_account", _never)
        monkeypatch.setattr(x_api, "lookup_user", _never)

        with pytest.raises(SystemExit, match="no such user"):
            self._run(tmp_path, apply=True, x_lookup=True, admin_id=999)
        assert APICostLog.query.count() == 0

    def test_reserved_names_are_listed_apart_and_the_count_excludes_them(self, app, tmp_path, monkeypatch):
        _add(username="system", approved=False)  # a system account, never claimable
        placeholder = _add(username="alice", approved=False)
        monkeypatch.setattr(ca_mod, "fetch_account", lambda h, timeout=None: {
            "account_id": "123", "username": "alice", "num_tweets": 3})

        result, out = self._run(tmp_path, apply=True)

        assert result["placeholders"] == 1 and result["reserved"] == ["system"]
        assert "1 placeholder(s) needing an id: 1 stamped, 0 unresolved, 0 conflicting" in out
        assert "1 reserved-name account(s) skipped" in out and "@system" in out
        _db.session.refresh(placeholder)
        assert placeholder.twitter_id == "123"
        # the runbook's final check can now pass
        result, out = self._run(tmp_path)
        assert "0 placeholder(s) needing an id" in out


class TestSignInStatus:
    def test_the_three_answers(self, app):
        from datetime import datetime
        from backend.utils.activity import sign_in_status, LAST_SEEN_SINCE
        assert sign_in_status(_add(username="a", last_seen_at=datetime(2026, 9, 1))) == "signed-in"
        assert sign_in_status(_add(username="b", accepted_terms_at=datetime(2026, 7, 1),
                                   created_at=datetime(2026, 6, 1))) == "signed-in"
        assert sign_in_status(_add(username="c")) == "never"  # created now, nothing recorded
        assert sign_in_status(_add(username="d", created_at=LAST_SEEN_SINCE)) == "never"
        assert sign_in_status(_add(username="e", created_at=datetime(2026, 8, 1))) == "unknown"

    def test_an_x_login_is_recorded_on_the_account(self, app, monkeypatch):
        """The sign-in itself stamps last_seen_at, so "never signed in" no
        longer depends on the app loading afterwards — for a fresh account
        and for a claimed placeholder alike."""
        placeholder = _add(username="alice", twitter_id="123", approved=False)
        assert placeholder.last_seen_at is None
        _x_login(app, x_id=123, screen_name="alice")
        _db.session.refresh(placeholder)
        assert placeholder.last_seen_at is not None
        assert placeholder.last_seen_path is None  # time only: no area yet
        _, new_id = _x_login(app, x_id=456, screen_name="bob")
        assert User.query.get(new_id).last_seen_at is not None

    def test_an_x_login_keeps_the_activity_tab_area(self, app):
        """Where the person last was is an area of the app; the login must
        not replace it with /auth/login, which the app's bootstrap calls
        would then leave in place until they opened something."""
        from datetime import datetime, timedelta
        returning = _add(username="carol", twitter_id="789", approved=True,
                         last_seen_at=datetime.utcnow() - timedelta(days=3),
                         last_seen_path="/api/log")
        _x_login(app, x_id=789, screen_name="carol")
        _db.session.refresh(returning)
        assert returning.last_seen_path == "/api/log"
        assert datetime.utcnow() - returning.last_seen_at < timedelta(minutes=1)


class TestResolveXId:
    class _Clock:
        """monotonic() advancing a fixed step per call."""
        def __init__(self, step):
            self.now, self.step = 0.0, step

        def monotonic(self):
            self.now += self.step
            return self.now - self.step

    def test_one_deadline_across_the_calls(self, app, monkeypatch):
        """Archive, then X: each call gets what is left of ONE budget, so a
        whitelist can never take three timeouts in a row; past the
        deadline the next call is not made."""
        seen = {}
        monkeypatch.setattr(ca_mod, "fetch_account",
                            lambda h, timeout=None: seen.setdefault("archive", timeout) and None)
        monkeypatch.setattr(x_api, "lookup_user", lambda h, c, timeout=None: seen.update(x=timeout) or {
            "id": "1", "username": "alice", "name": None, "tweet_count": 0, "protected": False})
        admin = _add(username="explore", email="a@example.com", approved=True, is_admin=True)

        monkeypatch.setattr(x_identity, "time", self._Clock(step=5))
        r = x_identity.resolve_x_id("alice", x_lookup=True, cost_user_id=admin.id, timeout=20)
        assert r.x_id == "1"
        assert seen["archive"] == 15 and seen["x"] == 10  # 20 − 5, 20 − 10

        seen.clear()
        monkeypatch.setattr(x_identity, "time", self._Clock(step=12))
        with pytest.raises(x_identity.XIdUnresolved, match="took longer than 20 s") as e:
            x_identity.resolve_x_id("alice", x_lookup=True, cost_user_id=admin.id, timeout=20)
        assert e.value.reason == "x-error" and seen == {"archive": 8}  # X never called
        assert APICostLog.query.count() == 1  # only the first run's read

    def test_cost_is_logged_whenever_the_read_was_sent(self, app, monkeypatch):
        admin = _add(username="explore", email="a@example.com", approved=True, is_admin=True)
        monkeypatch.setattr(ca_mod, "fetch_account", lambda h, timeout=None: None)

        def timed_out(h, c, timeout=None):
            raise x_api.XApiError("X API request failed: timed out", sent=True)
        monkeypatch.setattr(x_api, "lookup_user", timed_out)
        with pytest.raises(x_identity.XIdUnresolved, match="timed out"):
            x_identity.resolve_x_id("alice", x_lookup=True, cost_user_id=admin.id)
        assert APICostLog.query.filter_by(user_id=admin.id, request_type="x_id_lookup").count() == 1

        def no_token(h, c, timeout=None):
            raise x_api.XApiError("X API auth failed (401).")  # sent=False
        monkeypatch.setattr(x_api, "lookup_user", no_token)
        with pytest.raises(x_identity.XIdUnresolved, match="auth failed"):
            x_identity.resolve_x_id("alice", x_lookup=True, cost_user_id=admin.id)
        assert APICostLog.query.filter_by(user_id=admin.id, request_type="x_id_lookup").count() == 1

    def test_the_matched_record_travels_with_the_id(self, app, monkeypatch):
        monkeypatch.setattr(ca_mod, "fetch_account", lambda h, timeout=None: {
            "account_id": "123", "username": "Alice", "account_display_name": "A", "num_tweets": 42})
        r = x_identity.resolve_x_id("alice")
        assert r.account["num_tweets"] == 42 and r.username == "Alice"


class TestFailedCredentialsCheck:
    def test_failed_x_credentials_check_drops_the_token(self, app):
        """Access revoked on X: the stored token is what fails. Kept, every
        retry in this browser fails the same way (twitter.authorized stays
        true); dropped, the next attempt goes to X again."""

        class SessionX:
            """flask-dance's proxy in miniature: authorized iff a token is stored."""
            def __init__(self, resp):
                self.resp = resp

            @property
            def authorized(self):
                from flask import session
                return "twitter_oauth_token" in session

            def get(self, *a, **k):
                return self.resp

        client = app.test_client()
        with client.session_transaction() as sess:
            sess["twitter_oauth_token"] = {"oauth_token": "t", "oauth_token_secret": "s"}
        failing = SessionX(MagicMock(ok=False, status_code=401))
        with patch("backend.routes.auth.twitter", failing):
            first = client.get("/auth/login")
            second = client.get("/auth/login")

        assert first.status_code == 302 and first.headers["Location"] == FRONTEND_URL
        with client.session_transaction() as sess:
            assert "twitter_oauth_token" not in sess
            assert "_user_id" not in sess
        # no token any more → back to X to authorize
        assert second.status_code == 302
        assert second.headers["Location"].endswith("/auth/twitter")


class TestLogout:
    def test_logout_drops_the_stored_x_token(self, app):
        """Left in the session, flask-dance's token let the next "Sign in
        with X" in the same browser skip X and re-enter the account that
        just signed out."""
        user = _add(username="alice", twitter_id="123", approved=True)
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
            sess["twitter_oauth_token"] = {
                "oauth_token": "t", "oauth_token_secret": "s"}

        resp = client.get("/auth/logout")

        assert resp.status_code == 302
        with client.session_transaction() as sess:
            assert "twitter_oauth_token" not in sess
            assert "_user_id" not in sess

    def test_logout_without_a_stored_token(self, app):
        user = _add(username="alice", email="alice@example.com", approved=True)
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)

        resp = client.get("/auth/logout")

        assert resp.status_code == 302
        with client.session_transaction() as sess:
            assert "_user_id" not in sess

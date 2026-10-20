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
def real_app(monkeypatch):
    """The real app from create_app(): every blueprint, the login manager
    and, what the bare app lacks, the approval gate (block_unapproved_users).

    The database is forced to sqlite here, on the Config class itself.
    backend/tests is a package, so pytest imports backend/__init__.py — and
    with it backend.config — BEFORE any test module sets DATABASE_URL;
    Config.SQLALCHEMY_DATABASE_URI is then whatever the shell had, by
    default a Postgres on localhost. create_all()/drop_all() against that
    would create and then DROP the tables of a real database (it did, on a
    stale local one), and fails in CI where no Postgres runs."""
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

    import backend.config as _config
    monkeypatch.setattr(_config.Config, "SQLALCHEMY_DATABASE_URI", "sqlite:///:memory:")
    from backend import create_app
    app = create_app()
    assert app.config["SQLALCHEMY_DATABASE_URI"] == "sqlite:///:memory:", (
        "the real-app tests must never touch a real database")
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

    def test_former_handle_of_another_account_is_not_claimed(self, app):
        """A handle someone else published under and renamed away from
        still redirects to them (#253): a first X login with that screen
        name gets a derived username, like a taken one, instead of
        turning the redirect into an impersonation."""
        from backend.models import UsernameHistory
        owner = _add(username="alice_now", approved=True,
                     public_sharing_enabled=True)
        _db.session.add(UsernameHistory(user_id=owner.id, old_username="alice"))
        _db.session.commit()

        _, logged_in_id = _x_login(app, x_id=123, screen_name="alice")

        created = User.query.get(logged_in_id)
        assert created.id != owner.id
        assert created.username == "alice2"
        assert created.twitter_id == "123"

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
        # what the admin panel needs to offer the paid lookup in a dialog
        assert r.json["reason"] == "not-in-archive"
        assert r.json["x_lookup_cost_usd"] == x_api.COST_PER_USER_READ
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
        assert r.json["reason"] == "archive-error" and "x_lookup_cost_usd" in r.json
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

    def test_x_lookup_fallback_is_explicit_and_billed_to_the_new_account(self, app, monkeypatch):
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
        # the read found this account's id: its own ledger, like its pre-fills
        assert log.user_id == r.json["user"]["id"] and log.user_id != admin.id
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
        assert r.json["reason"] == "not-on-x" and "x_lookup_cost_usd" not in r.json
        # no account came of it: the read stays on the admin's ledger
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

        # found through a paid X read instead: that read is the holder's
        monkeypatch.setattr(ca_mod, "fetch_account", lambda h, timeout=None: None)
        monkeypatch.setattr(x_api, "lookup_user", lambda h, c, timeout=None: {
            "id": "123", "username": "alice", "name": "A", "tweet_count": 1, "protected": False})
        r = client.post("/api/admin/whitelist", json={"handle": "alice", "x_lookup": True})
        assert r.status_code == 409
        assert APICostLog.query.filter_by(request_type="x_id_lookup").one().user_id == holder.id

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
                      kw.pop("dumps_dir"), out=out)
        return result, out.getvalue()

    def test_dry_run_makes_no_paid_call_and_writes_nothing(self, app, tmp_path, monkeypatch):
        placeholder = _add(username="alice", approved=False)
        monkeypatch.setattr(ca_mod, "fetch_account", lambda h, timeout=None: None)
        monkeypatch.setattr(x_api, "lookup_user", _never)

        result, out = self._run(tmp_path, x_lookup=True)

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

    def test_paid_lookup_needs_apply_and_is_billed_to_the_placeholder(self, app, tmp_path, monkeypatch):
        """The read finds the placeholder's own X id: its ledger, like its
        pre-fills. And only with --apply (the dry run stays free)."""
        placeholder = _add(username="alice", approved=False)
        monkeypatch.setattr(ca_mod, "fetch_account", lambda h, timeout=None: None)
        monkeypatch.setattr(x_api, "lookup_user", lambda h, c, timeout=None: {
            "id": "777", "username": "alice", "name": "A", "tweet_count": 1, "protected": False})

        result, _out = self._run(tmp_path, apply=True, x_lookup=True)

        assert result["stamped"] == 1
        log = APICostLog.query.filter_by(request_type="x_id_lookup").one()
        assert log.user_id == placeholder.id
        assert log.model_id == "x-api/user-lookup"
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

    def test_deferred_cost_is_reported_not_logged(self, app, monkeypatch):
        """The whitelist bills the account it creates, which does not exist
        while the lookup runs: with defer_cost the read is counted on the
        result (or the exception), never logged here."""
        monkeypatch.setattr(ca_mod, "fetch_account", lambda h, timeout=None: None)
        monkeypatch.setattr(x_api, "lookup_user", lambda h, c, timeout=None: {
            "id": "1", "username": "alice", "name": None, "tweet_count": 0, "protected": False})
        r = x_identity.resolve_x_id("alice", x_lookup=True, defer_cost=True)
        assert r.paid_reads == 1 and APICostLog.query.count() == 0

        monkeypatch.setattr(x_api, "lookup_user", lambda h, c, timeout=None: None)
        with pytest.raises(x_identity.XIdUnresolved) as e:
            x_identity.resolve_x_id("alice", x_lookup=True, defer_cost=True)
        assert e.value.reason == "not-on-x" and e.value.paid_reads == 1
        assert APICostLog.query.count() == 0

        def no_token(h, c, timeout=None):
            raise x_api.XApiError("X API auth failed (401).")  # sent=False
        monkeypatch.setattr(x_api, "lookup_user", no_token)
        with pytest.raises(x_identity.XIdUnresolved) as e:
            x_identity.resolve_x_id("alice", x_lookup=True, defer_cost=True)
        assert e.value.paid_reads == 0
        # without defer, a user to bill is still required
        with pytest.raises(ValueError):
            x_identity.resolve_x_id("alice", x_lookup=True)

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


# ── Connect X (#311) ─────────────────────────────────────────────────────

def _signed_in(app, user):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
    return client


def _x_callback(client, x_id, screen_name, ok=True):
    """X sends the browser back: flask-dance stored a token and redirects
    to /auth/login, which reads the X account from verify_credentials."""
    x = MagicMock()
    x.authorized = True
    x.get.return_value = MagicMock(
        ok=ok, status_code=200 if ok else 401,
        json=lambda: {"id": x_id, "screen_name": screen_name})
    with patch("backend.routes.auth.twitter", x):
        return client.get("/auth/login")


def _session(client):
    with client.session_transaction() as sess:
        return dict(sess)


def _connect(app, user, x_id, screen_name, token_at_x=False):
    """Connect X from the Account page: start, then X's callback. With
    token_at_x the session holds a flask-dance token when the callback
    lands (what X's authorization leaves behind)."""
    client = _signed_in(app, user)
    start = client.get("/auth/x/connect")
    assert start.status_code == 302
    assert start.headers["Location"].endswith("/auth/twitter")
    if token_at_x:
        with client.session_transaction() as sess:
            sess["twitter_oauth_token"] = {"oauth_token": "t", "oauth_token_secret": "s"}
    return client, _x_callback(client, x_id, screen_name)


def _outcome(resp):
    assert resp.status_code == 302
    prefix = f"{FRONTEND_URL}/account?x_login="
    assert resp.headers["Location"].startswith(prefix), resp.headers["Location"]
    assert resp.headers["Location"].endswith("#x")
    return resp.headers["Location"][len(prefix):-len("#x")]


class TestConnectX:
    def test_connect_attaches_the_x_id_to_the_signed_in_account(self, app):
        alice = _add(username="alice", email="alice@example.com", approved=True)

        client, resp = _connect(app, alice, x_id=123, screen_name="AliceOnX")

        assert _outcome(resp) == "linked"
        _db.session.refresh(alice)
        assert alice.twitter_id == "123"
        assert alice.twitter_handle == "AliceOnX"
        assert alice.username == "alice"  # the handle renames nothing
        assert User.query.count() == 1
        sess = _session(client)
        assert sess.get("_user_id") == str(alice.id)
        assert "x_connect" not in sess

    def test_sign_in_with_x_then_opens_the_same_account(self, app):
        """The point of #311: after connecting, X sign-in on another device
        lands in the magic-link account instead of creating a second one."""
        alice = _add(username="alice", email="alice@example.com", approved=True)
        _connect(app, alice, x_id=123, screen_name="alice")

        _, logged_in_id = _x_login(app, x_id=123, screen_name="alice")

        assert logged_in_id == alice.id
        assert User.query.count() == 1

    @pytest.mark.parametrize("holder_kw, outcome", [
        # someone signs in to it (e.g. the duplicate an earlier "Sign in
        # with X" made for this very person)
        ({"last_seen_at": "now"}, "taken"),
        # set up ahead of its owner (whitelist / pre-fill), never signed in
        ({}, "taken_placeholder"),
        # predates the last-seen record: may be an early X signup
        ({"created_at": "2026-08-01"}, "taken"),
    ])
    def test_x_id_held_by_another_account_is_refused(self, app, holder_kw, outcome):
        from datetime import datetime
        kw = {k: (datetime.utcnow() if v == "now" else datetime.fromisoformat(v))
              for k, v in holder_kw.items()}
        holder = _add(username="alice_x", twitter_id="123", approved=False, **kw)
        alice = _add(username="alice", email="alice@example.com", approved=True)

        client, resp = _connect(app, alice, x_id=123, screen_name="alice_x",
                                token_at_x=True)

        assert _outcome(resp) == outcome
        _db.session.refresh(alice)
        _db.session.refresh(holder)
        assert alice.twitter_id is None and alice.twitter_handle is None
        assert holder.twitter_id == "123"
        sess = _session(client)
        assert sess.get("_user_id") == str(alice.id)
        # the next try asks X again, maybe for another X account
        assert "twitter_oauth_token" not in sess

    def test_an_account_keeps_the_x_id_it_has(self, app):
        alice = _add(username="alice", email="alice@example.com",
                     twitter_id="999", approved=True)

        client, resp = _connect(app, alice, x_id=123, screen_name="other",
                                token_at_x=True)

        assert _outcome(resp) == "other_x"
        _db.session.refresh(alice)
        assert alice.twitter_id == "999"
        assert User.query.filter_by(twitter_id="123").first() is None
        assert "twitter_oauth_token" not in _session(client)

    def test_connecting_the_same_x_account_again_is_fine(self, app):
        alice = _add(username="alice", twitter_id="123",
                     twitter_handle="old_name", approved=True)

        _, resp = _connect(app, alice, x_id=123, screen_name="new_name")

        assert _outcome(resp) == "linked"
        _db.session.refresh(alice)
        assert alice.twitter_id == "123" and alice.twitter_handle == "new_name"

    def test_x_id_taken_between_the_check_and_the_write(self, app, monkeypatch):
        """Another account (a first X sign-in elsewhere) takes the id while
        this connect runs: the unique X id refuses the write."""
        alice = _add(username="alice", email="alice@example.com", approved=True)
        client = _signed_in(app, alice)
        client.get("/auth/x/connect")
        real_commit = _db.session.commit
        raced = {"done": False}

        def racing_commit():
            if raced["done"]:
                return real_commit()
            raced["done"] = True
            _db.session.rollback()
            _db.session.add(User(username="alice_x", twitter_id="123"))
            real_commit()
            raise IntegrityError("UPDATE user", {}, Exception(
                "UNIQUE constraint failed: user.twitter_id"))

        monkeypatch.setattr(_db.session, "commit", racing_commit)
        resp = _x_callback(client, x_id=123, screen_name="alice_x")

        assert _outcome(resp) == "taken"
        _db.session.refresh(alice)
        assert alice.twitter_id is None
        assert User.query.filter_by(twitter_id="123").one().username == "alice_x"

    def test_connect_needs_a_signed_in_account(self, app):
        """Signed out, the start must not fall through to /auth/login: that
        is an X sign-in, and it would make a new account."""
        client = app.test_client()

        resp = client.get("/auth/x/connect")

        assert resp.status_code == 302
        assert resp.headers["Location"] == f"{FRONTEND_URL}/login?returnUrl=%2Faccount"
        assert "x_connect" not in _session(client)

    def test_start_forgets_an_x_token_from_an_earlier_sign_in(self, app):
        alice = _add(username="alice", email="alice@example.com", approved=True)
        client = _signed_in(app, alice)
        with client.session_transaction() as sess:
            sess["twitter_oauth_token"] = {"oauth_token": "t", "oauth_token_secret": "s"}

        client.get("/auth/x/connect")

        sess = _session(client)
        assert "twitter_oauth_token" not in sess
        assert sess["x_connect"] == {"user_id": alice.id}

    def test_cancelling_at_x_returns_to_the_account_page(self, app):
        """X sends the browser back without a token: back to Account, not
        round again to X's page."""
        alice = _add(username="alice", email="alice@example.com", approved=True)
        client = _signed_in(app, alice)
        client.get("/auth/x/connect")
        x = MagicMock()
        x.authorized = False
        with patch("backend.routes.auth.twitter", x):
            resp = client.get("/auth/login")

        assert _outcome(resp) == "cancelled"
        assert "x_connect" not in _session(client)
        _db.session.refresh(alice)
        assert alice.twitter_id is None

    def test_failed_credentials_check_returns_to_the_account_page(self, app):
        bob = _add(username="bob", email="bob@example.com", approved=True)
        client = _signed_in(app, bob)
        client.get("/auth/x/connect")
        with client.session_transaction() as sess:
            sess["twitter_oauth_token"] = {"oauth_token": "t", "oauth_token_secret": "s"}

        resp = _x_callback(client, x_id=456, screen_name="bob", ok=False)

        assert _outcome(resp) == "failed"
        sess = _session(client)
        assert "twitter_oauth_token" not in sess
        assert sess.get("_user_id") == str(bob.id)
        _db.session.refresh(bob)
        assert bob.twitter_id is None

    def test_intent_without_a_session_is_an_ordinary_sign_in(self, app):
        """Signed out since (session expired): the intent is dropped and
        the X login behaves as "Sign in with X" always has."""
        alice = _add(username="alice", email="alice@example.com", approved=True)
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["x_connect"] = {"user_id": alice.id}

        resp = _x_callback(client, x_id=123, screen_name="alice")

        assert resp.headers["Location"] == f"{FRONTEND_URL}/dashboard"
        sess = _session(client)
        assert "x_connect" not in sess
        created = User.query.filter_by(twitter_id="123").one()
        assert sess["_user_id"] == str(created.id) != str(alice.id)
        _db.session.refresh(alice)
        assert alice.twitter_id is None

    def test_intent_of_another_account_is_dropped(self, app):
        """Started by one account, another signed in since: nothing is
        attached to either."""
        alice = _add(username="alice", email="alice@example.com", approved=True)
        bob = _add(username="bob", email="bob@example.com", approved=True)
        client = _signed_in(app, bob)
        with client.session_transaction() as sess:
            sess["x_connect"] = {"user_id": alice.id}

        _x_callback(client, x_id=123, screen_name="bob_x")

        assert "x_connect" not in _session(client)
        _db.session.refresh(alice)
        _db.session.refresh(bob)
        assert alice.twitter_id is None and bob.twitter_id is None

    def test_stale_intent_is_dropped_on_the_way_to_x(self, app):
        """Abandoned at X, then signed out: the next "Sign in with X" goes to
        X as usual and the intent is gone before it comes back."""
        alice = _add(username="alice", email="alice@example.com", approved=True)
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["x_connect"] = {"user_id": alice.id}
        x = MagicMock()
        x.authorized = False
        with patch("backend.routes.auth.twitter", x):
            resp = client.get("/auth/login")

        assert resp.headers["Location"].endswith("/auth/twitter")
        assert "x_connect" not in _session(client)

    def test_logout_drops_the_intent(self, app):
        alice = _add(username="alice", email="alice@example.com", approved=True)
        client = _signed_in(app, alice)
        client.get("/auth/x/connect")

        client.get("/auth/logout")

        assert "x_connect" not in _session(client)

    def test_x_sign_in_keeps_the_shown_handle_current(self, app):
        existing = _add(username="alice", twitter_id="123",
                        twitter_handle="old_name", approved=True)
        _x_login(app, x_id=123, screen_name="new_name")
        _db.session.refresh(existing)
        assert existing.twitter_handle == "new_name"

        _, new_id = _x_login(app, x_id=456, screen_name="bob")
        assert User.query.get(new_id).twitter_handle == "bob"


class TestDisconnectX:
    def test_disconnect_keeps_the_email_login(self, real_app):
        alice = _add(username="alice", email="alice@example.com",
                     twitter_id="123", twitter_handle="alice", approved=True)
        client = _signed_in(real_app, alice)
        with client.session_transaction() as sess:
            sess["twitter_oauth_token"] = {"oauth_token": "t", "oauth_token_secret": "s"}

        r = client.delete("/api/dashboard/x")

        assert r.status_code == 200, r.get_data(as_text=True)[:200]
        assert r.get_json()["twitter_login"] is False
        _db.session.refresh(alice)
        assert alice.twitter_id is None and alice.twitter_handle is None
        assert "twitter_oauth_token" not in _session(client)
        # the dashboard now says so
        user = client.get("/api/dashboard/").get_json()["user"]
        assert user["twitter_login"] is False and user["twitter_handle"] is None

    def test_x_only_account_cannot_disconnect(self, real_app):
        alice = _add(username="alice", twitter_id="123", approved=True)
        client = _signed_in(real_app, alice)

        r = client.delete("/api/dashboard/x")

        assert r.status_code == 400
        _db.session.refresh(alice)
        assert alice.twitter_id == "123"


class TestConnectXExtras:
    def test_a_new_link_is_recorded_and_the_address_told(self, app, monkeypatch):
        import backend.routes.auth as auth_mod
        sent = []
        monkeypatch.setattr(auth_mod, "send_x_connected_notice",
                            lambda to, handle: sent.append((to, handle)))
        alice = _add(username="alice", email="alice@example.com", approved=True)

        _connect(app, alice, x_id=123, screen_name="AliceOnX")

        _db.session.refresh(alice)
        assert alice.x_connected_at is not None
        assert sent == [("alice@example.com", "AliceOnX")]

    def test_no_notice_without_a_new_link(self, app, monkeypatch):
        import backend.routes.auth as auth_mod
        sent = []
        monkeypatch.setattr(auth_mod, "send_x_connected_notice",
                            lambda to, handle: sent.append((to, handle)))
        # X sign-up account reconnecting its own X: nothing new, and it stays
        # an X sign-up (x_connected_at stays null)
        signup = _add(username="bob", email="bob@example.com",
                      twitter_id="456", approved=True)
        _connect(app, signup, x_id=456, screen_name="bob")
        # refused: the id is held elsewhere
        alice = _add(username="alice", email="alice@example.com", approved=True)
        _connect(app, alice, x_id=456, screen_name="bob")

        assert sent == []
        _db.session.refresh(signup)
        assert signup.x_connected_at is None

    def test_the_notice_mail(self, monkeypatch):
        from backend.utils import email as m
        seen = {}
        monkeypatch.setattr(m, "_deliver", lambda to, subj, text, html: seen.update(
            to=to, subj=subj, text=text, html=html))
        m.send_x_connected_notice("a@example.com", "<b>x</b>")
        assert seen["to"] == "a@example.com"
        assert "@<b>x</b>" in seen["text"] and "&lt;b&gt;" in seen["html"]
        # best-effort: a failed send does not fail the connect
        monkeypatch.setattr(m, "_deliver", MagicMock(side_effect=OSError("smtp down")))
        m.send_x_connected_notice("a@example.com", "x")


def _fake_x_exchange(access_x_id=666):
    """Stand-in for the HTTP calls requests_oauthlib makes to X. Request
    tokens come out numbered; the access exchange records what it was
    asked to exchange."""
    calls = {"request_tokens": 0, "exchanged": []}

    def fake_fetch_token(self, url, **kw):
        if "request_token" in url:
            calls["request_tokens"] += 1
            token = {"oauth_token": f"REQ-{calls['request_tokens']}",
                     "oauth_token_secret": "rs", "oauth_callback_confirmed": "true"}
        else:
            calls["exchanged"].append(self._client.client.resource_owner_key)
            token = {"oauth_token": "ACCESS", "oauth_token_secret": "as",
                     "user_id": str(access_x_id), "screen_name": "someone"}
        self._populate_attributes(token)
        return token

    return calls, patch("requests_oauthlib.OAuth1Session._fetch_token", fake_fetch_token)


class TestXCallbackBoundToSession:
    """flask-dance's OAuth 1 callback rebuilt the exchange from the URL alone,
    so a callback URL completed in one browser worked in any other: a login
    CSRF on Sign in with X, and with Connect X an attacker's X login on the
    victim's account. These go through flask-dance's real views; only the
    HTTP calls to X are faked."""

    def test_connect_round_trip_still_works(self, app, monkeypatch):
        import backend.routes.auth as auth_mod
        monkeypatch.setattr(auth_mod, "send_x_connected_notice", lambda *a: None)
        alice = _add(username="alice", email="alice@example.com", approved=True)
        client = _signed_in(app, alice)
        calls, fake = _fake_x_exchange()
        with fake:
            assert client.get("/auth/x/connect").headers["Location"].endswith("/auth/twitter")
            to_x = client.get("/auth/twitter")
            assert "oauth_token=REQ-1" in to_x.headers["Location"]
            assert _session(client)["x_oauth_request_token"] == "REQ-1"
            back = client.get("/auth/twitter/authorized?oauth_token=REQ-1&oauth_verifier=V")
        assert back.headers["Location"].endswith("/auth/login")
        assert calls["exchanged"] == ["REQ-1"]
        sess = _session(client)
        assert sess["twitter_oauth_token"]["oauth_token"] == "ACCESS"
        assert "x_oauth_request_token" not in sess  # used once

        resp = _x_callback(client, x_id=666, screen_name="alice_x")

        assert _outcome(resp) == "linked"
        _db.session.refresh(alice)
        assert alice.twitter_id == "666"

    @pytest.mark.parametrize("victim_went_to_x", [False, True])
    def test_attackers_callback_links_nothing(self, app, victim_went_to_x):
        victim = _add(username="victim", email="v@example.com", approved=True)
        client = _signed_in(app, victim)
        calls, fake = _fake_x_exchange()
        with fake:
            client.get("/auth/x/connect")
            if victim_went_to_x:
                client.get("/auth/twitter")  # the session now waits for REQ-1
            resp = client.get("/auth/twitter/authorized"
                              "?oauth_token=ATTACKER_REQ&oauth_verifier=ATTACKER_V")

        assert _outcome(resp) == "failed"
        assert calls["exchanged"] == []  # never sent to X
        sess = _session(client)
        assert "twitter_oauth_token" not in sess
        assert "x_connect" not in sess
        assert sess.get("_user_id") == str(victim.id)
        _db.session.refresh(victim)
        assert victim.twitter_id is None

    def test_forged_sign_in_callback_signs_nobody_in(self, app):
        _add(username="attacker", twitter_id="666", approved=True)
        client = app.test_client()
        calls, fake = _fake_x_exchange()
        with fake:
            resp = client.get("/auth/twitter/authorized"
                              "?oauth_token=ATTACKER_REQ&oauth_verifier=ATTACKER_V")

        assert resp.headers["Location"] == f"{FRONTEND_URL}/login?error=x_try_again"
        assert calls["exchanged"] == []
        sess = _session(client)
        assert "twitter_oauth_token" not in sess and "_user_id" not in sess

    def test_sign_in_round_trip_works_once(self, app):
        client = app.test_client()
        calls, fake = _fake_x_exchange()
        callback = "/auth/twitter/authorized?oauth_token=REQ-1&oauth_verifier=V"
        with fake:
            client.get("/auth/twitter")
            assert client.get(callback).headers["Location"].endswith("/auth/login")
            with client.session_transaction() as sess:
                del sess["twitter_oauth_token"]
            replay = client.get(callback)

        assert calls["exchanged"] == ["REQ-1"]
        assert replay.headers["Location"] == f"{FRONTEND_URL}/login?error=x_try_again"

    def test_cancel_at_x_still_comes_back_to_account(self, app):
        """X's Cancel returns with denied= and no token: nothing to exchange,
        so it passes the check and Connect X reports the cancel."""
        alice = _add(username="alice", email="alice@example.com", approved=True)
        client = _signed_in(app, alice)
        calls, fake = _fake_x_exchange()
        with fake:
            client.get("/auth/x/connect")
            client.get("/auth/twitter")
            back = client.get("/auth/twitter/authorized?denied=REQ-1")
            assert back.headers["Location"].endswith("/auth/login")
            resp = client.get("/auth/login")

        assert _outcome(resp) == "cancelled"
        assert calls["exchanged"] == []


    @pytest.mark.parametrize("path", ["authorize", "cancel", "x_refuses_exchange"])
    def test_a_connect_never_becomes_a_sign_in(self, app, monkeypatch, path):
        """Whatever X does and however long it takes, a Connect X started by
        the signed-in account ends on its Account page: never a new account
        for the X id (the duplicate #311 is about), never a switch into
        another account. There is no time limit on the intent."""
        import backend.routes.auth as auth_mod
        from requests_oauthlib.oauth1_session import TokenRequestDenied
        monkeypatch.setattr(auth_mod, "send_x_connected_notice", lambda *a: None)
        _add(username="holder", twitter_id="777", approved=True)
        alice = _add(username="alice", email="alice@example.com", approved=True)
        client = _signed_in(app, alice)
        calls, fake = _fake_x_exchange()
        real_fetch = fake.new

        def refusing_fetch(self, url, **kw):
            if "access_token" in url:
                raise TokenRequestDenied("Token request failed with code 401", MagicMock(status_code=401))
            return real_fetch(self, url, **kw)

        with patch("requests_oauthlib.OAuth1Session._fetch_token",
                   refusing_fetch if path == "x_refuses_exchange" else real_fetch):
            client.get("/auth/x/connect")
            client.get("/auth/twitter")
            query = "denied=REQ-1" if path == "cancel" else "oauth_token=REQ-1&oauth_verifier=V"
            back = client.get(f"/auth/twitter/authorized?{query}")
            assert back.headers["Location"].endswith("/auth/login")
            if path == "authorize":
                resp = _x_callback(client, x_id=555, screen_name="alice_x")
            else:
                resp = client.get("/auth/login")  # the real proxy: no token stored

        expected = {"authorize": "linked", "cancel": "cancelled",
                    "x_refuses_exchange": "cancelled"}[path]
        assert _outcome(resp) == expected
        assert _session(client).get("_user_id") == str(alice.id)
        assert User.query.count() == 2  # alice and the holder, nothing new

    def test_a_repeated_oauth_token_is_refused(self, app):
        """The check reads the query the way oauthlib does; two tokens (the
        pending one and another) are refused rather than guessed at."""
        alice = _add(username="alice", email="alice@example.com", approved=True)
        client = _signed_in(app, alice)
        calls, fake = _fake_x_exchange()
        with fake:
            client.get("/auth/x/connect")
            client.get("/auth/twitter")
            resp = client.get("/auth/twitter/authorized"
                              "?oauth_token=REQ-1&oauth_token=ATTACKER_REQ&oauth_verifier=V")

        assert _outcome(resp) == "failed"
        assert calls["exchanged"] == []
        assert "twitter_oauth_token" not in _session(client)

    def test_a_token_hidden_behind_an_encoded_separator_is_not_exchanged(self, app):
        """%3B decodes to ";": however the query is split, the guard and the
        exchange see the same thing (here: no oauth_token), and nothing is
        stored."""
        client = app.test_client()
        calls, fake = _fake_x_exchange()
        with fake:
            resp = client.get("/auth/twitter/authorized"
                              "?oauth_verifier=V%3Boauth_token=ATTACKER_REQ")

        assert resp.headers["Location"].endswith("/auth/login")
        assert calls["exchanged"] == []
        sess = _session(client)
        assert "twitter_oauth_token" not in sess and "_user_id" not in sess

class TestSignInMethodWrites:
    """Remove email and Disconnect X each keep the other way in. Both
    decide on the row their request loaded; the write itself re-checks, so
    two tabs removing one each cannot leave an account with neither."""

    @pytest.mark.parametrize("route, other", [
        ("/api/dashboard/x", "email"),
        ("/api/dashboard/email", "twitter_id"),
    ])
    def test_the_write_rechecks_the_other_method(self, real_app, route, other):
        from sqlalchemy import text
        from flask_login import current_user
        alice = _add(username="alice", email="alice@example.com",
                     twitter_id="123", approved=True)

        @real_app.before_request
        def other_tab_removed_it_meanwhile():
            # This request has loaded the account with both methods; the
            # other request's write lands before this one's.
            _ = current_user.email
            _db.session.execute(text(f'UPDATE "user" SET {other} = NULL WHERE id = :id'),
                                {"id": alice.id})

        r = _signed_in(real_app, alice).delete(route)

        assert r.status_code == 400
        _db.session.rollback()
        row = _db.session.execute(text('SELECT email, twitter_id FROM "user" WHERE id = :id'),
                                  {"id": alice.id}).one()
        # only the other request's removal happened
        assert (row.email is None) == (other == "email")
        assert (row.twitter_id is None) == (other == "twitter_id")

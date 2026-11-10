"""The both-providers question (#599, Peter 2026-11-05).

- The rules for asking (profile_providers.both_providers_ask): on a
  preferred-model change and on a visit (after the first entry, written
  or imported) only while the user has never answered; on an import also
  after a no at least 30 days old; never after a yes, never for an
  account set to AI usage None.
- PUT /dashboard/user takes the user's own answer, strictly true or false,
  and stamps when it was given; the user payload carries the answer
  (null: never answered), the provider a no keeps them to, and where to
  ask.
- An import's result asks only when it added entries AI may read.
Follows the real-app + sqlite pattern of test_prefill_consent.py.
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

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login  # noqa: E402
from backend.extensions import db as _db  # noqa: E402
from backend.models import Node, User  # noqa: E402
import backend.models as _real_backend_models  # noqa: E402

NOW = datetime(2026, 11, 6, 12, 0)


def _make_app():
    from flask_login import LoginManager
    from backend.config import Config
    app = Flask(__name__)
    app.config.update(
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:", SECRET_KEY="t",
        TESTING=True, SUPPORTED_MODELS=Config.SUPPORTED_MODELS,
        DEFAULT_LLM_MODEL="claude-opus-5.5")
    _db.init_app(app)
    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from backend.routes.admin import admin_bp
    from backend.routes.dashboard import dashboard_bp
    app.register_blueprint(admin_bp, url_prefix="/api/admin")
    app.register_blueprint(dashboard_bp, url_prefix="/api/dashboard")
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


_N = [0]


def _user(**kw):
    _N[0] += 1
    u = User(username=f"bp{_N[0]}", approved=True, plan="alpha", **kw)
    _db.session.add(u)
    _db.session.commit()
    return u


def _entry(user, **kw):
    kw.setdefault("node_type", "user")
    n = Node(user_id=user.id, token_count=1, ai_usage="chat", **kw)
    n.set_content("synthetic")
    _db.session.add(n)
    _db.session.commit()
    return n


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True


def _ask(user, now=NOW):
    from backend.utils.profile_providers import both_providers_ask
    return both_providers_ask(user, now=now)


# ── the rules ─────────────────────────────────────────────────────────────

def test_never_answered_asks_on_model_change_and_import(app):
    u = _user()
    assert _ask(u) == {"model": True, "visit": False, "import": True}


def test_a_visit_asks_after_the_first_entry_written_or_imported(app):
    written = _user()
    _entry(written)
    assert _ask(written)["visit"] is True
    imported = _user()
    _entry(imported, origin="chatgpt", source_key="k1")
    assert _ask(imported)["visit"] is True


def test_what_is_not_an_entry(app):
    u = _user()
    # The prompt node a Voice session starts with, a deleted entry, and
    # an AI reply the user owns are not entries of theirs.
    _entry(u, prompt_key="voice")
    _entry(u, deleted_at=NOW)
    llm = _user()
    _entry(llm, node_type="llm", human_owner_id=u.id)
    assert _ask(u)["visit"] is False


def test_a_yes_is_never_asked_again(app):
    from backend.utils.profile_providers import set_both_providers
    u = _user()
    _entry(u)
    set_both_providers(u, True, now=NOW - timedelta(days=400))
    _db.session.commit()
    assert _ask(u) == {"model": False, "visit": False, "import": False}


@pytest.mark.parametrize("age, again", [
    (timedelta(days=29, hours=23), False),
    (timedelta(days=30), True),
    (timedelta(days=90), True)])
def test_a_no_is_asked_again_only_on_an_import_a_month_later(app, age, again):
    from backend.utils.profile_providers import set_both_providers
    u = _user()
    _entry(u)
    set_both_providers(u, False, now=NOW - age)
    _db.session.commit()
    assert _ask(u) == {"model": False, "visit": False, "import": again}


def test_an_account_set_to_no_ai_is_never_asked(app):
    u = _user(default_ai_usage="none")
    _entry(u)
    assert _ask(u) == {"model": False, "visit": False, "import": False}


def test_an_import_asks_only_when_it_added_entries_ai_may_read(app):
    from backend.utils.profile_providers import ask_after_import
    u = _user()
    assert ask_after_import(u, "chat", 3) is True
    assert ask_after_import(u, "train", 1) is True
    assert ask_after_import(u, "none", 3) is False
    assert ask_after_import(u, "chat", 0) is False
    u.profile_both_providers = True
    u.profile_both_providers_answered_at = NOW
    _db.session.commit()
    assert ask_after_import(u, "chat", 3) is False


def test_the_consent_is_read_from_the_database(app):
    """The pass's check reads the answer as stored now, not the value a
    session loaded before the user withdrew it."""
    from backend.utils.profile_providers import agreed_to_both
    u = _user(profile_both_providers=True)
    assert agreed_to_both(u) is True
    _db.session.execute(User.__table__.update()
                        .where(User.__table__.c.id == u.id)
                        .values(profile_both_providers=False))
    assert u.profile_both_providers is True       # the stale object
    assert agreed_to_both(u) is False


# ── the route and the payload ─────────────────────────────────────────────

def test_the_payload_before_any_answer(app):
    u = _user(preferred_model="gpt-6-astra")
    _entry(u)
    client = app.test_client()
    _login(client, u.id)
    data = client.get("/api/dashboard/?profile=0").get_json()["user"]
    assert data["profile_both_providers"] is None       # never answered
    assert data["profile_both_providers_answered_at"] is None
    assert data["profile_provider"] == "openai"
    assert data["ask_both_providers"] == {
        "model": True, "visit": True, "import": True}


@pytest.mark.parametrize("answer", [True, False])
def test_the_user_answers(app, answer):
    u = _user()
    client = app.test_client()
    _login(client, u.id)
    before = datetime.utcnow()
    resp = client.put("/api/dashboard/user",
                      json={"profile_both_providers": answer})
    assert resp.status_code == 200
    data = resp.get_json()["user"]
    assert data["profile_both_providers"] is answer
    assert data["profile_both_providers_answered_at"].endswith("Z")
    assert data["profile_provider"] == "anthropic"
    assert data["ask_both_providers"] == {
        "model": False, "visit": False, "import": False}
    u = _db.session.get(User, u.id)
    assert u.profile_both_providers is answer
    assert u.profile_both_providers_answered_at >= before


def test_changing_the_answer_restamps_it(app):
    u = _user()
    old = datetime.utcnow() - timedelta(days=50)
    u.profile_both_providers = True
    u.profile_both_providers_answered_at = old
    _db.session.commit()
    client = app.test_client()
    _login(client, u.id)
    resp = client.put("/api/dashboard/user",
                      json={"profile_both_providers": False})
    assert resp.status_code == 200
    u = _db.session.get(User, u.id)
    assert u.profile_both_providers is False
    assert u.profile_both_providers_answered_at > old + timedelta(days=49)


@pytest.mark.parametrize("bad", ["yes", "true", 1, 0, None, [], {}])
def test_only_true_or_false_is_taken(app, bad):
    u = _user()
    client = app.test_client()
    _login(client, u.id)
    resp = client.put("/api/dashboard/user",
                      json={"profile_both_providers": bad})
    assert resp.status_code == 400
    u = _db.session.get(User, u.id)
    assert u.profile_both_providers is False
    assert u.profile_both_providers_answered_at is None


def test_other_settings_leave_the_answer_alone(app):
    u = _user()
    client = app.test_client()
    _login(client, u.id)
    resp = client.put("/api/dashboard/user",
                      json={"preferred_model": "gpt-6-astra"})
    assert resp.status_code == 200
    data = resp.get_json()["user"]
    # A changed preferred model: the client asks ("model"), and a no would
    # keep the user to the new model's provider.
    assert data["profile_both_providers"] is None
    assert data["profile_provider"] == "openai"
    assert data["ask_both_providers"]["model"] is True
    assert _db.session.get(User, u.id).profile_both_providers_answered_at \
        is None


def test_no_admin_route_sets_the_answer(app):
    """Only the user's own PUT /dashboard/user writes the flag: no admin
    route names it."""
    import inspect
    import backend.routes.admin as admin_routes
    assert "profile_both_providers" not in inspect.getsource(admin_routes)

"""A.112: the admin Activity tab counts the artifacts a user created
without naming them.

Peter, 2026-11-20: nobody other than the user sees the names of the
artifacts they create; admins never read users' content through the API.
Views of those artifacts are still counted: the tab shows one entry,
"user-created", with the number of views and of different artifacts.
Names Loore defines (UserArtifact.SYSTEM_KINDS) stay.

The real admin and artifacts blueprints on sqlite, patterned after
test_admin_access.py.
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
from flask import Flask, g  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login  # noqa: E402
from backend.extensions import db as _db  # noqa: E402
from backend.models import ArtifactView, User, UserArtifact  # noqa: E402
import backend.models as _real_backend_models  # noqa: E402
from backend.utils import activity as act  # noqa: E402

# Invented names, distinctive enough that any trace of them shows.
FIRST = "orchid-ledger-zq"
SECOND = "quartz-diary-vm"
NEVER_MADE = "never-made-kj"


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
    from backend.routes.artifacts import artifacts_bp
    app.register_blueprint(admin_bp, url_prefix="/api/admin")
    app.register_blueprint(artifacts_bp, url_prefix="/api/artifacts")
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
def people(app):
    week_ago = datetime.utcnow() - timedelta(days=7)
    admin = User(username="steward", approved=True, is_admin=True)
    reader = User(username="reader", approved=True, approved_at=week_ago)
    other = User(username="bystander", approved=True, is_admin=False)
    _db.session.add_all([admin, reader, other])
    _db.session.commit()
    return {"admin": admin, "reader": reader, "other": other}


def _client(app, user):
    # Flask-Login caches the user in g, which these requests share.
    g.pop("_login_user", None)
    client = app.test_client()
    if user is not None:
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
            sess["_fresh"] = True
    return client


def _make(user, kind, versions=1):
    rows = []
    for i in range(versions):
        row = UserArtifact(user_id=user.id, kind=kind, title=kind.title(),
                           generated_by="test", ai_usage="chat")
        row.set_content(f"version {i + 1}")
        _db.session.add(row)
        rows.append(row)
    _db.session.commit()
    return rows


def _activity_row(app, admin, username):
    resp = _client(app, admin).get("/api/admin/activity")
    assert resp.status_code == 200
    rows = [u for u in resp.get_json()["users"] if u["username"] == username]
    assert len(rows) == 1
    return resp.get_data(as_text=True), rows[0]


def test_admin_sees_counts_not_names(app, people):
    reader = people["reader"]
    _make(reader, FIRST, versions=2)
    _make(reader, SECOND)
    client = _client(app, reader)
    for kind in [FIRST, FIRST, FIRST, SECOND, SECOND, "intentions"]:
        assert client.post(f"/api/artifacts/{kind}/viewed").status_code == 204
    # A name the user has no artifact under is not stored.
    assert client.post(f"/api/artifacts/{NEVER_MADE}/viewed").status_code == 204
    # A last-seen path stored before the change, with a name in it.
    reader.last_seen_at = datetime.utcnow()
    reader.last_seen_path = f"/api/artifacts/{FIRST}/versions"
    _db.session.commit()

    text, row = _activity_row(app, people["admin"], "reader")
    for name in (FIRST, SECOND, NEVER_MADE):
        assert name not in text
    own = [v for v in row["artifact_views"] if v.get("user_created")]
    assert len(own) == 1
    assert (own[0]["kind"], own[0]["count"], own[0]["artifacts"]) == (
        "user-created", 5, 2)
    # A name Loore defines keeps its name.
    assert [(v["kind"], v["count"]) for v in row["artifact_views"]
            if not v.get("user_created")] == [("intentions", 1)]
    assert row["last_seen_path"] == "/api/artifacts/user-created/versions"

    # Nothing stored names the artifacts either.
    stored = {(v.kind, v.artifact_id) for v in ArtifactView.query.all()}
    first_id = UserArtifact.first_live_id(reader.id, FIRST)
    second_id = UserArtifact.first_live_id(reader.id, SECOND)
    assert stored == {(ArtifactView.USER_CREATED, first_id),
                      (ArtifactView.USER_CREATED, second_id),
                      ("intentions", None)}


def test_rows_stored_with_a_name_are_counted_not_sent(app, people):
    """Views stored before the change hold the name. They are counted
    with the artifact's newer views and never sent."""
    reader = people["reader"]
    _make(reader, FIRST)
    yesterday = datetime.utcnow() - timedelta(days=1)
    _db.session.add_all([
        ArtifactView(user_id=reader.id, kind=FIRST, viewed_at=yesterday),
        ArtifactView(user_id=reader.id, kind=FIRST, viewed_at=yesterday),
        # An artifact that is gone: still one artifact, still unnamed.
        ArtifactView(user_id=reader.id, kind=SECOND, viewed_at=yesterday),
        ArtifactView(user_id=reader.id, kind="memory", viewed_at=yesterday),
    ])
    _db.session.commit()
    assert _client(app, reader).post(
        f"/api/artifacts/{FIRST}/viewed").status_code == 204

    text, row = _activity_row(app, people["admin"], "reader")
    assert FIRST not in text and SECOND not in text
    own = next(v for v in row["artifact_views"] if v.get("user_created"))
    assert (own["count"], own["artifacts"]) == (4, 2)
    assert {v["kind"] for v in row["artifact_views"]} == {"user-created", "memory"}


def test_a_user_with_no_views_of_their_own_artifacts_gets_no_entry(app, people):
    reader = people["reader"]
    assert _client(app, reader).post(
        "/api/artifacts/memory/viewed").status_code == 204
    _, row = _activity_row(app, people["admin"], "reader")
    assert [v["kind"] for v in row["artifact_views"]] == ["memory"]


def test_control_only_admins_can_read_activity(app, people):
    assert _client(app, people["other"]).get(
        "/api/admin/activity").status_code == 403
    assert _client(app, None).get(
        "/api/admin/activity").status_code in (401, 403)
    assert _client(app, people["admin"]).get(
        "/api/admin/activity").status_code == 200


def _digest_kind():
    """DIGEST_KIND, read from its module's source: importing the task
    module would start the Celery app."""
    import ast
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "tasks"
              / "external_digest.py").read_text()
    for node in ast.parse(source).body:
        if (isinstance(node, ast.Assign)
                and [t.id for t in node.targets
                     if isinstance(t, ast.Name)] == ["DIGEST_KIND"]):
            return node.value.value
    raise AssertionError("DIGEST_KIND not found")


def test_every_name_loore_defines_is_on_the_list():
    DIGEST_KIND = _digest_kind()
    kinds = UserArtifact.SYSTEM_KINDS
    assert set(UserArtifact.DEFAULT_KINDS) <= kinds
    assert set(UserArtifact.RESERVED_KINDS) <= kinds
    assert DIGEST_KIND in kinds
    # The marker stored for a user-created view can't be anyone's name.
    from backend.routes.artifacts import _KIND_RE
    assert not _KIND_RE.match(ArtifactView.USER_CREATED)
    assert ArtifactView.USER_CREATED not in kinds


@pytest.mark.parametrize("path, stored", [
    (f"/api/artifacts/{FIRST}", "/api/artifacts/user-created"),
    (f"/api/artifacts/{FIRST}/viewed", "/api/artifacts/user-created/viewed"),
    (f"/api/artifacts/{FIRST}/revert/7", "/api/artifacts/user-created/revert/7"),
    (f"/api/artifacts/{FIRST}/{SECOND}", "/api/artifacts/user-created"),
    # A user's artifact may be called "versions" too.
    ("/api/artifacts/versions", "/api/artifacts/user-created"),
    # Control: names Loore defines, version ids and other areas stay.
    ("/api/artifacts/intentions/versions", "/api/artifacts/intentions/versions"),
    ("/api/artifacts/versions/12/changes", "/api/artifacts/versions/12/changes"),
    ("/api/artifacts/", "/api/artifacts/"),
    ("/api/nodes/42", "/api/nodes/42"),
])
def test_last_seen_path_stores_no_artifact_name(app, path, stored):
    u = User(username="walker", approved=True)
    _db.session.add(u)
    _db.session.commit()
    assert act.touch_last_seen(u, path, now=datetime(2026, 11, 20, 9)) is True
    assert u.last_seen_path == stored


def test_cleanup_of_a_deleted_artifact_removes_its_views(app, people):
    from backend.utils.artifact_deletion import (
        purge_deleted_artifacts, soft_delete_artifact,
    )
    reader = people["reader"]
    _make(reader, FIRST, versions=2)
    _make(reader, SECOND)
    client = _client(app, reader)
    for kind in (FIRST, FIRST, SECOND):
        client.post(f"/api/artifacts/{kind}/viewed")
    soft_delete_artifact(reader.id, FIRST,
                         now=datetime.utcnow() - timedelta(days=31))
    _db.session.commit()

    assert purge_deleted_artifacts(
        datetime.utcnow() - timedelta(days=30))["errors"] == 0
    second_id = UserArtifact.first_live_id(reader.id, SECOND)
    assert [(v.kind, v.artifact_id) for v in ArtifactView.query.all()] == [
        (ArtifactView.USER_CREATED, second_id)]

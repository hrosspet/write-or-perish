"""A.102: `flask artifact-names-cleanup` removes the names users gave
their artifacts from the records written before #729 (view rows and
last-seen paths), and the Activity tab counts the same before and after.

Peter, 2026-11-20: nobody other than the user sees the names of the
artifacts they create. The command prints counts only.

The real admin and artifacts blueprints, on sqlite, and also on a
throwaway Postgres database when LOORE_TEST_POSTGRES_URL names one (it
creates and drops every table in it), as test_invites_postgres.py.
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
from sqlalchemy import select  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login  # noqa: E402
from backend.extensions import db as _db  # noqa: E402
from backend.models import ArtifactView, User, UserArtifact  # noqa: E402
import backend.models as _real_backend_models  # noqa: E402
from backend.utils import activity as act  # noqa: E402
from backend.utils import artifact_name_cleanup as cleanup  # noqa: E402

PG_URL = os.environ.get("LOORE_TEST_POSTGRES_URL")
DB_URLS = ["sqlite:///:memory:"] + ([PG_URL] if PG_URL else [])

# Invented names, distinctive enough that any trace of them shows.
LIVE = "orchid-ledger-zq"        # the user has it (two versions)
DELETED = "quartz-diary-vm"      # deleted, not yet removed by the cleanup
NEVER = "never-made-kj"          # no artifact was ever made under it
REMADE = "amber-index-rw"        # deleted, then made again
SEEN_NEW = "cobalt-notes-xp"     # viewed after #729, then deleted
WITH_ID = "tin-atlas-qd"         # a row with an id and a name
NAMES = (LIVE, DELETED, NEVER, REMADE, SEEN_NEW, WITH_ID)

NOW = datetime.utcnow()


def _ago(days):
    return NOW - timedelta(days=days)


def _make_app(db_uri):
    from flask_login import LoginManager

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = db_uri
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    _db.init_app(app)
    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return _db.session.get(User, int(user_id))

    from backend.routes.admin import admin_bp
    from backend.routes.artifacts import artifacts_bp
    app.register_blueprint(admin_bp, url_prefix="/api/admin")
    app.register_blueprint(artifacts_bp, url_prefix="/api/artifacts")
    return app


@pytest.fixture(params=DB_URLS, ids=lambda u: u.split(":", 1)[0])
def app(request):
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

    app = _make_app(request.param)
    with app.app_context():
        _db.drop_all()
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()

    for k in [k for k in list(sys.modules) if _affected(k)]:
        if k not in saved:
            del sys.modules[k]
    for k, mod in saved.items():
        sys.modules[k] = mod


def _client(app, user):
    # Flask-Login caches the user in g, which these requests share.
    g.pop("_login_user", None)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return client


def _make(user, kind, versions=1):
    for i in range(versions):
        row = UserArtifact(user_id=user.id, kind=kind, title="T",
                           generated_by="test", ai_usage="chat")
        row.set_content(f"version {i + 1}")
        _db.session.add(row)
    _db.session.commit()


def _delete(user, kind, days_ago=1):
    from backend.utils.artifact_deletion import soft_delete_artifact
    soft_delete_artifact(user.id, kind, now=_ago(days_ago))
    _db.session.commit()


def _old_views(user, kind, *days):
    """View rows as the code before #729 stored them: the name in kind."""
    _db.session.add_all(ArtifactView(user_id=user.id, kind=kind,
                                     viewed_at=_ago(d)) for d in days)
    _db.session.commit()


@pytest.fixture
def world(app):
    """Two users with records from before #729 covering every case."""
    month_ago = _ago(30)
    admin = User(username="steward", approved=True, is_admin=True,
                 approved_at=month_ago)
    reader = User(username="reader", approved=True, approved_at=month_ago)
    other = User(username="bystander", approved=True, approved_at=month_ago)
    walker = User(username="walker", approved=True, approved_at=month_ago)
    _db.session.add_all([admin, reader, other, walker])
    _db.session.commit()

    _make(reader, LIVE, versions=2)
    _make(reader, DELETED)
    _make(reader, REMADE)
    _make(reader, SEEN_NEW)
    _old_views(reader, LIVE, 20, 10, 2)
    _old_views(reader, DELETED, 15, 3)
    _old_views(reader, NEVER, 12, 1)
    _old_views(reader, REMADE, 18, 4)
    _old_views(reader, SEEN_NEW, 9)
    _old_views(reader, "intentions", 8, 2)
    _old_views(reader, "memory", 6)
    _delete(reader, DELETED)
    _delete(reader, REMADE)
    _make(reader, REMADE)
    # Views since #729, stored by id; then SEEN_NEW is deleted, so its
    # first version's id already stands for a separately counted artifact.
    client = _client(app, reader)
    for kind in (LIVE, SEEN_NEW):
        assert client.post(f"/api/artifacts/{kind}/viewed").status_code == 204
    _delete(reader, SEEN_NEW, days_ago=0)
    # A row with an id that still holds a name (no code writes one).
    _db.session.add(ArtifactView(
        user_id=reader.id, kind=WITH_ID, viewed_at=_ago(5),
        artifact_id=UserArtifact.first_live_id(reader.id, LIVE)))
    # Another user: the same name, no artifact under it.
    _old_views(other, LIVE, 7)
    _old_views(other, "todo", 7)

    seen = {reader: f"/api/artifacts/{LIVE}/versions",
            other: f"/api/artifacts/{NEVER}/revert/7",
            admin: "/api/artifacts/intentions/versions",
            walker: "/api/artifacts/versions/12/changes"}
    for u, path in seen.items():
        u.last_seen_path = path
        u.last_seen_at = datetime(2026, 11, 19, 8, 30)
    _db.session.commit()
    return {"admin": admin, "reader": reader, "other": other,
            "walker": walker}


def _table(model):
    rows = _db.session.execute(select(model.__table__)).all()
    return sorted(tuple(r) for r in rows)


def _tab(app, world):
    """What the Activity tab shows, and the artifact counts over windows
    that start before, between and after the stored views."""
    resp = _client(app, world["admin"]).get("/api/admin/activity")
    assert resp.status_code == 200
    shown = {u["username"]: (u["artifact_views"], u["last_seen_path"])
             for u in resp.get_json()["users"]}
    windows = {(u.username, since): act.artifact_views(u.id, since)
               for u in world.values()
               for since in (datetime(2000, 1, 1), _ago(11), _ago(5),
                             _ago(2))}
    return shown, windows


def _run(app, *args):
    from backend.activity_cli import artifact_names_cleanup_command
    return app.test_cli_runner().invoke(artifact_names_cleanup_command,
                                        list(args))


def _assert_no_name(text):
    for name in NAMES:
        assert name not in text


def _stored_names():
    kinds = {k for (k,) in _db.session.query(ArtifactView.kind)}
    paths = {p for (p,) in _db.session.query(User.last_seen_path)}
    return {n for n in NAMES
            if n in kinds or any(p and n in p for p in paths)}


def test_dry_run_counts_and_changes_nothing(app, world):
    views, users = _table(ArtifactView), _table(User)
    r = _run(app)
    assert r.exit_code == 0, r.output
    _assert_no_name(r.output)
    assert _table(ArtifactView) == views and _table(User) == users
    lines = r.output.splitlines()
    assert lines[0].endswith("Dry run: nothing is changed.")
    assert lines[1:] == [
        "artifact_view: 18 rows",
        "  to change: 12 rows (7 names, 2 users)",
        "    5 rows (2 names): the artifact exists; stored by the id of "
        "its first version",
        "    2 rows (1 name): the artifact is deleted and waits for the "
        "daily cleanup; stored by the id of its earliest deleted version, "
        "so the cleanup removes them with it",
        "    4 rows (3 names): no artifact is left under the name, or its "
        "id already counts views stored since #729; stored by a negative "
        "number, one per name",
        "    1 row (1 name): already stored by id; only the name is "
        "removed",
        "  left as they are: 4 rows with a name Loore defines, 2 rows "
        "stored without a name",
        "user.last_seen_path:",
        "  to change: 2 values",
        "  left as they are: 2 artifact addresses without a name a user "
        "gave",
        "Run again with --apply to make these changes.",
    ]


def test_apply_removes_the_names_and_keeps_the_counts(app, world):
    before = _tab(app, world)
    reader_tab = before[0]["reader"][0]
    own = next(v for v in reader_tab if v.get("user_created"))
    # 13 views of 6 artifacts: SEEN_NEW counts twice (by id since #729,
    # by name before), as the tab counted it before the clean-up.
    assert (own["count"], own["artifacts"]) == (13, 6)
    views_before = _table(ArtifactView)

    r = _run(app, "--apply")
    assert r.exit_code == 0, r.output
    _assert_no_name(r.output)
    assert "  changed: 12 rows (7 names, 2 users)" in r.output
    assert "  changed: 2 values" in r.output
    assert _stored_names() == set()
    assert _tab(app, world) == before

    # Rows of the names Loore defines and rows stored by id since #729
    # are exactly as they were; the others keep their user and time.
    after = _table(ArtifactView)
    untouched = [v for v in views_before
                 if v[2] in UserArtifact.SYSTEM_KINDS
                 or v[2] == ArtifactView.USER_CREATED]
    assert all(v in after for v in untouched)
    assert ([(v[0], v[1], v[4]) for v in after]
            == [(v[0], v[1], v[4]) for v in views_before])
    reader = world["reader"]
    assert {v.artifact_id for v in ArtifactView.query.filter_by(
        user_id=reader.id, kind=ArtifactView.USER_CREATED)} >= {
        UserArtifact.first_live_id(reader.id, LIVE),
        UserArtifact.first_live_id(reader.id, REMADE)}
    # What #729 stores for a new visit to the same address.
    assert world["reader"].last_seen_path == (
        "/api/artifacts/user-created/versions")
    assert world["other"].last_seen_path == (
        "/api/artifacts/user-created/revert/7")
    assert world["admin"].last_seen_path == (
        "/api/artifacts/intentions/versions")


def test_second_run_changes_nothing(app, world):
    assert _run(app, "--apply").exit_code == 0
    views, users = _table(ArtifactView), _table(User)
    again = _run(app, "--apply")
    assert again.exit_code == 0, again.output
    assert "  changed: 0 rows (0 names, 0 users)" in again.output
    assert "  changed: 0 values" in again.output
    assert _table(ArtifactView) == views and _table(User) == users
    dry = _run(app)
    assert "  to change: 0 rows (0 names, 0 users)" in dry.output
    assert "  to change: 0 values" in dry.output
    assert "--apply to make" not in dry.output


def test_only_the_path_changes_on_the_account(app, world):
    assert all(c.onupdate is None and c.server_onupdate is None
               for c in User.__table__.c)
    path_col = list(User.__table__.c.keys()).index("last_seen_path")
    users = _table(User)
    assert _run(app, "--apply").exit_code == 0
    strip = lambda rows: [r[:path_col] + r[path_col + 1:]  # noqa: E731
                          for r in rows]
    assert strip(_table(User)) == strip(users)
    assert world["reader"].last_seen_at == datetime(2026, 11, 19, 8, 30)


def test_a_stopped_run_leaves_whole_names_and_the_next_one_finishes(
        app, world, monkeypatch):
    before = _tab(app, world)
    real_key_for = cleanup.key_for
    calls = []

    def failing_key_for(user_id, kind):
        calls.append(kind)
        if len(calls) == 3:
            # A database error quotes the statement's values.
            raise RuntimeError(f"failed on {kind}")
        return real_key_for(user_id, kind)

    monkeypatch.setattr(cleanup, "key_for", failing_key_for)
    r = _run(app, "--apply", "--batch-size", "1")
    assert r.exit_code != 0
    assert "Stopped by RuntimeError" in r.output
    _assert_no_name(r.output)
    # The first two names are done, every row of them; the rest untouched.
    remaining = {k for (k,) in _db.session.query(ArtifactView.kind).filter(
        ArtifactView.user_id == world["reader"].id)}
    assert set(calls[:2]).isdisjoint(remaining)
    assert {LIVE, DELETED, NEVER, REMADE, SEEN_NEW} - set(calls[:2]) <= (
        remaining)
    assert _tab(app, world) == before

    monkeypatch.setattr(cleanup, "key_for", real_key_for)
    r = _run(app, "--apply", "--batch-size", "1")
    assert r.exit_code == 0, r.output
    assert _stored_names() == set()
    assert _tab(app, world) == before


def test_the_daily_cleanup_still_removes_a_deleted_artifacts_views(app):
    from backend.utils.artifact_deletion import purge_deleted_artifacts
    reader = User(username="reader", approved=True)
    _db.session.add(reader)
    _db.session.commit()
    _make(reader, DELETED, versions=2)
    _make(reader, LIVE)
    _old_views(reader, DELETED, 40, 35)
    _old_views(reader, LIVE, 40)
    _delete(reader, DELETED, days_ago=31)
    assert _run(app, "--apply").exit_code == 0
    assert _stored_names() == set()

    assert purge_deleted_artifacts(_ago(30))["errors"] == 0
    left = [(v.kind, v.artifact_id) for v in ArtifactView.query.all()]
    assert left == [(ArtifactView.USER_CREATED,
                     UserArtifact.first_live_id(reader.id, LIVE))]


def test_control_nothing_to_change(app):
    """Control: records with no user-given name are left alone."""
    u = User(username="plain", approved=True,
             last_seen_path="/api/nodes/42")
    _db.session.add(u)
    _db.session.commit()
    _old_views(u, "intentions", 3)
    views, users = _table(ArtifactView), _table(User)
    r = _run(app, "--apply")
    assert r.exit_code == 0, r.output
    assert "  changed: 0 rows (0 names, 0 users)" in r.output
    assert "  left as they are: 1 row with a name Loore defines" in r.output
    assert _table(ArtifactView) == views and _table(User) == users

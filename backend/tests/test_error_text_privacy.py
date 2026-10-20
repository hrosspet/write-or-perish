"""No raw exception text in responses, logs or Sentry (Peter, 2026-10-20).

A database error's text quotes the row it refused (the SQL parameters and
Postgres's DETAIL line), so a refused save can carry what the user typed.
Responses carry a message written for the user, logs and Sentry keep the
exception's type and stack frames, and the engine hides SQL parameters.
"""
import io
import json
import logging
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
import sentry_sdk  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.exc import DataError, IntegrityError, OperationalError  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login  # noqa: E402
from backend.extensions import db as _db  # noqa: E402
from backend.models import User  # noqa: E402
import backend.models as _real_backend_models  # noqa: E402
from backend.utils import error_privacy  # noqa: E402

# Built at run time, so the marker is in no source line a traceback quotes
MARKER = "-".join(["typed", "by", "the", "user"])


def _affected(k):
    return (k == "flask_login" or k.startswith("backend.routes")
            or k == "backend.models")


@pytest.fixture
def app(monkeypatch):
    """create_app() on sqlite: Config's engine options and log filter."""
    saved = {k: sys.modules[k] for k in list(sys.modules) if _affected(k)}
    sys.modules["flask_login"] = _real_flask_login
    sys.modules["backend.models"] = _real_backend_models
    for _k in [k for k in list(sys.modules) if k.startswith("backend.routes")]:
        del sys.modules[_k]
    import backend.config as _config
    monkeypatch.setattr(_config.Config, "SQLALCHEMY_DATABASE_URI",
                        "sqlite:///:memory:")
    from backend import create_app
    app = create_app()
    assert app.config["SQLALCHEMY_DATABASE_URI"] == "sqlite:///:memory:"
    app.config["TESTING"] = True
    with app.app_context():
        _db.create_all()
        _db.session.add(User(username="alice", approved=True, plan="alpha"))
        _db.session.commit()
        yield app
        _db.session.remove()
        _db.drop_all()
    for k in [k for k in list(sys.modules) if _affected(k)]:
        if k not in saved:
            del sys.modules[k]
    for k, mod in saved.items():
        sys.modules[k] = mod


@pytest.fixture
def client(app):
    user = User.query.filter_by(username="alice").one()
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return c


class _PgError(Exception):
    """What psycopg2 raises: the message quotes the row in its DETAIL
    line; pgcode and diag carry codes and schema names."""
    pgcode = "23502"

    class diag:  # noqa: N801 - psycopg2's attribute name
        constraint_name = None
        table_name = "user_profile"
        column_name = "user_id"


def _db_error(cls=DataError):
    orig = _PgError(
        'null value in column "user_id" violates not-null constraint\n'
        f"DETAIL:  Failing row contains (7, {MARKER}\nsecond line, null).\n")
    return cls('INSERT INTO user_profile (content) VALUES (%(content)s)',
               {"content": MARKER}, orig)


def _refusing_commit(monkeypatch, cls=DataError):
    def commit():
        raise _db_error(cls)
    monkeypatch.setattr(_db.session, "commit", commit)


def _assert_clean(response, caplog, status):
    body = response.get_data(as_text=True)
    assert response.status_code == status, body
    assert MARKER not in body
    assert "details" not in (response.get_json() or {})
    assert MARKER not in caplog.text
    for record in caplog.records:
        assert MARKER not in record.getMessage()


# --- Responses and log lines -----------------------------------------------

@pytest.mark.parametrize("method,path,payload,error", [
    ("put", "/api/dashboard/user", {"description": "new text"},
     "Failed to update profile."),
    ("patch", "/api/dashboard/timezone", {"timezone": "Europe/Prague"},
     "Failed to update timezone."),
    ("post", "/api/profile/", {"content": "my own profile"},
     "Failed to create profile"),
])
def test_a_refused_save_returns_and_logs_no_database_text(
        client, monkeypatch, caplog, method, path, payload, error):
    caplog.set_level(logging.DEBUG)
    _refusing_commit(monkeypatch)
    response = getattr(client, method)(path, json=payload)
    _assert_clean(response, caplog, 500)
    assert response.get_json() == {"error": error}
    # What failed is still logged, as the type
    assert "DataError" in caplog.text


def test_a_refused_node_delete_returns_no_database_text(
        app, client, monkeypatch, caplog):
    from backend.models import Node
    user = User.query.filter_by(username="alice").one()
    node = Node(user_id=user.id, human_owner_id=user.id, node_type="user",
                privacy_level="private", ai_usage="none")
    node.set_content("an entry")
    _db.session.add(node)
    _db.session.commit()
    caplog.set_level(logging.DEBUG)
    _refusing_commit(monkeypatch, OperationalError)
    response = client.delete(f"/api/nodes/{node.id}")
    _assert_clean(response, caplog, 500)
    assert response.get_json() == {"error": "Error deleting node"}
    assert "OperationalError" in caplog.text


# --- The engine -------------------------------------------------------------

def test_the_engine_hides_sql_parameters(app):
    assert app.config["SQLALCHEMY_ENGINE_OPTIONS"]["hide_parameters"] is True
    assert _db.engine.hide_parameters is True
    with pytest.raises(OperationalError) as caught:
        _db.session.execute(text("SELECT * FROM no_such_table WHERE x = :v"),
                            {"v": MARKER})
    _db.session.rollback()
    assert MARKER not in str(caught.value)
    assert "hide_parameters=True" in str(caught.value)


# --- The server log ---------------------------------------------------------

def test_an_unhandled_exception_logs_type_and_frames_not_its_message(
        app, caplog):
    app.config["TESTING"] = False
    app.config["PROPAGATE_EXCEPTIONS"] = False

    @app.route("/boom-for-test")
    def boom_for_test():
        raise RuntimeError(f"cannot save {MARKER}")

    caplog.set_level(logging.ERROR, logger="backend")
    response = app.test_client().get("/boom-for-test")
    assert response.status_code == 500
    assert MARKER not in response.get_data(as_text=True)
    assert "Exception on /boom-for-test [GET]" in caplog.text
    assert "RuntimeError: [message removed]" in caplog.text
    assert "in boom_for_test" in caplog.text  # the frame: where it failed
    assert MARKER not in caplog.text


def test_create_app_filters_flasks_handler_without_creating_the_logger(app):
    """The filter goes on Flask's default handler and the app's logger by
    name; app.logger itself is not created (in a Celery worker that would
    attach Flask's handler before Celery's)."""
    from flask.logging import default_handler
    assert "logger" not in vars(app)
    for target in (default_handler, logging.getLogger(app.name)):
        assert sum(isinstance(f, error_privacy.TracebackWithoutMessages)
                   for f in target.filters) == 1


def test_the_filter_covers_child_loggers_through_the_handler():
    """Flask's default handler takes every backend.* module's lines in
    production; the filter on it formats their tracebacks."""
    parent = logging.getLogger("error-privacy-test")
    out = io.StringIO()
    handler = logging.StreamHandler(out)
    parent.addHandler(handler)
    parent.propagate = False
    try:
        error_privacy.install_traceback_filter(parent, [handler])
        error_privacy.install_traceback_filter(parent, [handler])  # once
        assert len(handler.filters) == 1 and len(parent.filters) == 1
        try:
            try:
                raise KeyError(MARKER)
            except KeyError as e:
                raise ValueError(f"wrapped {MARKER}") from e
        except ValueError:
            logging.getLogger("error-privacy-test.child").exception(
                "Save failed for node %s", 7)
    finally:
        parent.removeHandler(handler)
        parent.filters.clear()
    written = out.getvalue()
    assert "Save failed for node 7" in written
    assert "KeyError: [message removed]" in written
    assert "ValueError: [message removed]" in written
    assert "direct cause of the following exception" in written
    assert "test_the_filter_covers_child_loggers_through_the_handler" in written
    assert MARKER not in written


# --- Sentry -----------------------------------------------------------------

class _StopAfterSentryInit(Exception):
    pass


def _app_sentry_hooks(monkeypatch):
    """The before_send and before_breadcrumb create_app() hands to Sentry;
    create_app stops right after sentry_sdk.init."""
    passed = {}

    def fake_init(**kwargs):
        passed.update(kwargs)
        raise _StopAfterSentryInit

    monkeypatch.setenv("SENTRY_DSN", "https://key@example.invalid/1")
    monkeypatch.setattr(sentry_sdk, "init", fake_init)
    from backend import create_app
    with pytest.raises(_StopAfterSentryInit):
        create_app()
    monkeypatch.undo()
    return passed


def save_profile_for_test():
    """Stands in for a route whose save the database refuses."""
    raise _db_error(IntegrityError) from _PgError("cause " + MARKER)


def test_sentry_gets_types_and_frames_but_no_values(monkeypatch):
    from backend import SENTRY_PRIVACY_OPTIONS
    hooks = _app_sentry_hooks(monkeypatch)
    before_send = hooks["before_send"]
    events = []
    log = logging.getLogger("backend.routes.error_privacy_test")
    try:
        sentry_sdk.init(dsn="https://key@example.invalid/1",
                        transport=lambda envelope: None,
                        before_send=lambda e, h: events.append(
                            before_send(e, h)),
                        before_breadcrumb=hooks["before_breadcrumb"],
                        **SENTRY_PRIVACY_OPTIONS)
        try:
            save_profile_for_test()
        except IntegrityError as e:
            # A breadcrumb, a log event and a captured exception, each
            # with the exception's text in it the usual ways
            log.warning(f"Retrying the save: {e}")
            log.error("Save failed: %s", e)
            log.error(f"Save failed again: {e}")
            sentry_sdk.capture_exception()
    finally:
        sentry_sdk.init(dsn=None)

    assert len(events) == 3
    for event in events:
        dumped = json.dumps(event, default=str)
        assert MARKER not in dumped
        assert "Failing row contains (" not in dumped
        crumbs = event["breadcrumbs"]["values"]
        assert any("Retrying the save: IntegrityError" in c["message"]
                   for c in crumbs)

    logged = [e for e in events if e.get("logentry")]
    assert {e["logentry"]["formatted"] for e in logged} == {
        "Save failed: IntegrityError", "Save failed again: IntegrityError"}

    captured = [e for e in events if e.get("exception")][-1]
    values = captured["exception"]["values"]
    assert [v["type"] for v in values] == ["_PgError", "IntegrityError"]
    top = values[-1]
    assert top["value"] == ("[message removed] (sqlstate 23502, table "
                            "user_profile, column user_id)")
    frames = top["stacktrace"]["frames"]
    assert frames[-1]["function"] == "save_profile_for_test"
    assert frames[-1]["lineno"]
    assert all("vars" not in f for f in frames)


def test_database_values_are_stripped_from_any_text():
    pg = ('duplicate key value violates unique constraint "user_username_key"'
          f'\nDETAIL:  Key (username)=({MARKER}) already exists.'
          '\n[SQL: UPDATE "user" SET username=%(username)s]'
          f"\n[parameters: {{'username': '{MARKER}'}}]"
          "\n(Background on this error at: https://sqlalche.me/e/20/gkpj)")
    scrubbed = error_privacy.strip_database_values(pg)
    assert MARKER not in scrubbed
    assert "DETAIL: [removed]" in scrubbed
    assert "[parameters: removed]" in scrubbed
    assert 'unique constraint "user_username_key"' in scrubbed
    assert '[SQL: UPDATE "user" SET username=%(username)s]' in scrubbed
    assert "(Background on this error" in scrubbed

    multi_line_row = ("Failing row contains (5, first line\n"
                      f"{MARKER}\nlast line, null).")
    assert MARKER not in error_privacy.strip_database_values(multi_line_row)
    standalone = f"Key (email)=({MARKER}) is not present in table \"user\"."
    assert error_privacy.strip_database_values(standalone) == "Key [removed]"

    # Postgres quotes a value it cannot convert, and its LINE excerpt
    # quotes the statement as received (psycopg2 sends values inline);
    # the texts are the ones a local Postgres wrote for these errors
    for pg in [
            f'invalid input syntax for type integer: "{MARKER}"\n'
            f"LINE 1: INSERT INTO probe_t VALUES ('{MARKER}', 'z', 'c')\n"
            "                                    ^\n\n"
            "[SQL: INSERT INTO probe_t VALUES (%(i)s, 'z', 'c')]",
            f'value "{MARKER}" is out of range for type integer\n'
            f"LINE 1: SELECT CAST('{MARKER}' AS int)\n                    ^"]:
        scrubbed = error_privacy.strip_database_values(pg)
        assert MARKER not in scrubbed
        assert "LINE: [removed]" in scrubbed
    assert error_privacy.strip_database_values(
        f'invalid input syntax for type date: "{MARKER}"') == (
        "invalid input syntax for type date: [removed]")


def test_scrubbing_never_raises_and_drops_what_it_cannot_scrub():
    class Unprintable(Exception):
        def __str__(self):
            raise RuntimeError("no")

        __repr__ = __str__

    event = {"exception": {"values": [{"type": "Unprintable",
                                       "value": "x"}]},
             "logentry": {"message": "a %s", "params": [Unprintable()],
                          "formatted": "a b"}}
    out = error_privacy.scrub_sentry_event(
        event, {"exc_info": (Unprintable, Unprintable(), None)})
    assert out["exception"]["values"][0]["value"] == "[message removed]"
    assert out["logentry"]["params"] == ["Unprintable"]
    assert error_privacy.scrub_sentry_breadcrumb(
        {"message": "plain"}, None) == {"message": "plain"}

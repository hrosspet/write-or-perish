"""The notes import's confirm (POST /api/import/confirm) within limits.

- A confirm takes at most MAX_IMPORT_FILES files, and analyze lists at
  most that many: more get 400 with a message, and nothing is created.
- One notes import of an account runs at a time: a second confirm while
  one runs gets 409 ``import_running`` (the text both apps show) and
  creates nothing; the account's key is freed when the confirm ends, on
  success and on failure, and expires by itself when a run stops
  refreshing it.
- A confirm of real size is answered as before (control).
"""
import io
import json
import zipfile

from backend.tests.test_twitter_import import (  # noqa: F401
    app, _make_user, _login,
)
from backend.tests.fake_redis import install
from backend.extensions import db
from backend.models import Node
from backend.utils import run_locks


def _client(app, name="alice"):
    client = app.test_client()
    user = _make_user(name)
    db.session.commit()
    _login(client, user.id)
    return client, user


def _files(n, prefix="note"):
    return [{"filename_without_ext": f"{prefix} {i}", "content": f"text {i}",
             "modified_at": "2024-01-01T12:00:00"} for i in range(n)]


def _confirm(client, files, **extra):
    from flask import g
    g.pop("_login_user", None)  # each client signs in as its own user
    body = {"files": files, "import_type": "separate_nodes",
            "privacy_level": "private", "ai_usage": "none", **extra}
    return client.post("/api/import/confirm", data=json.dumps(body),
                       content_type="application/json")


def _entries(user):
    return Node.query.filter_by(human_owner_id=user.id).count()


def test_confirm_over_the_file_limit_is_refused(app, monkeypatch):
    from backend.routes import import_data
    monkeypatch.setattr(import_data, "MAX_IMPORT_FILES", 50)
    client, user = _client(app)
    r = _confirm(client, [{"filename_without_ext": str(i)}
                          for i in range(51)])
    assert r.status_code == 400
    assert "more files than Loore imports at once" in r.get_json()["error"]
    assert _entries(user) == 0


def test_analyze_over_the_file_limit_is_refused(app, monkeypatch):
    from backend.routes import import_data
    monkeypatch.setattr(import_data, "MAX_IMPORT_FILES", 50)
    client, _ = _client(app)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for i in range(51):
            z.writestr(f"n{i}.md", "x")
    r = client.post("/api/import/analyze",
                    data={"zip_file": (io.BytesIO(buf.getvalue()), "n.zip")},
                    content_type="multipart/form-data")
    assert r.status_code == 400
    assert "more files than Loore imports at once" in r.get_json()["error"]


def test_second_confirm_while_one_runs_is_refused(app, monkeypatch):
    from backend.routes import import_data
    install(monkeypatch)
    client, user = _client(app)
    running = run_locks.RunLock(
        import_data.FILE_IMPORT_LOCK_KEY.format(user_id=user.id), "first",
        import_data.FILE_IMPORT_LOCK_TTL)
    assert running.acquire()
    r = _confirm(client, _files(3))
    assert r.status_code == 409
    assert r.get_json()["code"] == "import_running"
    assert r.get_json()["error"] == import_data.IMPORT_RUNNING_MESSAGE
    assert _entries(user) == 0
    # Another account is not held up.
    other, someone = _client(app, "bob")
    assert _confirm(other, _files(2)).status_code == 201
    running.release()
    assert _confirm(client, _files(3)).status_code == 201
    assert _entries(user) == 3


def test_the_key_is_taken_before_the_body_is_read(app, monkeypatch):
    """A second confirm is refused without its body being read (a body
    that would not even parse gets the same 409)."""
    from flask import g
    from backend.routes import import_data
    install(monkeypatch)
    client, user = _client(app)
    running = run_locks.RunLock(
        import_data.FILE_IMPORT_LOCK_KEY.format(user_id=user.id), "first",
        import_data.FILE_IMPORT_LOCK_TTL)
    assert running.acquire()
    g.pop("_login_user", None)
    r = client.post("/api/import/confirm", data=b"{not json",
                    content_type="application/json")
    assert r.status_code == 409
    assert r.get_json()["code"] == "import_running"


def test_the_key_is_freed_when_a_confirm_ends(app, monkeypatch):
    from backend.routes import import_data
    fake = install(monkeypatch)
    client, user = _client(app)
    key = import_data.FILE_IMPORT_LOCK_KEY.format(user_id=user.id)
    seen = []
    real_beat = run_locks.RunLock.beat

    def beat(self):
        seen.append(fake.get(key) is not None)
        return real_beat(self)

    monkeypatch.setattr(run_locks.RunLock, "beat", beat)
    assert _confirm(client, _files(4)).status_code == 201
    assert seen == [True] * 4          # held while each file is stored
    assert fake.get(key) is None       # freed after success

    def fail(*a, **k):
        raise RuntimeError("store failed")

    monkeypatch.setattr(import_data, "_import_file_nodes", fail)
    assert _confirm(client, _files(2, "other")).status_code == 500
    assert fake.get(key) is None       # freed after a failure


def test_a_key_left_by_a_stopped_run_expires(app, monkeypatch):
    from backend.routes import import_data
    fake = install(monkeypatch)
    client, user = _client(app)
    stopped = run_locks.RunLock(
        import_data.FILE_IMPORT_LOCK_KEY.format(user_id=user.id), "stopped",
        import_data.FILE_IMPORT_LOCK_TTL)
    assert stopped.acquire()
    assert _confirm(client, _files(1)).status_code == 409
    fake.advance(import_data.FILE_IMPORT_LOCK_TTL + 1)
    assert _confirm(client, _files(1)).status_code == 201


def test_confirm_of_real_size_is_answered_as_before(app, monkeypatch):
    install(monkeypatch)
    client, user = _client(app)
    r = _confirm(client, _files(300))
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    assert body["created"] == 300 and body["skipped"] == 0
    for field in ("restored", "updated", "partly_updated",
                  "ask_profile_rebuild", "ask_both_providers"):
        assert field in body
    # A repeat skips what already arrived.
    r = _confirm(client, _files(300))
    assert r.status_code == 201 and r.get_json()["skipped"] == 300

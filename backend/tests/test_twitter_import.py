"""Tests for the Twitter/X archive import.

Covers:
- backend.utils.twitter_archive: streaming tweets.js parser, retweet
  filtering, chronological sort, stash round-trip and token hygiene.
- POST /api/import/twitter/analyze: counts + import_token, no tweet echo.
- POST /api/import/twitter/confirm: token validation, deleted-content
  409, Celery dispatch.
- create_twitter_nodes: separate/single-thread creation, encryption via
  set_content, dedup skip/restore, batched commits + progress callback.
- GET /api/import/status/<task_id>: state mapping and owner check.
"""

import io
import json
import os
import sys
import types
import zipfile
from unittest.mock import MagicMock

# ── Environment ──────────────────────────────────────────────────────────
os.environ["ENCRYPTION_DISABLED"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("TWITTER_API_KEY", "fake")
os.environ.setdefault("TWITTER_API_SECRET", "fake")

sys.modules.setdefault("celery", MagicMock())
sys.modules.setdefault("celery.utils", MagicMock())
sys.modules.setdefault("celery.utils.log", MagicMock())
sys.modules.setdefault("celery.result", MagicMock())
sys.modules.setdefault("ffmpeg", MagicMock())

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login          # noqa: E402
from backend.extensions import db as _db         # noqa: E402
from backend.models import User, Node            # noqa: E402
import backend.models as _real_backend_models    # noqa: E402
from backend.utils import twitter_archive as ta  # noqa: E402


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

    from backend.routes.import_data import import_bp
    app.register_blueprint(import_bp, url_prefix="/api")
    return app


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(ta, "STASH_ROOT", tmp_path / "imports")
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


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True


def _make_user(username):
    u = User(username=username, approved=True, plan="alpha")
    _db.session.add(u)
    _db.session.flush()
    return u


def _tweet(i, text, created="Wed Aug 26 08:17:00 +0000 2026", reply=None):
    t = {
        "id_str": str(i), "id": str(i), "full_text": text,
        "created_at": created, "favorite_count": "3", "retweet_count": "0",
        "in_reply_to_status_id_str": reply,
        "in_reply_to_screen_name": "someone" if reply else None,
    }
    return {"tweet": t}


def _tweets_js(entries, prefix="window.YTD.tweets.part0 = "):
    return prefix + json.dumps(entries, indent=2)


def _zip_bytes(tweets_js, name="data/tweets.js"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, tweets_js)
    buf.seek(0)
    return buf


SAMPLE = [
    _tweet(3, "third, newest", "Fri Aug 28 10:00:00 +0000 2026"),
    _tweet(1, "first, oldest", "Mon Aug 24 10:00:00 +0000 2026"),
    _tweet(2, "RT @someone: a retweet", "Tue Aug 25 10:00:00 +0000 2026"),
    _tweet(4, "a reply", "Thu Aug 27 10:00:00 +0000 2026", reply="99"),
]


# ── parser / stash ───────────────────────────────────────────────────────

def test_iter_tweets_js_streams_and_unwraps():
    out = list(ta.iter_tweets_js(_tweets_js(SAMPLE)))
    assert [t["id_str"] for t in out] == ["3", "1", "2", "4"]
    assert out[0]["full_text"] == "third, newest"


def test_iter_tweets_js_handles_nested_folder_and_bare_array():
    out = list(ta.iter_tweets_js("[" + json.dumps(SAMPLE[0]) + "]"))
    assert len(out) == 1
    zf = _zip_bytes(_tweets_js(SAMPLE), name="twitter-2026/data/tweets.js")
    rows, summary = ta.analyze_archive(zf)
    assert summary["total_tweets"] == 3


def test_iter_tweets_stream_across_chunk_boundaries(monkeypatch):
    entries = [_tweet(i, "x" * (i % 7) + " é🙂") for i in range(200)]
    text = _tweets_js(entries)
    for chunk in (1, 7, 64, 1000, len(text) + 5):
        monkeypatch.setattr(ta, "CHUNK_CHARS", chunk)
        out = list(ta.iter_tweets_stream(io.StringIO(text)))
        assert [t["id_str"] for t in out] == [str(i) for i in range(200)], chunk
    # empty array and trailing whitespace
    assert list(ta.iter_tweets_js("window.YTD.tweets.part0 = [ ]\n")) == []


def test_iter_tweets_js_rejects_garbage():
    with pytest.raises(ta.TwitterArchiveError):
        list(ta.iter_tweets_js("window.YTD.tweets.part0 = [{oops"))
    with pytest.raises(ta.TwitterArchiveError):
        list(ta.iter_tweets_js("no array here"))


def test_analyze_archive_filters_retweets_sorts_and_counts():
    rows, summary = ta.analyze_archive(_zip_bytes(_tweets_js(SAMPLE)))
    assert [r["id_str"] for r in rows] == ["1", "4", "3"]  # chronological
    assert summary == {
        "total_tweets": 3, "original_count": 2, "reply_count": 1,
        "skipped_retweets": 1,
        "total_tokens": sum(r["token_count"] for r in rows),
        "original_tokens": sum(r["token_count"] for r in rows if not r["is_reply"]),
        "total_size": sum(len(r["full_text"].encode()) for r in rows),
    }
    assert rows[1]["is_reply"] is True and rows[1]["in_reply_to_screen_name"] == "someone"
    assert rows[0]["favorite_count"] == 3


def test_analyze_archive_to_stash_matches_in_memory(tmp_path):
    rows, summary = ta.analyze_archive(_zip_bytes(_tweets_js(SAMPLE)))
    path = tmp_path / "out.jsonl"
    disk_summary = ta.analyze_archive_to_stash(_zip_bytes(_tweets_js(SAMPLE)), path)
    assert disk_summary == summary
    assert list(ta.stash_iter(path)) == rows
    assert not (tmp_path / "out.jsonl.unsorted").exists()


def test_stash_round_trip_and_token_hygiene(tmp_path, monkeypatch):
    monkeypatch.setattr(ta, "STASH_ROOT", tmp_path / "imports")
    rows, _ = ta.analyze_archive(_zip_bytes(_tweets_js(SAMPLE)))
    token = ta.stash_write(7, rows)
    path = ta.stash_path(7, token)
    assert path.exists() and ta.stash_count(path) == 3
    assert list(ta.stash_iter(path)) == rows
    # tokens never become path components
    assert ta.stash_path(7, "../../etc/passwd") is None
    assert ta.stash_path(7, "") is None
    assert ta.stash_path(8, token) != path
    ta.stash_delete(path)
    assert not path.exists()
    ta.stash_delete(path)  # idempotent


def test_sweep_expired_removes_old_files(tmp_path, monkeypatch):
    monkeypatch.setattr(ta, "STASH_ROOT", tmp_path / "imports")
    token = ta.stash_write(1, [])
    path = ta.stash_path(1, token)
    old = path.stat().st_mtime - ta.STASH_TTL_SECONDS - 10
    os.utime(path, (old, old))
    ta.sweep_expired()
    assert not path.exists()


# ── analyze endpoint ─────────────────────────────────────────────────────

def test_analyze_returns_counts_and_token_not_tweets(app):
    client = app.test_client()
    u = _make_user("alice")
    _login(client, u.id)
    resp = client.post(
        "/api/import/twitter/analyze",
        data={"zip_file": (_zip_bytes(_tweets_js(SAMPLE)), "archive.zip")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert "tweets" not in body
    assert body["total_tweets"] == 3 and body["skipped_retweets"] == 1
    assert body["original_tokens"] <= body["total_tokens"]
    assert ta.stash_path(u.id, body["import_token"]).exists()


def test_analyze_rejects_bad_archives(app):
    client = app.test_client()
    u = _make_user("alice")
    _login(client, u.id)
    resp = client.post(
        "/api/import/twitter/analyze",
        data={"zip_file": (_zip_bytes("x", name="data/other.js"), "a.zip")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert "tweets.js" in resp.get_json()["error"]
    resp = client.post(
        "/api/import/twitter/analyze",
        data={"zip_file": (io.BytesIO(b"not a zip"), "a.zip")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400


# ── confirm endpoint ─────────────────────────────────────────────────────

def _fake_task_module(monkeypatch):
    mod = types.ModuleType("backend.tasks.imports")
    task = MagicMock()
    task.delay.return_value = MagicMock(id="task-123")
    mod.import_twitter_archive = task
    monkeypatch.setitem(sys.modules, "backend.tasks.imports", mod)
    return task


def test_confirm_dispatches_task(app, monkeypatch):
    client = app.test_client()
    u = _make_user("alice")
    _login(client, u.id)
    task = _fake_task_module(monkeypatch)
    rows, _ = ta.analyze_archive(_zip_bytes(_tweets_js(SAMPLE)))
    token = ta.stash_write(u.id, rows)
    resp = client.post("/api/import/twitter/confirm", json={
        "import_token": token, "import_type": "separate_nodes",
        "include_replies": False, "privacy_level": "private", "ai_usage": "none",
    })
    assert resp.status_code == 202, resp.get_json()
    assert resp.get_json() == {"task_id": "task-123", "status": "queued", "total": 3}
    args, _ = task.delay.call_args
    assert args[0] == u.id and args[1] == token
    assert args[2]["include_replies"] is False and args[2]["on_deleted"] is None


def test_confirm_rejects_missing_or_foreign_token(app, monkeypatch):
    client = app.test_client()
    u = _make_user("alice")
    other = _make_user("bob")
    _login(client, u.id)
    _fake_task_module(monkeypatch)
    token = ta.stash_write(other.id, [])
    for bad in [None, "../x", token]:
        resp = client.post("/api/import/twitter/confirm",
                           json={"import_token": bad})
        assert resp.status_code == 400
        assert "expired" in resp.get_json()["error"]


def test_confirm_409_on_deleted_match_then_dispatch_with_choice(app, monkeypatch):
    client = app.test_client()
    u = _make_user("alice")
    _login(client, u.id)
    task = _fake_task_module(monkeypatch)
    from datetime import datetime
    n = Node(user_id=u.id, human_owner_id=u.id, node_type="user",
             source_key="twitter:1", deleted_at=datetime.utcnow())
    n.set_content("first, oldest")
    _db.session.add(n)
    _db.session.commit()
    rows, _ = ta.analyze_archive(_zip_bytes(_tweets_js(SAMPLE)))
    token = ta.stash_write(u.id, rows)
    resp = client.post("/api/import/twitter/confirm", json={"import_token": token})
    assert resp.status_code == 409
    assert resp.get_json()["deleted_matches"] == 1
    resp = client.post("/api/import/twitter/confirm",
                       json={"import_token": token, "on_deleted": "skip"})
    assert resp.status_code == 202
    assert task.delay.call_args[0][2]["on_deleted"] == "skip"


# ── node creation (what the task runs) ───────────────────────────────────

def _rows():
    rows, _ = ta.analyze_archive(_zip_bytes(_tweets_js(SAMPLE)))
    return rows


def test_create_nodes_separate_skips_replies_and_encrypts(app):
    from backend.routes.import_data import create_twitter_nodes
    u = _make_user("alice")
    progress = []
    result = create_twitter_nodes(
        user_id=u.id, rows=iter(_rows()), total=3,
        import_type="separate_nodes", include_replies=False,
        privacy_level="private", ai_usage="none", on_deleted=None,
        batch_size=1, on_progress=progress.append,
    )
    assert result["created"] == 2 and result["thread_count"] == 2
    assert result["skipped"] == 0 and result["profile_update_task_id"] is None
    nodes = Node.query.filter_by(human_owner_id=u.id).order_by(Node.created_at).all()
    assert [n.get_content() for n in nodes] == ["first, oldest", "third, newest"]
    assert all(n.parent_id is None and n.origin == "twitter" for n in nodes)
    assert nodes[0].created_at.year == 2026 and nodes[0].created_at.day == 24
    assert progress[-1] == 3 and len(progress) >= 2  # batched + final


def test_create_nodes_single_thread_chains_and_dedups(app):
    from backend.routes.import_data import create_twitter_nodes
    u = _make_user("alice")
    kwargs = dict(user_id=u.id, total=3, import_type="single_thread",
                  include_replies=True, privacy_level="private",
                  ai_usage="none", on_deleted=None)
    first = create_twitter_nodes(rows=iter(_rows()), **kwargs)
    assert first["created"] == 3 and first["thread_count"] == 1
    chain = Node.query.filter_by(human_owner_id=u.id).order_by(Node.created_at).all()
    assert chain[0].parent_id is None
    assert chain[1].parent_id == chain[0].id and chain[2].parent_id == chain[1].id
    # re-import: everything deduped on twitter:<id>
    second = create_twitter_nodes(rows=iter(_rows()), **kwargs)
    assert second["created"] == 0 and second["skipped"] == 3
    assert Node.query.filter_by(human_owner_id=u.id).count() == 3


def test_create_nodes_restores_deleted_when_asked(app):
    from backend.routes.import_data import create_twitter_nodes
    from datetime import datetime
    u = _make_user("alice")
    n = Node(user_id=u.id, human_owner_id=u.id, node_type="user",
             source_key="twitter:1", deleted_at=datetime.utcnow())
    n.set_content("stale")
    _db.session.add(n)
    _db.session.commit()
    result = create_twitter_nodes(
        user_id=u.id, rows=iter(_rows()), total=3,
        import_type="separate_nodes", include_replies=False,
        privacy_level="private", ai_usage="none", on_deleted="restore",
    )
    assert result["restored"] == 1 and result["created"] == 1
    _db.session.refresh(n)
    assert n.deleted_at is None and n.get_content() == "first, oldest"


# ── status endpoint ──────────────────────────────────────────────────────

def _fake_celery(monkeypatch, state, info):
    mod = types.ModuleType("backend.celery_app")
    res = MagicMock(state=state, info=info)
    mod.celery = MagicMock()
    mod.celery.AsyncResult.return_value = res
    monkeypatch.setitem(sys.modules, "backend.celery_app", mod)


def test_status_maps_states_and_hides_other_users(app, monkeypatch):
    client = app.test_client()
    u = _make_user("alice")
    _login(client, u.id)

    _fake_celery(monkeypatch, "PROGRESS", {"user_id": u.id, "done": 500, "total": 900})
    body = client.get("/api/import/status/t1").get_json()
    assert body["status"] == "running" and body["done"] == 500 and body["total"] == 900

    done = {"user_id": u.id, "created": 9, "skipped": 1, "restored": 0,
            "updated": 0, "thread_count": 9, "profile_update_task_id": None,
            "message": "Import successful", "nodes_created": 9, "total": 10}
    _fake_celery(monkeypatch, "SUCCESS", done)
    body = client.get("/api/import/status/t1").get_json()
    assert body["status"] == "completed" and body["result"]["created"] == 9
    assert "user_id" not in body["result"]

    _fake_celery(monkeypatch, "SUCCESS", dict(done, user_id=u.id + 1))
    assert client.get("/api/import/status/t1").get_json()["status"] == "queued"

    _fake_celery(monkeypatch, "FAILURE", RuntimeError("boom"))
    body = client.get("/api/import/status/t1").get_json()
    assert body["status"] == "failed" and "boom" in body["error"]

    _fake_celery(monkeypatch, "PENDING", None)
    assert client.get("/api/import/status/t1").get_json()["status"] == "queued"


# ── Community Archive pre-fill (admin cold-start bootstrap) ───────────────

def _ca_row(i, text, created="2026-08-24T10:00:00+00:00", reply=None):
    return {"tweet_id": str(i), "created_at": created, "full_text": text,
            "favorite_count": 1, "retweet_count": 0,
            "reply_to_tweet_id": reply,
            "reply_to_user_id": "7" if reply else None,
            "reply_to_username": "someone" if reply else None}


def test_community_archive_keyset_paging_and_export_shape(monkeypatch):
    from backend.utils import community_archive as ca
    calls = []

    def fake_get(table, params):
        calls.append(params)
        assert table == "enriched_tweets"
        if "tweet_id" not in params:
            return [_ca_row(1, "a"), _ca_row(2, "b")]
        assert params["tweet_id"] == "gt.2"
        return [_ca_row(3, "c")]

    monkeypatch.setattr(ca, "_get", fake_get)
    rows = list(ca.iter_tweets("Someone", page_size=2))
    assert [r["tweet_id"] for r in rows] == ["1", "2", "3"]
    assert "offset" not in calls[0] and calls[1]["order"] == "tweet_id.asc"

    entry = ca.to_export_entry(_ca_row(9, "hi", reply="5"))["tweet"]
    assert entry["created_at"] == "Mon Aug 24 10:00:00 +0000 2026"
    assert entry["in_reply_to_status_id_str"] == "5"
    row = ta.compact_row(entry)
    assert row["is_reply"] and row["full_text"] == "hi"


def test_import_handoff_routes_batch_users_to_seeder(app, monkeypatch):
    """A batch-pinned user never gets the synchronous profile task after an
    import; the from-scratch build is requested via the regen flag."""
    from backend.routes import import_data
    u = _make_user("batchy")
    u.profile_force_batch = True
    _db.session.commit()
    import backend.tasks.exports as ex
    sync = MagicMock(return_value="sync-task")
    monkeypatch.setattr(ex, "maybe_trigger_profile_update", sync)

    assert import_data._maybe_update_profile_after_import(u.id, None, 50000) is None
    assert sync.call_count == 0
    assert User.query.get(u.id).profile_needs_full_regen is True

    # Non-batch users keep the existing synchronous dispatch.
    v = _make_user("syncy")
    _db.session.commit()
    assert import_data._maybe_update_profile_after_import(v.id, None, 50000) == "sync-task"


def test_prefill_impl_imports_pins_batch_and_reports(app, monkeypatch):
    from backend.tasks import imports as imports_mod
    from backend.utils import community_archive as ca
    u = _make_user("tyler")
    _db.session.commit()
    monkeypatch.setattr(ca, "fetch_account", lambda h: {
        "account_id": "1", "username": "TylerAlterman", "num_tweets": 3})
    monkeypatch.setattr(ca, "iter_tweets", lambda h, on_page=None, **k: iter([
        _ca_row(2, "second", "2026-08-25T10:00:00+00:00"),
        _ca_row(1, "first"),
        _ca_row(1, "first (dup)"),
        _ca_row(3, "RT @x: retweet", "2026-08-26T10:00:00+00:00"),
        _ca_row(4, "a reply", "2026-08-27T10:00:00+00:00", reply="2"),
    ]))
    import backend.tasks.exports as ex
    sync = MagicMock(return_value="sync-task")
    monkeypatch.setattr(ex, "maybe_trigger_profile_update", sync)
    states = []

    result = imports_mod.prefill_community_archive_impl(
        u.id, "tyleralterman", {"include_replies": False},
        update_state=lambda **kw: states.append(kw["meta"]))

    assert result["created"] == 2 and result["handle"] == "TylerAlterman"
    nodes = Node.query.filter_by(human_owner_id=u.id).order_by(Node.created_at).all()
    assert [n.get_content() for n in nodes] == ["first", "second"]
    assert all(n.origin == "twitter" and n.ai_usage == "chat" for n in nodes)
    assert User.query.get(u.id).profile_force_batch is True
    assert User.query.get(u.id).prefilled_handle == "TylerAlterman"
    assert sync.call_count == 0  # never the synchronous path
    assert states[0]["stage"] == "fetching" and states[-1]["stage"] == "importing"
    assert result["profile_batch_queued"] is False  # tiny corpus < 10k tokens


def test_prefill_impl_unknown_handle(app, monkeypatch):
    from backend.tasks import imports as imports_mod
    from backend.utils import community_archive as ca
    u = _make_user("nobody")
    _db.session.commit()
    monkeypatch.setattr(ca, "fetch_account", lambda h: None)
    with pytest.raises(ca.CommunityArchiveError):
        imports_mod.prefill_community_archive_impl(u.id, "nobody", {})


# ── parquet snapshot path (large accounts) ────────────────────────────────

def _make_snapshot(d):
    """Tiny tweets.parquet + profiles.parquet with the real column names."""
    import duckdb
    d.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(f"""
        copy (select * from (values
            ('10', 'A1', 'alice', 'Alice', 3::UBIGINT),
            ('20', 'B2', 'bob', 'Bob', 1::UBIGINT))
            t(_, account_id, username, display_name, num_tweets))
        to '{d / "profiles.parquet"}' (format parquet)""")
    con.execute(f"""
        copy (select * from (values
            ('102', 'A1', timestamp with time zone '2026-08-25 10:00:00+00', 'second', 1::UBIGINT, 0::UBIGINT, NULL, NULL),
            ('101', 'A1', timestamp with time zone '2026-08-24 10:00:00+00', 'first', 2::UBIGINT, 0::UBIGINT, NULL, NULL),
            ('103', 'A1', timestamp with time zone '2026-08-26 10:00:00+00', 'reply to bob',
             0::UBIGINT, 0::UBIGINT, '77', 'B2'),
            ('201', 'B2', timestamp with time zone '2026-08-24 11:00:00+00', 'bobs tweet', 0::UBIGINT, 0::UBIGINT, NULL, NULL))
            t(tweet_id, account_id, created_at, full_text, favorite_count, retweet_count,
              reply_to_tweet_id, reply_to_account_id))
        to '{d / "tweets.parquet"}' (format parquet)""")
    (d / "export_id").write_text("2026-08-27T07-03-56Z")


def test_parquet_account_and_tweets(tmp_path):
    from backend.utils import community_archive as ca
    snap = tmp_path / "snap"
    _make_snapshot(snap)
    acct = ca.fetch_account_parquet("@Alice", snap)
    assert acct == {"account_id": "A1", "username": "alice",
                    "account_display_name": "Alice", "num_tweets": 3}
    assert ca.fetch_account_parquet("nobody", snap) is None
    pages = []
    rows = list(ca.iter_tweets_parquet("A1", snap, batch=2, on_page=pages.append))
    assert [r["tweet_id"] for r in rows] == ["101", "102", "103"]
    assert rows[2]["reply_to_username"] == "bob" and rows[2]["reply_to_user_id"] == "B2"
    assert pages == [2, 3]
    entry = ca.to_export_entry(rows[0])["tweet"]
    assert entry["created_at"] == "Mon Aug 24 10:00:00 +0000 2026"
    assert ta.compact_row(entry)["full_text"] == "first"


def test_ensure_snapshot_downloads_once_per_export(tmp_path, monkeypatch):
    from backend.utils import community_archive as ca
    manifest = {"export_id": "E1", "package_paths": [
        "v1/E1/tweets.parquet", "v1/E1/profiles.parquet", "v1/E1/manifest.json"]}
    opened = []

    class FakeResp(io.BytesIO):
        headers = {"Content-Length": "6"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(url):
        opened.append(url)
        return FakeResp(b"abcdef")

    monkeypatch.setattr(ca, "_urlopen", fake_urlopen)
    monkeypatch.setattr(ca, "DOWNLOAD_CHUNK", 4)
    progress = []
    snap = tmp_path / "snap"
    assert ca.ensure_snapshot(snap, on_progress=lambda *a: progress.append(a),
                              manifest=manifest) == "E1"
    assert sorted(p.name for p in snap.iterdir()) == [
        "export_id", "profiles.parquet", "tweets.parquet"]
    assert (snap / "tweets.parquet").read_bytes() == b"abcdef"
    assert opened[0].endswith("/v1/E1/tweets.parquet")
    assert progress[:2] == [("tweets.parquet", 4, 6), ("tweets.parquet", 6, 6)]
    # Same export cached → no download; new export → re-download.
    ca.ensure_snapshot(snap, manifest=manifest)
    assert len(opened) == 2
    ca.ensure_snapshot(snap, manifest={**manifest, "export_id": "E2"})
    assert len(opened) == 4 and ca.snapshot_export_id(snap) == "E2"
    # A half-written snapshot (no marker) is not trusted.
    (snap / "export_id").unlink()
    assert ca.snapshot_export_id(snap) is None


def test_prefill_impl_large_account_uses_parquet(app, tmp_path, monkeypatch):
    from backend.tasks import imports as imports_mod
    from backend.utils import community_archive as ca
    snap = tmp_path / "snap"
    _make_snapshot(snap)
    app.config["COMMUNITY_ARCHIVE_PARQUET_MIN_TWEETS"] = 3
    app.config["COMMUNITY_ARCHIVE_SNAPSHOT_DIR"] = str(snap)
    u = _make_user("alice_local")
    _db.session.commit()
    monkeypatch.setattr(ca, "fetch_account", lambda h: {
        "account_id": "A1", "username": "alice", "num_tweets": 3})
    monkeypatch.setattr(ca, "iter_tweets", MagicMock(
        side_effect=AssertionError("REST must not be used for large accounts")))
    ensured = []
    monkeypatch.setattr(ca, "ensure_snapshot", lambda d, on_progress=None, **k: (
        ensured.append(str(d)) or "2026-08-27T07-03-56Z"))
    import backend.tasks.exports as ex
    monkeypatch.setattr(ex, "maybe_trigger_profile_update", MagicMock())
    states = []

    result = imports_mod.prefill_community_archive_impl(
        u.id, "alice", {"include_replies": True},
        update_state=lambda **kw: states.append(kw["meta"]))

    assert ensured == [str(snap)]
    assert result["source"] == "parquet" and result["created"] == 3
    assert states[0]["stage"] == "downloading"
    nodes = Node.query.filter_by(human_owner_id=u.id).order_by(Node.created_at).all()
    assert [n.get_content() for n in nodes] == ["first", "second", "reply to bob"]

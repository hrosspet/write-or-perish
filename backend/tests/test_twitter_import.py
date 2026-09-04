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

from datetime import datetime  # noqa: E402
import pytest  # noqa: E402
from flask import Flask  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login          # noqa: E402
from backend.extensions import db as _db         # noqa: E402
from backend.models import User, Node, UserProfile  # noqa: E402
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


def _chain(user, cutoffs):
    """Non-integration versions one day apart, then an integration."""
    versions, parent = [], None
    for i, cutoff in enumerate(cutoffs):
        p = UserProfile(user_id=user.id, generated_by="m", tokens_used=0,
                        generation_type="iterative" if i == 0 else "update",
                        source_data_cutoff=cutoff,
                        parent_profile_id=parent.id if parent else None,
                        created_at=datetime(2026, 8, 1 + i))
        p.set_content(f"V{i}")
        _db.session.add(p)
        _db.session.flush()
        versions.append(p)
        parent = p
    integ = UserProfile(user_id=user.id, generated_by="m", tokens_used=0,
                        generation_type="integration",
                        source_data_cutoff=parent.source_data_cutoff,
                        parent_profile_id=parent.id,
                        created_at=datetime(2026, 8, 1 + len(cutoffs)))
    integ.set_content("I")
    _db.session.add(integ)
    _db.session.commit()
    return versions


def _tip(user):
    return (UserProfile.query.filter(UserProfile.user_id == user.id,
                                     UserProfile.generation_type != "integration")
            .order_by(UserProfile.created_at.desc()).first())


def test_import_invalidates_only_the_versions_it_touches(app, monkeypatch):
    """An import dated between two cutoffs supersedes the versions built
    after it, re-tips the chain at the last still-valid version (a revert
    copy stamped with the import moment) and hands the account to its
    pipeline to regenerate from there to the end. Shared by every importer."""
    from backend.routes import import_data
    import backend.tasks.exports as ex
    import backend.tasks.profile_batch as pb
    sync = MagicMock(return_value="sync-task")
    monkeypatch.setattr(ex, "maybe_trigger_profile_update", sync)
    seed = MagicMock()
    monkeypatch.setattr(pb.seed_profile_batch_for_user, "delay", seed)
    hand_off = import_data._hand_off_profile_update_after_import
    cutoffs = [datetime(2026, 1, 10), datetime(2026, 2, 10), datetime(2026, 3, 10)]

    # Batch account (prod): re-tip at v1, seed now, no full regen.
    app.config["PROFILE_USE_BATCH"] = True
    u = _make_user("batch_import")
    _db.session.commit()
    v = _chain(u, cutoffs)
    assert hand_off(u, datetime(2026, 1, 20), 2_000) is None   # size is irrelevant to invalidation
    tip = _tip(u)
    assert tip.generation_type == "revert" and tip.parent_profile_id == v[0].id
    assert tip.source_data_cutoff == cutoffs[0] and tip.source_rendered_at is not None
    assert User.query.get(u.id).profile_needs_full_regen is False
    assert seed.call_args.args == (u.id,) and sync.call_count == 0
    # v2 and v3 stay as history; the chain now walks copy → v1.
    assert [p.id for p in ex._collect_iterative_chain(tip.id)] == [v[0].id, tip.id]

    # Older than the chain's root: everything is invalid → from scratch.
    w = _make_user("batch_root")
    _db.session.commit()
    _chain(w, cutoffs)
    assert hand_off(w, datetime(2025, 12, 1), 2_000) is None
    assert User.query.get(w.id).profile_needs_full_regen is True
    assert _tip(w).generation_type == "update"                  # nothing re-tipped

    # Newer than the tip: nothing invalidated, nothing started, whatever
    # the size — the import is new data and counts toward the organic gate.
    x = _make_user("batch_newer")
    _db.session.commit()
    vx = _chain(x, cutoffs)
    seed.reset_mock()
    for tokens in (5_000, 500_000):
        assert hand_off(x, datetime(2026, 4, 1), tokens) is None
        assert _tip(x).id == vx[2].id and seed.call_count == 0
        assert User.query.get(x.id).profile_needs_full_regen is False

    # No profile yet: from scratch at the threshold, nothing below it.
    y = _make_user("batch_fresh")
    _db.session.commit()
    assert hand_off(y, None, 5_000) is None
    assert User.query.get(y.id).profile_needs_full_regen is False
    assert hand_off(y, None, 50_000) is None
    assert User.query.get(y.id).profile_needs_full_regen is True

    # Sync account: same decisions, dispatched directly.
    app.config["PROFILE_USE_BATCH"] = False
    z = _make_user("sync_import")
    _db.session.commit()
    vz = _chain(z, cutoffs)
    assert hand_off(z, datetime(2026, 2, 20), 2_000) == "sync-task"
    assert sync.call_args.kwargs == {"force_full_regen": False}
    assert _tip(z).parent_profile_id == vz[1].id
    assert hand_off(z, datetime(2025, 1, 1), 2_000) == "sync-task"
    assert sync.call_args.kwargs == {"force_full_regen": True}
    # The Twitter wrapper goes through the same hand-off: nothing for an
    # account whose profile the import leaves valid, a first build otherwise.
    assert import_data._maybe_update_profile_after_import(z.id, None, 50_000) is None
    fresh = _make_user("sync_fresh")
    _db.session.commit()
    assert import_data._maybe_update_profile_after_import(fresh.id, None, 50_000) == "sync-task"
    assert sync.call_args.kwargs == {"force_full_regen": True}


def test_prefill_impl_imports_pins_batch_and_reports(app, monkeypatch):
    from backend.tasks import imports as imports_mod
    from backend.utils import community_archive as ca
    u = _make_user("tyler")
    _db.session.commit()
    monkeypatch.setattr(ca, "fetch_account", lambda h: {
        "account_id": "1", "username": "TylerAlterman", "num_tweets": 3})
    monkeypatch.setattr(ca, "count_archived", lambda aid: 5)
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
    assert (result["archived"], result["retweets_skipped"],
            result["account_num_tweets"]) == (4, 1, 3)
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
    monkeypatch.setattr(ca, "count_archived", lambda aid: 3)
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


def test_coverage_summary_rest(monkeypatch):
    from backend.utils import community_archive as ca
    monkeypatch.setattr(ca, "fetch_account", lambda h: {
        "account_id": "9", "username": "cedcolas", "num_tweets": 571,
        "created_via": "twitter_import"})
    monkeypatch.setattr(ca, "count_archived", lambda aid: 6)
    monkeypatch.setattr(ca, "iter_tweets", lambda h, **k: iter([
        _ca_row(1, "a" * 40), _ca_row(2, "RT @x: b" * 5),
        _ca_row(3, "reply" * 8, reply="1"), _ca_row(3, "dup", reply="1"),
        _ca_row(4, "c" * 20), _ca_row(5, "d" * 20), _ca_row(6, "e" * 20)]))
    s = ca.coverage_summary("cedcolas")
    assert (s["archived"], s["retweets"], s["replies"], s["originals"]) == (6, 1, 1, 4)
    assert s["est_tokens"] == (40 + 40 + 60) // 4
    assert s["ingestion"] == "twitter_import" and s["detail_source"] == "rest"
    assert "archived_live" not in s
    # Beyond the scan limit → count only.
    monkeypatch.setattr(ca, "count_archived", lambda aid: 50000)
    s = ca.coverage_summary("cedcolas")
    assert s["archived"] == 50000 and s["est_tokens"] is None
    assert s["detail_source"] == "count_only"


def test_coverage_summary_parquet(tmp_path, monkeypatch):
    from backend.utils import community_archive as ca
    snap = tmp_path / "snap"
    _make_snapshot(snap)
    monkeypatch.setattr(ca, "fetch_account", lambda h: {
        "account_id": "A1", "username": "alice", "num_tweets": 3, "created_via": "archive"})
    monkeypatch.setattr(ca, "count_archived", lambda aid: 5)  # live archive grew
    s = ca.coverage_summary("alice", snapshot_dir=snap)
    assert (s["archived"], s["retweets"], s["replies"], s["originals"]) == (3, 0, 1, 2)
    assert s["est_tokens"] == (len("first") + len("second") + len("reply to bob")) // 4
    assert s["detail_source"] == "parquet" and s["archived_live"] == 5


def test_prefill_falls_back_to_rest_when_snapshot_lags(app, tmp_path, monkeypatch):
    """Lifetime counter says 13k, live archive has 1k, snapshot has 0 (account
    ingested after the export) → must import via REST, not fail with
    'no own tweets found'."""
    from backend.tasks import imports as imports_mod
    from backend.utils import community_archive as ca
    snap = tmp_path / "snap"
    _make_snapshot(snap)  # has A1/B2 only
    app.config["COMMUNITY_ARCHIVE_PARQUET_MIN_TWEETS"] = 1000
    app.config["COMMUNITY_ARCHIVE_SNAPSHOT_DIR"] = str(snap)
    u = _make_user("marvin")
    _db.session.commit()
    monkeypatch.setattr(ca, "fetch_account", lambda h: {
        "account_id": "M9", "username": "MarvinKeilbach", "num_tweets": 13762})
    monkeypatch.setattr(ca, "count_archived", lambda aid: 1069)
    monkeypatch.setattr(ca, "ensure_snapshot", lambda d, on_progress=None, **k: "E1")
    monkeypatch.setattr(ca, "iter_tweets", lambda h, on_page=None, **k: iter([
        _ca_row(1, "first"), _ca_row(2, "second", "2026-08-25T10:00:00+00:00")]))
    import backend.tasks.exports as ex
    monkeypatch.setattr(ex, "maybe_trigger_profile_update", MagicMock())
    result = imports_mod.prefill_community_archive_impl(u.id, "MarvinKeilbach", {})
    assert result["source"] == "rest" and result["created"] == 2
    assert result["account_num_tweets"] == 13762

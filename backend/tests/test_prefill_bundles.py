"""Pre-fill bundles from the lab (#698): export on the lab, the bucket
layout, and the load on production (admin and automatic paths).

No network: storage is a local directory (LocalDirStorage), and a fake
key service stands in for KMS. Each "server" is its own sqlite file: the
lab (encryption off, as on the lab), and one or more production
databases (encryption on).
"""
import gzip
import inspect
import json
import os
import socket
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
from backend.extensions import db  # noqa: E402
import backend.models as _real_backend_models  # noqa: E402
from backend.models import (  # noqa: E402
    Node, PrefillBundleLoad, PrefillBundleNodeMap, User, UserArtifact,
    UserProfile, UserRecentContext,
)
from backend.utils import encryption  # noqa: E402
from backend.utils import prefill_bundles as pb  # noqa: E402
from backend.utils import prefill_bundle_load as pbl  # noqa: E402
from backend.utils.bundle_storage import (  # noqa: E402
    GCSStorage, LocalDirStorage, storage_for,
)
from backend.scripts import load_user  # noqa: E402
from backend.scripts import prefill_bundles as cli  # noqa: E402

KMS_KEY = "projects/t/locations/l/keyRings/r/cryptoKeys/k"


# ── Servers ───────────────────────────────────────────────────────────────

def _make_app(db_path, location, enabled=True):
    from flask_login import LoginManager
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["PREFILL_BUNDLE_LOCATION"] = str(location)
    app.config["PREFILL_BUNDLES_ENABLED"] = enabled
    db.init_app(app)
    login_manager = LoginManager(app)

    @login_manager.user_loader
    def _load(user_id):
        return db.session.get(User, int(user_id))

    from backend.routes.dashboard import dashboard_bp
    from backend.routes.prefill_bundles import prefill_bundles_bp
    app.register_blueprint(dashboard_bp, url_prefix="/api/dashboard")
    app.register_blueprint(prefill_bundles_bp,
                           url_prefix="/api/admin/prefill-bundles")
    with app.app_context():
        db.create_all()
    return app


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A lab app, a bucket (a directory) and a factory for production
    apps. Routes are imported fresh against the real flask_login."""
    _affected = lambda k: (  # noqa: E731
        k == "flask_login" or k.startswith("backend.routes")
        or k == "backend.models")
    saved = {k: sys.modules[k] for k in list(sys.modules) if _affected(k)}
    sys.modules["flask_login"] = _real_flask_login
    sys.modules["backend.models"] = _real_backend_models
    for k in [k for k in list(sys.modules) if k.startswith("backend.routes")]:
        del sys.modules[k]

    monkeypatch.setattr(encryption, "_wrap_dek", lambda dek: b"w:" + dek)
    monkeypatch.setattr(encryption, "_unwrap_dek", lambda w: w[2:])
    bucket = tmp_path / "bucket"
    bucket.mkdir()
    dispatched = []
    monkeypatch.setattr(pbl, "_dispatch",
                        lambda load_id, countdown=0: dispatched.append(load_id))
    work = tmp_path / "work"
    monkeypatch.setattr(pbl, "_work_dir", lambda: str(work))

    class World:
        pass
    w = World()
    w.tmp = tmp_path
    w.bucket = bucket
    w.storage = LocalDirStorage(str(bucket))
    w.dispatched = dispatched
    w.lab = _make_app(tmp_path / "lab.db", bucket)
    w.prod = lambda name="prod", enabled=True: _make_app(
        tmp_path / f"{name}.db", bucket, enabled)
    yield w
    for k in [k for k in list(sys.modules) if _affected(k)]:
        if k not in saved:
            del sys.modules[k]
    for k, mod in saved.items():
        sys.modules[k] = mod


def _lab_mode(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_DISABLED", "true")


def _prod_mode(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_DISABLED", "false")
    monkeypatch.setenv("GCP_KMS_KEY_NAME", KMS_KEY)


T0 = datetime(2024, 1, 1, 12, 0, 0)


def _seed_lab_member(username="ca_alice", handle="alice", x_id="1001", n=7,
                     **user_kw):
    """A pre-filled lab account: n tweets (a reply chain, a continuation),
    a model's reply, a one-window profile set with its final merge, an
    intentions artifact and a recent context."""
    user = User(username=username, prefilled_handle=handle, twitter_id=x_id,
                approved=False, **user_kw)
    llm = User.query.filter_by(username="claude-opus-5").first()
    if llm is None:
        llm = User(username="claude-opus-5", twitter_id="llm-claude-opus-5")
        db.session.add(llm)
    db.session.add(user)
    db.session.flush()
    nodes = []
    for i in range(n):
        node = Node(user_id=user.id, human_owner_id=user.id, node_type="user",
                    privacy_level="private", ai_usage="chat", origin="twitter",
                    provenance="prefill_ca", source_key=f"twitter:{x_id}{i:03d}",
                    token_count=5,
                    parent_id=nodes[i - 1].id if i in (2, 3) else None)
        node.set_content(f"tweet {i} by {handle}")
        node.created_at = T0 + timedelta(days=i)
        db.session.add(node)
        db.session.flush()
        nodes.append(node)
    if n > 4:
        nodes[1].continuation_node_id = nodes[4].id
    reply = Node(user_id=llm.id, human_owner_id=user.id, node_type="llm",
                 llm_model="claude-opus-5", privacy_level="private",
                 ai_usage="chat", parent_id=nodes[0].id, token_count=3)
    reply.set_content(f"a reply to {handle}")
    reply.created_at = T0 + timedelta(days=n)
    db.session.add(reply)
    newest = T0 + timedelta(days=n - 1)
    window = UserProfile(
        user_id=user.id, generated_by="gpt-6-luna", generation_type="window",
        window_set="set-1", covers_start=None, source_data_cutoff=newest,
        source_rendered_at=newest + timedelta(hours=1), source_tokens_used=35,
        source_origin_stats={"twitter": {"nodes": n, "tokens": 35}})
    window.set_content(f"window profile of {handle}")
    window.created_at = newest + timedelta(hours=2)
    db.session.add(window)
    db.session.flush()
    final = UserProfile(
        user_id=user.id, generated_by="gpt-6.1-sol",
        generation_type="integration", window_set="set-1",
        parent_profile_id=window.id, source_data_cutoff=newest,
        source_tokens_used=35)
    final.set_content(f"final profile of {handle}")
    final.created_at = newest + timedelta(hours=3)
    db.session.add(final)
    db.session.flush()
    intentions = UserArtifact(user_id=user.id, kind="intentions",
                              title="Intentions", generated_by="gpt-6.1-sol",
                              tokens_used=100)
    intentions.set_content(f"intentions of {handle}")
    intentions.created_at = newest + timedelta(hours=4)
    db.session.add(intentions)
    rc = UserRecentContext(user_id=user.id, generated_by="gpt-6-luna",
                           profile_id=final.id)
    rc.set_content(f"recent context of {handle}")
    rc.created_at = newest + timedelta(hours=5)
    db.session.add(rc)
    db.session.commit()
    return user


def _export(world, monkeypatch, **kw):
    _lab_mode(monkeypatch)
    with world.lab.app_context():
        return cli.run_export(world.storage, out=lambda *_: None, **kw)


class _Started:
    """A started load's ids (a run clears the session, so the test keeps
    no ORM rows across one)."""

    def __init__(self, load):
        self.id = load.id
        self.user_id = load.user_id
        self.origin = load.origin
        self.forced = load.forced


def _start(query, admin_id=None, **kw):
    load, refusal = pbl.start_admin_load(query, admin_id=admin_id, **kw)
    return (_Started(load) if load is not None else None), refusal


def _run_until_done(load_id, max_runs=50, **kw):
    """Run a load to its end. Its runs clear the session (expunge_all), so
    rows the test held are detached afterwards: re-read them by id, and
    the signed-in user Flask-Login cached in ``g`` is dropped."""
    from flask import g
    try:
        for _ in range(max_runs):
            outcome = pbl.run_load(load_id, dispatch=lambda *a, **k: None, **kw)
            if outcome != "continuing":
                return outcome
        raise AssertionError("the load did not finish")
    finally:
        g.pop("_login_user", None)


def _login(client, user_id):
    from flask import g
    g.pop("_login_user", None)   # Flask-Login caches the user per app context
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True


def _snapshot(user_id):
    """The account's loaded material, independent of row ids."""
    nodes = Node.query.filter_by(human_owner_id=user_id).order_by(Node.created_at, Node.id).all()
    by_id = {n.id: n for n in nodes}

    def ref(nid):
        return by_id[nid].get_content() if nid in by_id else None
    authors = {u.id: u.username for u in User.query.all()}
    out_nodes = sorted(
        (n.get_content(), n.source_key, n.provenance, n.origin, n.privacy_level,
         n.ai_usage, n.node_type, n.created_at, n.token_count,
         ref(n.parent_id), ref(n.continuation_node_id),
         "self" if n.user_id == user_id else authors.get(n.user_id))
        for n in nodes)
    profiles = UserProfile.query.filter_by(user_id=user_id).order_by(UserProfile.id).all()
    pid = {p.id: p for p in profiles}
    out_profiles = [
        (p.get_content(), p.generated_by, p.generation_type, p.window_set,
         p.covers_start, p.source_rendered_at, p.source_data_cutoff,
         pid[p.parent_profile_id].get_content() if p.parent_profile_id in pid else None,
         p.created_at)
        for p in profiles]
    arts = [(a.kind, a.get_content(), a.generated_by, a.created_at)
            for a in UserArtifact.query.filter_by(user_id=user_id).order_by(UserArtifact.id)]
    rcs = [(r.get_content(), pid[r.profile_id].get_content() if r.profile_id in pid else None)
           for r in UserRecentContext.query.filter_by(user_id=user_id)]
    return out_nodes, out_profiles, arts, rcs


def _raw(node_id):
    return db.session.execute(db.text("SELECT content FROM node WHERE id=:i"),
                              {"i": node_id}).scalar()


# ── Storage ───────────────────────────────────────────────────────────────

def test_storage_for_reads_the_location():
    assert storage_for("") is None and storage_for(None) is None
    gcs = storage_for("gs://some-bucket/pre/fix/")
    assert isinstance(gcs, GCSStorage)
    assert gcs.bucket_name == "some-bucket" and gcs.prefix == "pre/fix"
    assert isinstance(storage_for("/tmp/x"), LocalDirStorage)
    assert isinstance(storage_for("file:///tmp/x"), LocalDirStorage)
    with pytest.raises(ValueError):
        storage_for("gs://")


def test_local_storage_round_trip_and_names(tmp_path):
    s = LocalDirStorage(str(tmp_path))
    assert s.read_bytes("a/b.json") is None and not s.exists("a/b.json")
    s.write_bytes("a/b.json", b"{}")
    src = tmp_path / "src.bin"
    src.write_bytes(b"x" * 10)
    s.upload("a/c.gz", str(src))
    assert sorted(s.list("a")) == ["a/b.json", "a/c.gz"]
    assert s.download("a/c.gz", str(tmp_path / "out")) is True
    assert (tmp_path / "out").read_bytes() == b"x" * 10
    assert s.download("a/none", str(tmp_path / "out2")) is False
    assert s.delete("a/b.json") is True and s.delete("a/b.json") is False
    with pytest.raises(ValueError):
        s.read_bytes("../escape")


class _NotFound(Exception):
    code = 404


class _FakeBlob:
    def __init__(self, store, name, calls):
        self.store, self.name, self.calls = store, name, calls

    def exists(self, timeout=None):
        return self.name in self.store

    def download_as_bytes(self, timeout=None):
        self.calls.append(("download_as_bytes", self.name))
        if self.name not in self.store:
            raise _NotFound()
        return self.store[self.name]

    def download_to_filename(self, path, timeout=None):
        self.calls.append(("download_to_filename", self.name))
        if self.name not in self.store:
            open(path, "wb").close()   # the library leaves an empty file
            raise _NotFound()
        with open(path, "wb") as f:
            f.write(self.store[self.name])

    def upload_from_filename(self, path, content_type=None, timeout=None):
        self.calls.append(("upload_from_filename", self.name))
        with open(path, "rb") as f:
            self.store[self.name] = f.read()

    def upload_from_string(self, data, content_type=None, timeout=None):
        self.store[self.name] = data

    def delete(self, timeout=None):
        if self.name not in self.store:
            raise _NotFound()
        del self.store[self.name]


class _FakeClient:
    def __init__(self):
        self.store, self.calls = {}, []

    def bucket(self, name):
        client = self

        class _B:
            def blob(self, key):
                return _FakeBlob(client.store, key, client.calls)
        return _B()

    def list_blobs(self, bucket, prefix=None, timeout=None):
        return [type("B", (), {"name": k})() for k in sorted(self.store)
                if prefix is None or k.startswith(prefix)]


def test_gcs_storage_keys_under_its_prefix_and_streams_downloads(tmp_path):
    """The bucket implementation, against a stand-in client: names live
    under the prefix, a missing object reads as None / False, and a
    download goes to a file in chunks, never through download_as_bytes."""
    client = _FakeClient()
    s = GCSStorage("bkt", "pf", client=client)
    s.write_bytes("bundles/1/manifest.json", b"{}")
    assert "pf/bundles/1/manifest.json" in client.store
    src = tmp_path / "b.gz"
    src.write_bytes(b"data")
    s.upload("bundles/1/bundle.jsonl.gz", str(src))
    assert list(s.list("bundles")) == ["bundles/1/bundle.jsonl.gz",
                                       "bundles/1/manifest.json"]
    assert s.download("bundles/1/bundle.jsonl.gz", str(tmp_path / "o")) is True
    assert ("download_to_filename", "pf/bundles/1/bundle.jsonl.gz") in client.calls
    assert not any(c[0] == "download_as_bytes" and c[1].endswith(".gz")
                   for c in client.calls)
    assert s.read_bytes("nope") is None
    assert s.download("nope", str(tmp_path / "n")) is False
    assert not (tmp_path / "n").exists()
    assert s.delete("nope") is False


# ── Export on the lab ─────────────────────────────────────────────────────

def _seed_lab_population():
    alice = _seed_lab_member()
    _seed_lab_member("ca_bob", "bob", "1002", n=3)
    _seed_lab_member("ca_admin", "boss", "1003", n=2, is_admin=True)
    _seed_lab_member("ca_seen", "seen", "1004", n=2,
                     last_seen_at=datetime.utcnow())
    _seed_lab_member("ca_mail", "mail", "1005", n=2, email="x@example.com")
    _seed_lab_member("ca_none", "none", "1006", n=2, default_ai_usage="none")
    _seed_lab_member("ca_no", "no", "1007", n=2, prefill_consent="no")
    _seed_lab_member("ca_noid", "noid", None, n=2)
    _seed_lab_member("ca_out", "out", "1009", n=2)
    noprof = User(username="ca_np", prefilled_handle="np", twitter_id="1010")
    db.session.add(noprof)
    db.session.add(User(username="organic"))
    db.session.commit()
    return alice


def test_export_dry_run_writes_nothing_and_counts_every_skip(world, monkeypatch):
    with world.lab.app_context():
        _seed_lab_population()
    pb.write_exclusion(world.storage, "1009", "protected")
    counts = _export(world, monkeypatch, dry_run=True)
    assert counts["would_export"] == 2
    assert counts["would_export_nodes"] == 8 + 4   # alice 7+1, bob 3+1
    for reason in ("admin", "signed_in", "ai_none", "declined", "no_x_id",
                   "excluded", "no_profile"):
        assert counts.get(f"skipped_{reason}") >= 1, reason
    assert counts["skipped_signed_in"] == 2      # a sign-in, an email login
    assert not list(world.storage.list("bundles"))


def test_export_writes_the_layout_with_a_contentless_manifest(world, monkeypatch):
    with world.lab.app_context():
        _seed_lab_member()
    counts = _export(world, monkeypatch)
    assert counts["exported"] == 1
    names = sorted(world.storage.list(""))
    assert names == ["bundles/1001/bundle.jsonl.gz", "bundles/1001/manifest.json",
                     "handles/alice.json"]
    raw_manifest = world.storage.read_bytes("bundles/1001/manifest.json")
    m = json.loads(raw_manifest)
    assert m["x_id"] == "1001" and m["handle"] == "alice"
    assert m["bundle_format"] == pb.BUNDLE_FORMAT and m["dump_format"] == 2
    assert m["counts"]["nodes"] == 8 and m["counts"]["profiles"] == 2
    assert m["counts"]["intentions"] == 1 and m["counts"]["recent_contexts"] == 1
    assert m["newest_entry_at"] == "2024-01-08T12:00:00Z"
    assert m["profile_generated_by"] == "gpt-6.1-sol"
    # No content in the manifest or the index.
    for text in (b"tweet", b"profile of", b"intentions of", b"reply to"):
        assert text not in raw_manifest
        assert text not in world.storage.read_bytes("handles/alice.json")
    local = world.tmp / "b.gz"
    world.storage.download("bundles/1001/bundle.jsonl.gz", str(local))
    assert pb.sha256_file(str(local)) == m["object"]["sha256"]
    with gzip.open(local, "rt") as f:
        lines = [json.loads(x) for x in f]
    assert len(lines) == m["object"]["lines"] == 9
    assert all(n["provenance"] == "prefill_ca" for n in lines[1:] if n["node_type"] == "user")
    assert lines[0]["profiles"][1]["window_set"] == "set-1"

    # Idempotent: a second run finds the fingerprint unchanged.
    again = _export(world, monkeypatch)
    assert again.get("exported", 0) == 0 and again["unchanged"] == 1
    # A change on the lab makes the account export again.
    with world.lab.app_context():
        u = User.query.filter_by(username="ca_alice").one()
        n = Node(user_id=u.id, human_owner_id=u.id, node_type="user",
                 privacy_level="private", ai_usage="chat", origin="twitter",
                 provenance="prefill_ca", source_key="twitter:1001999", token_count=1)
        n.set_content("a late tweet")
        db.session.add(n)
        db.session.commit()
    assert _export(world, monkeypatch, only="alice")["exported"] == 1


def test_export_only_and_the_encryption_guard(world, monkeypatch):
    with world.lab.app_context():
        _seed_lab_member()
        _seed_lab_member("ca_bob", "bob", "1002", n=3)
    assert _export(world, monkeypatch, only="1002")["exported"] == 1
    assert world.storage.exists("bundles/1002/manifest.json")
    assert not world.storage.exists("bundles/1001/manifest.json")
    # Where content is encrypted (any server but the lab) export refuses.
    _prod_mode(monkeypatch)
    with world.lab.app_context(), pytest.raises(SystemExit):
        cli.run_export(world.storage, out=lambda *_: None)


# ── Load on production ────────────────────────────────────────────────────

def _bundle(world, monkeypatch, **seed):
    with world.lab.app_context():
        _seed_lab_member(**seed)
    _export(world, monkeypatch)
    _prod_mode(monkeypatch)


def test_round_trip_equals_a_direct_dump_and_load(world, monkeypatch):
    """dump -> bucket -> bundle load into a fresh database holds exactly
    what load_user.py makes from the same dump in another fresh one."""
    _bundle(world, monkeypatch)
    raw = world.tmp / "direct.jsonl"
    world.storage.download("bundles/1001/bundle.jsonl.gz", str(raw) + ".gz")
    with gzip.open(str(raw) + ".gz", "rb") as src, open(raw, "wb") as dst:
        dst.write(src.read())

    direct = world.prod("direct")
    with direct.app_context():
        load_user._run(str(raw), "alice", merge=False, create_approved=False)
        expected = _snapshot(User.query.filter_by(username="alice").one().id)

    prod = world.prod()
    with prod.app_context():
        load, refusal = _start("alice", admin_id=None)
        assert refusal is None and load.origin == "admin"
        assert _run_until_done(load.id) == "done"
        user = User.query.filter_by(twitter_id="1001").one()
        assert user.username == "alice"
        got = _snapshot(user.id)
        assert got == expected
        # Encrypted on write with this server's key (one data key per chunk).
        tweet = Node.query.filter_by(human_owner_id=user.id, source_key="twitter:1001000").one()
        assert _raw(tweet.id).startswith(encryption.ENCRYPTED_PREFIX_V2)
        # A window set keeps its columns (no rebuild reads it as unfinished).
        final = UserProfile.query.filter_by(user_id=user.id, generation_type="integration").one()
        assert final.window_set == "set-1"
        assert final.parent_profile_id == UserProfile.query.filter_by(
            user_id=user.id, generation_type="window").one().id


def test_admin_load_makes_an_account_its_owner_x_sign_in_finds(world, monkeypatch):
    _bundle(world, monkeypatch)
    prod = world.prod()
    with prod.app_context():
        load, _ = _start("1001", admin_id=7, approve=True)
        assert world.dispatched == [load.id]
        assert _run_until_done(load.id) == "done"
        load = db.session.get(PrefillBundleLoad, load.id)
        assert (load.created_count, load.skipped_count) == (8, 0)
        assert load.profiles_count == 2 and load.artifacts_count == 1
        assert load.relinked_count == 4      # 2 replies, 1 continuation, the AI reply
        assert PrefillBundleNodeMap.query.count() == 0
        user = db.session.get(User, load.user_id)
        assert user.twitter_id == "1001" and user.approved is True
        # Recognisable for the deletion of unclaimed pre-fills (#685).
        from backend.utils.activity import sign_in_status
        assert user.prefilled_handle == "alice" and sign_in_status(user) == "never"
        assert user.profile_private_read is True and user.profile_force_batch is True
        assert user.preferred_model is None     # the lab's model is not the person's
        # Safe to repeat: the same bundle is not loaded twice.
        again, refusal = pbl.start_admin_load("alice", admin_id=7)
        assert again is None and refusal[0] == "already_loaded"
        assert Node.query.filter_by(human_owner_id=user.id).count() == 8


def test_a_chunked_load_resumes_after_a_killed_run(world, monkeypatch):
    """Chunks of two; the third run is killed mid-chunk (no release, as a
    SIGKILL leaves it); the beat sends it again once stale and the load
    ends exactly as an uninterrupted one."""
    _bundle(world, monkeypatch)
    prod = world.prod()
    with prod.app_context():
        load, _ = _start("alice", admin_id=None)
        assert pbl.run_load(load.id, chunk=2, max_chunks=1) == "continuing"
        assert pbl.run_load(load.id, chunk=2, max_chunks=1) == "continuing"
        assert db.session.get(PrefillBundleLoad, load.id).nodes_done == 4

        class Killed(BaseException):
            pass
        real = pbl.dump_rows.new_node
        calls = {"n": 0}

        def dying(*a, **k):
            calls["n"] += 1
            if calls["n"] == 2:
                raise Killed()
            return real(*a, **k)
        monkeypatch.setattr(pbl.dump_rows, "new_node", dying)
        with pytest.raises(Killed):
            pbl.run_load(load.id, chunk=2, max_chunks=1)
        db.session.rollback()
        monkeypatch.setattr(pbl.dump_rows, "new_node", real)
        # The dead run still holds the load until its heartbeat is stale.
        assert pbl.run_load(load.id, chunk=2) == "busy"
        assert pbl.stalled_load_ids() == []
        db.session.query(PrefillBundleLoad).update(
            {PrefillBundleLoad.heartbeat_at: datetime.utcnow() - timedelta(minutes=6)})
        db.session.commit()
        assert pbl.stalled_load_ids() == [load.id]
        assert _run_until_done(load.id, chunk=2) == "done"
        user = User.query.filter_by(twitter_id="1001").one()
        got = _snapshot(user.id)

    straight = world.prod("straight")
    with straight.app_context():
        load, _ = _start("alice", admin_id=None)
        assert _run_until_done(load.id) == "done"
        assert _snapshot(User.query.filter_by(twitter_id="1001").one().id) == got


def test_runs_killed_again_and_again_stop_the_load(world, monkeypatch):
    _bundle(world, monkeypatch)
    prod = world.prod()
    with prod.app_context():
        load, _ = _start("alice", admin_id=None)
        for _ in range(pbl.MAX_STALLED_RUNS):
            db.session.query(PrefillBundleLoad).update(
                {PrefillBundleLoad.run_token: None})
            db.session.commit()
            pbl._claim(load.id)          # a run that started and died
        db.session.query(PrefillBundleLoad).update({PrefillBundleLoad.run_token: None})
        db.session.commit()
        assert pbl.run_load(load.id) == "failed"
        assert db.session.get(PrefillBundleLoad, load.id).error == "stalled"


def test_a_checksum_mismatch_is_refused_before_anything_is_written(world, monkeypatch):
    _bundle(world, monkeypatch)
    obj = world.bucket / "bundles" / "1001" / "bundle.jsonl.gz"
    obj.write_bytes(obj.read_bytes() + b"tampered")
    prod = world.prod()
    with prod.app_context():
        load, _ = _start("alice", admin_id=None)
        assert pbl.run_load(load.id) == "failed"
        load = db.session.get(PrefillBundleLoad, load.id)
        assert load.status == "failed" and load.error == "checksum"
        assert Node.query.filter_by(human_owner_id=load.user_id).count() == 0
        assert UserProfile.query.filter_by(user_id=load.user_id).count() == 0


def test_an_x_id_mismatch_is_refused(world, monkeypatch):
    _bundle(world, monkeypatch)
    # A manifest stored under one X id that names another.
    m = json.loads(world.storage.read_bytes("bundles/1001/manifest.json"))
    world.storage.write_bytes("bundles/2002/manifest.json", json.dumps(m).encode())
    prod = world.prod()
    with prod.app_context():
        load, refusal = _start("2002", admin_id=None)
        assert load is None and refusal[0] == "x_id_mismatch"
        # The account loses the bundle's X id while the load runs.
        load, _ = _start("1001", admin_id=None)
        user = db.session.get(User, load.user_id)
        user.twitter_id = None
        db.session.commit()
        assert pbl.run_load(load.id) == "failed"
        assert db.session.get(PrefillBundleLoad, load.id).error == "x_id_mismatch"
        assert Node.query.filter_by(human_owner_id=user.id).count() == 0


def test_a_handle_never_matches_an_account(world, monkeypatch):
    """An account under the bundle's handle but with another X id gets
    nothing: the admin's load opens a separate account for the X id, and
    that account's own Yes finds no bundle."""
    _bundle(world, monkeypatch)
    prod = world.prod()
    with prod.app_context():
        other = User(username="alice", twitter_id="3003", twitter_handle="alice",
                     approved=True, last_seen_at=datetime.utcnow(),
                     prefill_consent="yes")
        db.session.add(other)
        db.session.commit()
        other_id = other.id
        assert pbl.maybe_start_self_load(other) is None
        load, _ = _start("alice", admin_id=None)
        assert load.user_id != other_id
        assert db.session.get(User, load.user_id).username == "alice2"
        assert _run_until_done(load.id) == "done"
        assert Node.query.filter_by(human_owner_id=other_id).count() == 0


@pytest.mark.parametrize("field,value,code", [
    ("prefill_consent", "no", "prefill_declined"),
    ("default_ai_usage", "none", "ai_opt_out"),
])
def test_a_no_and_ai_none_are_refused_even_when_forced(world, monkeypatch, field, value, code):
    _bundle(world, monkeypatch)
    prod = world.prod()
    with prod.app_context():
        user = User(username="alice", twitter_id="1001", **{field: value})
        db.session.add(user)
        db.session.commit()
        load, refusal = _start("alice", admin_id=None, force=True)
        assert load is None and refusal[0] == code


def test_a_no_given_while_loading_stops_the_load(world, monkeypatch):
    _bundle(world, monkeypatch)
    prod = world.prod()
    with prod.app_context():
        load, _ = _start("alice", admin_id=None)
        assert pbl.run_load(load.id, chunk=2, max_chunks=1) == "continuing"
        user = db.session.get(User, load.user_id)
        user.prefill_consent = "no"
        db.session.commit()
        assert pbl.run_load(load.id, chunk=2) == "failed"
        assert db.session.get(PrefillBundleLoad, load.id).error == "prefill_declined"
        assert Node.query.filter_by(human_owner_id=user.id).count() == 2


def test_an_owner_who_signed_in_or_wrote_is_protected_unless_forced(world, monkeypatch):
    _bundle(world, monkeypatch)
    prod = world.prod()
    with prod.app_context():
        user = User(username="alice", twitter_id="1001",
                    last_seen_at=datetime.utcnow())
        db.session.add(user)
        db.session.commit()
        load, refusal = _start("alice", admin_id=None)
        assert load is None and refusal[0] == "signed_in"
        assert pbl.check("alice")["refusal"]["forceable"] is True
        user.last_seen_at = None
        own = Node(user_id=user.id, human_owner_id=user.id, node_type="user",
                   privacy_level="private", ai_usage="chat", token_count=1)
        own.set_content("my own entry")
        db.session.add(own)
        db.session.commit()
        load, refusal = _start("alice", admin_id=None)
        assert load is None and refusal[0] == "own_writing"
        user_id = user.id
        load, refusal = _start("alice", admin_id=None, force=True)
        assert refusal is None and load.forced is True
        assert _run_until_done(load.id) == "done"
        assert Node.query.filter_by(human_owner_id=user_id).count() == 9


def test_no_model_and_no_network_during_a_load(world, monkeypatch):
    """The whole load, from the admin's start to done, opens no network
    connection (no model, no provider), and afterwards the profile
    pipeline has nothing to build: the loaded profile is current."""
    _bundle(world, monkeypatch)
    prod = world.prod()

    def no_network(*a, **k):
        raise AssertionError("a network connection during a bundle load")
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    with prod.app_context():
        load, _ = _start("alice", admin_id=None)
        assert _run_until_done(load.id) == "done"
        user = db.session.get(User, load.user_id)
        user.approved = True
        user.plan = "alpha"
        db.session.commit()
        from backend.tasks.profile_batch import _should_seed
        assert _should_seed(user) is False
        assert user.profile_needs_full_regen is False


def test_the_load_reads_the_bundle_as_a_stream(world, monkeypatch):
    """Entries are read one line at a time: a bundle whose last line is
    broken has its first chunks committed before the broken line is
    reached (a whole-file parse would write nothing), and the bundle is
    never read as one bytes object."""
    _bundle(world, monkeypatch)
    obj = world.bucket / "bundles" / "1001" / "bundle.jsonl.gz"
    with gzip.open(obj, "rt") as f:
        lines = f.read().splitlines()
    lines.append("{broken")
    obj.write_bytes(b"")
    with gzip.GzipFile(filename="", mode="wb", fileobj=open(obj, "wb"), mtime=0) as out:
        out.write(("\n".join(lines) + "\n").encode())
    m = json.loads(world.storage.read_bytes("bundles/1001/manifest.json"))
    m["object"]["sha256"] = pb.sha256_file(str(obj))
    pb.write_manifest(world.storage, m)
    reads = []
    real_read = LocalDirStorage.read_bytes
    monkeypatch.setattr(LocalDirStorage, "read_bytes",
                        lambda self, name: reads.append(name) or real_read(self, name))
    prod = world.prod()
    with prod.app_context():
        load, _ = _start("alice", admin_id=None)
        outcomes = [pbl.run_load(load.id, chunk=2, dispatch=lambda *a, **k: None)
                    for _ in range(pbl.MAX_STALLED_RUNS + 2)]
        assert outcomes[0] == "retrying"
        assert outcomes[-1] == "failed"
        assert Node.query.filter_by(human_owner_id=load.user_id).count() == 8
    assert not any(n.endswith(".gz") for n in reads)
    header, entries = pb.open_bundle(str(obj))
    assert inspect.isgenerator(entries)
    entries.close()


def test_switched_off_nothing_reads_the_storage(world, monkeypatch):
    _bundle(world, monkeypatch)
    prod = world.prod(enabled=False)
    monkeypatch.setattr(LocalDirStorage, "read_bytes",
                        lambda *a: (_ for _ in ()).throw(AssertionError("read")))
    with prod.app_context():
        user = User(username="alice", twitter_id="1001", twitter_handle="alice",
                    prefill_consent="yes")
        db.session.add(user)
        db.session.commit()
        assert pbl.maybe_start_self_load(user) is None
        assert pbl.start_admin_load("alice", admin_id=None)[1][0] == "disabled"
        assert pbl.run_load(1) == "disabled"
        assert pbl.stalled_load_ids() == []
        client = prod.test_client()
        _login(client, user.id)
        assert client.get("/api/dashboard/starting-point").get_json()["starting_point"] is None


# ── The automatic path ───────────────────────────────────────────────────

def _x_user(**kw):
    """An account as an X sign-in makes it (twitter_handle is written
    only by an X sign-in or Connect X)."""
    fields = dict(username="alice", twitter_id="1001", twitter_handle="alice",
                  approved=False, last_seen_at=datetime.utcnow())
    fields.update(kw)
    user = User(**fields)
    db.session.add(user)
    db.session.commit()
    return user


def test_a_yes_loads_the_prepared_material_and_lets_the_person_in(world, monkeypatch):
    _bundle(world, monkeypatch)
    prod = world.prod()
    with prod.app_context():
        user = _x_user()
        user_id = user.id
        client = prod.test_client()
        _login(client, user_id)
        resp = client.put("/api/dashboard/user", json={"prefill_consent": "yes"})
        assert resp.status_code == 200
        assert resp.get_json()["user"]["starting_point"]["status"] == "preparing"
        assert len(world.dispatched) == 1
        state = client.get("/api/dashboard/starting-point").get_json()
        assert state["starting_point"]["status"] == "preparing" and state["approved"] is False
        assert _run_until_done(world.dispatched[0]) == "done"
        state = client.get("/api/dashboard/starting-point").get_json()
        assert state["starting_point"]["status"] == "ready" and state["approved"] is True
        user = db.session.get(User, user_id)
        assert user.prefilled_handle == "alice"
        assert UserProfile.query.filter_by(user_id=user.id).count() == 2
        assert UserArtifact.latest_for(user.id, "intentions").get_content() == "intentions of alice"
        tweets = Node.query.filter_by(human_owner_id=user.id, provenance="prefill_ca").count()
        assert tweets == 7
        load = PrefillBundleLoad.query.one()
        assert load.origin == "self" and load.approve_when_done is True
        # A second Yes starts nothing new.
        client.put("/api/dashboard/user", json={"prefill_consent": "yes"})
        assert PrefillBundleLoad.query.count() == 1


@pytest.mark.parametrize("answer", ["no", None])
def test_a_no_or_no_answer_never_loads(world, monkeypatch, answer):
    _bundle(world, monkeypatch)
    prod = world.prod()
    with prod.app_context():
        user = _x_user()
        client = prod.test_client()
        _login(client, user.id)
        body = {"prefill_consent": answer} if answer else {"craft_mode": True}
        resp = client.put("/api/dashboard/user", json=body)
        assert resp.status_code == 200
        assert resp.get_json()["user"]["starting_point"] is None
        assert world.dispatched == [] and PrefillBundleLoad.query.count() == 0


def test_a_yes_needs_an_x_sign_in_and_a_bundle(world, monkeypatch):
    _bundle(world, monkeypatch)
    prod = world.prod()
    with prod.app_context():
        # The X id set by an admin, never proven by an X sign-in.
        unproven = _x_user(twitter_handle=None)
        unproven.prefill_consent = "yes"
        db.session.commit()
        assert pbl.maybe_start_self_load(unproven) is None
        # A proven X id the lab has no bundle for: nothing changes.
        stranger = _x_user(username="carol", twitter_id="4004", twitter_handle="carol",
                           prefill_consent="yes")
        assert pbl.maybe_start_self_load(stranger) is None
        assert PrefillBundleLoad.query.count() == 0 and world.dispatched == []


def test_a_yes_from_someone_with_own_writing_is_recorded_not_loaded(world, monkeypatch):
    _bundle(world, monkeypatch)
    prod = world.prod()
    with prod.app_context():
        user = _x_user(prefill_consent="yes")
        own = Node(user_id=user.id, human_owner_id=user.id, node_type="user",
                   privacy_level="private", ai_usage="chat", token_count=1)
        own.set_content("mine")
        db.session.add(own)
        db.session.commit()
        assert pbl.maybe_start_self_load(user) is None
        assert pbl.maybe_start_self_load(user) is None
        refused = PrefillBundleLoad.query.one()
        assert (refused.status, refused.error) == ("refused", "own_writing")
        assert pbl.starting_point(user.id) is None
        assert world.dispatched == []


# ── Removal and the exclusion list ───────────────────────────────────────

def test_delete_excludes_the_x_id_from_export_and_load(world, monkeypatch):
    _bundle(world, monkeypatch)
    deleted = pb.delete_bundle(world.storage, "1001", "deletion_request")
    assert sorted(deleted) == ["bundles/1001/bundle.jsonl.gz",
                               "bundles/1001/manifest.json", "handles/alice.json"]
    assert pb.exclusion(world.storage, "1001")["reason"] == "deletion_request"
    counts = _export(world, monkeypatch)
    assert counts["skipped_excluded"] == 1 and not world.storage.exists(
        "bundles/1001/manifest.json")
    # A bundle that reappeared (an old export's copy) is still not loaded.
    pb.remove_exclusion(world.storage, "1001")
    _export(world, monkeypatch)
    pb.write_exclusion(world.storage, "1001", "declined")
    _prod_mode(monkeypatch)
    prod = world.prod()
    with prod.app_context():
        assert pbl.start_admin_load("1001", admin_id=None)[1][0] == "excluded"
        user = _x_user(prefill_consent="yes")
        assert pbl.maybe_start_self_load(user) is None
        assert PrefillBundleLoad.query.one().error == "excluded"


def test_the_cli_delete_and_status(world, monkeypatch, capsys):
    _bundle(world, monkeypatch)
    with world.lab.app_context():
        cli.run_status(world.storage)
        cli.run_status(world.storage, "1001")
    out = capsys.readouterr().out
    assert "bundles: 1, excluded X ids: 0" in out and '"handle": "alice"' in out
    assert "tweet" not in out


# ── Admin routes ─────────────────────────────────────────────────────────

def test_admin_routes_check_start_and_follow_a_load(world, monkeypatch):
    _bundle(world, monkeypatch)
    prod = world.prod()
    with prod.app_context():
        admin = User(username="boss", is_admin=True, approved=True)
        plain = User(username="plain", approved=True)
        db.session.add_all([admin, plain])
        db.session.commit()
        client = prod.test_client()
        _login(client, plain.id)
        assert client.get("/api/admin/prefill-bundles/check?q=alice").status_code == 403
        _login(client, admin.id)
        assert client.get("/api/admin/prefill-bundles/config").get_json() == {"enabled": True}
        check = client.get("/api/admin/prefill-bundles/check?q=@alice").get_json()
        assert check["x_id"] == "1001" and check["account"]["exists"] is False
        assert check["bundle"]["counts"]["nodes"] == 8
        assert "tweet" not in json.dumps(check)
        assert client.get("/api/admin/prefill-bundles/check?q=nobody").status_code == 404
        resp = client.post("/api/admin/prefill-bundles/loads",
                           json={"q": "alice", "approve": False})
        assert resp.status_code == 202
        load_id = resp.get_json()["load"]["id"]
        _run_until_done(load_id)
        got = client.get(f"/api/admin/prefill-bundles/loads/{load_id}").get_json()["load"]
        assert got["status"] == "done" and got["created"] == 8 and got["approved"] is False
        listed = client.get("/api/admin/prefill-bundles/loads").get_json()["loads"]
        assert [ld["id"] for ld in listed] == [load_id]
        assert "tweet" not in json.dumps(listed)
        again = client.post("/api/admin/prefill-bundles/loads", json={"q": "alice"})
        assert again.status_code == 409 and again.get_json()["code"] == "already_loaded"

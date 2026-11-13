"""Pre-fill bundles from the lab (#698): export on the lab, the bucket
layout, and the load on production (admin and automatic paths).

No network: storage is a local directory (LocalDirStorage), and a fake
key service stands in for KMS. Each "server" is its own sqlite file: the
lab (encryption off, as on the lab), and one or more production
databases (encryption on). Bundle parts hold 3 entries here (500 in
production), so every load spans several parts.
"""
import gzip
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
    GCSStorage, LocalDirStorage, StorageError, storage_for,
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
    monkeypatch.setattr(pb, "MEMBER_LINES", 3)
    bucket = tmp_path / "bucket"
    bucket.mkdir()
    dispatched = []
    monkeypatch.setattr(pbl, "_dispatch",
                        lambda load_id, countdown=0: dispatched.append(load_id))

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
    a one-window profile set with its final merge, an intentions artifact
    and a recent context."""
    user = User(username=username, prefilled_handle=handle, twitter_id=x_id,
                approved=False, **user_kw)
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


def _export(world, monkeypatch, storage=None, **kw):
    _lab_mode(monkeypatch)
    with world.lab.app_context():
        return cli.run_export(storage or world.storage, out=lambda *_: None, **kw)


def _bundle(world, monkeypatch, **seed):
    with world.lab.app_context():
        _seed_lab_member(**seed)
    _export(world, monkeypatch)
    _prod_mode(monkeypatch)


def _entry(i, x_id="1001", **over):
    """One dumped tweet as dump_user writes it."""
    row = {"id": 100 + i, "author_username": "ca_hand", "owner_is_user": True,
           "parent_id": None, "continuation_node_id": None,
           "linked_node_id": None, "node_type": "user", "llm_model": None,
           "content": f"hand tweet {i}", "prompt": None, "prompt_key": None,
           "token_count": 4, "distributed_tokens": 0,
           "privacy_level": "private", "ai_usage": "chat",
           "source_key": f"twitter:{x_id}9{i:02d}", "origin": "twitter",
           "provenance": "prefill_ca", "tool_calls_meta": None,
           "created_at": (T0 + timedelta(days=i)).isoformat(),
           "updated_at": "2026-11-03T00:00:00", "embedding": None}
    row.update(over)
    return row


def _put_bundle(world, entries, x_id="1001", handle="hand", **header_over):
    """A bundle written by hand (members, manifest, handle index): what a
    lab-side bug or an edited file could put in the bucket."""
    header = {"format": 2, "user": {"username": "ca_hand"},
              "profiles": [{"id": 1, "content": "a drafted profile",
                            "generated_by": "gpt-6.1-sol",
                            "generation_type": "integration",
                            "created_at": "2024-02-01T00:00:00",
                            "privacy_level": "private", "ai_usage": "chat"}],
              "recent_contexts": [], "todos": [], "artifacts": []}
    header.update(header_over)
    lines = [json.dumps(header)] + [json.dumps(e) for e in entries]
    path = world.tmp / f"hand-{x_id}.jsonl.gz"
    obj = pb.write_members(iter(lines), str(path))
    world.storage.upload(pb.bundle_name(x_id), str(path))
    pb.write_manifest(world.storage, {
        "bundle_format": pb.BUNDLE_FORMAT, "dump_format": 2, "x_id": x_id,
        "handle": handle, "counts": {"nodes": len(entries)}, "object": obj})
    pb.write_handle_index(world.storage, handle, x_id)


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
    out_nodes = sorted(
        (n.get_content(), n.source_key, n.provenance, n.origin, n.privacy_level,
         n.ai_usage, n.node_type, n.created_at, n.token_count,
         ref(n.parent_id), ref(n.continuation_node_id), n.user_id == user_id)
        for n in nodes)
    profiles = UserProfile.query.filter_by(user_id=user_id).order_by(UserProfile.id).all()
    pid = {p.id: p for p in profiles}
    out_profiles = [
        (p.get_content(), p.generated_by, p.generation_type, p.window_set,
         p.covers_start, p.source_rendered_at, p.source_data_cutoff,
         pid[p.parent_profile_id].get_content() if p.parent_profile_id in pid else None,
         p.created_at, p.privacy_level, p.ai_usage)
        for p in profiles]
    arts = [(a.kind, a.get_content(), a.generated_by, a.created_at, a.privacy_level)
            for a in UserArtifact.query.filter_by(user_id=user_id).order_by(UserArtifact.id)]
    rcs = [(r.get_content(), pid[r.profile_id].get_content() if r.profile_id in pid else None)
           for r in UserRecentContext.query.filter_by(user_id=user_id)]
    return out_nodes, out_profiles, arts, rcs


def _raw(node_id):
    return db.session.execute(db.text("SELECT content FROM node WHERE id=:i"),
                              {"i": node_id}).scalar()


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


def _yes(client):
    return client.put("/api/dashboard/user", json={"prefill_consent": "yes"})


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


def test_local_storage_round_trip_and_ranges(tmp_path):
    s = LocalDirStorage(str(tmp_path))
    assert s.read_bytes("a/b.json") is None and not s.exists("a/b.json")
    s.write_bytes("a/b.json", b"{}")
    src = tmp_path / "src.bin"
    src.write_bytes(b"0123456789")
    s.upload("a/c.gz", str(src))
    assert sorted(s.list("a")) == ["a/b.json", "a/c.gz"]
    assert s.read_range("a/c.gz", 3, 4) == b"3456"
    assert s.read_range("a/none", 0, 1) is None
    assert s.delete("a/b.json") is True and s.delete("a/b.json") is False
    with pytest.raises(ValueError):
        s.read_bytes("../escape")


class _NotFound(Exception):
    code = 404


class _FakeBlob:
    def __init__(self, store, name, calls):
        self.store, self.name, self.calls = store, name, calls

    def exists(self, **kw):
        self.calls.append(("exists", self.name, kw))
        return self.name in self.store

    def download_as_bytes(self, start=None, end=None, **kw):
        self.calls.append(("download_as_bytes", self.name, kw))
        if self.name not in self.store:
            raise _NotFound()
        data = self.store[self.name]
        return data if start is None else data[start:end + 1]

    def upload_from_filename(self, path, content_type=None, **kw):
        self.calls.append(("upload_from_filename", self.name, kw))
        with open(path, "rb") as f:
            self.store[self.name] = f.read()

    def upload_from_string(self, data, content_type=None, **kw):
        self.calls.append(("upload_from_string", self.name, kw))
        self.store[self.name] = data

    def delete(self, **kw):
        self.calls.append(("delete", self.name, kw))
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

    def list_blobs(self, bucket, prefix=None, **kw):
        self.calls.append(("list_blobs", prefix, kw))
        return [type("B", (), {"name": k})() for k in sorted(self.store)
                if prefix is None or k.startswith(prefix)]


def test_gcs_storage_reads_ranges_under_its_prefix_without_retries(tmp_path):
    """The bucket implementation, against a stand-in client: names live
    under the prefix, a part is read by its byte range, a missing object
    reads as None / False, and no call uses the library's own retry (its
    default waits up to two minutes)."""
    client = _FakeClient()
    s = GCSStorage("bkt", "pf", client=client)
    s.write_bytes("bundles/1/manifest.json", b"{}")
    assert "pf/bundles/1/manifest.json" in client.store
    src = tmp_path / "b.gz"
    src.write_bytes(b"0123456789")
    s.upload("bundles/1/bundle.jsonl.gz", str(src))
    assert list(s.list("bundles")) == ["bundles/1/bundle.jsonl.gz",
                                       "bundles/1/manifest.json"]
    assert s.read_range("bundles/1/bundle.jsonl.gz", 2, 3) == b"234"
    assert s.read_bytes("nope") is None and s.read_range("nope", 0, 1) is None
    assert s.delete("nope") is False and s.exists("nope") is False
    for name, _key, kw in client.calls:
        if name != "upload_from_filename":
            assert kw.get("retry", "unset") is None, name
            assert kw.get("timeout") <= 10, name


def test_gcs_storage_turns_every_failure_into_a_storage_error(monkeypatch):
    """Missing credentials (raised when the client is made) and timeouts
    are StorageError, so an admin route answers 502, not 500."""
    from google.auth.exceptions import DefaultCredentialsError
    import google.cloud.storage as gcs_lib

    def no_creds(*a, **k):
        raise DefaultCredentialsError("no credentials")
    monkeypatch.setattr(gcs_lib, "Client", no_creds)
    with pytest.raises(StorageError):
        GCSStorage("bkt").read_bytes("bundles/1/manifest.json")

    class Slow(_FakeClient):
        def bucket(self, name):
            raise TimeoutError("read timed out")
    with pytest.raises(StorageError):
        GCSStorage("bkt", client=Slow()).read_range("x", 0, 1)


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
    assert counts["would_export_nodes"] == 7 + 3
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
    assert m["counts"]["nodes"] == 7 and m["counts"]["profiles"] == 2
    assert m["counts"]["intentions"] == 1 and m["counts"]["recent_contexts"] == 1
    assert m["counts"]["embeddings"] == 0
    assert m["newest_entry_at"] == "2024-01-07T12:00:00Z"
    assert m["profile_generated_by"] == "gpt-6.1-sol"
    # Parts: the header alone, then 3 entries each (500 in production).
    assert [p["lines"] for p in m["object"]["members"]] == [1, 3, 3, 1]
    assert pb.manifest_problem(m, "1001") is None
    # No content in the manifest or the index.
    for text in (b"tweet", b"profile of", b"intentions of"):
        assert text not in raw_manifest
        assert text not in world.storage.read_bytes("handles/alice.json")
    local = world.tmp / "b.gz"
    local.write_bytes(world.storage.read_bytes("bundles/1001/bundle.jsonl.gz"))
    assert pb.sha256_file(str(local)) == m["object"]["sha256"]
    with gzip.open(local, "rt") as f:      # one gzip file to any reader
        lines = [json.loads(x) for x in f]
    assert len(lines) == m["object"]["lines"] == 8
    assert all(n["provenance"] == "prefill_ca" for n in lines[1:])
    assert lines[0]["profiles"][1]["window_set"] == "set-1"
    # Each part reads alone by its range.
    second = pb.read_member(world.storage, "1001", m["object"]["members"][2])
    assert [n["content"] for n in second] == ["tweet 3 by alice", "tweet 4 by alice",
                                              "tweet 5 by alice"]

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


def test_export_reads_embeddings_with_their_entries(world, monkeypatch):
    """An account's vectors are read per batch of entries, not all at once
    (they are about 14 KB per entry), and travel in the bundle."""
    import base64
    from backend.models import NodeEmbedding
    from backend.scripts import dump_user
    with world.lab.app_context():
        u = _seed_lab_member()
        for node in Node.query.filter_by(human_owner_id=u.id):
            db.session.add(NodeEmbedding(node_id=node.id, user_id=u.id,
                                         model="text-embedding-3-small",
                                         content_hash="h", vector=b"\x00" * 8))
        db.session.commit()
    from sqlalchemy import event
    monkeypatch.setattr(dump_user, "NODE_BATCH", 3)
    queries = []

    def spy(conn, cursor, statement, params, context, many):
        if "FROM node_embedding" in statement and "vector" in statement:
            queries.append(statement)
    with world.lab.app_context():
        event.listen(db.engine, "before_cursor_execute", spy)
        try:
            counts = _export(world, monkeypatch)
        finally:
            event.remove(db.engine, "before_cursor_execute", spy)
    # One query per batch of 3 entries (7 entries: 3 batches), not one for
    # the whole account.
    assert counts["exported"] == 1 and len(queries) == 3
    m = json.loads(world.storage.read_bytes("bundles/1001/manifest.json"))
    assert m["counts"]["embeddings"] == 7
    entries = pb.read_member(world.storage, "1001", m["object"]["members"][1])
    assert base64.b64decode(entries[0]["embedding"]["vector"]) == b"\x00" * 8


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


def test_a_deletion_before_an_account_is_written_is_kept(world, monkeypatch):
    """The export read the exclusion list, then a deletion came for an X
    id it has still to write: the check just before writing sees it."""
    with world.lab.app_context():
        _seed_lab_member()
    monkeypatch.setattr(pb, "excluded_x_ids", lambda storage: set())  # read earlier
    pb.delete_bundle(world.storage, "1001", "deletion_request")
    counts = _export(world, monkeypatch)
    assert counts.get("exported", 0) == 0 and counts["skipped_excluded"] == 1
    assert sorted(world.storage.list("")) == ["excluded/1001.json"]


def test_a_deletion_while_an_account_is_written_is_kept(world, monkeypatch):
    """A deletion made while the account's bundle is uploading: either the
    check after writing sees it and removes what was written, or the
    deletion removes it. Nothing of the person stays."""
    with world.lab.app_context():
        _seed_lab_member()

    class DeleteDuringUpload(LocalDirStorage):
        def upload(self, name, src_path, content_type="application/gzip"):
            super().upload(name, src_path, content_type)
            pb.delete_bundle(LocalDirStorage(self.root), "1001", "deletion_request")
    counts = _export(world, monkeypatch, storage=DeleteDuringUpload(str(world.bucket)))
    assert counts.get("exported", 0) == 0 and counts["skipped_excluded"] == 1
    assert sorted(world.storage.list("")) == ["excluded/1001.json"]
    # And a later run writes nothing either.
    assert _export(world, monkeypatch)["skipped_excluded"] == 1
    assert sorted(world.storage.list("")) == ["excluded/1001.json"]


def test_a_bundle_holds_only_what_a_prefill_makes(world, monkeypatch):
    """A model's reply is no pre-fill material: the lab does not export
    an account holding one."""
    with world.lab.app_context():
        alice = _seed_lab_member()
        llm = User(username="claude-opus-5", twitter_id="llm-claude-opus-5")
        db.session.add(llm)
        db.session.flush()
        reply = Node(user_id=llm.id, human_owner_id=alice.id, node_type="llm",
                     privacy_level="private", ai_usage="chat", token_count=1)
        reply.set_content("a model's reply")
        db.session.add(reply)
        db.session.commit()
    counts = _export(world, monkeypatch)
    assert counts.get("exported", 0) == 0 and counts["skipped_other_material"] == 1


def test_the_lab_does_not_export_an_unfinished_profile(world, monkeypatch):
    """A window set whose final merge never came: production would build
    it, which is a model call. Not exported."""
    with world.lab.app_context():
        alice = _seed_lab_member()
        UserProfile.query.filter_by(user_id=alice.id,
                                    generation_type="integration").delete()
        db.session.commit()
    counts = _export(world, monkeypatch)
    assert counts.get("exported", 0) == 0 and counts["skipped_unfinished_profile"] == 1


@pytest.mark.parametrize("extra", ["todo", "ai_preferences"])
def test_the_lab_does_not_export_what_production_refuses(world, monkeypatch, extra):
    """A todo list or an artifact kind no pre-fill drafts: production
    would refuse the bundle, so the lab does not write it."""
    from backend.models import UserTodo
    with world.lab.app_context():
        alice = _seed_lab_member()
        row = (UserTodo(user_id=alice.id, generated_by="m") if extra == "todo"
               else UserArtifact(user_id=alice.id, kind=extra, title=extra,
                                 generated_by="m"))
        row.set_content("x")
        db.session.add(row)
        db.session.commit()
    counts = _export(world, monkeypatch)
    assert counts.get("exported", 0) == 0 and counts["skipped_other_material"] == 1


# ── Load on production ────────────────────────────────────────────────────

def test_round_trip_equals_a_direct_dump_and_load(world, monkeypatch):
    """dump -> bucket -> bundle load into a fresh database holds exactly
    what load_user.py makes from the same dump in another fresh one."""
    _bundle(world, monkeypatch)
    raw = world.tmp / "direct.jsonl"
    gz = world.tmp / "direct.jsonl.gz"
    gz.write_bytes(world.storage.read_bytes("bundles/1001/bundle.jsonl.gz"))
    with gzip.open(str(gz), "rb") as src, open(raw, "wb") as dst:
        dst.write(src.read())

    direct = world.prod("direct")
    with direct.app_context():
        load_user._run(str(raw), "alice", merge=False, create_approved=False)
        expected = _snapshot(User.query.filter_by(username="alice").one().id)

    prod = world.prod()
    with prod.app_context():
        load, refusal = _start("alice")
        assert refusal is None and load.origin == "admin"
        assert _run_until_done(load.id) == "done"
        user = User.query.filter_by(twitter_id="1001").one()
        assert user.username == "alice"
        assert _snapshot(user.id) == expected
        # Encrypted on write with this server's key (one data key per part).
        tweet = Node.query.filter_by(human_owner_id=user.id, source_key="twitter:1001000").one()
        assert _raw(tweet.id).startswith(encryption.ENCRYPTED_PREFIX_V2)
        # A window set keeps its columns.
        final = UserProfile.query.filter_by(user_id=user.id, generation_type="integration").one()
        assert final.window_set == "set-1"
        assert final.parent_profile_id == UserProfile.query.filter_by(
            user_id=user.id, generation_type="window").one().id


def test_admin_load_makes_an_account_its_owner_x_sign_in_finds(world, monkeypatch):
    _bundle(world, monkeypatch)
    prod = world.prod()
    with prod.app_context():
        load, _ = _start("1001", admin_id=7)
        assert world.dispatched == [load.id]
        assert _run_until_done(load.id) == "done"
        load = db.session.get(PrefillBundleLoad, load.id)
        assert (load.created_count, load.skipped_count) == (7, 0)
        assert load.profiles_count == 2 and load.artifacts_count == 1
        assert load.relinked_count == 3      # 2 replies, 1 continuation
        assert PrefillBundleNodeMap.query.count() == 0
        user = db.session.get(User, load.user_id)
        assert user.twitter_id == "1001"
        # The admin's load lets nobody in and sets no plan (#700's rule
        # owns that): the admin approves as for any account.
        assert user.approved is False and user.approved_at is None
        assert user.plan == "alpha"
        assert user.profile_private_read is True and user.profile_force_batch is True
        assert user.preferred_model is None     # the lab's model is not the person's
        # Safe to repeat: the same bundle is not loaded twice.
        again, refusal = pbl.start_admin_load("alice", admin_id=7)
        assert again is None and refusal[0] == "already_loaded"
        assert Node.query.filter_by(human_owner_id=user.id).count() == 7


def _person_signs(user):
    """What would tell the deletion of unclaimed pre-filled accounts
    (#685, PR #701) that a person used the account: the eleven user-row
    signals of PR #701 (utils/unclaimed_accounts._COLUMN_SIGNALS), and the
    row checks a load could trip. That module is not on this branch, so
    the fields are asserted here directly."""
    from backend.models import (
        ApiToken, ChangelogReadState, Draft, ExternalAccount, NodeVersion,
        UsernameHistory, UserPrompt, UserTodo, APICostLog)
    checks = {
        "signed in": user.last_seen_at is not None,
        "terms": user.accepted_terms_at is not None or bool(user.accepted_terms_version),
        "signed in with X": bool(user.twitter_handle) or user.x_connected_at is not None,
        "consent": user.prefill_consent is not None or user.prefill_consent_at is not None,
        "email change": bool(user.pending_email) or bool(user.email_change_token_hash),
        "deletion asked": bool(user.account_deletion_token_hash),
        "profile models": bool(user.profile_both_providers)
        or user.profile_both_providers_answered_at is not None,
        "glean": user.glean_enabled is not None,
        "settings": bool(user.craft_mode or user.public_sharing_enabled
                         or user.external_content_enabled),
        "defaults": (user.default_ai_usage or "chat") != "chat"
        or (user.default_privacy_level or "private") != "private",
        "time zone": (user.timezone or "UTC") != "UTC",
    }
    out = [k for k, v in checks.items() if v]
    uid = user.id
    own = Node.query.filter((Node.user_id == uid) | (Node.human_owner_id == uid))
    if own.filter((Node.origin.is_(None)) | (Node.origin != "twitter")
                  | (Node.provenance.notin_(("prefill_ca", "prefill_x")))).first():
        out.append("an entry of their own")
    if NodeVersion.query.join(Node, NodeVersion.node_id == Node.id).filter(
            (Node.user_id == uid) | (Node.human_owner_id == uid)).first():
        out.append("edited an entry")
    for model in (UserProfile, UserTodo, UserArtifact, UserPrompt):
        if model.query.filter(model.user_id == uid,
                              model.generated_by.in_(("user", "revert"))).first():
            out.append(f"saved {model.__name__}")
    if UserArtifact.query.filter(UserArtifact.user_id == uid,
                                 UserArtifact.updated_at.isnot(None)).first():
        out.append("saved a document")
    for model in (APICostLog, Draft, ApiToken, ChangelogReadState,
                  UsernameHistory, ExternalAccount):
        if model.query.filter(model.user_id == uid).first():
            out.append(model.__name__)
    return out


def test_an_account_made_ready_stays_recognisably_unclaimed(world, monkeypatch):
    """An account the admin made ready that nobody signed in to must still
    look unclaimed to the deletion after 90 days (#685): the load sets
    prefilled_handle and nothing a person's own action sets, not even the
    lab account's time zone, and its entries' updated_at is when this
    server stored them (the deletion counts from the first)."""
    _bundle(world, monkeypatch, timezone="Europe/Prague")
    prod = world.prod()
    with prod.app_context():
        before = datetime.utcnow()
        load, _ = _start("alice")
        assert _run_until_done(load.id) == "done"
        user = db.session.get(User, load.user_id)
        from backend.utils.activity import sign_in_status
        assert user.prefilled_handle == "alice" and sign_in_status(user) == "never"
        assert _person_signs(user) == []
        assert user.timezone == "UTC"
        first_stored = db.session.query(db.func.min(Node.updated_at)).filter(
            Node.human_owner_id == user.id).scalar()
        assert first_stored >= before


def test_a_chunked_load_resumes_after_a_killed_run(world, monkeypatch):
    """One part per step; the third run is killed mid-part (no release, as
    a SIGKILL leaves it); the beat sends it again once stale and the load
    ends exactly as an uninterrupted one."""
    _bundle(world, monkeypatch)
    prod = world.prod()
    with prod.app_context():
        load, _ = _start("alice")
        assert pbl.run_load(load.id, max_chunks=1) == "continuing"
        assert pbl.run_load(load.id, max_chunks=1) == "continuing"
        assert db.session.get(PrefillBundleLoad, load.id).nodes_done == 6

        class Killed(BaseException):
            pass
        real = pbl.dump_rows.new_node

        def dying(*a, **k):
            raise Killed()
        monkeypatch.setattr(pbl.dump_rows, "new_node", dying)
        with pytest.raises(Killed):
            pbl.run_load(load.id, max_chunks=1)
        db.session.rollback()
        monkeypatch.setattr(pbl.dump_rows, "new_node", real)
        # The dead run still holds the load until its heartbeat is stale.
        assert pbl.run_load(load.id) == "busy"
        assert pbl.stalled_load_ids() == []
        db.session.query(PrefillBundleLoad).update(
            {PrefillBundleLoad.heartbeat_at: datetime.utcnow() - timedelta(minutes=6)})
        db.session.commit()
        assert pbl.stalled_load_ids() == [load.id]
        assert _run_until_done(load.id) == "done"
        user = User.query.filter_by(twitter_id="1001").one()
        got = _snapshot(user.id)

    straight = world.prod("straight")
    with straight.app_context():
        load, _ = _start("alice")
        assert _run_until_done(load.id) == "done"
        assert _snapshot(User.query.filter_by(twitter_id="1001").one().id) == got


def test_each_part_is_read_once_by_its_range(world, monkeypatch):
    """A load reads each part by its byte range: once to check it before
    anything is written, once per pass. Never the whole object at once,
    never a part again for a later part."""
    _bundle(world, monkeypatch)
    reads = []
    real = LocalDirStorage.read_range
    monkeypatch.setattr(LocalDirStorage, "read_range",
                        lambda self, n, s, ln: reads.append((n, s)) or real(self, n, s, ln))
    monkeypatch.setattr(LocalDirStorage, "read_bytes",
                        lambda self, n, _r=LocalDirStorage.read_bytes: (
                            (_ for _ in ()).throw(AssertionError(n))
                            if n.endswith(".gz") else _r(self, n)))
    prod = world.prod()
    with prod.app_context():
        load, _ = _start("alice")
        reads.clear()
        assert _run_until_done(load.id, max_chunks=1) == "done"
    m = json.loads(world.storage.read_bytes("bundles/1001/manifest.json"))
    offsets = [p["offset"] for p in m["object"]["members"]]
    entry_parts = len(offsets) - 1
    assert all(n == "bundles/1001/bundle.jsonl.gz" for n, _ in reads)
    # verify (all parts) + header per run + each entry part twice
    # (entries, links); runs: one per part here.
    per_part = {o: sum(1 for _, s in reads if s == o) for o in offsets[1:]}
    assert all(c == 3 for c in per_part.values()), per_part
    assert len(per_part) == entry_parts


def test_runs_killed_again_and_again_stop_the_load(world, monkeypatch):
    _bundle(world, monkeypatch)
    prod = world.prod()
    with prod.app_context():
        load, _ = _start("alice")
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
    data = bytearray(obj.read_bytes())
    data[-5] ^= 0xFF          # a byte of the last part
    obj.write_bytes(bytes(data))
    prod = world.prod()
    with prod.app_context():
        load, _ = _start("alice")
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
        load, refusal = _start("2002")
        assert load is None and refusal[0] == "x_id_mismatch"
        # The account loses the bundle's X id while the load runs.
        load, _ = _start("1001")
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
        other = _x_user(twitter_id="3003", approved=True, prefill_consent="yes")
        other_id = other.id
        check = pbl.maybe_start_self_load(other)
        assert _run_until_done(check.id) == "no_bundle"
        load, _ = _start("alice")
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
        load, refusal = _start("alice", force=True)
        assert load is None and refusal[0] == code


def test_an_owner_who_signed_in_or_wrote_is_protected_unless_forced(world, monkeypatch):
    _bundle(world, monkeypatch)
    prod = world.prod()
    with prod.app_context():
        user = User(username="alice", twitter_id="1001",
                    last_seen_at=datetime.utcnow())
        db.session.add(user)
        db.session.commit()
        load, refusal = _start("alice")
        assert load is None and refusal[0] == "signed_in"
        assert pbl.check("alice")["refusal"]["forceable"] is True
        user.last_seen_at = None
        own = Node(user_id=user.id, human_owner_id=user.id, node_type="user",
                   privacy_level="private", ai_usage="chat", token_count=1)
        own.set_content("my own entry")
        db.session.add(own)
        db.session.commit()
        load, refusal = _start("alice")
        assert load is None and refusal[0] == "own_writing"
        user_id = user.id
        load, refusal = _start("alice", force=True)
        assert refusal is None and load.forced is True
        assert _run_until_done(load.id) == "done"
        assert Node.query.filter_by(human_owner_id=user_id).count() == 8


def test_old_unlabelled_prefills_are_named_not_guessed(world, monkeypatch):
    """Tweets a production pre-fill stored before 2026-09-21 have no
    provenance: they are neither guessed to be fetched nor counted as
    the person's writing. A Yes is refused with its own reason, which the
    admin sees (#691's labelling settles them)."""
    _bundle(world, monkeypatch)
    prod = world.prod()
    with prod.app_context():
        user = _x_user(prefill_consent="yes")
        old = Node(user_id=user.id, human_owner_id=user.id, node_type="user",
                   privacy_level="private", ai_usage="chat", token_count=1,
                   origin="twitter", source_key="twitter:5")
        old.set_content("an old pre-filled tweet")
        db.session.add(old)
        db.session.commit()
        assert pbl.own_writing_reason(user.id) == "unlabelled_tweets"
        check = pbl.maybe_start_self_load(user)
        assert _run_until_done(check.id) == "refused"
        refused = PrefillBundleLoad.query.one()
        assert refused.error == "unlabelled_tweets"
        assert pbl.serialize_load(refused)["error"] == "unlabelled_tweets"
        assert pbl.check("1001")["account"]["own_writing"] == "unlabelled_tweets"


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
        load, _ = _start("alice")
        assert _run_until_done(load.id) == "done"
        user = db.session.get(User, load.user_id)
        user.approved = True
        db.session.commit()
        from backend.tasks.profile_batch import _should_seed
        assert _should_seed(user) is False
        assert user.profile_needs_full_regen is False


def test_two_loads_into_one_account_run_one_after_the_other(world, monkeypatch):
    """Two loads started at once (a double click, or the admin and the
    person's Yes): the later one waits for the earlier, then finds every
    entry there and adds nothing twice."""
    _bundle(world, monkeypatch)
    prod = world.prod()
    with prod.app_context():
        first, _ = _start("alice")
        earlier = db.session.get(PrefillBundleLoad, first.id)
        second = PrefillBundleLoad(
            user_id=earlier.user_id, x_id="1001", handle="alice", origin="admin",
            stage="header", bundle_sha256=earlier.bundle_sha256,
            members=earlier.members, nodes_total=earlier.nodes_total,
            heartbeat_at=datetime.utcnow())
        db.session.add(second)
        db.session.commit()
        second_id = second.id
        assert pbl.run_load(second_id) == "busy"
        assert _run_until_done(first.id) == "done"
        assert _run_until_done(second_id) == "done"
        later = db.session.get(PrefillBundleLoad, second_id)
        assert (later.created_count, later.skipped_count, later.profiles_count) == (0, 7, 0)
        assert Node.query.filter_by(human_owner_id=first.user_id).count() == 7
        assert UserProfile.query.filter_by(user_id=first.user_id).count() == 2


def test_switched_off_nothing_reads_the_storage(world, monkeypatch):
    _bundle(world, monkeypatch)
    prod = world.prod(enabled=False)
    monkeypatch.setattr(LocalDirStorage, "read_bytes",
                        lambda *a: (_ for _ in ()).throw(AssertionError("read")))
    with prod.app_context():
        user = _x_user(prefill_consent="yes")
        assert pbl.maybe_start_self_load(user) is None
        assert pbl.start_admin_load("alice", admin_id=None)[1][0] == "disabled"
        assert pbl.run_load(1) == "disabled"
        assert pbl.stalled_load_ids() == []
        client = prod.test_client()
        _login(client, user.id)
        assert client.get("/api/dashboard/starting-point").get_json()["starting_point"] is None


# ── A bundle is not trusted ──────────────────────────────────────────────

def test_a_bundle_cannot_make_an_entry_public_or_trainable(world, monkeypatch):
    """Whatever the bundle says, entries are stored private, with AI usage
    at most "chat" (never "train"; a lower value stays), encrypted; so are
    the profile, recent contexts and intentions. Fields outside the
    allow-list are dropped."""
    entries = [_entry(0, privacy_level="public", ai_usage="train",
                      distributed_tokens=99, is_pinned=True, pinned_by=1),
               _entry(1, ai_usage="none")]
    _put_bundle(world, entries,
                profiles=[{"id": 1, "content": "a drafted profile",
                           "generated_by": "gpt-6.1-sol",
                           "generation_type": "integration",
                           "created_at": "2024-02-01T00:00:00",
                           "privacy_level": "public", "ai_usage": "train"}],
                recent_contexts=[{"content": "recent", "generated_by": "m",
                                  "created_at": "2024-02-02T00:00:00",
                                  "ai_usage": "train"}],
                artifacts=[{"kind": "intentions", "title": "Intentions",
                            "content": "to write", "generated_by": "m",
                            "created_at": "2024-02-03T00:00:00",
                            "privacy_level": "public", "ai_usage": "train"}])
    _prod_mode(monkeypatch)
    prod = world.prod()
    with prod.app_context():
        load, _ = _start("hand")
        assert _run_until_done(load.id) == "done"
        nodes = Node.query.filter_by(human_owner_id=load.user_id).all()
        # Never public, never more than "chat"; a "none" stays "none".
        assert sorted((n.source_key[-2:], n.privacy_level, n.ai_usage,
                       n.distributed_tokens) for n in nodes) == [
            ("00", "private", "chat", 0), ("01", "private", "none", 0)]
        assert all(_raw(n.id).startswith(encryption.ENCRYPTED_PREFIX_V2) for n in nodes)
        p = UserProfile.query.filter_by(user_id=load.user_id).one()
        a = UserArtifact.query.filter_by(user_id=load.user_id).one()
        r = UserRecentContext.query.filter_by(user_id=load.user_id).one()
        assert (p.privacy_level, p.ai_usage) == ("private", "chat")
        assert (a.privacy_level, a.ai_usage) == ("private", "chat")
        assert r.ai_usage == "chat"
        assert p.content.startswith(encryption.ENCRYPTED_PREFIX_V2)


@pytest.mark.parametrize("over", [
    {"prompt_key": "glean"},
    {"tool_calls_meta": {"calls": 1}},
    {"llm_model": "claude-opus-5"},
    {"node_type": "llm"},
    {"origin": None},
    {"provenance": "archive_upload"},
    {"source_key": None},
    {"owner_is_user": False},
    {"prompt": {"prompt_key": "textmode", "content": "You are Loore."}},
])
def test_an_entry_no_prefill_makes_is_refused_before_its_part_is_written(world, monkeypatch, over):
    entries = [_entry(0), _entry(1), _entry(2), _entry(3, **over)]
    _put_bundle(world, entries)
    _prod_mode(monkeypatch)
    prod = world.prod()
    with prod.app_context():
        load, _ = _start("hand")
        assert _run_until_done(load.id) == "failed"
        assert db.session.get(PrefillBundleLoad, load.id).error == "not_prefill"
        # The first part (3 entries) went in; the part holding it did not.
        assert Node.query.filter_by(human_owner_id=load.user_id).count() == 3


def test_an_entry_by_another_account_is_refused(world, monkeypatch):
    _put_bundle(world, [_entry(0, author_username="someone_else")])
    _prod_mode(monkeypatch)
    prod = world.prod()
    with prod.app_context():
        load, _ = _start("hand")
        assert _run_until_done(load.id) == "failed"
        assert db.session.get(PrefillBundleLoad, load.id).error == "foreign_author"


@pytest.mark.parametrize("header_over", [
    {"todos": [{"content": "a todo", "generated_by": "m"}]},
    {"artifacts": [{"kind": "ai_preferences", "content": "x", "generated_by": "m"}]},
    {"profiles": [{"id": 1, "content": "mine", "generated_by": "user",
                   "generation_type": "user"}]},
])
def test_a_header_no_prefill_makes_is_refused_before_anything_is_written(world, monkeypatch, header_over):
    _put_bundle(world, [_entry(0)], **header_over)
    _prod_mode(monkeypatch)
    prod = world.prod()
    with prod.app_context():
        load, _ = _start("hand")
        assert _run_until_done(load.id) == "failed"
        assert db.session.get(PrefillBundleLoad, load.id).error == "not_prefill"
        assert Node.query.filter_by(human_owner_id=load.user_id).count() == 0
        assert UserProfile.query.filter_by(user_id=load.user_id).count() == 0


# ── The automatic path ───────────────────────────────────────────────────

def test_a_yes_loads_the_prepared_material_and_lets_the_person_in(world, monkeypatch):
    """The Yes request does no storage work: it queues a check, and the
    background task finds the bundle, loads it and lets the person in."""
    _bundle(world, monkeypatch)
    prod = world.prod()
    with prod.app_context():
        user = _x_user()
        user_id = user.id
        client = prod.test_client()
        _login(client, user_id)
        touched = []
        for name in ("read_bytes", "read_range", "exists", "list"):
            real = getattr(LocalDirStorage, name)
            monkeypatch.setattr(LocalDirStorage, name,
                                lambda self, *a, _r=real, _n=name: touched.append(_n) or _r(self, *a))
        resp = _yes(client)
        assert resp.status_code == 200 and touched == []
        assert resp.get_json()["user"]["starting_point"]["status"] == "checking"
        assert len(world.dispatched) == 1
        assert _run_until_done(world.dispatched[0], max_chunks=1) == "done"
        state = client.get("/api/dashboard/starting-point").get_json()
        assert state["starting_point"]["status"] == "ready" and state["approved"] is True
        user = db.session.get(User, user_id)
        assert user.prefilled_handle == "alice" and user.plan == "alpha"
        assert UserProfile.query.filter_by(user_id=user_id).count() == 2
        assert UserArtifact.latest_for(user_id, "intentions").get_content() == "intentions of alice"
        assert Node.query.filter_by(human_owner_id=user_id, provenance="prefill_ca").count() == 7
        load = PrefillBundleLoad.query.one()
        assert load.origin == "self" and load.approve_when_done is True
        # A second Yes starts nothing new.
        _yes(client)
        assert PrefillBundleLoad.query.count() == 1


def test_a_yes_after_a_no_continues_where_the_no_stopped(world, monkeypatch):
    """Yes, part of the load, No: the run stops there, links what it
    loaded, and lets nobody in (what was loaded stays, #662). Yes again:
    a new load skips what is there and loads the rest, with nothing
    twice, and lets the person in."""
    _bundle(world, monkeypatch)
    prod = world.prod()
    with prod.app_context():
        user_id = _x_user().id
        client = prod.test_client()
        _login(client, user_id)
        _yes(client)
        first = world.dispatched[-1]
        assert pbl.run_load(first, max_chunks=1) == "continuing"
        assert Node.query.filter_by(human_owner_id=user_id).count() == 3
        _login(client, user_id)
        client.put("/api/dashboard/user", json={"prefill_consent": "no"})
        assert _run_until_done(first) == "stopped"
        stopped = db.session.get(PrefillBundleLoad, first)
        assert (stopped.status, stopped.error) == ("stopped", "prefill_declined")
        assert stopped.links_done == stopped.nodes_done == 3
        assert db.session.get(User, user_id).approved is False
        reply = Node.query.filter_by(human_owner_id=user_id,
                                     source_key="twitter:1001002").one()
        assert reply.parent_id is not None       # linked though stopped
        assert client.get("/api/dashboard/starting-point").get_json()["starting_point"] is None
        assert PrefillBundleNodeMap.query.count() == 0

        _yes(client)
        second = world.dispatched[-1]
        assert second != first
        assert _run_until_done(second) == "done"
        later = db.session.get(PrefillBundleLoad, second)
        assert (later.created_count, later.skipped_count) == (4, 3)
        assert Node.query.filter_by(human_owner_id=user_id).count() == 7
        assert UserProfile.query.filter_by(user_id=user_id).count() == 2
        assert db.session.get(User, user_id).approved is True
        # The continuation from an entry of the first load to one of the
        # second is set: an entry already there gets the links it lacks.
        first_entry = Node.query.filter_by(human_owner_id=user_id,
                                           source_key="twitter:1001001").one()
        assert first_entry.continuation_node_id == Node.query.filter_by(
            human_owner_id=user_id, source_key="twitter:1001004").one().id


def test_a_yes_after_a_failed_load_loads_again(world, monkeypatch):
    """A load that failed (the bundle was gone for a moment) does not
    block the next Yes."""
    _bundle(world, monkeypatch)
    obj = world.bucket / "bundles" / "1001" / "bundle.jsonl.gz"
    data = obj.read_bytes()
    prod = world.prod()
    with prod.app_context():
        user_id = _x_user().id
        client = prod.test_client()
        _login(client, user_id)
        _yes(client)
        first = world.dispatched[-1]
        obj.unlink()
        assert _run_until_done(first) == "failed"
        assert db.session.get(PrefillBundleLoad, first).error == "no_bundle"
        obj.write_bytes(data)
        _yes(client)
        second = world.dispatched[-1]
        assert second != first and _run_until_done(second) == "done"
        assert Node.query.filter_by(human_owner_id=user_id).count() == 7


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
        unproven = _x_user(twitter_handle=None, prefill_consent="yes")
        assert pbl.maybe_start_self_load(unproven) is None
        # A proven X id the lab has no bundle for: the check finds none,
        # leaves no trace, and nothing changes.
        stranger = _x_user(username="carol", twitter_id="4004", twitter_handle="carol",
                           prefill_consent="yes")
        stranger_id = stranger.id
        check = pbl.maybe_start_self_load(stranger)
        assert pbl.starting_point(stranger_id)["status"] == "checking"
        assert _run_until_done(check.id) == "no_bundle"
        assert PrefillBundleLoad.query.count() == 0
        assert pbl.starting_point(stranger_id) is None


def test_a_yes_from_someone_with_own_writing_is_recorded_not_loaded(world, monkeypatch):
    _bundle(world, monkeypatch)
    prod = world.prod()
    with prod.app_context():
        user = _x_user(prefill_consent="yes")
        user_id = user.id
        own = Node(user_id=user.id, human_owner_id=user.id, node_type="user",
                   privacy_level="private", ai_usage="chat", token_count=1)
        own.set_content("mine")
        db.session.add(own)
        db.session.commit()
        for _ in range(2):
            check = pbl.maybe_start_self_load(db.session.get(User, user_id))
            assert _run_until_done(check.id) == "refused"
        refused = PrefillBundleLoad.query.one()
        assert (refused.status, refused.error) == ("refused", "own_writing")
        assert pbl.starting_point(user_id) is None


def test_let_in_when_ready_leaves_a_blocked_account_out(world, monkeypatch):
    prod = world.prod()
    with prod.app_context():
        blocked = User(username="b", approved=False, deactivated_at=datetime.utcnow())
        fresh = User(username="f", approved=False)
        db.session.add_all([blocked, fresh])
        db.session.commit()
        assert pbl.let_in_when_ready(blocked) is False and blocked.approved is False
        assert pbl.let_in_when_ready(fresh) is True and fresh.approved is True
        assert fresh.plan == "alpha"     # the plan is #700's rule to own


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
        check = pbl.maybe_start_self_load(user)
        assert _run_until_done(check.id) == "refused"
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
        assert check["bundle"]["counts"]["nodes"] == 7
        assert "tweet" not in json.dumps(check)
        assert client.get("/api/admin/prefill-bundles/check?q=nobody").status_code == 404
        resp = client.post("/api/admin/prefill-bundles/loads", json={"q": "alice"})
        assert resp.status_code == 202
        load_id = resp.get_json()["load"]["id"]
        _run_until_done(load_id)
        got = client.get(f"/api/admin/prefill-bundles/loads/{load_id}").get_json()["load"]
        assert got["status"] == "done" and got["created"] == 7 and got["approved"] is False
        listed = client.get("/api/admin/prefill-bundles/loads").get_json()["loads"]
        assert [ld["id"] for ld in listed] == [load_id]
        assert "tweet" not in json.dumps(listed)
        again = client.post("/api/admin/prefill-bundles/loads", json={"q": "alice"})
        assert again.status_code == 409 and again.get_json()["code"] == "already_loaded"


def test_admin_routes_answer_502_when_the_storage_fails(world, monkeypatch):
    prod = world.prod()
    with prod.app_context():
        admin = User(username="boss", is_admin=True, approved=True)
        db.session.add(admin)
        db.session.commit()
        client = prod.test_client()
        _login(client, admin.id)

        class Down(LocalDirStorage):
            def read_bytes(self, name):
                raise StorageError("storage call failed (TimeoutError)")
        monkeypatch.setattr(pb, "configured_storage", lambda config=None: Down(str(world.bucket)))
        for resp in (client.get("/api/admin/prefill-bundles/check?q=alice"),
                     client.post("/api/admin/prefill-bundles/loads", json={"q": "alice"})):
            assert resp.status_code == 502 and resp.get_json()["code"] == "storage"

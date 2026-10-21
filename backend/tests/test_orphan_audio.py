"""Audio files of deleted entries (#541).

1. The daily cleanup of deleted entries (backend/tasks/node_cleanup.py)
   deletes an entry's audio folders after its rows are committed: the
   recording, the chunks, the speech (an AI reply's is under the model
   account's folder) and the folders of the recording sessions its rows
   named.
2. backend/scripts/sweep_orphan_audio.py deletes the folders already on
   disk whose owner is certainly gone, dry run by default, and keeps
   everything else: accounts in their grace period, a waiting "Delete all
   my writing", folders a stored URL points into, recent folders,
   symlinks, recording sessions and paths it cannot explain.

sqlite in-memory, a temporary storage root, Celery stubbed; nothing is
ever decrypted.
"""
import importlib
import importlib.util
import os
import sys
import time
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

import backend.models as _real_backend_models  # noqa: E402
from backend.extensions import db as _db  # noqa: E402
from backend.models import (  # noqa: E402
    Draft, ExternalItem, Node, NodeTranscriptChunk, TTSChunk, User,
    UserDataPurge, UserDataPurgeHidden, UserProfile,
)


def _make_app():
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    _db.init_app(app)
    return app


_app = _make_app()

# The real cleanup module against an identity-decorator Celery stub and
# this file's app (as test_bookmark_nightly_sync.py does), so the task
# itself runs: its commit, then the files.
_celery_stub = MagicMock()
_celery_stub.celery.task = lambda *a, **k: (lambda fn: fn)
_celery_stub.flask_app = _app


def _import_real_cleanup():
    import backend.tasks as tasks_pkg
    glue = ("backend.celery_app", "backend.tasks.node_cleanup")
    saved = {k: sys.modules.get(k) for k in glue}
    saved_attr = getattr(tasks_pkg, "node_cleanup", None)
    sys.modules["backend.celery_app"] = _celery_stub
    sys.modules.pop("backend.tasks.node_cleanup", None)
    try:
        return importlib.import_module("backend.tasks.node_cleanup")
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        if saved_attr is None:
            if hasattr(tasks_pkg, "node_cleanup"):
                delattr(tasks_pkg, "node_cleanup")
        else:
            tasks_pkg.node_cleanup = saved_attr


_cleanup = _import_real_cleanup()
assert not isinstance(_cleanup.cleanup_deleted_nodes, MagicMock)


def _load_sweep():
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "scripts", "sweep_orphan_audio.py")
    spec = importlib.util.spec_from_file_location("_sweep_orphan_audio",
                                                  path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sweep_mod = _load_sweep()

NOW = datetime.utcnow()
OLD = NOW - timedelta(days=40)
OLD_EPOCH = time.time() - 10 * 86400


@pytest.fixture
def app(monkeypatch, tmp_path):
    saved_flask_app = _cleanup.flask_app
    _cleanup.flask_app = _app
    root = tmp_path / "audio"
    root.mkdir()
    monkeypatch.setattr(_cleanup.audio_storage, "AUDIO_STORAGE_ROOT", root)
    with _app.app_context():
        _db.create_all()
        _app.audio_root = root
        _app.outside = tmp_path / "outside"
        _app.outside.mkdir()
        yield _app
        _db.session.remove()
        _db.drop_all()
    _cleanup.flask_app = saved_flask_app


@pytest.fixture
def no_decrypt(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("content was decrypted")
    import backend.utils.encryption as enc
    monkeypatch.setattr(enc, "decrypt_content", boom)
    monkeypatch.setattr(_real_backend_models, "decrypt_content", boom)


def _add(obj):
    _db.session.add(obj)
    _db.session.flush()
    return obj


def _user(name, **kw):
    return _add(User(username=name, twitter_id=f"tw-{name}", **kw))


def _node(user, parent=None, *, owner="same", text="words", **kw):
    n = Node(user_id=user.id,
             human_owner_id=user.id if owner == "same" else owner,
             parent_id=parent.id if parent else None, node_type="user",
             privacy_level="private", ai_usage="none", token_count=1, **kw)
    n.set_content(text)
    return _add(n)


def _file(root, rel, data=b"audio"):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _run_cleanup():
    """The task runs in an app context of its own (its own session); the
    test reads the database afresh after it."""
    result = _cleanup.cleanup_deleted_nodes()
    _db.session.expunge_all()
    return result


def _age_tree(root):
    """Date every file and folder under *root* 10 days back, so the
    sweep's age gate lets them through."""
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = os.path.join(dirpath, name)
            if not os.path.islink(p):
                os.utime(p, (OLD_EPOCH, OLD_EPOCH))


# ── 1. The cleanup deletes the files ────────────────────────────────────

def test_cleanup_purge_deletes_the_entry_and_ai_reply_folders(app):
    root = app.audio_root
    alice, llm = _user("alice"), _user("llm-model")
    gone = _node(alice, audio_original_url="/media/x", deleted_at=OLD)
    reply = Node(user_id=llm.id, human_owner_id=alice.id, node_type="llm",
                 privacy_level="private", ai_usage="none", token_count=1,
                 deleted_at=OLD, audio_tts_url="/media/y")
    reply.set_content("reply")
    _add(reply)
    live = _node(alice)
    _db.session.commit()
    f_rec = _file(root, f"user/{alice.id}/node/{gone.id}/original.webm.enc")
    f_chunks = _file(root, f"nodes/{alice.id}/{gone.id}/batch_0.webm.enc")
    f_speech = _file(root, f"user/{llm.id}/node/{reply.id}/tts_chunk_0.mp3")
    f_live = _file(root, f"user/{alice.id}/node/{live.id}/original.webm.enc")
    gone_id, reply_id = gone.id, reply.id

    result = _run_cleanup()

    assert result["purged"] == 2 and result["errors"] == 0
    assert result["audio_folders"] == 3
    assert _db.session.get(Node, gone_id) is None
    assert _db.session.get(Node, reply_id) is None
    for f in (f_rec, f_chunks, f_speech):
        assert not f.exists() and not f.parent.exists()
    assert f_live.exists()


def test_cleanup_wipe_deletes_folders_and_the_session(app):
    root = app.audio_root
    alice = _user("alice")
    parent = _node(alice, deleted_at=OLD, audio_original_url="/media/x",
                   audio_tts_url="/media/y", streaming_session_id="sess-old")
    child = _node(alice, parent)
    _db.session.commit()
    f_rec = _file(root, f"user/{alice.id}/node/{parent.id}/original.webm.enc")
    f_stream = _file(root, f"streaming/{alice.id}/sess-old/chunk_0000.webm")
    f_child = _file(root, f"user/{alice.id}/node/{child.id}/tts.mp3")

    result = _run_cleanup()

    assert result["wiped"] == 1 and result["audio_folders"] == 2
    row = _db.session.get(Node, parent.id)
    assert row is not None and row.content is None
    assert row.audio_original_url is None and row.audio_tts_url is None
    # Kept, so the sweep still finds the session's folder should its
    # delete ever fail.
    assert row.streaming_session_id == "sess-old"
    assert not f_rec.exists() and not f_stream.exists()
    assert f_child.exists()


def test_cleanup_handles_more_candidates_than_one_batch(app):
    """The candidates' ids are read first, then each node is processed
    and committed: no cursor stays open across the commits (on Postgres
    a commit closed the old yield_per cursor after the first batch)."""
    root = app.audio_root
    alice = _user("alice")
    n = _cleanup.CLEANUP_BATCH_SIZE + 5
    ids = [_node(alice, deleted_at=OLD).id for _ in range(n)]
    restored = _node(alice, deleted_at=OLD)
    _db.session.commit()
    for nid in ids:
        _file(root, f"user/{alice.id}/node/{nid}/original.webm.enc")
    f_restored = _file(root, f"user/{alice.id}/node/{restored.id}/a.mp3")
    real = _cleanup._still_candidates

    def restore_one_first(batch, cutoff):
        # A node restored after the first select is left alone.
        Node.query.filter_by(id=restored.id).update({"deleted_at": None})
        _db.session.commit()
        return real(batch, cutoff)
    _cleanup._still_candidates = restore_one_first
    try:
        result = _run_cleanup()
    finally:
        _cleanup._still_candidates = real

    assert result == {"purged": n, "wiped": 0, "errors": 0,
                      "audio_folders": n}
    assert Node.query.filter(Node.id.in_(ids)).count() == 0
    assert _db.session.get(Node, restored.id).deleted_at is None
    assert f_restored.exists()


def test_cleanup_keeps_a_session_folder_another_row_names(app):
    root = app.audio_root
    alice = _user("alice")
    a = _node(alice, deleted_at=OLD)
    b = _node(alice, deleted_at=OLD)
    _add(NodeTranscriptChunk(node_id=a.id, session_id="s-draft",
                             chunk_index=0, status="completed"))
    _add(NodeTranscriptChunk(node_id=b.id, session_id="s-alone",
                             chunk_index=0, status="completed"))
    d = Draft(user_id=alice.id, session_id="s-draft")
    d.set_content("")
    _add(d)
    _db.session.commit()
    kept = _file(root, f"drafts/{alice.id}/s-draft/chunk_0.webm")
    gone = _file(root, f"drafts/{alice.id}/s-alone/chunk_0.webm")

    _run_cleanup()

    assert kept.exists()
    assert not gone.exists()


def test_cleanup_keeps_files_of_nodes_it_does_not_finish(app):
    root = app.audio_root
    alice = _user("alice")
    recent = _node(alice, deleted_at=NOW - timedelta(days=3))
    hidden = _node(alice, deleted_at=OLD)
    job = _add(UserDataPurge(user_id=alice.id, source="self",
                             scope="hidden", scheduled_for=NOW))
    _add(UserDataPurgeHidden(job_id=job.id, kind="node", row_id=hidden.id))
    _db.session.commit()
    f_recent = _file(root, f"user/{alice.id}/node/{recent.id}/original.webm")
    f_hidden = _file(root, f"user/{alice.id}/node/{hidden.id}/original.webm")

    _run_cleanup()

    assert f_recent.exists() and f_hidden.exists()


def test_a_failed_commit_keeps_the_files(app, monkeypatch):
    root = app.audio_root
    alice = _user("alice")
    n = _node(alice, deleted_at=OLD)
    _db.session.commit()
    f = _file(root, f"user/{alice.id}/node/{n.id}/original.webm.enc")

    def fail():
        raise RuntimeError("database went away")
    monkeypatch.setattr(_db.session, "commit", fail)
    result = _run_cleanup()
    monkeypatch.undo()

    assert result["errors"] == 1 and result["audio_folders"] == 0
    assert f.exists()


def test_remove_audio_dirs_never_follows_a_symlink(app):
    from backend.utils import audio_storage
    target = app.outside / "keep-me"
    target.mkdir()
    (target / "x.mp3").write_bytes(b"x")
    link = app.audio_root / "user" / "1" / "node" / "5"
    link.parent.mkdir(parents=True)
    link.symlink_to(target, target_is_directory=True)

    assert audio_storage.remove_audio_dirs([link], MagicMock()) == 0
    assert (target / "x.mp3").exists()


# ── 2. The sweep ────────────────────────────────────────────────────────

class World:
    pass


@pytest.fixture
def world(app):
    """Users and rows, and a folder for each case. ``w.delete`` are the
    folders the sweep must delete, ``w.keep`` the ones it must keep."""
    root = app.audio_root
    w = World()
    w.alice = _user("alice")
    w.bob = _user("bob", deleted_at=NOW - timedelta(days=2))  # in grace
    w.carol = _user("carol")                                 # writing deletion
    w.erased = _user("loore-erased")
    w.llm = _user("llm-model")
    a, b, c, llm = w.alice.id, w.bob.id, w.carol.id, w.llm.id
    _add(UserDataPurge(user_id=c, source="self", scope="hidden",
                       scheduled_for=NOW + timedelta(days=20)))

    live = _node(w.alice, streaming_session_id="s-live-stream")
    restorable = _node(w.alice, deleted_at=NOW - timedelta(days=5))
    # Wiped by the old cleanup, which kept the streaming session; a live
    # reply below keeps the tombstone.
    wiped = _node(w.alice, deleted_at=OLD, streaming_session_id="s-wiped")
    wiped.content = None
    _node(w.alice, wiped)
    fresh_tombstone = _node(w.alice, deleted_at=NOW - timedelta(days=5),
                            streaming_session_id="s-fresh")
    fresh_tombstone.content = None
    bobs_wiped = _node(w.bob, deleted_at=OLD, streaming_session_id="s-bob")
    bobs_wiped.content = None
    # A purged account's tombstone, handed to the erased account.
    handed = _node(w.erased, owner=w.erased.id,
                   deleted_at=NOW - timedelta(days=2))
    handed.content = None
    bobs_reply = Node(user_id=llm, human_owner_id=b, node_type="llm",
                      privacy_level="private", ai_usage="none",
                      token_count=1, deleted_at=OLD)
    _add(bobs_reply)
    # A legacy AI reply (no human owner) a waiting writing deletion hid.
    legacy = Node(user_id=llm, human_owner_id=None, node_type="llm",
                  privacy_level="private", ai_usage="none", token_count=1,
                  deleted_at=OLD)
    _add(legacy)
    job = _add(UserDataPurge(user_id=a, source="self", scope="hidden",
                             status="done", scheduled_for=OLD))
    _add(UserDataPurgeHidden(job_id=job.id, kind="node", row_id=legacy.id))

    profile = _add(UserProfile(user_id=a, content="p", generated_by="m"))
    # A profile version sharing a gone version's speech folder.
    _add(UserProfile(user_id=a, content="p2", generated_by="m",
                     audio_tts_url=f"/media/user/{a}/profile/9002/tts.mp3?v=1"))
    hidden_profile = _add(UserProfile(user_id=a, content="p3",
                                      generated_by="m"))
    _add(UserDataPurgeHidden(job_id=job.id, kind="user_profile",
                             row_id=hidden_profile.id))
    item = _add(ExternalItem(user_id=a, source="web_clip", external_id="e1",
                             content="clip"))
    _add(TTSChunk(item_id=item.id, chunk_index=0, status="completed",
                  audio_url=f"/media/user/{a}/item/9102/tts_chunk_0.mp3"))
    d = Draft(user_id=a, session_id="s-live")
    d.set_content("")
    _add(d)
    _db.session.commit()

    gone, gone2 = 9001, 9011   # node ids that do not exist
    w.delete = {
        sweep_mod.NODE_GONE: [
            f"user/{a}/node/{gone}", f"nodes/{a}/{gone}",
            f"user/{llm}/node/{gone2}"],
        sweep_mod.TOMBSTONE: [
            f"user/{a}/node/{wiped.id}",
            f"user/777/node/{handed.id}"],      # account 777 is gone
        sweep_mod.STREAM_TOMBSTONE: [f"streaming/{a}/s-wiped"],
        sweep_mod.PROFILE_GONE: [f"user/{a}/profile/9001"],
        sweep_mod.ITEM_GONE: [f"user/{a}/item/9101"],
    }
    w.keep = {
        sweep_mod.OWNER_THERE: [
            f"user/{a}/node/{live.id}", f"user/{a}/profile/{profile.id}",
            f"user/{a}/profile/{hidden_profile.id}",
            f"user/{a}/item/{item.id}"],
        sweep_mod.RESTORABLE: [
            f"user/{a}/node/{restorable.id}",
            f"user/{a}/node/{fresh_tombstone.id}"],
        sweep_mod.PENDING: [
            f"user/{b}/node/{gone}", f"user/{llm}/node/{bobs_reply.id}",
            f"user/{c}/node/{gone}", f"user/{c}/profile/9001",
            f"user/{llm}/node/{legacy.id}", f"streaming/{b}/s-bob"],
        sweep_mod.REFERENCED: [
            f"user/{a}/profile/9002", f"user/{a}/item/9102"],
        sweep_mod.STREAM_ROW: [
            f"streaming/{a}/s-live-stream", f"streaming/{a}/s-fresh"],
        sweep_mod.STREAM_NO_ROW: [f"streaming/{a}/s-old"],
        sweep_mod.DRAFT_ROW: [f"drafts/{a}/s-live"],
        sweep_mod.DRAFT_NO_ROW: [f"drafts/{a}/s-lost"],
        sweep_mod.UPLOAD: [f"chunks/{a}/up-1"],
        sweep_mod.UNKNOWN: [
            f"user/{a}/node/0123", f"user/{a}/notes", "misc", "nodes/x"],
    }
    for rels in list(w.delete.values()) + list(w.keep.values()):
        for rel in rels:
            name = "metadata.json" if rel.startswith("chunks/") else "a.mp3"
            _file(root, f"{rel}/{name}", data=b"12345")
    # Recent, with a symlink inside, and a symlinked folder: all kept,
    # and nothing outside the root is touched.
    w.recent = f"user/{a}/node/9021"
    _file(root, f"{w.recent}/a.mp3")
    w.odd = f"user/{a}/node/9031"
    _file(root, f"{w.odd}/a.mp3")
    w.target = app.outside / "elsewhere.mp3"
    w.target.write_bytes(b"x")
    (root / w.odd / "link.mp3").symlink_to(w.target)
    w.linked = f"user/{a}/node/9041"
    w.linked_target = app.outside / "dir"
    w.linked_target.mkdir()
    (w.linked_target / "a.mp3").write_bytes(b"x")
    (root / w.linked).symlink_to(w.linked_target, target_is_directory=True)
    w.keep[sweep_mod.UNKNOWN].append(w.linked)
    _age_tree(root)
    os.utime(root / w.recent / "a.mp3", None)
    return w


def _exists(root, rel):
    return os.path.lexists(root / rel)


def test_dry_run_counts_and_deletes_nothing(world, app, no_decrypt):
    root = app.audio_root
    report = sweep_mod.sweep(root, now=NOW)
    for reason, rels in world.delete.items():
        assert report.folders[(sweep_mod.DELETE, reason)] == len(rels), reason
        assert report.bytes[(sweep_mod.DELETE, reason)] == 5 * len(rels)
    for reason, rels in world.keep.items():
        assert report.folders[(sweep_mod.KEEP, reason)] == len(rels), reason
    assert report.folders[(sweep_mod.KEEP, "changed in the last 24 h")] == 1
    assert report.folders[(sweep_mod.KEEP, sweep_mod.ODD)] == 1
    for rels in list(world.delete.values()) + list(world.keep.values()):
        for rel in rels:
            assert _exists(root, rel), rel


def test_apply_deletes_only_folders_whose_owner_is_gone(world, app,
                                                        no_decrypt):
    root = app.audio_root
    report = sweep_mod.sweep(root, apply=True, now=NOW)
    for reason, rels in world.delete.items():
        assert report.folders[(sweep_mod.DELETE, reason)] == len(rels)
        for rel in rels:
            assert not _exists(root, rel), rel
    for rels in world.keep.values():
        for rel in rels:
            assert _exists(root, rel), rel
    for rel in (world.recent, world.odd, world.linked):
        assert _exists(root, rel), rel
    assert world.target.exists()
    assert (world.linked_target / "a.mp3").exists()

    again = sweep_mod.sweep(root, now=NOW)
    assert again.total(sweep_mod.DELETE) == (0, 0, 0)


def test_apply_looks_again_before_deleting(world, app, monkeypatch):
    """A folder that changes between the walk and the delete is kept."""
    root = app.audio_root
    rel = world.delete[sweep_mod.NODE_GONE][0]
    real = sweep_mod.referenced

    def touch_then_scan(rels):
        os.utime(root / rel / "a.mp3", None)
        return real(rels)
    monkeypatch.setattr(sweep_mod, "referenced", touch_then_scan)

    report = sweep_mod.sweep(root, apply=True, now=NOW)

    assert _exists(root, rel)
    assert report.folders[(sweep_mod.KEEP, "changed in the last 24 h")] == 2


def test_apply_refuses_when_too_much_would_go(world, app):
    """Against the wrong database most folders look orphaned: --apply
    then deletes nothing."""
    root = app.audio_root
    report = sweep_mod.sweep(root, apply=True, now=NOW, max_share=0.1)
    assert report.refused == 0.1
    assert report.folders[(sweep_mod.DELETE, sweep_mod.NODE_GONE)] == 3
    for rels in world.delete.values():
        for rel in rels:
            assert _exists(root, rel), rel


def test_age_gate_can_be_lowered(world, app):
    report = sweep_mod.sweep(app.audio_root, now=datetime.utcnow(),
                             min_age=timedelta(0))
    assert report.folders[(sweep_mod.DELETE, sweep_mod.NODE_GONE)] == 4


def test_url_folder():
    assert sweep_mod.url_folder("/media/user/5/node/12/tts.mp3?v=3") == \
        "user/5/node/12"
    assert sweep_mod.url_folder("/media/nodes/5/12/batch_0.webm") == \
        "nodes/5/12"
    assert sweep_mod.url_folder("https://x.org/media/user/5/item/7/a.mp3") \
        == "user/5/item/7"
    assert sweep_mod.url_folder("/media/x") is None
    assert sweep_mod.url_folder(None) is None


def test_missing_root_reports_nothing(app, tmp_path):
    report = sweep_mod.sweep(tmp_path / "nowhere", apply=True)
    assert report.total(sweep_mod.DELETE) == (0, 0, 0)
    assert report.total(sweep_mod.KEEP) == (0, 0, 0)

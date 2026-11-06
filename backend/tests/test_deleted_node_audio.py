"""A finalized node's audio files go with it.

The daily cleanup (backend/tasks/node_cleanup.py) deletes a node's audio
folders before it purges the row or wipes a tombstone, and leaves a node
whose files it could not delete for the next run. The one-off script
backend/scripts/purge_orphaned_node_audio.py deletes the folders earlier
runs left behind. Neither touches a live node's files, a node within the
grace period, a node a waiting "Delete all my writing" hid, or anything
outside the audio root.

sqlite in-memory, ENCRYPTION_DISABLED, a temporary audio root.
"""
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
from backend.models import Node, TTSChunk, User, UserDataPurgeHidden  # noqa: E402
from backend.utils import audio_storage  # noqa: E402

DAY = 86400


@pytest.fixture
def app():
    saved = sys.modules.get("backend.models")
    sys.modules["backend.models"] = _real_backend_models
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    _db.init_app(app)
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()
    if saved is not None:
        sys.modules["backend.models"] = saved


@pytest.fixture
def root(tmp_path, monkeypatch):
    audio = (tmp_path / "audio").resolve()
    audio.mkdir()
    monkeypatch.setattr(audio_storage, "AUDIO_STORAGE_ROOT", audio)
    return audio


@pytest.fixture
def users(app):
    out = []
    for name in ("alice", "bob", "model"):
        u = User(username=name, twitter_id=f"{name}-tw")
        _db.session.add(u)
        out.append(u)
    _db.session.commit()
    return out


def _node(user, parent=None, *, owner=None, deleted_days=None, **kw):
    n = Node(user_id=user.id,
             human_owner_id=owner.id if owner else user.id,
             parent_id=parent.id if parent else None, node_type="user",
             privacy_level="private", ai_usage="none", token_count=1, **kw)
    n.set_content("words")
    if deleted_days is not None:
        n.deleted_at = datetime.utcnow() - timedelta(days=deleted_days)
    _db.session.add(n)
    _db.session.commit()
    return n


def _file(path, data=b"audio"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _node_files(root, node, author=None):
    """One file of each kind in each of the node's folders: an upload,
    generated speech (merged + chunk), a recording's chunks (plain and
    .enc)."""
    a = (author or node.user_id)
    return [
        _file(root / f"user/{a}/node/{node.id}/original.webm.enc"),
        _file(root / f"user/{a}/node/{node.id}/tts.mp3"),
        _file(root / f"user/{a}/node/{node.id}/tts_chunk_0.mp3.enc"),
        _file(root / f"nodes/{a}/{node.id}/batch_0000.webm.enc"),
        _file(root / f"nodes/{a}/{node.id}/chunk_0001.webm"),
    ]


def _cleanup():
    from backend.tasks.node_cleanup import finalize_deleted_nodes
    return finalize_deleted_nodes()


# ── The daily cleanup ───────────────────────────────────────────────────

def test_purge_deletes_both_layouts_and_the_streaming_session(app, root, users):
    alice = users[0]
    n = _node(alice, deleted_days=31, streaming_session_id="sess-1",
              audio_original_url="/media/x", audio_tts_url="/media/y")
    files = _node_files(root, n)
    files.append(_file(root / f"streaming/{alice.id}/sess-1/chunk_0000.webm"))
    nid = n.id

    result = _cleanup()

    assert _db.session.get(Node, nid) is None
    assert result["purged"] == 1
    assert result["audio_files_deleted"] == len(files)
    assert result["audio_errors"] == 0
    assert not any(f.exists() for f in files)
    assert not (root / f"user/{alice.id}/node/{nid}").exists()
    assert not (root / f"nodes/{alice.id}/{nid}").exists()
    assert not (root / f"streaming/{alice.id}/sess-1").exists()


def test_purge_finds_an_ai_replys_audio_under_the_model_account(app, root, users):
    alice, _, model = users
    reply = _node(model, owner=alice, deleted_days=31)
    files = _node_files(root, reply, author=model.id)

    _cleanup()

    assert _db.session.get(Node, reply.id) is None
    assert not any(f.exists() for f in files)


def test_wipe_deletes_the_tombstones_audio_and_keeps_the_childs(app, root, users):
    alice, bob, _ = users
    parent = _node(alice, deleted_days=31, audio_tts_url="/media/t")
    child = _node(bob, parent=parent)
    _db.session.add(TTSChunk(node_id=parent.id, chunk_index=0,
                             status="completed"))
    _db.session.commit()
    parent_files = _node_files(root, parent)
    child_files = _node_files(root, child)

    result = _cleanup()

    tomb = _db.session.get(Node, parent.id)
    assert tomb is not None and tomb.content is None
    assert tomb.audio_tts_url is None
    assert TTSChunk.query.filter_by(node_id=parent.id).count() == 0
    assert result["wiped"] == 1
    assert not any(f.exists() for f in parent_files)
    assert all(f.exists() for f in child_files)


def test_live_in_grace_and_hidden_nodes_keep_their_files(app, root, users):
    alice = users[0]
    live = _node(alice)
    recent = _node(alice, deleted_days=10)
    hidden = _node(alice, deleted_days=40)
    gone = _node(alice, deleted_days=31)
    _db.session.add(UserDataPurgeHidden(job_id=1, kind="node",
                                        row_id=hidden.id))
    _db.session.commit()
    kept = (_node_files(root, live) + _node_files(root, recent)
            + _node_files(root, hidden))
    doomed = _node_files(root, gone)

    _cleanup()

    assert all(f.exists() for f in kept)
    assert not any(f.exists() for f in doomed)
    for n in (live, recent, hidden):
        assert _db.session.get(Node, n.id) is not None


def test_a_file_that_cannot_be_deleted_keeps_the_node_for_the_next_run(
        app, root, users, monkeypatch):
    alice = users[0]
    stuck = _node(alice, deleted_days=32)
    other = _node(alice, deleted_days=31)
    stuck_files = _node_files(root, stuck)
    other_files = _node_files(root, other)
    blocked = str(stuck_files[1])
    real_unlink = os.unlink

    def unlink(path, *a, **kw):
        if str(path) == blocked:
            raise PermissionError(13, "denied")
        return real_unlink(path, *a, **kw)
    monkeypatch.setattr(os, "unlink", unlink)

    result = _cleanup()

    assert result["audio_errors"] == 1
    assert result["purged"] == 1                 # the rest of the batch went on
    assert _db.session.get(Node, other.id) is None
    assert not any(f.exists() for f in other_files)
    kept = _db.session.get(Node, stuck.id)
    assert kept is not None and kept.content == "words"   # nothing changed
    assert stuck_files[1].exists()

    monkeypatch.setattr(os, "unlink", real_unlink)
    result = _cleanup()
    assert result["purged"] == 1 and result["audio_errors"] == 0
    assert _db.session.get(Node, stuck.id) is None
    assert not any(f.exists() for f in stuck_files)


def test_cleanup_never_follows_a_symlink_out_of_the_root(
        app, root, users, tmp_path):
    alice = users[0]
    outside = tmp_path / "outside"
    target = _file(outside / "precious.webm")
    linked_target = _file(tmp_path / "elsewhere" / "other.webm")
    n = _node(alice, deleted_days=31)
    # The node's user/ folder is a symlink out of the root; its nodes/
    # folder holds a symlink to a file outside the root.
    (root / f"user/{alice.id}/node").mkdir(parents=True)
    os.symlink(outside, root / f"user/{alice.id}/node/{n.id}")
    (root / f"nodes/{alice.id}/{n.id}").mkdir(parents=True)
    os.symlink(linked_target, root / f"nodes/{alice.id}/{n.id}/link.webm")

    _cleanup()

    assert target.exists()
    assert linked_target.exists()
    assert not os.path.lexists(root / f"nodes/{alice.id}/{n.id}/link.webm")
    # A symlinked folder is left alone and does not hold the node back.
    assert _db.session.get(Node, n.id) is None


def test_delete_folders_never_touches_a_path_outside_the_root(root, tmp_path):
    outside = _file(tmp_path / "not-audio" / "keep.txt")
    result = audio_storage.delete_folders([outside.parent], root)
    assert outside.exists()
    assert result.skipped == 1 and result.files == 0


def test_node_audio_dirs_refuse_a_bad_session_id(root):
    n = Node(id=7, user_id=3, human_owner_id=3,
             streaming_session_id="../../etc")
    dirs = audio_storage.node_audio_dirs(n, root)
    assert all(str(d).startswith(str(root) + os.sep) for d in dirs)
    assert len(dirs) == 2


# ── The one-off script ──────────────────────────────────────────────────

def _script():
    from backend.scripts import purge_orphaned_node_audio as script
    return script


def _layout(root, users, tmp_path):
    """Every category the script tells apart, and files it must never
    touch. Returns (doomed, kept) file lists."""
    alice, bob, model = users
    live = _node(alice)
    in_grace = _node(alice, deleted_days=10)
    past = _node(bob, deleted_days=40)
    hidden = _node(alice, deleted_days=40)
    _db.session.add(UserDataPurgeHidden(job_id=1, kind="node",
                                        row_id=hidden.id))
    reply = _node(model, owner=alice)
    _db.session.commit()
    gone_ids = (90001, 90002)

    doomed = _node_files(root, past)
    for gid in gone_ids:
        doomed += [_file(root / f"user/{alice.id}/node/{gid}/tts.mp3.enc"),
                   _file(root / f"nodes/{alice.id}/{gid}/batch_0000.webm")]
    kept = (_node_files(root, live) + _node_files(root, in_grace)
            + _node_files(root, hidden)
            + _node_files(root, reply, author=model.id))
    # Not node folders: never looked at.
    kept += [
        _file(root / f"user/{alice.id}/profile/90003/tts.mp3.enc"),
        _file(root / f"user/{alice.id}/item/90004/tts.mp3.enc"),
        _file(root / f"drafts/{alice.id}/sess-9/chunk_0000.webm.enc"),
        _file(root / f"streaming/{alice.id}/sess-8/chunk_0000.webm"),
        _file(root / f"chunks/{alice.id}/up-1/chunk_0000"),
        _file(root / f"user/{alice.id}/node/notanid/x.webm"),
        _file(root / "stray.webm"),
    ]
    # A node folder that is a symlink out of the root, for a gone node.
    outside = _file(tmp_path / "outside" / "precious.webm")
    os.symlink(outside.parent, root / f"nodes/{alice.id}/90005")
    kept.append(outside)
    return doomed, kept


def test_script_dry_run_deletes_nothing(app, root, users, tmp_path):
    doomed, kept = _layout(root, users, tmp_path)
    lines = []
    result = _script().run(apply=False, now=time.time() + 2 * DAY,
                           out=lines.append)
    assert all(f.exists() for f in doomed + kept)
    assert result["to_delete"] == 6      # past: 2 folders; 2 gone ids x 2
    text = "\n".join(lines)
    assert "would delete" in text and "node row gone" in text
    assert "90001" not in text           # node ids only with --verbose


def test_script_apply_deletes_exactly_the_orphan_categories(
        app, root, users, tmp_path):
    doomed, kept = _layout(root, users, tmp_path)
    script = _script()
    later = time.time() + 2 * DAY
    lines = []
    result = script.run(apply=True, verbose=True, now=later, out=lines.append)

    assert not any(f.exists() for f in doomed)
    assert all(f.exists() for f in kept)
    assert result["failed"] == 0
    assert result["deleted_files"] == len(doomed)
    assert any("node 90001" in line for line in lines)
    # Nothing left to delete.
    again = script.run(apply=False, now=later, out=lambda s: None)
    assert again["to_delete"] == 0


def test_script_leaves_recently_changed_orphans_for_a_later_run(
        app, root, users, tmp_path):
    doomed, kept = _layout(root, users, tmp_path)
    lines = []
    result = _script().run(apply=True, out=lines.append)   # files are new
    assert result["to_delete"] == 0
    assert all(f.exists() for f in doomed + kept)
    assert any("changed in the last 24 h" in line for line in lines)

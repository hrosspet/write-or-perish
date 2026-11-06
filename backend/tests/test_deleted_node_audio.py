"""Audio files go when nothing needs them any more.

* The daily cleanup (backend/tasks/node_cleanup.py) deletes a node's
  audio folders before it purges the row or wipes a tombstone; a file it
  cannot delete never holds the rows back, and the audio sweep at the end
  of the run (backend/utils/audio_sweep.py) deletes it on a later day.
* The sweep, also run by hand through
  backend/scripts/purge_orphaned_node_audio.py, deletes the folders of
  gone or long-deleted nodes, the speech of gone references and profiles,
  and superseded speech of live rows (#66), never a file a row points at,
  a live node's recording, a node within the grace period, a hidden row,
  a recently changed folder, or anything outside the audio root.
* Editing away speech marks its folder changed, so the sweep waits a day.
* The staging seed's --force deletes the nodes' audio with them.

The reference delete route is covered in test_reference_log.py.
sqlite in-memory, ENCRYPTION_DISABLED, a temporary audio root.
"""
import itertools
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
    ExternalItem, Node, TTSChunk, User, UserDataPurgeHidden, UserProfile,
)
from backend.utils import audio_storage, audio_sweep  # noqa: E402

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


def _later(days=2):
    return time.time() + days * DAY


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


_ext_ids = itertools.count(1)


def _item(user, **kw):
    item = ExternalItem(user_id=user.id, source="web_clip", url="https://e.x",
                        external_id=f"ext-{next(_ext_ids)}", **kw)
    item.set_content("a reference")
    _db.session.add(item)
    _db.session.commit()
    return item


def _profile(user, **kw):
    p = UserProfile(user_id=user.id, generated_by="test", **kw)
    p.set_content("a profile")
    _db.session.add(p)
    _db.session.commit()
    return p


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


def _live_files(root, node):
    """_node_files for a live node, with its rows pointing at its
    upload and speech (so none of it is superseded)."""
    files = _node_files(root, node)
    node.audio_original_url = _media(files[0], root)
    node.audio_tts_url = _media(files[1], root)
    _db.session.add(TTSChunk(node_id=node.id, chunk_index=0,
                             status="completed",
                             audio_url=_media(files[2], root)))
    _db.session.commit()
    return files


def _cleanup(now=None):
    from backend.tasks.node_cleanup import finalize_deleted_nodes
    return finalize_deleted_nodes(now=now)


def _media(path, root):
    """The stored URL of a file (URLs leave out .enc, and carry ?v=)."""
    rel = path.relative_to(root).as_posix()
    return "/media/" + rel[:-4 if rel.endswith(".enc") else None] + "?v=7"


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
    kept = (_live_files(root, live) + _node_files(root, recent)
            + _node_files(root, hidden))
    doomed = _node_files(root, gone)

    _cleanup(now=_later())

    assert all(f.exists() for f in kept)
    assert not any(f.exists() for f in doomed)
    for n in (live, recent, hidden):
        assert _db.session.get(Node, n.id) is not None


@pytest.mark.parametrize("wipe", [False, True], ids=["purge", "wipe"])
def test_a_stuck_file_never_holds_the_rows_back_and_a_later_run_deletes_it(
        app, root, users, monkeypatch, wipe):
    alice, bob, _ = users
    stuck = _node(alice, deleted_days=32)
    if wipe:
        _node(bob, parent=stuck)               # keeps stuck as a tombstone
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
    assert result["errors"] == 0
    left = _db.session.get(Node, stuck.id)
    if wipe:
        assert result["wiped"] == 1 and left.content is None
    else:
        assert left is None                     # purged all the same
    assert _db.session.get(Node, other.id) is None
    assert not any(f.exists() for f in other_files)
    assert stuck_files[1].exists()

    # The next daily run, a day and more later: the sweep deletes it.
    monkeypatch.setattr(os, "unlink", real_unlink)
    result = _cleanup(now=_later())
    assert result["orphan_files_deleted"] >= 1
    assert result["orphan_files_failed"] == 0
    assert not any(f.exists() for f in stuck_files)


def test_the_daily_run_sweeps_what_earlier_runs_left(app, root, users):
    alice = users[0]
    live = _node(alice)
    in_grace = _node(alice, deleted_days=10)
    kept = _live_files(root, live) + _node_files(root, in_grace)
    left = [_file(root / f"user/{alice.id}/node/90001/tts.mp3.enc"),
            _file(root / f"nodes/{alice.id}/90001/batch_0000.webm.enc")]

    fresh = _cleanup()                         # the files are new: kept
    assert fresh["orphan_files_deleted"] == 0
    assert all(f.exists() for f in kept + left)

    result = _cleanup(now=_later())
    assert result["orphan_folders_deleted"] == 2
    assert not any(f.exists() for f in left)
    assert all(f.exists() for f in kept)


def test_a_sweep_error_never_stops_the_node_cleanup(app, root, users,
                                                    monkeypatch):
    alice = users[0]
    n = _node(alice, deleted_days=31)

    def boom(**kw):
        raise RuntimeError("disk")
    monkeypatch.setattr(audio_sweep, "sweep", boom)

    result = _cleanup()

    assert result["orphan_sweep_failed"] is True
    assert result["purged"] == 1
    assert _db.session.get(Node, n.id) is None


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

    _cleanup(now=_later())

    assert target.exists()
    assert linked_target.exists()
    assert not os.path.lexists(root / f"nodes/{alice.id}/{n.id}/link.webm")
    assert _db.session.get(Node, n.id) is None


def test_delete_helpers_never_touch_a_path_outside_the_root(root, tmp_path):
    outside = _file(tmp_path / "not-audio" / "keep.txt")
    folders = audio_storage.delete_folders([outside.parent], root)
    files = audio_storage.delete_files([outside], root)
    assert outside.exists()
    assert folders.skipped == 1 and folders.files == 0
    assert files.skipped == 1 and files.files == 0


def test_node_audio_dirs_refuse_a_bad_session_id(root):
    n = Node(id=7, user_id=3, human_owner_id=3,
             streaming_session_id="../../etc")
    dirs = audio_storage.node_audio_dirs(n, root)
    assert all(str(d).startswith(str(root) + os.sep) for d in dirs)
    assert len(dirs) == 2


# ── Superseded speech (#66) ─────────────────────────────────────────────

def _speech(root, folder, n_chunks):
    merged = _file(root / folder / "tts.mp3.enc")
    chunks = [_file(root / folder / f"tts_chunk_{i}.mp3.enc")
              for i in range(n_chunks)]
    return merged, chunks


def test_superseded_speech_of_a_live_node_goes_and_the_rest_stays(
        app, root, users):
    alice = users[0]
    n = _node(alice)
    folder = f"user/{alice.id}/node/{n.id}"
    merged, chunks = _speech(root, folder, 5)
    original = _file(root / folder / "original.webm.enc")
    stray_recording = _file(root / folder / "original.m4a")  # not speech
    plain_twin = _file(root / folder / "tts_chunk_9.mp3")
    # The current speech: merged + 3 chunks (a regeneration made fewer).
    n.audio_tts_url = _media(merged, root)
    n.audio_original_url = _media(original, root)
    for i in range(3):
        _db.session.add(TTSChunk(node_id=n.id, chunk_index=i,
                                 status="completed",
                                 audio_url=_media(chunks[i], root)))
    _db.session.commit()

    result = audio_sweep.sweep(apply=True, now=_later())

    assert all(p.exists() for p in [merged, original, stray_recording]
               + chunks[:3])
    assert not chunks[3].exists() and not chunks[4].exists()
    assert not plain_twin.exists()
    assert result["done"][1] == 3


def test_a_live_nodes_recording_folder_is_never_superseded(app, root, users):
    alice = users[0]
    n = _node(alice)
    chunks = [_file(root / f"nodes/{alice.id}/{n.id}/batch_0000.webm.enc"),
              _file(root / f"nodes/{alice.id}/{n.id}/tts_chunk_0.mp3")]
    audio_sweep.sweep(apply=True, now=_later())
    assert all(p.exists() for p in chunks)


def test_clearing_speech_marks_its_folder_so_the_sweep_waits_a_day(
        app, root, users):
    alice = users[0]
    n = _node(alice)
    folder = root / f"user/{alice.id}/node/{n.id}"
    merged, chunks = _speech(root, f"user/{alice.id}/node/{n.id}", 2)
    n.audio_tts_url = _media(merged, root)
    for i, c in enumerate(chunks):
        _db.session.add(TTSChunk(node_id=n.id, chunk_index=i,
                                 status="completed", audio_url=_media(c, root)))
    _db.session.commit()
    old = time.time() - 10 * DAY
    for p in [merged, *chunks, folder]:
        os.utime(p, (old, old))

    audio_storage.clear_tts_artifacts(n)       # the edit: "regenerate"
    _db.session.commit()

    assert folder.stat().st_mtime > time.time() - 60
    # A playback started before the edit keeps its files for a day...
    audio_sweep.sweep(apply=True, now=time.time() + 3600)
    assert merged.exists() and all(c.exists() for c in chunks)
    # ...and then they go.
    audio_sweep.sweep(apply=True, now=_later())
    assert not merged.exists() and not any(c.exists() for c in chunks)


def test_profile_versions_share_speech_files(app, root, users):
    alice = users[0]
    a = _profile(alice)
    b = _profile(alice)
    folder = f"user/{alice.id}/profile/{a.id}"
    merged, chunks = _speech(root, folder, 2)
    # Version b plays a's files (routes/profile.py _copy_audio); a's own
    # speech was cleared.
    b.audio_tts_url = _media(merged, root)
    _db.session.add(TTSChunk(profile_id=b.id, chunk_index=0,
                             status="completed",
                             audio_url=_media(chunks[0], root)))
    _db.session.commit()

    audio_sweep.sweep(apply=True, now=_later())

    assert merged.exists() and chunks[0].exists()
    assert not chunks[1].exists()


def test_a_gone_references_speech_goes_unless_a_merged_copy_plays_it(
        app, root, users):
    alice = users[0]
    keep = _item(alice)
    gone_ids = (90011, 90012)
    shared = _file(root / f"user/{alice.id}/item/{gone_ids[0]}/tts.mp3.enc")
    unused = _file(root / f"user/{alice.id}/item/{gone_ids[0]}/"
                   "tts_chunk_1.mp3.enc")
    whole = _file(root / f"user/{alice.id}/item/{gone_ids[1]}/tts.mp3.enc")
    # The kept copy plays the merged copy's merged file
    # (scripts/backfill_read_picks.py).
    keep.audio_tts_url = _media(shared, root)
    _db.session.commit()

    audio_sweep.sweep(apply=True, now=_later())

    assert shared.exists()
    assert not unused.exists()
    assert not whole.exists() and not whole.parent.exists()


def test_a_hidden_references_speech_stays(app, root, users):
    alice = users[0]
    item = _item(alice)
    _db.session.add(UserDataPurgeHidden(
        job_id=1, kind=ExternalItem.__tablename__, row_id=item.id))
    _db.session.commit()
    stale = _file(root / f"user/{alice.id}/item/{item.id}/tts_chunk_3.mp3.enc")
    audio_sweep.sweep(apply=True, now=_later())
    assert stale.exists()


# ── The script ──────────────────────────────────────────────────────────

def _script():
    from backend.scripts import purge_orphaned_node_audio as script
    return script


def _layout(root, users, tmp_path):
    """Every category the sweep tells apart, and files it must never
    touch. Returns (doomed, kept) file lists."""
    alice, bob, model = users
    live = _node(alice)
    in_grace = _node(alice, deleted_days=10)
    past = _node(bob, deleted_days=40)
    hidden = _node(alice, deleted_days=40)
    _db.session.add(UserDataPurgeHidden(job_id=1, kind="node",
                                        row_id=hidden.id))
    reply = _node(model, owner=alice)
    item = _item(alice)
    profile = _profile(alice)
    _db.session.commit()
    gone_ids = (90001, 90002)

    doomed = _node_files(root, past)
    for gid in gone_ids:
        doomed += [_file(root / f"user/{alice.id}/node/{gid}/tts.mp3.enc"),
                   _file(root / f"nodes/{alice.id}/{gid}/batch_0000.webm")]
    # A gone reference's and a gone profile's speech.
    doomed += [_file(root / f"user/{alice.id}/item/90003/tts.mp3.enc"),
               _file(root / f"user/{alice.id}/profile/90004/tts.mp3.enc")]
    # Live rows: their current speech stays, superseded chunks go.
    item_speech = _file(root / f"user/{alice.id}/item/{item.id}/tts.mp3.enc")
    item.audio_tts_url = _media(item_speech, root)
    profile_speech = _file(
        root / f"user/{alice.id}/profile/{profile.id}/tts.mp3.enc")
    profile.audio_tts_url = _media(profile_speech, root)
    _db.session.commit()
    doomed += [
        _file(root / f"user/{alice.id}/item/{item.id}/tts_chunk_2.mp3.enc"),
        _file(root / f"user/{alice.id}/profile/{profile.id}/tts_chunk_2.mp3"),
    ]
    kept = [item_speech, profile_speech,
            _file(root / f"user/{alice.id}/node/{live.id}/original.webm.enc"),
            _file(root / f"nodes/{alice.id}/{live.id}/batch_0000.webm.enc")]
    kept += (_node_files(root, in_grace) + _node_files(root, hidden))
    reply_speech = _file(root / f"user/{model.id}/node/{reply.id}/tts.mp3")
    reply.audio_tts_url = _media(reply_speech, root)
    _db.session.commit()
    kept.append(reply_speech)
    # Not row folders: never looked at.
    kept += [
        _file(root / f"drafts/{alice.id}/sess-9/chunk_0000.webm.enc"),
        _file(root / f"streaming/{alice.id}/sess-8/chunk_0000.webm"),
        _file(root / f"chunks/{alice.id}/up-1/chunk_0000"),
        _file(root / f"user/{alice.id}/node/notanid/x.webm"),
        _file(root / f"user/{alice.id}/other/1/tts.mp3"),
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
    result = _script().run(apply=False, now=_later(), out=lines.append)
    assert all(f.exists() for f in doomed + kept)
    # past: 2 folders; 2 gone node ids x 2; gone item, gone profile;
    # superseded speech of the live item and the live profile.
    assert result["planned"][0] == 10
    assert result["planned"][1] == len(doomed)
    text = "\n".join(lines)
    assert "would delete" in text and "row gone" in text
    assert "90001" not in text           # ids only with --verbose


def test_script_apply_deletes_exactly_the_orphan_categories(
        app, root, users, tmp_path):
    doomed, kept = _layout(root, users, tmp_path)
    script = _script()
    later = _later()
    lines = []
    result = script.run(apply=True, verbose=True, now=later, out=lines.append)

    assert not any(f.exists() for f in doomed)
    assert all(f.exists() for f in kept)
    assert result["failed"] == 0
    assert result["done"][1] == len(doomed)
    assert any("node 90001" in line for line in lines)
    # Nothing left to delete.
    again = script.run(apply=False, now=later, out=lambda s: None)
    assert again["planned"][0] == 0


def test_script_leaves_recently_changed_folders_for_a_later_run(
        app, root, users, tmp_path):
    doomed, kept = _layout(root, users, tmp_path)
    lines = []
    result = _script().run(apply=True, out=lines.append)   # files are new
    assert result["planned"][0] == 0
    assert all(f.exists() for f in doomed + kept)
    assert any("changed in the last 24 h" in line for line in lines)


# ── The staging seed's --force ──────────────────────────────────────────

def test_seed_force_deletes_the_nodes_audio_with_them(app, root, users):
    from backend.scripts.seed_staging_from_fixture import _delete_owner_nodes
    alice, bob, model = users
    mine = _node(alice)
    reply = _node(model, parent=mine, owner=alice)
    theirs = _node(bob)
    doomed = _node_files(root, mine) + _node_files(root, reply, author=model.id)
    kept = _node_files(root, theirs)

    assert _delete_owner_nodes(alice) == 2

    assert not any(f.exists() for f in doomed)
    assert all(f.exists() for f in kept)

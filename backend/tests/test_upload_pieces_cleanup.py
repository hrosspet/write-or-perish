"""The nightly audio sweep and the pieces of an audio file's upload
(``chunks/<uid>/<upload>/``).

- An upload that stopped arriving: its pieces stay while the server would
  still take a piece (24 hours) and for a day after, then the folder goes
  and the entry, still waiting for its file, ends ``failed`` with the
  words the upload routes answer for such an upload.
- An upload that still takes pieces keeps its folder whatever its age,
  and goes on to its finalize after a sweep.
- The folder of an entry that is deleted or gone, pieces a finished
  upload left behind, and a folder without readable metadata go by the
  same age.
- A dry run changes nothing; a symlink is never followed; another
  account's entry named in the metadata is never changed.
"""
import json
import os
import time
from datetime import datetime, timedelta

from backend.tests.test_recording_limits import (  # noqa: F401
    app, tasks, alice, client, _init_upload, _send_piece, _finalize_upload,
    limits, Node, _db, User)
from backend.utils import audio_sweep

PIECE = b"audio-bytes" * 1000
HOUR = 3600


def _start(client, upload_id="up-1", pieces=2, sent=1):
    resp = _init_upload(client, len(PIECE) * pieces, pieces,
                        upload_id=upload_id)
    assert resp.status_code == 201, resp.get_data(as_text=True)
    node_id = resp.get_json()["node_id"]
    for i in range(sent):
        assert _send_piece(client, node_id, i, PIECE,
                           upload_id=upload_id).status_code == 200
    return node_id


def _folder(app, user, upload_id="up-1"):
    return app.media_root / f"chunks/{user.id}/{upload_id}"


def _sweep(app, hours, apply=True, **kw):
    """The sweep as it would run *hours* from now: the folders are that
    old then."""
    return audio_sweep.sweep(apply=apply, root=app.media_root,
                             now=time.time() + hours * HOUR, **kw)


def _node(node_id):
    _db.session.expire_all()
    return _db.session.get(Node, node_id)


def _set_activity(node_id, hours_from_now):
    Node.query.filter(Node.id == node_id).update(
        {Node.upload_activity_at:
         datetime.utcnow() + timedelta(hours=hours_from_now)})
    _db.session.commit()


def _categories(result):
    return {k: v[0] for k, v in result["categories"].items()
            if k.startswith("chunks/")}


def test_pieces_of_an_upload_that_stopped_arriving_go_after_two_days(
        app, client, alice):
    node_id = _start(client)
    folder = _folder(app, alice)

    # 30 hours on: the server takes no more pieces, the folder is kept.
    result = _sweep(app, 30)
    assert (folder / "chunk_0000").read_bytes() == PIECE
    assert _categories(result) == {
        "chunks/<uid>/<upload>: upload no longer takes pieces, "
        "changed in the last 48 h (kept for a later run)": 1}
    assert _node(node_id).transcription_status == "pending"

    # 49 hours on: the folder goes, the entry says why.
    result = _sweep(app, 49, measure_kept=False)
    assert not folder.exists()
    assert _categories(result) == {
        "chunks/<uid>/<upload>: upload no longer takes pieces": 1}
    assert result["done"][0] == 1 and result["failed"] == 0
    node = _node(node_id)
    assert node.transcription_status == "failed"
    assert node.transcription_error == limits.UPLOAD_EXPIRED_TEXT
    assert node.deleted_at is None


def test_the_age_is_twice_the_time_the_server_takes_pieces():
    assert audio_sweep.STAGED_UPLOAD_SECONDS == 48 * HOUR
    assert (audio_sweep.STAGED_UPLOAD_SECONDS
            == 2 * limits.UPLOAD_COUNTS_FOR.total_seconds())


def test_an_upload_that_still_takes_pieces_keeps_its_folder_at_any_age(
        app, client, alice):
    node_id = _start(client)
    # The sweep runs 50 hours from now; the entry's last activity is 10
    # hours before that.
    _set_activity(node_id, 40)

    result = _sweep(app, 50)

    assert (_folder(app, alice) / "chunk_0000").read_bytes() == PIECE
    assert _categories(result) == {
        "chunks/<uid>/<upload>: upload still takes pieces (kept)": 1}
    assert _node(node_id).transcription_status == "pending"


def test_an_upload_in_progress_goes_on_to_its_finalize_after_a_sweep(
        app, client, alice):
    node_id = _start(client)

    _sweep(app, 0)
    _sweep(app, 23)

    assert _send_piece(client, node_id, 1, PIECE).status_code == 200
    resp = _finalize_upload(client, node_id)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert _node(node_id).audio_original_url is not None


def test_the_folder_of_a_deleted_or_gone_entry_goes_by_the_same_age(
        app, client, alice):
    deleted = _start(client, "up-deleted")
    gone = _start(client, "up-gone")
    _node(deleted).deleted_at = datetime.utcnow()
    _db.session.delete(_node(gone))
    _db.session.commit()

    _sweep(app, 47)
    assert _folder(app, alice, "up-deleted").exists()
    assert _folder(app, alice, "up-gone").exists()

    _sweep(app, 49)
    assert not _folder(app, alice, "up-deleted").exists()
    assert not _folder(app, alice, "up-gone").exists()


def test_pieces_left_by_a_finished_upload_go_and_its_entry_stays_as_it_is(
        app, client, alice):
    node_id = _start(client, pieces=1)
    assert _finalize_upload(client, node_id).status_code == 200
    before = _node(node_id)
    status, url = before.transcription_status, before.audio_original_url
    # What a finalize leaves when it cannot remove the pieces.
    left = _folder(app, alice, "up-1.finalizing")
    left.mkdir(parents=True)
    (left / "chunk_0000").write_bytes(PIECE)
    (left / "metadata.json").write_text(json.dumps({"node_id": node_id}))

    _sweep(app, 47)
    assert left.exists()
    _sweep(app, 49)

    assert not left.exists()
    after = _node(node_id)
    assert (after.transcription_status, after.audio_original_url) == (
        status, url)
    stored = app.media_root / f"user/{alice.id}/node/{node_id}"
    assert list(stored.glob("original.*"))


def test_a_folder_without_readable_metadata_goes_by_its_age(app, alice):
    base = app.media_root / f"chunks/{alice.id}"
    for name, metadata in (("no-metadata", None), ("not-json", "{"),
                           ("a-list", "[1]"), ("no-entry", "{}"),
                           ("huge", '{"node_id": 1, "pad": "%s"}'
                            % ("x" * audio_sweep.UPLOAD_METADATA_MAX_BYTES))):
        (base / name).mkdir(parents=True)
        (base / name / "chunk_0000").write_bytes(PIECE)
        if metadata is not None:
            (base / name / "metadata.json").write_text(metadata)

    _sweep(app, 47)
    assert len(list(base.iterdir())) == 5
    _sweep(app, 49)
    assert list(base.iterdir()) == []


def test_a_dry_run_changes_nothing(app, client, alice):
    node_id = _start(client)

    result = _sweep(app, 49, apply=False)

    assert result["planned"][0] == 1 and result["done"][0] == 0
    assert (_folder(app, alice) / "chunk_0000").read_bytes() == PIECE
    assert _node(node_id).transcription_status == "pending"


def test_symlinks_are_never_followed(app, client, alice, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (outside / "chunk_0000").write_bytes(PIECE)
    base = app.media_root / f"chunks/{alice.id}"
    base.mkdir(parents=True)
    os.symlink(outside, base / "linked")
    # A folder of its own whose metadata is a link to another upload's.
    node_id = _start(client, "up-real")
    _set_activity(node_id, 40)        # still takes pieces 50 hours on
    (base / "borrowed").mkdir()
    (base / "borrowed" / "chunk_0000").write_bytes(PIECE)
    os.symlink(base / "up-real" / "metadata.json",
               base / "borrowed" / "metadata.json")

    result = _sweep(app, 50)

    assert (outside / "chunk_0000").read_bytes() == PIECE
    assert (base / "linked").is_symlink()
    assert _categories(result)[
        "chunks/<uid>/<upload>: symlink (left alone)"] == 1
    # The linked metadata is not read: the folder goes by its age, and the
    # upload it pointed at is untouched.
    assert not (base / "borrowed").exists()
    assert (base / "up-real" / "chunk_0000").read_bytes() == PIECE
    assert _node(node_id).transcription_status == "pending"


def test_metadata_naming_another_accounts_entry_changes_no_entry(
        app, client, alice):
    node_id = _start(client)
    other = User(username="bob", twitter_id="bob-id")
    _db.session.add(other)
    _db.session.commit()
    theirs = app.media_root / f"chunks/{other.id}/up-9"
    theirs.mkdir(parents=True)
    (theirs / "chunk_0000").write_bytes(PIECE)
    (theirs / "metadata.json").write_text(json.dumps({"node_id": node_id}))
    _set_activity(node_id, 40)        # alice's upload still takes pieces

    _sweep(app, 50)

    assert not theirs.exists()
    assert (_folder(app, alice) / "chunk_0000").read_bytes() == PIECE
    assert _node(node_id).transcription_status == "pending"


def test_other_names_in_the_chunks_folder_are_left_alone(app, alice):
    base = app.media_root / f"chunks/{alice.id}"
    (base / "has.dot").mkdir(parents=True)
    (base / "has.dot" / "file").write_bytes(PIECE)
    (base / "a-file").write_bytes(PIECE)

    _sweep(app, 24 * 40)

    assert (base / "has.dot" / "file").exists() and (base / "a-file").exists()

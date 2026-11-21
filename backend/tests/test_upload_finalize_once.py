"""POST /api/nodes/upload/finalize finishes an upload once: the request
that moves the upload's pieces folder aside goes on, and another finalize
of the same upload (sent twice at once, or again while the first runs)
gets the 404 a finalize sent after the upload finished gets. A finalize
that fails after that step puts the pieces back, so the upload can be
finalized again.

The file is assembled and measured under a name of its own and reaches
original.<ext> once encrypted."""
import os

import pytest

from backend.tests.test_recording_limits import (  # noqa: F401
    app, tasks, alice, client, _init_upload, _send_piece, _finalize_upload,
    limits, Node)

DATA = b"x" * 3000


def _same_account_client(app, alice):
    other = app.test_client()
    with other.session_transaction() as sess:
        sess["_user_id"] = str(alice.id)
        sess["_fresh"] = True
    return other


@pytest.fixture
def upload(client, monkeypatch, tmp_path):
    import backend.routes.nodes as nodes_routes
    monkeypatch.setattr(nodes_routes, "get_openai_chat_key",
                        lambda cfg: "key")
    resp = _init_upload(client, len(DATA), 1)
    assert resp.status_code == 201, resp.get_data(as_text=True)
    node_id = resp.get_json()["node_id"]
    assert _send_piece(client, node_id, 0, DATA).status_code == 200
    return node_id


def _pieces_dir(tmp_path, alice):
    return tmp_path / "chunks" / str(alice.id) / "up-1"


def test_finalize_sent_again_while_the_first_measures_is_refused(
        app, client, alice, tasks, upload, monkeypatch, tmp_path):
    second = {}
    measured = []

    def measuring(path):
        measured.append(os.path.basename(path))
        if len(measured) == 1:
            second["resp"] = _finalize_upload(
                _same_account_client(app, alice), upload)
        return 60.0

    monkeypatch.setattr(limits, "finalized_upload_seconds", measuring)
    first = _finalize_upload(client, upload)
    assert first.status_code == 200
    assert second["resp"].status_code == 404
    assert measured[0].startswith("original-upload-")
    assert tasks.transcribe_audio.delay.call_count == 1
    assert not _pieces_dir(tmp_path, alice).exists()
    assert not _pieces_dir(tmp_path, alice).with_name(
        "up-1.finalizing").exists()
    node = Node.query.get(upload)
    assert node.audio_original_url.endswith("/original.m4a")
    folder = tmp_path / f"user/{alice.id}/node/{upload}"
    assert sorted(p.name for p in folder.iterdir()) == ["original.m4a"]
    assert (folder / "original.m4a").read_bytes() == DATA


def test_finalize_sent_again_after_the_file_is_stored_is_refused(
        app, client, alice, tasks, upload, monkeypatch):
    from backend.utils import encryption
    real = encryption.encrypt_file_atomically
    second = {}

    def store_then_send_again(src, dest):
        out = real(src, dest)
        second["resp"] = _finalize_upload(
            _same_account_client(app, alice), upload)
        return out

    monkeypatch.setattr(encryption, "encrypt_file_atomically",
                        store_then_send_again)
    monkeypatch.setattr(limits, "finalized_upload_seconds", lambda p: 60.0)
    assert _finalize_upload(client, upload).status_code == 200
    assert second["resp"].status_code == 404
    assert tasks.transcribe_audio.delay.call_count == 1


def test_finalize_that_fails_puts_the_pieces_back(
        app, client, alice, tasks, upload, monkeypatch, tmp_path):
    from backend.utils import encryption
    real = encryption.encrypt_file_atomically
    calls = []

    def fails_once(src, dest):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("disk full")
        return real(src, dest)

    monkeypatch.setattr(encryption, "encrypt_file_atomically", fails_once)
    monkeypatch.setattr(limits, "finalized_upload_seconds", lambda p: 60.0)
    with pytest.raises(OSError):
        _finalize_upload(client, upload)
    assert _pieces_dir(tmp_path, alice).is_dir()
    folder = tmp_path / f"user/{alice.id}/node/{upload}"
    assert list(folder.iterdir()) == []
    assert tasks.transcribe_audio.delay.call_count == 0

    assert _finalize_upload(client, upload).status_code == 200
    assert tasks.transcribe_audio.delay.call_count == 1


def test_finalize_after_the_upload_finished_is_refused_as_before(
        client, tasks, upload, monkeypatch):
    monkeypatch.setattr(limits, "finalized_upload_seconds", lambda p: 60.0)
    assert _finalize_upload(client, upload).status_code == 200
    again = _finalize_upload(client, upload)
    assert again.status_code == 404
    assert tasks.transcribe_audio.delay.call_count == 1

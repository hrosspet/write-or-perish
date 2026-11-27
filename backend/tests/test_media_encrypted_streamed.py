"""An encrypted media file (routes/media.serve_media) is decrypted into an
unnamed temporary file and streamed from it: the response does not hold
the file in memory, ranges are answered from the file (206 with
Content-Range), no decrypted copy has a name on disk, and at most
MEDIA_DECRYPTS_PER_WORKER decryptions run at once (a request that waits
longer than MEDIA_DECRYPT_WAIT_SECONDS gets 503).

Access is not the subject (tests/test_route_access_checks.py covers it),
so the access rule is stubbed to "private"; KMS is a local stand-in."""
import gc
import os
import tracemalloc

import pytest
from flask import Flask

SIZE = 8 * 1024 * 1024
URL = "/media/user/1/node/7/original.mp3"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    from backend.routes import media
    from backend.utils import encryption

    monkeypatch.delenv("ENCRYPTION_DISABLED", raising=False)
    monkeypatch.setenv("GCP_KMS_KEY_NAME", "test/key")
    monkeypatch.setattr(encryption, "_wrap_dek", lambda dek: b"W" + dek)
    monkeypatch.setattr(encryption, "_unwrap_dek", lambda w: w[1:])

    folder = tmp_path / "media" / "user" / "1" / "node" / "7"
    folder.mkdir(parents=True)
    body = os.urandom(1024) * (SIZE // 1024)
    (folder / "original.mp3").write_bytes(body)
    encryption.encrypt_file(str(folder / "original.mp3"))

    monkeypatch.setattr(media, "MEDIA_ROOT", (tmp_path / "media").resolve())
    monkeypatch.setattr(media, "_cache_control_for",
                        lambda rel: media.PRIVATE_CACHE_CONTROL)
    # Every temporary file is made here.
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(scratch))

    app = Flask(__name__)
    app.register_blueprint(media.media_bp, url_prefix="/media")
    app.media, app.body, app.scratch = media, body, scratch
    return app


def test_whole_file_is_streamed_from_disk(setup):
    app = setup
    with app.test_request_context(URL):
        resp = app.media.serve_media("user/1/node/7/original.mp3")
    assert resp.status_code == 200
    assert int(resp.headers["Content-Length"]) == SIZE
    assert resp.headers["Accept-Ranges"] == "bytes"
    assert resp.headers["Cache-Control"].startswith("private")
    assert not isinstance(resp.response, (bytes, list))
    assert b"".join(resp.response) == app.body
    resp.close()


def test_response_does_not_hold_the_file_in_memory(setup):
    app = setup
    with app.test_request_context(URL):
        gc.collect()
        tracemalloc.start()
        try:
            base = tracemalloc.get_traced_memory()[0]
            resp = app.media.serve_media("user/1/node/7/original.mp3")
            gc.collect()
            held = tracemalloc.get_traced_memory()[0] - base
        finally:
            tracemalloc.stop()
    assert resp.status_code == 200
    assert held < SIZE / 8, held
    resp.close()


def test_range_is_answered_from_the_copy(setup):
    app = setup
    client = app.test_client()
    resp = client.get(URL, headers={"Range": "bytes=1000-1009"})
    assert resp.status_code == 206
    assert resp.headers["Content-Range"] == f"bytes 1000-1009/{SIZE}"
    assert resp.headers["Content-Type"] == "audio/mpeg"
    assert resp.data == app.body[1000:1010]
    resp.close()

    resp = client.get(URL, headers={"Range": "bytes=-4"})
    assert resp.status_code == 206
    assert resp.data == app.body[-4:]
    resp.close()


def test_no_decrypted_copy_has_a_name_on_disk(setup):
    app = setup
    resp = app.test_client().get(URL, headers={"Range": "bytes=0-1"})
    assert resp.status_code == 206
    assert os.listdir(app.scratch) == []
    resp.close()
    assert os.listdir(app.scratch) == []


def test_request_waits_for_a_decryption_slot_then_gets_503(
        setup, monkeypatch):
    app = setup
    media = app.media
    monkeypatch.setattr(media, "MEDIA_DECRYPT_WAIT_SECONDS", 0.05)
    taken = 0
    while media._decrypt_slots.acquire(blocking=False):
        taken += 1
    try:
        assert taken == media.MEDIA_DECRYPTS_PER_WORKER
        resp = app.test_client().get(URL)
        assert resp.status_code == 503
    finally:
        for _ in range(taken):
            media._decrypt_slots.release()
    resp = app.test_client().get(URL, headers={"Range": "bytes=0-1"})
    assert resp.status_code == 206
    resp.close()

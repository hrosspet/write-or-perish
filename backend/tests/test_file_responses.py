"""utils/file_responses.send_temporary_file: the file's name is gone once
the response is made, its bytes are sent in full or by range, and the
close callback runs when the response is closed."""
import os

import pytest
from flask import Flask


@pytest.fixture
def app(tmp_path):
    from backend.utils.file_responses import send_temporary_file
    app = Flask(__name__)
    closed = []
    body = bytes(range(256)) * 40

    @app.route("/f")
    def serve():
        path = tmp_path / "copy.bin"
        path.write_bytes(body)
        resp = send_temporary_file(
            str(path), on_close=lambda: closed.append(True),
            mimetype="audio/mpeg", conditional=True, etag=False)
        app.left_on_disk = path.exists()
        return resp

    app.closed, app.body = closed, body
    return app


def test_whole_file_is_sent_and_its_name_is_gone(app):
    resp = app.test_client().get("/f")
    assert resp.status_code == 200
    assert resp.data == app.body
    assert app.left_on_disk is False
    resp.close()
    assert app.closed == [True]


def test_range_is_answered_from_the_open_file(app):
    resp = app.test_client().get("/f", headers={"Range": "bytes=10-19"})
    assert resp.status_code == 206
    assert resp.headers["Content-Range"] == f"bytes 10-19/{len(app.body)}"
    assert resp.data == app.body[10:20]
    resp.close()
    assert app.closed == [True]


def test_range_outside_the_file_removes_it_too(app, tmp_path):
    resp = app.test_client().get("/f", headers={"Range": "bytes=99999-"})
    assert resp.status_code == 416
    assert not os.path.exists(tmp_path / "copy.bin")

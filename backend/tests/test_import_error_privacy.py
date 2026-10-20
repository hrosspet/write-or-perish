"""Import error handlers must not log or return raw exception text.

A database error's text includes the failing SQL and its parameters,
which can hold imported text. The error log line reaches Sentry, so it
carries the exception type only, and the response carries a plain
message. File names from a zip are not logged either.
"""

import io
import json
import logging

import pytest

# Reuse the app/client fixtures and helpers of the .docx import tests
from backend.tests.test_docx_import import (  # noqa: F401
    app, client, _zip, _analyze, _confirm,
)

MARKER = "SECRET-MARKER"


def _assert_clean(response, caplog, status):
    assert response.status_code == status
    assert MARKER not in response.get_data(as_text=True)
    assert "details" not in (response.get_json() or {})
    assert MARKER not in caplog.text
    for record in caplog.records:
        assert MARKER not in record.getMessage()


def _boom(*args, **kwargs):
    raise RuntimeError(f"INSERT parameters: {MARKER}")


def test_confirm_failure_does_not_leak_exception_text(
        client, monkeypatch, caplog):
    monkeypatch.setattr(
        "backend.routes.import_data._load_source_key_index", _boom)
    caplog.set_level(logging.DEBUG)
    r = _confirm(client, [{"filename_without_ext": "a", "content": "text",
                           "modified_at": "2024-01-01T00:00:00"}])
    _assert_clean(r, caplog, 500)
    assert r.get_json()["error"] == "Failed to import files"
    assert "RuntimeError" in caplog.text


def test_analyze_failure_does_not_leak_exception_text(
        client, monkeypatch, caplog):
    monkeypatch.setattr(
        "backend.routes.import_data.approximate_token_count", _boom)
    caplog.set_level(logging.DEBUG)
    r = _analyze(client, "notes.zip", _zip({"a.md": "hello"}))
    _assert_clean(r, caplog, 500)
    assert r.get_json()["error"] == "Failed to analyze zip file"
    assert "RuntimeError" in caplog.text


def test_non_utf8_zip_entry_name_not_logged(client, caplog):
    caplog.set_level(logging.DEBUG)
    data = _zip({"private-name-SECRET-MARKER.md": b"\xff\xfe\xfa bad",
                 "ok.md": "fine"})
    r = _analyze(client, "notes.zip", data)
    assert r.status_code == 200
    assert [f["name"] for f in r.get_json()["files"]] == ["ok.md"]
    assert MARKER not in caplog.text
    assert "not valid UTF-8" in caplog.text


@pytest.mark.parametrize("kind", ["claude", "chatgpt"])
def test_conversation_file_errors_are_specific_without_details(
        client, kind):
    url = f"/api/import/{kind}/analyze"
    for payload, expected in [
            (b"\xff\xfe\xfa", "not valid UTF-8"),
            (b"{not json " + MARKER.encode(), "not valid JSON")]:
        r = client.post(
            url,
            data={"conversations_file": (io.BytesIO(payload), "c.json")},
            content_type="multipart/form-data")
        assert r.status_code == 400, r.get_data(as_text=True)
        body = r.get_json()
        assert expected in body["error"]
        assert "details" not in body
        assert MARKER not in json.dumps(body)

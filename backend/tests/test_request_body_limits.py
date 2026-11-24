"""Request body sizes (backend/utils/request_size.py).

- A route takes at most DEFAULT_MAX_BODY_BYTES unless it allows more; a
  larger body gets a JSON 413 ``request_too_large`` before it is read:
  the sign-in email and the webhooks with no account, also a body sent
  without a Content-Length.
- The routes the apps send large bodies to take the largest body the apps
  send, and a body just above their limit gets the 413: a whole entry's
  text and its autosaved draft, the documents edited in place, the
  markdown import's confirm, file uploads and imports (200 MB files),
  and the recording and upload chunks, whose own coded 413 the apps act
  on.

Uses create_app() on an in-memory sqlite database; nothing is sent.
"""
import io
import os
import sys
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

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login  # noqa: E402
from backend.extensions import db as _db  # noqa: E402
from backend.models import Draft, User, UserArtifact  # noqa: E402
import backend.models as _real_backend_models  # noqa: E402
from backend.utils import recording_limits  # noqa: E402
from backend.utils import request_size as rs  # noqa: E402

MB = 1024 * 1024


def _affected(k):
    return (k == "flask_login" or k.startswith("backend.routes")
            or k == "backend.models")


@pytest.fixture
def app(monkeypatch):
    saved = {k: sys.modules[k] for k in list(sys.modules) if _affected(k)}
    sys.modules["flask_login"] = _real_flask_login
    sys.modules["backend.models"] = _real_backend_models
    for k in [k for k in list(sys.modules) if k.startswith("backend.routes")]:
        del sys.modules[k]
    import backend.config as _config
    monkeypatch.setattr(_config.Config, "SQLALCHEMY_DATABASE_URI",
                        "sqlite:///:memory:")
    from backend import create_app
    app = create_app()
    app.config["TESTING"] = True
    app.config["GITHUB_WEBHOOK_SECRET"] = "gh-secret"
    app.config["STRIPE_WEBHOOK_SECRET"] = "whsec_test"
    with app.app_context():
        _db.create_all()
        _db.session.add(User(username="writer", email="w@example.com",
                             approved=True))
        _db.session.commit()
        yield app
        _db.session.remove()
        _db.drop_all()
    for k in [k for k in list(sys.modules) if _affected(k)]:
        if k not in saved:
            del sys.modules[k]
    for k, mod in saved.items():
        sys.modules[k] = mod


def _signed_in(app):
    from flask import g
    g.pop("_login_user", None)
    c = app.test_client()
    user = User.query.filter_by(username="writer").first()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return c, user


class _Body(io.RawIOBase):
    """A request body of *size* bytes that is made as it is read: *head*,
    then zero bytes, then *tail*. Seekable to its end, so the test client
    reads its length without holding it."""

    def __init__(self, size, head=b"", tail=b""):
        self.size, self.head, self.tail = size, head, tail
        self.pos = 0

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=0):
        base = {0: 0, 1: self.pos, 2: self.size}[whence]
        self.pos = max(0, min(self.size, base + offset))
        return self.pos

    def readinto(self, buf):
        n = min(len(buf), self.size - self.pos)
        if n <= 0:
            return 0
        start, end = self.pos, self.pos + n
        out = bytearray(n)
        head_end = min(end, len(self.head))
        if start < head_end:
            out[:head_end - start] = self.head[start:head_end]
        tail_start = self.size - len(self.tail)
        if end > tail_start:
            s = max(start, tail_start)
            out[s - start:] = self.tail[s - tail_start:end - tail_start]
        buf[:n] = out
        self.pos = end
        return n


def _multipart_body(field, filename, file_size, extra=()):
    boundary = "----limitsTestBoundary"
    head = b""
    for name, value in extra:
        head += (f"--{boundary}\r\nContent-Disposition: form-data; "
                 f'name="{name}"\r\n\r\n{value}\r\n').encode()
    head += (f"--{boundary}\r\nContent-Disposition: form-data; "
             f'name="{field}"; filename="{filename}"\r\n'
             "Content-Type: application/octet-stream\r\n\r\n").encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    body = _Body(len(head) + file_size + len(tail), head, tail)
    return body, f"multipart/form-data; boundary={boundary}"


def _post_stream(client, path, body, content_type, chunked=False):
    overrides = {}
    if chunked:
        overrides = {"HTTP_TRANSFER_ENCODING": "chunked",
                     "wsgi.input_terminated": True}
    return client.post(path, input_stream=body, content_type=content_type,
                       environ_overrides=overrides)


def _too_large(resp):
    return (resp.status_code == 413
            and resp.get_json().get("code") == "request_too_large")


# ── Routes without a limit of their own ──────────────────────────────────

def test_default_limit_is_set(app):
    assert app.config["MAX_CONTENT_LENGTH"] == rs.DEFAULT_MAX_BODY_BYTES


def test_sign_in_email_with_a_body_over_the_default_gets_413(app):
    body = b'{"email": "' + b"a" * (rs.DEFAULT_MAX_BODY_BYTES) + b'"}'
    r = app.test_client().post("/auth/magic-link/send", data=body,
                               content_type="application/json")
    assert _too_large(r)
    assert r.get_json()["error"] == rs.TOO_LARGE_MESSAGE


def test_sign_in_email_of_normal_size_is_answered_as_before(app):
    r = app.test_client().post("/auth/magic-link/send",
                               json={"email": "not an address"})
    assert r.status_code == 400
    assert "valid email" in r.get_json()["error"]


def test_webhooks_with_a_body_over_the_default_get_413(app):
    c = app.test_client()
    big = b"x" * (rs.DEFAULT_MAX_BODY_BYTES + 1)
    r = c.post("/api/webhooks/github", data=big,
               headers={"X-Hub-Signature-256": "sha256=00"})
    assert _too_large(r)
    r = c.post("/api/webhooks/stripe", data=big,
               headers={"Stripe-Signature": "t=1,v1=00"})
    assert _too_large(r)


def test_webhook_of_normal_size_reaches_the_signature_check(app):
    r = app.test_client().post(
        "/api/webhooks/github", data=b"x" * (rs.DEFAULT_MAX_BODY_BYTES - 1),
        headers={"X-Hub-Signature-256": "sha256=00"})
    assert r.status_code == 403


def test_body_without_content_length_is_read_only_up_to_the_limit(app):
    """Werkzeug reads a body sent without a Content-Length up to the limit
    and no further: the JSON then does not parse (400), a form gets 413."""
    body = _Body(rs.DEFAULT_MAX_BODY_BYTES + 8 * MB,
                 head=b'{"email": "', tail=b'"}')
    r = _post_stream(app.test_client(), "/auth/magic-link/send", body,
                     "application/json", chunked=True)
    assert r.status_code == 400
    assert body.tell() <= rs.DEFAULT_MAX_BODY_BYTES + 64 * 1024

    c, _ = _signed_in(app)
    limit = (recording_limits.MAX_UPLOAD_CHUNK_BYTES
             + recording_limits.FORM_OVERHEAD_BYTES)
    form, ctype = _multipart_body("chunk", "blob", limit + 8 * MB)
    r = _post_stream(c, "/api/nodes/upload/chunk", form, ctype,
                     chunked=True)
    assert _too_large(r)
    assert form.tell() <= limit + 64 * 1024


# ── Long text ────────────────────────────────────────────────────────────

def _text_body(total):
    """A JSON object {"content": "aaa..."} of *total* bytes."""
    head, tail = b'{"content": "', b'"}'
    return head + b"a" * (total - len(head) - len(tail)) + tail


def test_draft_of_the_largest_entry_is_saved(app):
    c, user = _signed_in(app)
    r = c.post("/api/drafts/", data=_text_body(rs.LONG_TEXT_MAX_BODY_BYTES),
               content_type="application/json")
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    assert Draft.query.filter_by(user_id=user.id).count() == 1


def test_draft_just_over_the_long_text_limit_gets_413(app):
    c, user = _signed_in(app)
    r = c.post("/api/drafts/",
               data=_text_body(rs.LONG_TEXT_MAX_BODY_BYTES + 1),
               content_type="application/json")
    assert _too_large(r)
    assert Draft.query.filter_by(user_id=user.id).count() == 0


def test_artifact_over_the_long_text_limit_stores_nothing(app):
    c, user = _signed_in(app)
    r = c.put("/api/artifacts/notes",
              data=_text_body(rs.LONG_TEXT_MAX_BODY_BYTES + 1),
              content_type="application/json")
    assert _too_large(r)
    assert UserArtifact.query.filter_by(user_id=user.id).count() == 0


def test_artifact_of_a_long_document_is_saved(app):
    c, user = _signed_in(app)
    r = c.put("/api/artifacts/notes",
              data=_text_body(rs.DEFAULT_MAX_BODY_BYTES * 3),
              content_type="application/json")
    assert r.status_code == 200, r.get_data(as_text=True)[:300]


def test_new_entry_as_json_takes_long_text_not_the_upload_size(app):
    c, _ = _signed_in(app)
    body = _Body(rs.LONG_TEXT_MAX_BODY_BYTES + 1,
                 head=b'{"content": "', tail=b'"}')
    r = _post_stream(c, "/api/nodes/", body, "application/json")
    assert _too_large(r)


# ── Uploads and imports ──────────────────────────────────────────────────

def test_import_of_a_200_mb_file_is_read_by_the_route(app):
    """The largest file the apps upload (200 MB) reaches the route, which
    answers for the file itself (an old .doc here)."""
    c, _ = _signed_in(app)
    body, ctype = _multipart_body("zip_file", "notes.doc", 200 * MB)
    r = _post_stream(c, "/api/import/analyze", body, ctype)
    assert r.status_code == 400
    assert "doc" in r.get_json()["error"].lower()


def test_import_just_over_the_upload_limit_gets_413(app):
    c, _ = _signed_in(app)
    body, ctype = _multipart_body("zip_file", "notes.zip", 1)
    body.size = rs.UPLOAD_MAX_BODY_BYTES + 1
    r = _post_stream(c, "/api/import/analyze", body, ctype)
    assert _too_large(r)
    assert body.tell() == 0  # refused before any of it was read


def test_audio_file_of_200_mb_reaches_the_route(app, monkeypatch):
    """A new entry from an audio file: the multipart form may hold the
    largest audio file (MAX_AUDIO_BYTES)."""
    from backend.routes import nodes
    c, _ = _signed_in(app)
    body, ctype = _multipart_body(
        "audio_file", "talk.unknown", nodes.MAX_AUDIO_BYTES,
        extra=(("privacy_level", "private"),))
    r = _post_stream(c, "/api/nodes/", body, ctype)
    assert r.status_code == 415  # the route's own answer for the name


def test_upload_and_import_routes_take_the_apps_largest_bodies(app):
    """Every route the apps send a large body to, with the largest body
    the apps send there (a 200 MB file, a recording or upload chunk with
    its form, an entry's text, the confirm's posted-back text)."""
    from flask import request
    from backend.routes import nodes
    chunk = recording_limits.MAX_STREAMING_CHUNK_BYTES
    piece = recording_limits.MAX_UPLOAD_CHUNK_BYTES
    form = "multipart/form-data; boundary=x"
    cases = [
        ("POST", "/api/nodes/", form, nodes.MAX_AUDIO_BYTES + MB // 2),
        ("POST", "/api/nodes/", "application/json",
         rs.LONG_TEXT_MAX_BODY_BYTES),
        ("POST", "/api/nodes/upload/chunk", form,
         piece + recording_limits.FORM_OVERHEAD_BYTES),
        ("POST", "/api/nodes/7/audio-chunk", form,
         chunk + recording_limits.FORM_OVERHEAD_BYTES),
        ("POST", "/api/drafts/streaming/abc/audio-chunk", form,
         chunk + recording_limits.FORM_OVERHEAD_BYTES),
        ("POST", "/api/drafts/", "application/json",
         rs.LONG_TEXT_MAX_BODY_BYTES),
        ("POST", "/api/drafts/streaming/abc/save-as-node",
         "application/json", rs.LONG_TEXT_MAX_BODY_BYTES),
        ("POST", "/api/textmode/start", "application/json",
         rs.LONG_TEXT_MAX_BODY_BYTES),
        ("PUT", "/api/artifacts/notes", "application/json",
         rs.LONG_TEXT_MAX_BODY_BYTES),
        ("PATCH", "/api/artifacts/notes", "application/json",
         rs.LONG_TEXT_MAX_BODY_BYTES),
        ("POST", "/api/profile/", "application/json",
         rs.LONG_TEXT_MAX_BODY_BYTES),
        ("PUT", "/api/profile/3", "application/json",
         rs.LONG_TEXT_MAX_BODY_BYTES),
        ("PUT", "/api/prompts/main", "application/json",
         rs.LONG_TEXT_MAX_BODY_BYTES),
        ("PUT", "/api/todo/", "application/json",
         rs.LONG_TEXT_MAX_BODY_BYTES),
        ("PATCH", "/api/todo/", "application/json",
         rs.LONG_TEXT_MAX_BODY_BYTES),
        ("POST", "/api/share", "application/json",
         rs.LONG_TEXT_MAX_BODY_BYTES),
        ("PATCH", "/api/share/3", "application/json",
         rs.LONG_TEXT_MAX_BODY_BYTES),
        ("POST", "/api/import/confirm", "application/json",
         rs.IMPORT_CONFIRM_MAX_BODY_BYTES),
        ("POST", "/api/import/analyze", form, 200 * MB + MB // 2),
        ("POST", "/api/import/twitter/analyze", form, 200 * MB + MB // 2),
        ("POST", "/api/import/claude/analyze", form, 200 * MB + MB // 2),
        ("POST", "/api/import/chatgpt/analyze", form, 200 * MB + MB // 2),
        ("POST", "/api/external/bookmarks/import", "application/json",
         200 * MB),
        ("POST", "/api/external/bookmarks/import", form, 200 * MB),
    ]
    for method, path, ctype, largest in cases:
        with app.test_request_context(path, method=method,
                                      content_type=ctype):
            assert request.url_rule is not None, path
            assert request.max_content_length >= largest, (method, path)
    # A route the apps send only small bodies to keeps the default.
    with app.test_request_context("/api/nodes/7", method="PUT",
                                  content_type="application/json"):
        assert request.max_content_length == rs.DEFAULT_MAX_BODY_BYTES


def test_chunk_over_its_size_gets_the_routes_own_answer(app):
    """A recording chunk larger than a chunk may be is answered by the
    route (``chunk_too_large``, which the apps act on), not by the body
    limit, also well above that size."""
    c, _ = _signed_in(app)
    size = (recording_limits.MAX_STREAMING_CHUNK_BYTES
            + recording_limits.FORM_OVERHEAD_BYTES + 1)
    for path in ("/api/drafts/streaming/abc/audio-chunk",
                 "/api/nodes/upload/chunk"):
        body, ctype = _multipart_body("chunk", "blob", size)
        r = _post_stream(c, path, body, ctype)
        assert r.status_code == 413, path
        assert r.get_json()["code"] == "chunk_too_large", path

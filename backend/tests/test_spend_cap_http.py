"""The date a spend cap lifts reaches the clients over HTTP (#594).

A user capped into a later month (a carry of twice the limit) gets that
date in every refusal: the 402 of the route decorator, of the multipart
upload, and of the SpendCapExceeded handler (``lifts_on`` and the text),
and the 202 of a text-mode start that saved the entry but skipped the
reply (``spend_cap_lifts_on``). Built on create_app(), so the handler is
the one the app registers.
"""
import os
import sys
from io import BytesIO
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
sys.modules.setdefault("ffmpeg", MagicMock())

import pytest  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login  # noqa: E402
from backend.extensions import db as _db  # noqa: E402
from backend.models import Node, User  # noqa: E402
import backend.models as _real_backend_models  # noqa: E402
from backend.utils.spend import (  # noqa: E402
    SpendCapExceeded, _month_first_day, current_month,
)


def _affected(k):
    return (k == "flask_login" or k.startswith("backend.routes")
            or k == "backend.models")


@pytest.fixture
def app(monkeypatch):
    saved = {k: sys.modules[k] for k in list(sys.modules) if _affected(k)}
    sys.modules["flask_login"] = _real_flask_login
    sys.modules["backend.models"] = _real_backend_models
    for _k in [k for k in list(sys.modules) if k.startswith("backend.routes")]:
        del sys.modules[_k]
    import backend.config as _config
    monkeypatch.setattr(_config.Config, "SQLALCHEMY_DATABASE_URI",
                        "sqlite:///:memory:")
    from backend import create_app
    app = create_app()
    app.config["TESTING"] = True

    def raises_the_cap():
        raise SpendCapExceeded()

    # A route that reaches the app's own SpendCapExceeded handler, as a
    # reply route does when the cap flips after its decorator ran.
    app.add_url_rule("/api/_test/raise-spend-cap", "raise_spend_cap",
                     raises_the_cap, methods=["POST"])
    with app.app_context():
        _db.create_all()
        month = current_month()
        _db.session.add(User(
            username="alice", approved=True, plan="alpha",
            monthly_spend_limit_usd=50.0, spend_carry_month=month,
            spend_carry_microdollars=110_000_000,   # $60 left over after
            spend_blocked_month=month))            # this month: capped next
        _db.session.commit()
        yield app
        _db.session.remove()
        _db.drop_all()
    for k in [k for k in list(sys.modules) if _affected(k)]:
        if k not in saved:
            del sys.modules[k]
    for k, mod in saved.items():
        sys.modules[k] = mod


@pytest.fixture
def client(app):
    user = User.query.filter_by(username="alice").one()
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return c


def _lifts_on():
    return _month_first_day(current_month(), 2)


def _assert_402_names_the_date(resp):
    assert resp.status_code == 402, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["error"] == "monthly_spend_limit_reached"
    lifts = _lifts_on()
    assert body["lifts_on"] == lifts.isoformat()
    assert body["message"].endswith(
        f"It resets on {lifts.strftime('%B')} {lifts.day}.")


def test_the_multipart_upload_402_names_the_date(client):
    resp = client.post("/api/nodes/", data={
        "audio_file": (BytesIO(b"fake audio"), "talk.m4a"),
    }, content_type="multipart/form-data")
    _assert_402_names_the_date(resp)
    assert Node.query.count() == 0


def test_the_decorated_upload_init_402_names_the_date(client):
    _assert_402_names_the_date(client.post("/api/nodes/upload/init", json={
        "filename": "talk.m4a", "filesize": 20 * 1024 * 1024,
        "total_chunks": 4, "upload_id": "up-1",
    }))


def test_the_spend_cap_handler_402_names_the_date(client):
    _assert_402_names_the_date(client.post("/api/_test/raise-spend-cap"))


def test_a_textmode_start_without_reply_names_the_date(client):
    resp = client.post("/api/textmode/start", json={
        "content": "an entry", "auto_generate": True,
    })
    assert resp.status_code == 202, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["spend_capped"] is True
    assert body["spend_cap_lifts_on"] == _lifts_on().isoformat()

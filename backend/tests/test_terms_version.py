"""The Terms version (#440).

frontend/src/legal/terms.json is the one source of the Terms text: the
public /terms page, the acceptance dialog and the iPhone app all show it.
Its "version" must be the version the server asks users to accept, so a
change to the text that bumps the version also asks everyone to accept
again, and a bump without new text cannot happen by accident.

POST /api/terms/accept records the version the client showed, and only
the current one: a page or app build that still shows older Terms (or
sends no version, as iPhone builds up to 0.1.0 (4) do) records nothing
and gets 409.

Same harness as test_log_dashboard_privacy (minimal app, sqlite)."""
import json
import pathlib
from datetime import datetime
from types import SimpleNamespace

import pytest

from backend.tests.test_log_dashboard_privacy import (  # noqa: F401 - fixture
    _db, _login, app,
)
from backend.models import User

ROOT = pathlib.Path(__file__).resolve().parents[2]
TERMS_JSON = ROOT / "frontend" / "src" / "legal" / "terms.json"


@pytest.fixture
def terms_client(app, monkeypatch):
    # Imported after the harness has re-imported the route modules.
    import backend.routes.terms as terms_routes
    monkeypatch.setattr(terms_routes, "send_admin_signup_notification",
                        lambda *a, **k: None)
    app.register_blueprint(terms_routes.terms_bp, url_prefix="/api/terms")
    user = User(username="mia", approved=True,
                accepted_terms_version="2.0",
                accepted_terms_at=datetime(2026, 3, 1))
    _db.session.add(user)
    _db.session.commit()
    client = app.test_client()
    _login(client, user.id)
    return client, user.id, terms_routes.CURRENT_TERMS_VERSION


def _stored(user_id):
    _db.session.expire_all()
    user = _db.session.get(User, user_id)
    return user.accepted_terms_version, user.accepted_terms_at


def test_the_server_asks_for_the_version_the_text_carries():
    from backend.routes.terms import CURRENT_TERMS_VERSION
    doc = json.loads(TERMS_JSON.read_text(encoding="utf-8"))
    assert doc["version"] == CURRENT_TERMS_VERSION
    footnotes = [b["text"] for b in doc["blocks"] if b["type"] == "footnote"]
    assert any(CURRENT_TERMS_VERSION in text for text in footnotes), \
        "the text names its own version"


def test_the_beta_terms_ask_everyone_who_accepted_2_0_again():
    from backend.routes.dashboard import _terms_up_to_date
    from backend.routes.terms import CURRENT_TERMS_VERSION
    assert CURRENT_TERMS_VERSION == "3.0"
    accepted = datetime(2026, 3, 1)
    old = SimpleNamespace(accepted_terms_version="2.0",
                          accepted_terms_at=accepted, deactivated_at=None)
    new = SimpleNamespace(accepted_terms_version="3.0",
                          accepted_terms_at=accepted, deactivated_at=None)
    assert _terms_up_to_date(old) is False
    assert _terms_up_to_date(new) is True


def test_accepting_without_a_version_records_nothing(terms_client):
    client, user_id, current = terms_client
    for kwargs in ({}, {"json": {}}, {"data": "not json"}):
        res = client.post("/api/terms/accept", **kwargs)
        assert res.status_code == 409
        assert res.get_json()["error"] == "terms_version_mismatch"
        assert res.get_json()["current_version"] == current
    assert _stored(user_id) == ("2.0", datetime(2026, 3, 1))


def test_a_client_that_showed_2_0_can_no_longer_record_3_0(terms_client):
    client, user_id, _ = terms_client
    res = client.post("/api/terms/accept", json={"version": "2.0"})
    assert res.status_code == 409
    assert res.get_json()["error"] == "terms_version_mismatch"
    assert _stored(user_id) == ("2.0", datetime(2026, 3, 1))
    # A number is not the version string either.
    res = client.post("/api/terms/accept", json={"version": 3.0})
    assert res.status_code == 409


def test_accepting_the_current_version_records_it(terms_client):
    client, user_id, current = terms_client
    res = client.post("/api/terms/accept", json={"version": current})
    assert res.status_code == 200
    assert res.get_json()["accepted_terms_version"] == current
    version, at = _stored(user_id)
    assert version == current
    assert at > datetime(2026, 3, 1)

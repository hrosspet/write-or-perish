"""The Terms version (#440).

frontend/src/legal/terms.json is the one source of the Terms text: the
public /terms page, the acceptance dialog and the iPhone app all show it.
Its "version" must be the version the server asks users to accept, so a
change to the text that bumps the version also asks everyone to accept
again, and a bump without new text cannot happen by accident.

Same harness as test_log_dashboard_privacy (minimal app, sqlite)."""
import json
import pathlib
from datetime import datetime
from types import SimpleNamespace

from backend.tests.test_log_dashboard_privacy import (  # noqa: F401 - fixture
    _db, app,
)
from backend.routes.dashboard import _terms_up_to_date
from backend.routes.terms import CURRENT_TERMS_VERSION

ROOT = pathlib.Path(__file__).resolve().parents[2]
TERMS_JSON = ROOT / "frontend" / "src" / "legal" / "terms.json"


def test_the_server_asks_for_the_version_the_text_carries():
    doc = json.loads(TERMS_JSON.read_text(encoding="utf-8"))
    assert doc["version"] == CURRENT_TERMS_VERSION
    footnotes = [b["text"] for b in doc["blocks"] if b["type"] == "footnote"]
    assert any(CURRENT_TERMS_VERSION in text for text in footnotes), \
        "the text names its own version"


def test_the_beta_terms_ask_everyone_who_accepted_2_0_again():
    assert CURRENT_TERMS_VERSION == "3.0"
    accepted = datetime(2026, 3, 1)
    old = SimpleNamespace(accepted_terms_version="2.0",
                          accepted_terms_at=accepted, deactivated_at=None)
    new = SimpleNamespace(accepted_terms_version="3.0",
                          accepted_terms_at=accepted, deactivated_at=None)
    assert _terms_up_to_date(old) is False
    assert _terms_up_to_date(new) is True

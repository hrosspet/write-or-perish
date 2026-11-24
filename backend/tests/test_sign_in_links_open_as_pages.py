"""Signing in with a link and signing out act only when the URL is
opened as a page. A request the browser marks as loading something into
a page (Sec-Fetch-Dest: image, script, empty, ...) is refused and
changes no session; requests without Sec-Fetch headers (the iPhone app,
mail apps) work as before.

Fixtures from test_email_change (real create_app, sqlite in memory).
"""
import re

import pytest
from flask import g

from backend.tests.test_email_change import (  # noqa: F401 (fixtures)
    _anonymous, _client, _session_user_id, _user, mails, real_app)

AS_IMAGE = {"Sec-Fetch-Dest": "image", "Sec-Fetch-Mode": "no-cors",
            "Sec-Fetch-Site": "same-origin"}
AS_PAGE = {"Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
           "Sec-Fetch-Site": "none"}


def _sign_in_token(app, mails, address):
    r = _anonymous(app).post("/auth/magic-link/send", json={"email": address})
    assert r.status_code == 200
    mail = [m for m in mails if m["to"] == address][-1]
    return re.search(r"verify\?token=(\S+)", mail["text"]).group(1)


@pytest.mark.parametrize("dest", ["image", "script", "empty", "iframe"])
def test_sign_out_loaded_into_a_page_is_refused(real_app, dest):
    reader = _client(real_app, "alice")
    r = reader.get("/auth/logout", headers={**AS_IMAGE, "Sec-Fetch-Dest": dest})
    assert r.status_code == 403
    assert _session_user_id(reader) == str(_user("alice").id)


@pytest.mark.parametrize("headers", [AS_PAGE, {}])
def test_sign_out_opened_as_a_page_signs_out(real_app, headers):
    reader = _client(real_app, "alice")
    r = reader.get("/auth/logout", headers=headers)
    assert r.status_code == 302
    assert _session_user_id(reader) is None


def test_sign_in_link_loaded_into_a_page_changes_nothing(real_app, mails):
    token = _sign_in_token(real_app, mails, "bob@example.com")
    reader = _client(real_app, "alice")
    r = reader.get(f"/auth/magic-link/verify?token={token}",
                   headers=AS_IMAGE)
    assert r.status_code == 403
    assert _session_user_id(reader) == str(_user("alice").id)


@pytest.mark.parametrize("headers", [AS_PAGE, {}])
def test_sign_in_link_opened_as_a_page_signs_in(real_app, mails, headers):
    token = _sign_in_token(real_app, mails, "bob@example.com")
    bob_id = str(_user("bob").id)
    g.pop("_login_user", None)
    browser = real_app.test_client()
    r = browser.get(f"/auth/magic-link/verify?token={token}", headers=headers)
    assert r.status_code == 302
    assert _session_user_id(browser) == bob_id

"""Links that leave the app (the sign-in and welcome emails, X's callback)
are on the configured address (FRONTEND_URL), whatever Host,
X-Forwarded-Host or X-Forwarded-Prefix the request carries; and the app
reads neither X-Forwarded-Host nor X-Forwarded-Prefix.

create_app() with ProxyFix as deployed; sqlite in memory; mail captured
below the senders; the call to X is answered here.
"""
import re
from unittest.mock import patch
from urllib.parse import unquote

import pytest
import requests

from backend.tests.test_sign_in_links import (  # noqa: F401
    FRONTEND_URL, SIGN_IN_SUBJECT, WELCOME_SUBJECT,
    _anonymous, _open, _token, _user, mails, real_app)

# What nginx sets (configs/nginx.txt), and other values a request may carry.
PROXIED = {"X-Forwarded-Proto": "https", "X-Forwarded-For": "198.51.100.7"}
OTHER_HOST = {"Host": "other.example"}
OTHER_FORWARDED_HOST = {"Host": "loore.test",
                        "X-Forwarded-Host": "other.example"}
ALL_THREE = {"Host": "other.example", "X-Forwarded-Host": "other.example",
             "X-Forwarded-Prefix": "//other.example"}
HEADER_SETS = pytest.mark.parametrize(
    "headers", [OTHER_HOST, OTHER_FORWARDED_HOST, ALL_THREE],
    ids=["host", "x-forwarded-host", "all-three"])


def _link(sent, subject):
    mail = [m for m in sent if m["subject"] == subject][-1]
    return re.search(r"\S+/auth/magic-link/verify\?token=\S+",
                     mail["text"]).group(0)


@HEADER_SETS
def test_the_sign_in_link_is_on_the_configured_address(real_app, mails, headers):
    r = _anonymous(real_app).post(
        "/auth/magic-link/send", json={"email": "alice@example.com"},
        headers={**PROXIED, **headers})
    assert r.status_code == 200

    link = _link(mails, SIGN_IN_SUBJECT)
    assert link.startswith(f"{FRONTEND_URL}/auth/magic-link/verify?token=")
    assert _open(real_app, _token(mails)) == (
        f"{FRONTEND_URL}/dashboard", str(_user("alice").id))


def _admin_at(app, host):
    """The admin, signed in; the test client sends a cookie only to the
    host it was set for."""
    c = _anonymous(app)
    with c.session_transaction(headers={"Host": host}) as sess:
        sess["_user_id"] = str(_user("root").id)
        sess["_fresh"] = True
    return c


@HEADER_SETS
def test_the_welcome_link_is_on_the_configured_address(real_app, mails, headers):
    r = _admin_at(real_app, headers["Host"]).post(
        f"/api/admin/users/{_user('bob').id}/activate_and_welcome",
        headers={**PROXIED, **headers})
    assert r.get_json()["email_sent"] is True

    link = _link(mails, WELCOME_SUBJECT)
    assert link.startswith(f"{FRONTEND_URL}/auth/magic-link/verify?token=")


def _x_request_token_answer():
    r = requests.Response()
    r.status_code = 200
    r.headers["Content-Type"] = "application/x-www-form-urlencoded"
    r._content = (b"oauth_token=REQUEST&oauth_token_secret=s"
                  b"&oauth_callback_confirmed=true")
    return r


@HEADER_SETS
def test_x_is_given_the_callback_on_the_configured_address(real_app, headers):
    """X sign-in and Connect X both start at /auth/twitter."""
    callbacks = []

    def fake_send(self, prep, **kw):
        auth = prep.headers.get("Authorization", "")
        auth = auth.decode() if isinstance(auth, bytes) else auth
        callbacks.append(unquote(re.search(
            r'oauth_callback="([^"]*)"', auth).group(1)))
        return _x_request_token_answer()

    with patch("requests.sessions.Session.send", fake_send):
        r = _anonymous(real_app).get("/auth/twitter",
                                     headers={**PROXIED, **headers})

    assert r.status_code == 302
    assert "oauth_token=REQUEST" in r.headers["Location"]
    assert callbacks == [f"{FRONTEND_URL}/auth/twitter/authorized"]


def test_forwarded_host_and_prefix_do_not_change_the_apps_urls(real_app):
    c = _anonymous(real_app)
    forwarded = {**PROXIED, "Host": "loore.test",
                 "X-Forwarded-Host": "other.example",
                 "X-Forwarded-Prefix": "//other.example"}

    # The start of Sign in with X: a redirect built with url_for.
    r = c.get("/auth/login", headers=forwarded)
    assert r.status_code == 302
    assert r.headers["Location"] == "/auth/twitter"

    # A redirect werkzeug builds as an absolute URL from the request's host.
    r = c.get("/api/dashboard", headers=forwarded)
    assert r.status_code == 308
    assert r.headers["Location"] == "https://loore.test/api/dashboard/"

"""After signing in, back to the page where the sign-in started (#704).

- The one check of a destination (backend/utils/destination.py) against
  the table of hostile values the web's twin reads too.
- Sign in with X: the destination survives the round trip to X, X's Cancel
  and an X error (both used to open X's page again), and a sign-in started
  without one forgets what an abandoned one left.
- Sign in by email: the destination rides in the signed link, so it works
  in another browser on another device; the admin's welcome link too.
- No destination is ever written to the logs.

Uses the app of test_x_login_linking (auth + admin + the real flask-dance
blueprint, sqlite in memory); the HTTP calls to X are faked as there.
Tests marked "control" pass on the base as well: they pin what already
worked.
"""
import json
import logging
import re
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

import pytest

from backend.tests.test_x_login_linking import (  # noqa: F401 - fixtures
    FRONTEND_URL, User, _add, _db, _fake_x_exchange, _session, _x_callback,
    app)

CASES_FILE = (Path(__file__).resolve().parents[2]
              / "frontend" / "src" / "utils" / "destinationCases.json")

# The table's core, here as well, for a checkout without the frontend.
HOSTILE = [
    "//evil.example", "/\\evil.example", "https:evil.example",
    "/%2F%2Fevil.example", "/@user\nhttps://evil.example",
    "/" + "a" * 2048,
]


def _cases():
    if not CASES_FILE.exists():
        pytest.skip("the shared table lives in frontend/src/utils")
    return json.loads(CASES_FILE.read_text())


class TestTheCheck:
    def test_the_shared_table(self):
        from backend.utils.destination import safe_destination
        cases = _cases()
        for value in cases["accepted"]:
            assert safe_destination(value) == value, value
        for value in cases["dropped"]:
            assert safe_destination(value) is None, repr(value)

    @pytest.mark.parametrize("value", HOSTILE)
    def test_hostile_values_are_dropped(self, value):
        from backend.routes.auth import is_safe_redirect_url
        from backend.utils.destination import safe_destination
        assert safe_destination(value) is None
        assert is_safe_redirect_url(value) is False

    def test_length_bound(self):
        from backend.utils.destination import (
            MAX_DESTINATION_LENGTH, safe_destination)
        longest = "/" + "a" * (MAX_DESTINATION_LENGTH - 1)
        assert safe_destination(longest) == longest
        assert safe_destination(longest + "a") is None

    @pytest.mark.parametrize("value", [None, 5, ["/node/1"], {"a": 1}, b"/node/1"])
    def test_not_a_string(self, value):
        from backend.utils.destination import safe_destination
        assert safe_destination(value) is None


# ── Sign in with X ───────────────────────────────────────────────────────

def _start_x(client, query):
    """The sign-in page's "Sign in with X": /auth/login?next=… sends the
    browser on to /auth/twitter, which sends it to X."""
    start = client.get(f"/auth/login?{query}" if query else "/auth/login")
    assert start.status_code == 302
    assert start.headers["Location"].endswith("/auth/twitter")
    to_x = client.get("/auth/twitter")
    assert to_x.status_code == 302
    return to_x


def _next(value):
    return "next=" + quote(value, safe="")


class TestSignInWithX:
    def test_control_the_destination_survives_the_round_trip(self, app):
        """Control: the page, its query and its fragment come back."""
        _add(username="ana", twitter_id="666", approved=True)
        client = app.test_client()
        calls, fake = _fake_x_exchange(access_x_id=666)
        with fake:
            _start_x(client, _next("/@ana/a-first-entry?x=1#reply-7"))
            back = client.get(
                "/auth/twitter/authorized?oauth_token=REQ-1&oauth_verifier=V")
            assert back.headers["Location"].endswith("/auth/login")
        resp = _x_callback(client, x_id=666, screen_name="ana")
        assert resp.headers["Location"] == (
            f"{FRONTEND_URL}/@ana/a-first-entry?x=1#reply-7")

    def test_cancel_at_x_returns_to_the_sign_in_page_with_the_destination(self, app):
        """X's Cancel comes back with denied= and no token. It used to start
        the sign-in over, which opened X's page again."""
        client = app.test_client()
        calls, fake = _fake_x_exchange()
        with fake:
            _start_x(client, _next("/node/5"))
            back = client.get("/auth/twitter/authorized?denied=REQ-1")
            assert back.headers["Location"].endswith("/auth/login")
            resp = client.get("/auth/login")
        assert resp.headers["Location"] == (
            f"{FRONTEND_URL}/login?error=x_cancelled&returnUrl=%2Fnode%2F5")
        assert calls["exchanged"] == []
        sess = _session(client)
        assert "next_url" not in sess and "x_sign_in_returned" not in sess
        assert "_user_id" not in sess

    def test_an_x_error_returns_to_the_sign_in_page_instead_of_a_loop(self, app):
        """X refuses the request token (an outage): flask-dance sends the
        browser to /auth/login, which used to send it straight back."""
        from requests_oauthlib.oauth1_session import TokenRequestDenied
        from unittest.mock import MagicMock
        client = app.test_client()

        def refusing(self, url, **kw):
            raise TokenRequestDenied("Token request failed with code 503",
                                     MagicMock(status_code=503))

        with patch("requests_oauthlib.OAuth1Session._fetch_token", refusing):
            start = client.get("/auth/login?" + _next("/@ana"))
            to_x = client.get(start.headers["Location"])
            assert to_x.headers["Location"].endswith("/auth/login")
            resp = client.get("/auth/login")
        assert resp.headers["Location"] == (
            f"{FRONTEND_URL}/login?error=x_failed&returnUrl=%2F%40ana")

    def test_control_the_next_start_after_a_cancel_goes_to_x_again(self, app):
        """Control (passes on the base, which had no flag): the return flag
        is used once, and a new start from the sign-in page goes to X."""
        client = app.test_client()
        calls, fake = _fake_x_exchange()
        with fake:
            _start_x(client, _next("/node/5"))
            client.get("/auth/twitter/authorized?denied=REQ-1")
            client.get("/auth/login")
            again = client.get("/auth/login?" + _next("/node/6"))
        assert again.headers["Location"].endswith("/auth/twitter")
        assert _session(client)["next_url"] == "/node/6"

    def test_control_a_flag_left_behind_does_not_stop_a_start_from_a_page(self, app):
        """Control (passes on the base, which had no flag)."""
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["x_sign_in_returned"] = "failed"
        start = client.get("/auth/login?" + _next("/node/5"))
        assert start.headers["Location"].endswith("/auth/twitter")

    def test_a_start_without_a_destination_forgets_an_abandoned_one(self, app):
        """Someone left X's page from a sign-in that started on /node/5;
        a later sign-in in this browser that names no page must not land
        there."""
        _add(username="ana", twitter_id="666", approved=True)
        client = app.test_client()
        calls, fake = _fake_x_exchange(access_x_id=666)
        with fake:
            _start_x(client, _next("/node/5"))  # abandoned at X
            _start_x(client, "")
            client.get("/auth/twitter/authorized?oauth_token=REQ-2&oauth_verifier=V")
        resp = _x_callback(client, x_id=666, screen_name="ana")
        assert resp.headers["Location"] == f"{FRONTEND_URL}/dashboard"

    @pytest.mark.parametrize("value", HOSTILE)
    def test_a_hostile_destination_is_dropped(self, app, value):
        _add(username="ana", twitter_id="666", approved=True)
        client = app.test_client()
        calls, fake = _fake_x_exchange(access_x_id=666)
        with fake:
            _start_x(client, _next("/node/5"))
            _start_x(client, _next(value))  # replaces, and is dropped
            client.get("/auth/twitter/authorized?oauth_token=REQ-2&oauth_verifier=V")
        resp = _x_callback(client, x_id=666, screen_name="ana")
        assert resp.headers["Location"] == f"{FRONTEND_URL}/dashboard"

    def test_a_refused_callback_keeps_the_destination(self, app):
        """A callback this browser did not start (backend/oauth.py) sends it
        to the sign-in page; the page it started on is still the way on."""
        client = app.test_client()
        calls, fake = _fake_x_exchange()
        with fake:
            _start_x(client, _next("/node/5"))
            resp = client.get("/auth/twitter/authorized"
                              "?oauth_token=SOMEONE_ELSES&oauth_verifier=V")
        assert resp.headers["Location"] == (
            f"{FRONTEND_URL}/login?error=x_try_again&returnUrl=%2Fnode%2F5")

    def test_control_a_new_x_account_lands_on_the_destination(self, app):
        """Control: a new account goes to the page as well; the web shows
        the terms over it, then its first step (web tests)."""
        client = app.test_client()
        calls, fake = _fake_x_exchange(access_x_id=777)
        with fake:
            _start_x(client, _next("/node/5"))
            client.get("/auth/twitter/authorized?oauth_token=REQ-1&oauth_verifier=V")
        resp = _x_callback(client, x_id=777, screen_name="newcomer")
        assert resp.headers["Location"] == f"{FRONTEND_URL}/node/5"
        new = User.query.filter_by(twitter_id="777").one()
        assert new.approved is False and new.accepted_terms_at is None


# ── Sign in by email ─────────────────────────────────────────────────────

@pytest.fixture
def mails(monkeypatch):
    sent = []
    import backend.utils.email as email_mod
    monkeypatch.setattr(
        email_mod, "_deliver",
        lambda to, subject, text, html: sent.append(
            {"to": to, "subject": subject, "text": text}))
    return sent


def _client(app, addr):
    from flask import g
    g.pop("_login_user", None)
    c = app.test_client()
    c.environ_base["REMOTE_ADDR"] = addr
    return c


def _ask(app, email, next_url, addr="203.0.113.1"):
    body = {"email": email}
    if next_url is not None:
        body["next_url"] = next_url
    resp = _client(app, addr).post("/auth/magic-link/send", json=body)
    assert resp.status_code == 200
    return resp


def _link_token(mails):
    return re.search(r"verify\?token=(\S+)", mails[-1]["text"]).group(1)


def _open_elsewhere(app, token):
    """Another browser on another device: no cookie of the one that asked,
    another address."""
    c = _client(app, "198.51.100.7")
    resp = c.get(f"/auth/magic-link/verify?token={token}")
    assert resp.status_code == 302
    with c.session_transaction() as sess:
        return resp.headers["Location"], sess.get("_user_id")


class TestSignInByEmail:
    def test_control_the_link_opened_on_another_device_returns_there(self, app, mails):
        """Control: the destination is inside the signed link, not in the
        browser that asked for it."""
        ana = _add(username="ana", email="ana@example.com", approved=True)
        _ask(app, "ana@example.com", "/@ana/a-first-entry?x=1#reply-7")
        location, user_id = _open_elsewhere(app, _link_token(mails))
        assert location == f"{FRONTEND_URL}/@ana/a-first-entry?x=1#reply-7"
        assert user_id == str(ana.id)

    @pytest.mark.parametrize("value", HOSTILE[:-1] + ["/%5Cevil.example"])
    def test_a_hostile_destination_is_dropped(self, app, mails, value):
        """The base dropped "//evil.example" and "https:evil.example" too
        (controls); it sent the rest on."""
        ana = _add(username="ana", email="ana@example.com", approved=True)
        _ask(app, "ana@example.com", value)
        location, user_id = _open_elsewhere(app, _link_token(mails))
        assert location == f"{FRONTEND_URL}/dashboard"
        assert user_id == str(ana.id)

    def test_a_destination_minted_into_a_link_by_hand_is_checked_again(self, app):
        """A link made with the helper (#703) and a destination the routes
        would have refused: the open drops it."""
        from backend.utils.sign_in_links import make_link_token
        _add(username="ana", email="ana@example.com", approved=True)
        token = make_link_token("ana@example.com", next_url="/%2F%2Fevil.example")
        location, _ = _open_elsewhere(app, token)
        assert location == f"{FRONTEND_URL}/dashboard"

    def test_control_a_new_account_lands_on_the_destination(self, app, mails):
        """Control: the link that makes the account goes to the page; the
        steps (terms, then the waiting screen or the subscribe page) are
        the web's (web tests)."""
        _ask(app, "fresh@example.com", "/node/5")
        location, user_id = _open_elsewhere(app, _link_token(mails))
        assert location == f"{FRONTEND_URL}/node/5"
        fresh = User.query.filter_by(email="fresh@example.com").one()
        assert user_id == str(fresh.id)
        assert fresh.approved is False and fresh.accepted_terms_at is None


class TestWelcomeLink:
    def _activate(self, app, admin, user, body=None):
        from flask import g
        g.pop("_login_user", None)
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(admin.id)
        kwargs = {"json": body} if body is not None else {}
        resp = client.post(f"/api/admin/users/{user.id}/activate_and_welcome",
                           **kwargs)
        assert resp.status_code == 200, resp.get_json()
        return resp

    @pytest.mark.parametrize("body, landing", [
        (None, "/welcome"),
        ({}, "/welcome"),
        ({"next_url": "/node/5#reply-2"}, "/node/5#reply-2"),
        ({"next_url": "//evil.example"}, "/welcome"),
        ({"next_url": "/%2F%2Fevil.example"}, "/welcome"),
        ({"next_url": 5}, "/welcome"),
    ])
    def test_where_the_welcome_link_lands(self, app, mails, body, landing):
        """A named page is new; the rows that land on /welcome are controls
        (the base ignored the body)."""
        admin = _add(username="root", approved=True, is_admin=True)
        ana = _add(username="ana", email="ana@example.com", approved=False)
        self._activate(app, admin, ana, body)
        location, user_id = _open_elsewhere(app, _link_token(mails))
        assert location == f"{FRONTEND_URL}{landing}"
        assert user_id == str(ana.id)


# ── Logs ─────────────────────────────────────────────────────────────────

MARKER = "zz-destination-704-zz"


def test_control_no_destination_in_the_logs(app, mails, caplog):
    """Control (the base logged none either). The X round trip (done, cancelled), the email link (asked, opened,
    refused) and the welcome link, with a destination that is easy to find:
    no log line names it."""
    caplog.set_level(logging.DEBUG)
    dest = f"/node/5?m={MARKER}"
    _add(username="ana", twitter_id="666", email="ana@example.com", approved=True)
    admin = _add(username="root", approved=True, is_admin=True)
    bea = _add(username="bea", email="bea@example.com", approved=False)

    client = app.test_client()
    calls, fake = _fake_x_exchange(access_x_id=666)
    with fake:
        _start_x(client, _next(dest))
        client.get("/auth/twitter/authorized?denied=REQ-1")
        client.get("/auth/login")
        _start_x(client, _next(dest))
        client.get("/auth/twitter/authorized?oauth_token=REQ-2&oauth_verifier=V")
    _x_callback(client, x_id=666, screen_name="ana")
    _start_x(app.test_client(), _next("//" + MARKER))

    _ask(app, "ana@example.com", dest)
    _open_elsewhere(app, _link_token(mails))
    _ask(app, "ana@example.com", "/%2F%2F" + MARKER)
    _open_elsewhere(app, _link_token(mails))
    TestWelcomeLink()._activate(app, admin, bea, {"next_url": dest})

    assert caplog.records, "the flows log something; the check looked"
    for record in caplog.records:
        assert MARKER not in record.getMessage(), record.getMessage()
    _db.session.remove()

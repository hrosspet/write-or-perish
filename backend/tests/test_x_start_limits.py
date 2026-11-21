"""Starts of Sign in with X and Connect X (GET /auth/twitter) per client.

- Each start asks X for a request token. A client (its address) may
  start X_START_LIMITS of them; past the limit nothing is asked of X: a
  sign-in goes back to the sign-in page ("x_wait"), still on the way to
  where it started and with its invite code kept, and Connect X to the
  Account page ("wait"); both pages then say to wait a few minutes.
  Other addresses are not held up, and X's callback is not counted.
- The calls to X have a timeout.
- A person who cancels at X and tries again a few times, and several
  people behind one address, are let through (controls).
"""
from unittest.mock import patch

from backend.oauth import (
    X_CALL_TIMEOUT_SECONDS, X_START_LIMITS, _with_timeout,
)
from backend.tests.test_x_login_linking import (  # noqa: F401 - fixtures
    FRONTEND_URL, _add, _fake_x_exchange, _session, _signed_in, app)

ADDRESS = {"REMOTE_ADDR": "203.0.113.9"}
MOST = X_START_LIMITS[0][1]


def _start(client, environ=ADDRESS):
    return client.get("/auth/twitter", environ_base=environ)


def test_starts_past_the_limit_do_not_call_x(app):
    calls, fake = _fake_x_exchange()
    with fake:
        for _ in range(MOST):
            resp = _start(app.test_client())
            assert "api.twitter.com/oauth/" in resp.headers["Location"]
        refused = _start(app.test_client())
    assert calls["request_tokens"] == MOST
    assert refused.status_code == 302
    assert refused.headers["Location"].startswith(
        f"{FRONTEND_URL}/login?error=x_wait")


def test_a_refused_sign_in_keeps_its_destination_and_invite(app):
    calls, fake = _fake_x_exchange()
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["next_url"] = "/journal"
        sess["invite_code"] = "ABC123"
    with fake:
        for _ in range(MOST):
            _start(app.test_client())
        refused = _start(client)
    assert "returnUrl=%2Fjournal" in refused.headers["Location"]
    assert _session(client).get("invite_code") == "ABC123"


def test_a_refused_connect_goes_back_to_the_account_page(app):
    alice = _add(username="alice", email="alice@example.com", approved=True)
    calls, fake = _fake_x_exchange()
    with fake:
        for _ in range(MOST):
            _start(app.test_client())
        client = _signed_in(app, alice)
        assert client.get("/auth/x/connect", environ_base=ADDRESS) \
            .headers["Location"].endswith("/auth/twitter")
        refused = _start(client)
    assert refused.headers["Location"] == \
        f"{FRONTEND_URL}/account?x_login=wait#x"
    assert "x_connect" not in _session(client)
    assert calls["request_tokens"] == MOST


def test_other_addresses_and_the_callback_are_not_held_up(app):
    calls, fake = _fake_x_exchange()
    with fake:
        for _ in range(MOST + 1):
            _start(app.test_client())
        other = _start(app.test_client(), {"REMOTE_ADDR": "198.51.100.7"})
        assert "api.twitter.com/oauth/" in other.headers["Location"]
        # X coming back is not a start: a cancel still reaches the
        # sign-in page's own answer.
        back = app.test_client().get("/auth/twitter/authorized?denied=x",
                                     environ_base=ADDRESS)
    assert back.headers["Location"].endswith("/auth/login")


def test_a_person_retrying_a_few_times_is_let_through(app):
    """Control: several people behind one address, each cancelling at X
    and trying again twice."""
    calls, fake = _fake_x_exchange()
    with fake:
        for _ in range(10):
            for _ in range(3):
                resp = _start(app.test_client())
                assert "api.twitter.com/oauth/" in resp.headers["Location"]
    assert calls["request_tokens"] == 30


def test_calls_to_x_have_a_timeout(app):
    seen = []

    def send(method, url, *args, **kwargs):
        seen.append(kwargs.get("timeout"))

    wrapped = _with_timeout(send)
    wrapped("POST", "https://api.twitter.com/oauth/request_token")
    wrapped("GET", "https://api.twitter.com/x", timeout=3)
    assert seen == [X_CALL_TIMEOUT_SECONDS, 3]

    # Through flask-dance's start: the request-token call carries it.
    from requests_oauthlib.oauth1_session import TokenRequestDenied
    timeouts = []

    class _Refused:
        status_code = 503
        text = "unavailable"

    def fake_request(self, method, url, **kw):
        timeouts.append(kw.get("timeout"))
        raise TokenRequestDenied("stop here", _Refused())

    with patch("requests.Session.request", fake_request):
        resp = app.test_client().get("/auth/twitter", environ_base=ADDRESS)
    assert resp.status_code == 302
    assert timeouts == [X_CALL_TIMEOUT_SECONDS]

"""The X OAuth1 session is per request, never shared between requests.

flask-dance keeps one OAuth1Session per blueprint (a werkzeug cached_property
on the process-global blueprint object). Under `gunicorn -k gevent`, requests
that overlap in one worker would share it: the token it loaded first, and the
client state the OAuth dance writes (request token, verifier). These tests
run a second request while the first is inside its HTTP call to X, which is
where gevent switches between greenlets. Each nested request runs in a fresh
contextvars context, as a gevent greenlet does, so it gets its own app
context and `g` even though the test fixture holds one open.
"""
import contextvars
import json
from unittest.mock import patch

import pytest
import requests

from backend.tests.test_x_login_linking import (  # noqa: F401
    app, _add)


def _session(client):
    with client.session_transaction() as sess:
        return dict(sess)


X_ID_BY_ACCESS = {"ACCESS-V": "1001", "ACCESS-B": "2002"}


def _x_response(payload):
    r = requests.Response()
    r.status_code = 200
    r._content = json.dumps(payload).encode()
    return r


@pytest.mark.parametrize("b_has_token", [True, False])
def test_overlapping_x_logins_do_not_share_a_token(app, b_has_token):
    v_user = _add(username="v", twitter_id="1001", approved=True)
    b_user = _add(username="b", twitter_id="2002", approved=True)
    v = app.test_client()
    b = app.test_client()
    with v.session_transaction() as s:
        s["twitter_oauth_token"] = {"oauth_token": "ACCESS-V", "oauth_token_secret": "sv"}
    if b_has_token:
        with b.session_transaction() as s:
            s["twitter_oauth_token"] = {"oauth_token": "ACCESS-B", "oauth_token_secret": "sb"}
    state = {"nested": False, "calls": []}

    def fake_send(self, prep, **kw):
        auth = prep.headers.get("Authorization", "")
        auth = auth.decode() if isinstance(auth, bytes) else auth
        tok = auth.split('oauth_token="')[1].split('"')[0]
        state["calls"].append(tok)
        if not state["nested"]:
            # V is waiting on X here; under gevent B's request runs now.
            state["nested"] = True
            contextvars.Context().run(b.get, "/auth/login")
        return _x_response({"id": int(X_ID_BY_ACCESS[tok]), "screen_name": "x"})

    with patch("requests.sessions.Session.send", fake_send):
        v.get("/auth/login")

    assert _session(v).get("_user_id") == str(v_user.id)
    b_uid = _session(b).get("_user_id")
    if b_has_token:
        assert b_uid == str(b_user.id)
        assert state["calls"] == ["ACCESS-V", "ACCESS-B"]
    else:
        # No token of its own: B must not be signed in at all.
        assert b_uid is None
        assert state["calls"] == ["ACCESS-V"]


def test_overlapping_oauth_callbacks_keep_their_own_verifier(app):
    """Two users return from X at the same moment: each access-token exchange
    must be signed with that browser's own request token and verifier."""
    from backend.oauth import X_REQUEST_TOKEN_SESSION_KEY
    a = app.test_client()
    b = app.test_client()
    # Each browser started its own authorization (the #335 callback guard
    # only lets a session exchange the request token it was sent to X with).
    for client, req in ((a, "REQ-A"), (b, "REQ-B")):
        with client.session_transaction() as s:
            s[X_REQUEST_TOKEN_SESSION_KEY] = req
    state = {"nested": False, "signed": []}

    def fake_send(self, prep, **kw):
        auth = prep.headers.get("Authorization", "")
        auth = auth.decode() if isinstance(auth, bytes) else auth
        if "access_token" in prep.url:
            tok = auth.split('oauth_token="')[1].split('"')[0]
            ver = auth.split('oauth_verifier="')[1].split('"')[0]
            state["signed"].append((tok, ver))
            if not state["nested"]:
                state["nested"] = True
                contextvars.Context().run(
                    b.get, "/auth/twitter/authorized?oauth_token=REQ-B&oauth_verifier=VER-B")
            who = "A" if tok == "REQ-A" else "B"
            r = requests.Response()
            r.status_code = 200
            r._content = f"oauth_token=ACCESS-{who}&oauth_token_secret=s{who}".encode()
            return r
        # verify_credentials after the callback redirect is not followed here
        return _x_response({"id": 1, "screen_name": "x"})

    with patch("requests.sessions.Session.send", fake_send):
        a.get("/auth/twitter/authorized?oauth_token=REQ-A&oauth_verifier=VER-A")

    assert state["signed"] == [("REQ-A", "VER-A"), ("REQ-B", "VER-B")]
    assert _session(a).get("twitter_oauth_token", {}).get("oauth_token") == "ACCESS-A"
    assert _session(b).get("twitter_oauth_token", {}).get("oauth_token") == "ACCESS-B"

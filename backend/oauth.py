import logging
from urllib.parse import parse_qs, parse_qsl, urlparse

from flask import g, request, session
from flask_dance.consumer import OAuth1ConsumerBlueprint, oauth_before_login
from flask_dance.contrib.twitter import make_twitter_blueprint

logger = logging.getLogger(__name__)

# The request token of the X authorization this browser session started.
# flask-dance's OAuth 1 flow keeps nothing between /auth/twitter and X's
# callback: the callback rebuilds the exchange from the oauth_token and
# oauth_verifier in its own URL, so a callback URL completed in one browser
# could be replayed in another. There it signed the victim into the
# attacker's account (login CSRF), and with Connect X (#311) it attached the
# attacker's X login to the victim's account. The callback must now carry
# the request token this session was sent to X with.
X_REQUEST_TOKEN_SESSION_KEY = "x_oauth_request_token"


def _remember_request_token(sender, url, **kwargs):
    """oauth_before_login: flask-dance is about to send the browser to X
    with this request token."""
    token = parse_qs(urlparse(url).query).get("oauth_token", [None])[0]
    session[X_REQUEST_TOKEN_SESSION_KEY] = token


def _refuse_foreign_callback():
    """Before flask-dance's callback view: a callback that would exchange a
    request token must present the one this session started with (used
    once). A callback without one (X's Cancel sends ``denied=``) stores
    nothing and goes through, so a cancel still reaches /auth/login.

    The token is read the way oauthlib will read it for the exchange:
    parse_qsl over request.url's query. Parsed any other way (werkzeug's
    args, or an older parse_qsl that also split on ";"), a crafted query
    could show this check one token and the exchange another; a repeated
    oauth_token is refused for the same reason."""
    if request.endpoint != "twitter.authorized":
        return None
    expected = session.pop(X_REQUEST_TOKEN_SESSION_KEY, None)
    presented = [value for key, value in
                 parse_qsl(urlparse(request.url).query, keep_blank_values=True)
                 if key == "oauth_token"]
    if not presented or (expected is not None and presented == [expected]):
        return None
    logger.warning("Refused an X callback this session did not start")
    from backend.routes.auth import refuse_x_callback
    return refuse_x_callback()


_make_session = OAuth1ConsumerBlueprint.__dict__["session"].fget


class _RequestLocalSessionBlueprint(OAuth1ConsumerBlueprint):
    """An OAuth1 blueprint whose provider session lives on `g`.

    flask-dance caches one OAuth1Session on the blueprint (a werkzeug
    cached_property, deleted in every request's teardown). The blueprint is
    process-global, so under `gunicorn -k gevent` requests that overlap in
    one worker would share that session, and with it the token it loaded
    first: one person's /auth/login could verify, and sign in with, another
    person's X token. `g` belongs to a single request.
    """

    def _session_key(self):
        return "_oauth1_session_" + self.name

    def _get_session(self):
        key = self._session_key()
        oauth_session = g.get(key)
        if oauth_session is None:
            oauth_session = _make_session(self)
            setattr(g, key, oauth_session)
        return oauth_session

    def _drop_session(self):
        # flask-dance's teardown (`del self.session`) runs after every
        # request. `g` normally goes away with the request anyway; popping
        # also covers an app context that outlives one request (tests).
        g.pop(self._session_key(), None)

    session = property(_get_session, None, _drop_session)


def init_twitter_blueprint(app):
    # After Twitter OAuth completes, redirect back to /auth/login
    # which handles the next_url session variable for proper redirect
    redirect_url = "/auth/login"

    twitter_bp = make_twitter_blueprint(
        api_key=app.config["TWITTER_API_KEY"],
        api_secret=app.config["TWITTER_API_SECRET"],
        redirect_url = redirect_url
    )
    # make_twitter_blueprint always builds a plain OAuth1ConsumerBlueprint;
    # the subclass only replaces how `session` is stored.
    twitter_bp.__class__ = _RequestLocalSessionBlueprint
    oauth_before_login.connect(_remember_request_token, sender=twitter_bp)
    twitter_bp.before_request(_refuse_foreign_callback)
    # Register this blueprint on the app
    app.register_blueprint(twitter_bp, url_prefix="/auth")
    return twitter_bp

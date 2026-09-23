import logging
from urllib.parse import parse_qs, parse_qsl, urlparse

from flask import request, session
from flask_dance.consumer import oauth_before_login
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


def init_twitter_blueprint(app):
    # After Twitter OAuth completes, redirect back to /auth/login
    # which handles the next_url session variable for proper redirect
    redirect_url = "/auth/login"

    twitter_bp = make_twitter_blueprint(
        api_key=app.config["TWITTER_API_KEY"],
        api_secret=app.config["TWITTER_API_SECRET"],
        redirect_url = redirect_url
    )
    oauth_before_login.connect(_remember_request_token, sender=twitter_bp)
    twitter_bp.before_request(_refuse_foreign_callback)
    # Register this blueprint on the app
    app.register_blueprint(twitter_bp, url_prefix="/auth")
    return twitter_bp

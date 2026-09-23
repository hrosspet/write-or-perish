from flask import g
from flask_dance.consumer import OAuth1ConsumerBlueprint
from flask_dance.contrib.twitter import make_twitter_blueprint

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

    def _get_session(self):
        key = "_oauth1_session_" + self.name
        session = g.get(key)
        if session is None:
            session = _make_session(self)
            setattr(g, key, session)
        return session

    def _drop_session(self):
        # flask-dance's teardown does `del self.session`; `g` goes away with
        # the request, so there is nothing to drop.
        pass

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
    # Register this blueprint on the app
    app.register_blueprint(twitter_bp, url_prefix="/auth")
    return twitter_bp

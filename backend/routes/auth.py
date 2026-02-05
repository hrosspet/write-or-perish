from flask import Blueprint, redirect, url_for, flash, current_app, request
from flask_login import login_user, logout_user, login_required, current_user
from backend.models import User
from backend.extensions import db
from flask_dance.contrib.twitter import twitter
from flask import session
import logging
from urllib.parse import urlparse

logger = logging.getLogger(__name__)
auth_bp = Blueprint("auth_bp", __name__)


def is_safe_redirect_url(target):
    """
    Validate that the redirect URL is safe (relative path only).
    Prevents open redirect vulnerabilities.
    """
    if not target:
        return False
    # Must start with / and not // (which would be protocol-relative)
    if not target.startswith('/') or target.startswith('//'):
        return False
    # Parse the URL to check for any tricks
    parsed = urlparse(target)
    # Ensure no scheme or netloc (hostname)
    if parsed.scheme or parsed.netloc:
        return False
    return True


@auth_bp.route("/login")
def login():
    # Capture the 'next' parameter for post-login redirect
    next_url = request.args.get('next')
    if next_url and is_safe_redirect_url(next_url):
        session['next_url'] = next_url

    if not twitter.authorized:
        # If not authorized, start the OAuth flow.
        return redirect(url_for("twitter.login"))

    # Fetch Twitter info
    resp = twitter.get("account/verify_credentials.json")
    if not resp.ok:
        logger.error(f"Failed to fetch Twitter credentials. Status: {resp.status_code}")
        flash("Failed to fetch user info from Twitter.", "error")
        # Redirect to frontend instead of non-existent 'index' route
        return redirect(current_app.config.get('FRONTEND_URL', '/'))
    tw_info = resp.json()
    twitter_id = str(tw_info["id"])
    username = tw_info["screen_name"]
    user = User.query.filter_by(twitter_id=twitter_id).first()
    if not user:
        # check whether user created just with handle (eg via whitelist)
        user = User.query.filter_by(username=username).first()
        if user:
            # set correct twitter id
            user.twitter_id = twitter_id
            db.session.commit()
        else:
            # create new user
            user = User(twitter_id=twitter_id, username=username)
            db.session.add(user)
            db.session.commit()

    login_user(user)
    flash("Logged in successfully!", "success")

    # Redirect to stored next_url if available, otherwise dashboard
    frontend_url = current_app.config.get('FRONTEND_URL')
    next_url = session.pop('next_url', None)
    if next_url and is_safe_redirect_url(next_url):
        redirect_url = f"{frontend_url}{next_url}"
    else:
        redirect_url = f"{frontend_url}/dashboard"
    return redirect(redirect_url)

@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    session.clear()
    flash("Logged out successfully", "success")
    frontend_url = current_app.config.get("FRONTEND_URL")
    return redirect(frontend_url)

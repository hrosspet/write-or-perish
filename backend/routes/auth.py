import re
from datetime import datetime, timedelta

from flask import Blueprint, redirect, url_for, flash, current_app, request, jsonify
from flask_login import login_user, logout_user, login_required, current_user
from sqlalchemy.exc import IntegrityError
from backend.models import User
from backend.extensions import db
from flask_dance.contrib.twitter import twitter
from flask import session
from backend.utils.magic_link import (
    generate_magic_link_token, verify_magic_link_token,
    hash_token, generate_unique_username,
)
from backend.utils.email import send_magic_link_email
from backend.utils.reserved_usernames import derive_available_username
import logging
from urllib.parse import urlparse

logger = logging.getLogger(__name__)
auth_bp = Blueprint("auth_bp", __name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


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


def _drop_x_token():
    """Forget flask-dance's stored X token for this session, so the next
    "Sign in with X" goes through X again instead of reusing it."""
    twitter_bp = current_app.blueprints.get("twitter")
    if twitter_bp is not None:
        try:
            del twitter_bp.token
        except KeyError:
            pass


def _create_x_user(twitter_id, screen_name):
    """First X login for this X id: a fresh account.

    The X id is the only identity an X login carries. The handle is never
    used to find an existing account: magic-link usernames are the email's
    local part and X handles are freely re-registrable, so a handle match
    proves nothing, and linking on it let whoever held a handle log into
    someone else's account. Accounts created ahead of their owner's first
    login (admin whitelist, pre-fill) carry the X id from creation and are
    found by it. Attaching an X id to an account that signs in another way
    needs that account's owner signed in (a Connect-X flow, not built).

    The handle only seeds the username; a taken or reserved handle gets a
    derived one. Two callbacks for the same X id can race here: the loser's
    insert fails on the unique X id, so re-read and use the winner's row.
    """
    for _attempt in range(2):
        user = User(twitter_id=twitter_id,
                    username=derive_available_username(screen_name))
        db.session.add(user)
        try:
            db.session.commit()
            return user
        except IntegrityError:
            db.session.rollback()
            existing = User.query.filter_by(twitter_id=twitter_id).first()
            if existing is not None:
                return existing
            # Lost a race on the derived username instead: derive again.
    raise RuntimeError(f"could not create an account for X id {twitter_id}")


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
        # The token is what failed (revoked on X, expired app auth): keep
        # it and every retry in this browser fails the same way until the
        # cookies are cleared, since twitter.authorized stays true.
        _drop_x_token()
        flash("Failed to fetch user info from Twitter.", "error")
        # Redirect to frontend instead of non-existent 'index' route
        return redirect(current_app.config.get('FRONTEND_URL', '/'))
    tw_info = resp.json()
    twitter_id = str(tw_info["id"])
    username = tw_info["screen_name"]
    user = User.query.filter_by(twitter_id=twitter_id).first()
    if not user:
        user = _create_x_user(twitter_id, username)

    login_user(user, remember=True)
    # Record the sign-in itself, not only the app requests that follow:
    # "has anyone ever signed into this account" (a placeholder has not)
    # must not depend on the app loading afterwards. Time only, no path:
    # the Activity tab's "where they last were" is an area of the app, and
    # the app's bootstrap calls would not overwrite "/auth/login" until
    # the person opened something.
    from backend.utils.activity import touch_last_seen
    touch_last_seen(user, None)
    flash("Logged in successfully!", "success")

    # Redirect to stored next_url if available, otherwise dashboard
    frontend_url = current_app.config.get('FRONTEND_URL')
    next_url = session.pop('next_url', None)
    if next_url and is_safe_redirect_url(next_url):
        redirect_url = f"{frontend_url}{next_url}"
    else:
        redirect_url = f"{frontend_url}/dashboard"
    return redirect(redirect_url)

@auth_bp.route("/magic-link/send", methods=["POST"])
def magic_link_send():
    data = request.get_json() or {}
    email = (data.get("email") or "").strip().lower()
    next_url = data.get("next_url")

    if not email or not EMAIL_RE.match(email):
        return jsonify({"error": "Please enter a valid email address."}), 400

    if next_url and not is_safe_redirect_url(next_url):
        next_url = None

    try:
        token = generate_magic_link_token(email, next_url)
        token_h = hash_token(token)
        expiry = datetime.utcnow() + timedelta(
            seconds=current_app.config.get("MAGIC_LINK_EXPIRY_SECONDS", 900)
        )

        user = User.query.filter_by(email=email).first()
        if user:
            user.magic_link_token_hash = token_h
            user.magic_link_expires_at = expiry
            db.session.commit()

        backend_url = request.host_url.rstrip("/")
        magic_link_url = f"{backend_url}/auth/magic-link/verify?token={token}"
        send_magic_link_email(email, magic_link_url)
    except Exception:
        logger.exception("Error in magic link send")
        # Fall through — always return the same message to prevent enumeration

    return jsonify({
        "message": "If that email is associated with an account, "
                   "you'll receive a sign-in link shortly. "
                   "If you're new, a link has been sent to create your account."
    }), 200


@auth_bp.route("/magic-link/verify")
def magic_link_verify():
    frontend_url = current_app.config.get("FRONTEND_URL", "http://localhost:3000")
    token = request.args.get("token", "")

    payload = verify_magic_link_token(token)
    if not payload:
        return redirect(f"{frontend_url}/login?error=invalid_or_expired")

    email = payload.get("email")
    next_url = payload.get("next_url")
    if not email:
        return redirect(f"{frontend_url}/login?error=invalid_or_expired")

    token_h = hash_token(token)

    user = User.query.filter_by(email=email).first()
    if user:
        # Check token hash matches (but don't clear it — let the token
        # remain valid until its natural expiry so that email-client
        # prefetch doesn't consume it before the user clicks)
        if user.magic_link_token_hash != token_h:
            return redirect(f"{frontend_url}/login?error=link_already_used")
    else:
        # New user — create account
        username = generate_unique_username(email)
        user = User(
            twitter_id=None,
            username=username,
            email=email,
            approved=False,
        )
        db.session.add(user)
        db.session.commit()

    login_user(user, remember=True)

    if next_url and is_safe_redirect_url(next_url):
        return redirect(f"{frontend_url}{next_url}")
    return redirect(f"{frontend_url}/dashboard")


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    # Drop flask-dance's stored X token as well. Left in the session, the
    # next "Sign in with X" in this browser skipped X and went straight
    # back into the account that just signed out (shared devices).
    _drop_x_token()
    flash("Logged out successfully", "success")
    frontend_url = current_app.config.get("FRONTEND_URL")
    return redirect(frontend_url)

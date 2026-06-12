import re
from datetime import datetime, timedelta

from flask import Blueprint, redirect, url_for, flash, current_app, request, jsonify
from flask_login import login_user, logout_user, login_required, current_user
from backend.models import User
from backend.extensions import db
from flask_dance.contrib.twitter import twitter
from flask import session
from backend.utils.magic_link import (
    generate_magic_link_token, verify_magic_link_token,
    hash_token, generate_unique_username,
)
from backend.utils.email import send_magic_link_email
from backend.utils.reserved_usernames import is_username_reserved
import logging
from urllib.parse import urlparse

logger = logging.getLogger(__name__)
auth_bp = Blueprint("auth_bp", __name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _derive_non_reserved_username(handle):
    """Derive a username that is neither reserved nor already taken.

    Appends an incrementing numeric suffix to the original handle until both
    the reserved check and the uniqueness check (case-insensitive, matching
    validate_username) pass. Used when a Twitter screen_name collides with a
    reserved name during OAuth signup.
    """
    # Brand-substring / founder-prefix reserved matches can't be escaped by
    # appending digits (e.g. 'myloore1' still contains 'loore'), so fall back
    # to the neutral 'user' base instead of suffixing forever.
    if is_username_reserved(f"{handle}1"):
        handle = "user"
    suffix = 1
    while True:
        candidate = f"{handle}{suffix}"
        if (not is_username_reserved(candidate)
                and not User.query.filter(
                    db.func.lower(User.username) == candidate.lower()
                ).first()):
            return candidate
        suffix += 1


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
            # create new user. If the Twitter handle collides with a reserved
            # name (brand/founder/system), derive a non-reserved, unique
            # fallback rather than 400-ing the OAuth callback.
            if is_username_reserved(username):
                username = _derive_non_reserved_username(username)
            user = User(twitter_id=twitter_id, username=username)
            db.session.add(user)
            db.session.commit()

    login_user(user, remember=True)
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
    flash("Logged out successfully", "success")
    frontend_url = current_app.config.get("FRONTEND_URL")
    return redirect(frontend_url)

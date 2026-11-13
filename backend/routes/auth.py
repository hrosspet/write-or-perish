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
from backend.utils.email import (
    send_magic_link_email, send_x_connected_notice, is_valid_email)
from backend.utils.reserved_usernames import derive_available_username
from backend.utils import referrals
import logging
from urllib.parse import urlparse, quote

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


def _confirm_email_flow_redirect(next_url):
    """Where to send a sign-in that would have to CREATE an account while
    the person is on their way to /confirm-email (#260), or None.

    The page behind the mailed confirmation link asks a signed-out visitor
    to sign in. The address in front of them is the one they are confirming,
    so typing it into the sign-in form is the natural mistake — and sign-in
    doubles as sign-up: it would create a second account that owns the
    address, and the account that asked could never bind it. Signing in on
    the way to a confirmation therefore never creates an account (by magic
    link or by X); an ordinary sign-up for the same address is untouched,
    so a pending change cannot be used to keep someone from signing up."""
    if (not next_url or not is_safe_redirect_url(next_url)
            or urlparse(next_url).path != "/confirm-email"):
        return None
    frontend_url = current_app.config.get("FRONTEND_URL", "")
    return redirect(f"{frontend_url}/login?error=confirm_needs_account"
                    f"&returnUrl={quote(next_url, safe='')}")


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
    needs that account's owner signed in: Connect X (_connect_x below).

    The handle only seeds the username; a taken or reserved handle gets a
    derived one. Two callbacks for the same X id can race here: the loser's
    insert fails on the unique X id, so re-read and use the winner's row.
    """
    for _attempt in range(2):
        user = User(twitter_id=twitter_id, twitter_handle=screen_name,
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


# Session key for a Connect X round trip in progress (#311): the id of the
# account that asked. flask-dance sends every X authorization back to
# /auth/login, so this is how that route tells "attach X to the signed-in
# account" from "sign in with X".
X_CONNECT_SESSION_KEY = "x_connect"


def _account_redirect(outcome):
    """Back to the Account page's X row with the outcome of Connect X."""
    frontend_url = current_app.config.get("FRONTEND_URL", "")
    return redirect(f"{frontend_url}/account?x_login={outcome}#x")


def _connect_x(user, twitter_id, screen_name):
    """Attach the X account that just authorized to ``user`` (#311).

    The same rules as a sign-in: the X id is the identity, the handle
    identifies nothing. An account keeps the X id it has, and an X id
    stays with the account that holds it. Pointing an X id at a second
    account would move that account's X sign-in away from its owner.
    A refused connect forgets the X token, so the next try asks X again
    (possibly for a different X account)."""
    if user.twitter_id == twitter_id:
        user.twitter_handle = screen_name
        db.session.commit()
        return _account_redirect("linked")
    if user.twitter_id is not None:
        _drop_x_token()
        return _account_redirect("other_x")
    holder = User.query.filter_by(twitter_id=twitter_id).first()
    if holder is not None:
        _drop_x_token()
        # An account set up ahead of its owner (admin whitelist, pre-fill)
        # that nobody has signed in to: the owner is the person in front of
        # us, and joining the two is a job for an admin, not an error.
        from backend.utils.activity import sign_in_status
        placeholder = sign_in_status(holder) == "never"
        return _account_redirect("taken_placeholder" if placeholder else "taken")
    # Conditional write: a second connect for this account (another tab)
    # may have attached an X id since the read above.
    try:
        written = (User.query
                   .filter(User.id == user.id, User.twitter_id.is_(None))
                   .update({"twitter_id": twitter_id,
                            "twitter_handle": screen_name,
                            "x_connected_at": datetime.utcnow()},
                           synchronize_session=False))
        db.session.commit()
    except IntegrityError:
        # Another account took this X id between the check and the write.
        db.session.rollback()
        _drop_x_token()
        return _account_redirect("taken")
    db.session.refresh(user)
    if not written and user.twitter_id != twitter_id:
        _drop_x_token()
        return _account_redirect("other_x")
    logger.info("Connected X id %s to user %s", twitter_id, user.id)
    if written and user.email:
        # A new way into the account: tell its address, as an email change
        # tells the old one, in case the session was not the owner's.
        send_x_connected_notice(user.email, screen_name)
    return _account_redirect("linked")


def _pop_connect_intent():
    """Whether this return from X is a Connect X round trip.

    It counts only for the account that started it and is still signed
    in. Any other intent is stale (abandoned at X, then signed out, or
    another account signed in since): it is dropped here, once, and the
    request is an ordinary X sign-in.

    No time limit. For the account that started it, a connect must never
    turn into a sign-in: that makes the duplicate account #311 is about,
    or moves the browser into whichever account holds the X id. And a
    limit would protect nothing: the callback must come from this
    session's own authorization at X (backend/oauth.py), so only the
    person in this browser can complete it, however long they take."""
    intent = session.pop(X_CONNECT_SESSION_KEY, None)
    if intent is None:
        return False
    if current_user.is_authenticated and intent.get("user_id") == current_user.id:
        return True
    logger.info("Dropped a stale Connect X intent")
    return False


def refuse_x_callback():
    """Response for an X callback this browser session did not start
    (backend/oauth.py checks the request token before flask-dance
    exchanges it). Nothing is stored and a pending Connect X is dropped."""
    if _pop_connect_intent():
        return _account_redirect("failed")
    frontend_url = current_app.config.get("FRONTEND_URL", "")
    return redirect(f"{frontend_url}/login?error=x_try_again")


@auth_bp.route("/x/connect")
def x_connect():
    """Start Connect X for the signed-in account (#311): the X login for
    someone who already has an account (magic-link signup). "Sign in with
    X" cannot do it: signed out, an unknown X id gets a new account.

    Not @login_required: the unauthorized handler sends a signed-out
    browser to /auth/login, which is an X sign-in and would create one."""
    if not current_user.is_authenticated:
        frontend_url = current_app.config.get("FRONTEND_URL", "")
        return redirect(f"{frontend_url}/login?returnUrl="
                        f"{quote('/account', safe='')}")
    session[X_CONNECT_SESSION_KEY] = {"user_id": current_user.id}
    # A token left from an earlier X sign-in in this browser would skip X
    # and connect whichever X account that was; ask X which one instead.
    _drop_x_token()
    return redirect(url_for("twitter.login"))


@auth_bp.route("/login")
def login():
    # Capture the 'next' parameter for post-login redirect
    next_url = request.args.get('next')
    if next_url and is_safe_redirect_url(next_url):
        session['next_url'] = next_url

    connecting = _pop_connect_intent()
    # An invite code (#439) waits in the session through the round trip
    # to X (OAuth comes back to the browser that started it); it counts
    # when the X account that comes back is the one the invite names.
    invite = referrals.clean_code(request.args.get('invite'))
    if invite and not connecting:
        session[referrals.SESSION_KEY] = invite

    if not twitter.authorized:
        if connecting:
            # Connect X goes straight to X, never through here; arriving
            # here without a token means X sent the person back without one
            # (they cancelled). Starting over would open X's page again.
            return _account_redirect("cancelled")
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
        if connecting:
            return _account_redirect("failed")
        flash("Failed to fetch user info from Twitter.", "error")
        # Redirect to frontend instead of non-existent 'index' route
        return redirect(current_app.config.get('FRONTEND_URL', '/'))
    tw_info = resp.json()
    twitter_id = str(tw_info["id"])
    username = tw_info["screen_name"]
    if connecting:
        return _connect_x(current_user._get_current_object(), twitter_id, username)
    user = User.query.filter_by(twitter_id=twitter_id).first()
    new_account = user is None
    invite = session.pop(referrals.SESSION_KEY, None)
    if not user:
        refused = _confirm_email_flow_redirect(session.get('next_url'))
        if refused is not None:
            # Not the X account they meant, or an account that signs in by
            # email: forget this X token so the next try goes through X.
            session.pop('next_url', None)
            _drop_x_token()
            return refused
    if _counts_as_new(user):
        # An invite made for someone else (#439): ask first; no account
        # is made until they choose to go on without it.
        kind = referrals.invite_mismatch(invite, x_handle=username,
                                         x_id=twitter_id)
        if kind:
            return _ask_about_invite(kind, {
                "method": "x", "twitter_id": twitter_id,
                "username": username})
    if not user:
        user = _create_x_user(twitter_id, username)
    return redirect(_finish_x_sign_in(user, new_account, invite, username))


def _counts_as_new(user):
    """A sign-in that makes the account, or the first one into an account
    set up ahead of its owner (admin whitelist, pre-fill): the sign-ups an
    invite applies to."""
    if user is None:
        return True
    if user.deleted_at is not None:
        return False
    from backend.utils.activity import sign_in_status
    return sign_in_status(user) == "never"


# An invite followed, then a sign-in as someone it was not made for
# (#439; Peter, 2026-11-13: "They should know the invitation was for
# somebody else. They should still have the option to sign up without the
# invitation, thus also without reward"). The proven identity waits in
# this browser's session, for as long as a sign-in link lasts, until the
# person chooses: sign in differently (nothing was made) or continue
# without the invitation (the account is made then, with no inviter).
INVITE_CHOICE_KEY = "invite_choice"


def _ask_about_invite(kind, pending):
    pending["next_url"] = session.pop('next_url', None)
    pending["at"] = datetime.utcnow().timestamp()
    session[INVITE_CHOICE_KEY] = pending
    frontend_url = current_app.config.get("FRONTEND_URL", "")
    return redirect(f"{frontend_url}/invite-mismatch?for={kind}")


def _finish_x_sign_in(user, new_account, invite, username):
    """The rest of an X sign-in, once the account exists: the URL to send
    the browser to."""
    if user.deleted_at is not None:
        # A deleted account in its grace period (#269): no sign-in; the
        # browser is asked whether to restore it.
        from backend.routes.account_deletion import offer_restore
        return offer_restore(user, session.pop('next_url', None))
    # Handles change on X; keep the one the Account page shows current
    # (an unchanged value writes nothing).
    user.twitter_handle = username
    db.session.commit()

    # Who invited this account (#439), before the sign-in is recorded: an
    # account set up ahead of its owner counts as new until then. Only an
    # invite made for this X account counts (its id; its handle when the
    # invite's lookup was down).
    referrals.redeem_invite(user, invite, new_account, x_handle=username,
                            x_id=user.twitter_id)
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
        return f"{frontend_url}{next_url}"
    return f"{frontend_url}/dashboard"


@auth_bp.route("/magic-link/send", methods=["POST"])
def magic_link_send():
    data = request.get_json() or {}
    email = (data.get("email") or "").strip().lower()
    next_url = data.get("next_url")
    # An invite code (#439) rides in the signed link. It counts only for
    # an invite made for this address, so it does whichever browser or
    # device opens the link. Nothing in the answer depends on it.
    invite = referrals.clean_code(data.get("invite"))

    if not is_valid_email(email):
        return jsonify({"error": "Please enter a valid email address."}), 400

    if next_url and not is_safe_redirect_url(next_url):
        next_url = None

    try:
        token = generate_magic_link_token(email, next_url, invite=invite)
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
        send_magic_link_email(email, magic_link_url,
                              user_id=user.id if user else None)
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
    # The invite in the link (#439) counts only when it was made for this
    # address (utils/referrals.redeem_invite), in any browser.
    invite = payload.get("invite")

    user = User.query.filter_by(email=email).first()
    if user:
        # Check token hash matches (but don't clear it — let the token
        # remain valid until its natural expiry so that email-client
        # prefetch doesn't consume it before the user clicks)
        if user.magic_link_token_hash != token_h:
            return redirect(f"{frontend_url}/login?error=link_already_used")
        if user.deleted_at is not None:
            # A deleted account in its grace period (#269): no sign-in;
            # the browser is asked whether to restore it.
            from backend.routes.account_deletion import offer_restore
            return redirect(offer_restore(
                user, next_url if next_url and is_safe_redirect_url(next_url)
                else None))
    else:
        refused = _confirm_email_flow_redirect(next_url)
        if refused is not None:
            return refused
    if _counts_as_new(user):
        # An invite made for someone else (#439): ask first; no account
        # is made until they choose to go on without it.
        kind = referrals.invite_mismatch(invite, email=email)
        if kind:
            session['next_url'] = next_url
            return _ask_about_invite(kind, {"method": "email",
                                            "email": email})
    return redirect(_finish_email_sign_in(user, email, next_url, invite))


def _finish_email_sign_in(user, email, next_url, invite):
    """The rest of an email sign-in: the account made if there is none,
    the invite recorded, the session started. The URL to go to."""
    frontend_url = current_app.config.get("FRONTEND_URL", "http://localhost:3000")
    new_account = user is None
    if user is None:
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

    # Who invited this account (#439); approval is untouched.
    referrals.redeem_invite(user, invite, new_account, email=email)
    login_user(user, remember=True)

    if next_url and is_safe_redirect_url(next_url):
        return f"{frontend_url}{next_url}"
    return f"{frontend_url}/dashboard"


@auth_bp.route("/invite-choice/continue", methods=["POST"])
def invite_choice_continue():
    """Go on without the invitation (#439): finish the sign-in that was
    held, with no inviter; the invite stays open for the person it names.
    410 when there is nothing held, or it waited longer than a sign-in
    link lasts. {"redirect": url}. 404 while referrals are off."""
    if not referrals.referrals_enabled():
        return jsonify({"error": "Not found"}), 404
    pending = session.pop(INVITE_CHOICE_KEY, None)
    max_age = current_app.config.get("MAGIC_LINK_EXPIRY_SECONDS", 900)
    if (not isinstance(pending, dict)
            or datetime.utcnow().timestamp() - (pending.get("at") or 0)
            > max_age):
        if isinstance(pending, dict) and pending.get("method") == "x":
            _drop_x_token()
        return jsonify({"error": "This sign-in has expired. Please sign "
                                 "in again."}), 410
    next_url = pending.get("next_url")
    if pending.get("method") == "email":
        email = pending.get("email")
        user = User.query.filter_by(email=email).first()
        if user is not None and user.deleted_at is not None:
            from backend.routes.account_deletion import offer_restore
            return jsonify({"redirect": offer_restore(
                user, next_url if is_safe_redirect_url(next_url) else None)})
        return jsonify({"redirect": _finish_email_sign_in(
            user, email, next_url, None)})
    twitter_id = pending.get("twitter_id")
    username = pending.get("username")
    user = User.query.filter_by(twitter_id=twitter_id).first()
    new_account = user is None
    if user is None:
        user = _create_x_user(twitter_id, username)
    session['next_url'] = next_url
    return jsonify({"redirect": _finish_x_sign_in(
        user, new_account, None, username)})


@auth_bp.route("/invite-choice/cancel", methods=["POST"])
def invite_choice_cancel():
    """Sign in differently (#439): forget the held sign-in. No account was
    made; the X sign-in's token is dropped so the next one asks X which
    account. The invite code stays in the browser for the right one. 404
    while referrals are off."""
    if not referrals.referrals_enabled():
        return jsonify({"error": "Not found"}), 404
    pending = session.pop(INVITE_CHOICE_KEY, None)
    if isinstance(pending, dict) and pending.get("method") == "x":
        _drop_x_token()
    return jsonify({"ok": True})


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    # Drop flask-dance's stored X token as well. Left in the session, the
    # next "Sign in with X" in this browser skipped X and went straight
    # back into the account that just signed out (shared devices).
    _drop_x_token()
    session.pop(X_CONNECT_SESSION_KEY, None)
    flash("Logged out successfully", "success")
    frontend_url = current_app.config.get("FRONTEND_URL")
    # "Sign out and use the other account" on /confirm-email comes back to
    # the confirmation, which then asks for the right sign-in.
    next_url = request.args.get("next")
    if next_url and is_safe_redirect_url(next_url):
        return redirect(f"{frontend_url}{next_url}")
    return redirect(frontend_url)

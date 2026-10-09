"""The user's own account deletion (#269), and the restore offered when a
deleted account signs in during its grace period.

- POST /api/account/delete {"confirm": "<username>"}: an account with an
  email address gets a confirmation link by email; one without is
  scheduled at once, and the session ends.
- POST /api/account/delete/confirm {"token"}: the link's page, inside a
  signed-in session of the same account (as the email change of #260):
  schedules the deletion and ends the session.
- Scheduling (either route) also revokes at X the "Sign in with X" token
  this session holds, if it is the account's, and drops it from the
  session; best effort, it never stops the deletion.
- GET / POST /api/account/restore and POST /api/account/restore/decline:
  the question a sign-in into a deleted account leads to. The offer is
  in the browser's session (set by routes/auth.py), not a sign-in: the
  account stays signed out until the person chooses to restore it.

The work is in backend/utils/account_deletion.py.
"""
import logging
import time

from flask import Blueprint, current_app, jsonify, request, session
from flask_login import current_user, login_required, login_user, logout_user

from backend.extensions import db
from backend.models import User
from backend.utils.account_deletion import (
    ACCOUNT_DELETION_GRACE_DAYS, DELETION_LINK_SECONDS, RESTORE_OFFER_SECONDS,
    RESTORE_SESSION_KEY, AccountDeletionRefused, check_confirmation,
    deletion_refusal, end_x_sign_in, format_day, issue_confirmation_link,
    restore_account, restore_offer, schedule_account_deletion,
)
from backend.utils.timefmt import iso_utc

logger = logging.getLogger(__name__)

account_deletion_bp = Blueprint("account_deletion_bp", __name__)

_LINK_INVALID = ("This confirmation link is no longer valid. Ask for the "
                 "deletion again on the Account page.")


def _json_object():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _refused(e):
    status = 409 if e.code in ("last_admin", "already_running") else 403
    return jsonify({"error": e.message, "code": e.code}), status


def _schedule_and_sign_out(user):
    """Schedule the user's own deletion, end this session, mail the
    confirmation. Returns the JSON response."""
    job = schedule_account_deletion(
        user, requested_by_id=user.id, source="self")
    email, username, due = user.email, user.username, job.scheduled_for
    # The X sign-in this session holds is revoked now (best effort); the
    # stored bookmark connection is revoked by the purge after 30 days.
    end_x_sign_in(user)
    logout_user()
    session.pop(RESTORE_SESSION_KEY, None)
    if email:
        from backend.utils.email import send_account_deletion_scheduled_email
        send_account_deletion_scheduled_email(email, username, format_day(due))
    return jsonify({"status": "scheduled", "delete_on": iso_utc(due),
                    "grace_days": ACCOUNT_DELETION_GRACE_DAYS}), 202


@account_deletion_bp.route("/delete", methods=["POST"])
@login_required
def request_account_deletion():
    """Start the deletion of the signed-in account. Body:
    {"confirm": "<username>"}, typed by the user in the dialog."""
    typed = (_json_object().get("confirm") or "")
    typed = typed.strip() if isinstance(typed, str) else ""
    if not typed or typed.lower() != (current_user.username or "").lower():
        return jsonify({"error": "Type your username to confirm.",
                        "code": "confirm_mismatch"}), 400
    user = current_user._get_current_object()
    refusal = deletion_refusal(user)
    if refusal:
        return _refused(AccountDeletionRefused(*refusal))

    if not user.email:
        # No address to confirm from: the typed username is the
        # confirmation (an X sign-in account).
        try:
            return _schedule_and_sign_out(user)
        except AccountDeletionRefused as e:
            return _refused(e)

    token = issue_confirmation_link(user)
    frontend_url = current_app.config.get("FRONTEND_URL", "")
    url = f"{frontend_url}/confirm-account-deletion?token={token}"
    try:
        from backend.utils.email import send_account_deletion_link_email
        send_account_deletion_link_email(
            user.email, user.username, url, DELETION_LINK_SECONDS,
            ACCOUNT_DELETION_GRACE_DAYS)
    except Exception:  # noqa: BLE001 - logged by the mailer
        user.account_deletion_token_hash = None
        user.account_deletion_expires_at = None
        db.session.commit()
        return jsonify({"error": "Could not send the confirmation email. "
                                 "Please try again.",
                        "code": "email_failed"}), 502
    logger.info("User %s asked to delete the account; link sent", user.id)
    return jsonify({"status": "confirm_email",
                    "expires_in": DELETION_LINK_SECONDS}), 202


@account_deletion_bp.route("/delete/confirm", methods=["POST"])
@login_required
def confirm_account_deletion():
    """The emailed link's page confirms. Only inside a session of the
    account the link was sent for; a POST from the app, so mail scanners
    that fetch the link change nothing."""
    user = current_user._get_current_object()
    reason = check_confirmation(user, _json_object().get("token"))
    if reason == "other_account":
        return jsonify({
            "error": "This link was sent for a different Loore account. "
                     "Sign in to that account to use it.",
            "reason": "other_account"}), 403
    if reason:
        return jsonify({"error": _LINK_INVALID, "reason": reason}), 400
    try:
        return _schedule_and_sign_out(user)
    except AccountDeletionRefused as e:
        return _refused(e)


# ── Restore ─────────────────────────────────────────────────────────────

def _restore_user():
    """The deleted account this browser just signed in to, while the
    offer is fresh; None otherwise (the offer is dropped)."""
    offer = session.get(RESTORE_SESSION_KEY)
    if not isinstance(offer, dict):
        return None
    if time.time() - float(offer.get("at") or 0) > RESTORE_OFFER_SECONDS:
        session.pop(RESTORE_SESSION_KEY, None)
        return None
    user_id = offer.get("user_id")
    user = db.session.get(User, user_id) if isinstance(user_id, int) else None
    if user is None or user.deleted_at is None:
        session.pop(RESTORE_SESSION_KEY, None)
        return None
    return user


def offer_restore(user, next_url=None):
    """Called by the sign-in routes instead of login_user for a deleted
    account in its grace period. Returns the frontend URL to send the
    browser to."""
    session[RESTORE_SESSION_KEY] = {
        "user_id": user.id, "at": time.time(), "next": next_url}
    frontend_url = current_app.config.get("FRONTEND_URL", "")
    return f"{frontend_url}/account-restore"


@account_deletion_bp.route("/restore", methods=["GET"])
def get_restore_offer():
    user = _restore_user()
    if user is None:
        return jsonify({"error": "Nothing to restore here. Sign in again.",
                        "code": "no_offer"}), 404
    return jsonify(restore_offer(user)), 200


@account_deletion_bp.route("/restore", methods=["POST"])
def restore():
    """Restore the deleted account and sign in to it."""
    user = _restore_user()
    if user is None:
        return jsonify({"error": "Nothing to restore here. Sign in again.",
                        "code": "no_offer"}), 404
    if not restore_account(user):
        return jsonify({"error": "The deletion has already started, so the "
                                 "account can no longer be restored.",
                        "code": "already_started"}), 409
    offer = session.pop(RESTORE_SESSION_KEY, None) or {}
    login_user(user, remember=True)
    from backend.utils.activity import touch_last_seen
    touch_last_seen(user, None)
    nxt = offer.get("next")
    from backend.routes.auth import is_safe_redirect_url
    return jsonify({"status": "restored",
                    "next": nxt if nxt and is_safe_redirect_url(nxt) else "/"}), 200


@account_deletion_bp.route("/restore/decline", methods=["POST"])
def decline_restore():
    """Keep the account deleted: forget the offer, stay signed out."""
    session.pop(RESTORE_SESSION_KEY, None)
    return jsonify({"status": "declined"}), 200

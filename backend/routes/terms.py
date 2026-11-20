import logging
from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user
from backend.extensions import db
from backend.utils.email import (
    send_admin_signup_notification, send_admin_signup_info)
from backend.utils.approval import (
    SIGNUP_NOTICE_INFO, claim_signup_notice, signup_notice_due)
from backend.utils.timefmt import iso_utc
from datetime import datetime

logger = logging.getLogger(__name__)

terms_bp = Blueprint("terms_bp", __name__)

CURRENT_TERMS_VERSION = "2.0"


@terms_bp.route("/accept", methods=["POST"])
@login_required
def accept_terms():
    # Decided before the timestamp is set: only a first acceptance is a
    # signup (#715), not the one after a terms update.
    notice = None if current_user.spam else signup_notice_due(current_user)
    current_user.accepted_terms_at = datetime.utcnow()
    current_user.accepted_terms_version = CURRENT_TERMS_VERSION
    db.session.commit()

    # Payments off: an account waiting for approval asks for it. Payments
    # on: an information email, no approval request (#715). Either one only
    # while an admin has "Email me about new signups" on, and once per
    # account (the claim survives a retry).
    if notice and claim_signup_notice(current_user):
        try:
            if notice == SIGNUP_NOTICE_INFO:
                send_admin_signup_info(current_user)
            else:
                send_admin_signup_notification(
                    current_user.username, current_user.email
                )
        except Exception:
            logger.exception("Failed to send admin signup notification")

    return jsonify({
        "message": "Terms accepted",
        "accepted_terms_at": iso_utc(current_user.accepted_terms_at),
        "accepted_terms_version": current_user.accepted_terms_version
    }), 200

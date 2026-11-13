import logging
from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user
from backend.extensions import db
from backend.utils.email import send_admin_signup_notification
from backend.utils.approval import signup_notice
from backend.utils.timefmt import iso_utc
from datetime import datetime

logger = logging.getLogger(__name__)

terms_bp = Blueprint("terms_bp", __name__)

CURRENT_TERMS_VERSION = "2.0"


@terms_bp.route("/accept", methods=["POST"])
@login_required
def accept_terms():
    first_acceptance = current_user.accepted_terms_at is None
    current_user.accepted_terms_at = datetime.utcnow()
    current_user.accepted_terms_version = CURRENT_TERMS_VERSION
    db.session.commit()

    # Payments off: an account waiting for approval. Payments on (#697):
    # a new account's first acceptance, for information; nobody waits.
    notice = signup_notice(current_user, first_acceptance)
    if notice:
        try:
            send_admin_signup_notification(
                current_user.username, current_user.email,
                let_in=notice == "let_in",
            )
        except Exception:
            logger.exception("Failed to send admin signup notification")

    return jsonify({
        "message": "Terms accepted",
        "accepted_terms_at": iso_utc(current_user.accepted_terms_at),
        "accepted_terms_version": current_user.accepted_terms_version
    }), 200

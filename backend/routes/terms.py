import logging
from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user
from backend.extensions import db
from backend.utils.email import send_admin_signup_notification
from backend.utils.timefmt import iso_utc
from datetime import datetime

logger = logging.getLogger(__name__)

terms_bp = Blueprint("terms_bp", __name__)

# Must equal "version" in frontend/src/legal/terms.json, the one source of
# the Terms text (#440; test_terms_version.py checks it). A new version asks
# every user to accept the Terms again.
CURRENT_TERMS_VERSION = "3.0"

# The client accepted a text other than the current one (#440).
TERMS_VERSION_MISMATCH = "terms_version_mismatch"


@terms_bp.route("/accept", methods=["POST"])
@login_required
def accept_terms():
    # The client says which version it showed (web: terms.json's
    # "version", iPhone: TermsText.version). Only the current one is
    # recorded: an older page or app build that showed older Terms, or
    # sends no version at all, records nothing and is told to load the
    # current text.
    body = request.get_json(silent=True)
    shown = body.get("version") if isinstance(body, dict) else None
    if shown != CURRENT_TERMS_VERSION:
        return jsonify({
            "error": TERMS_VERSION_MISMATCH,
            "message": ("The Terms have changed since this screen loaded. "
                        "Load the current Terms to read and accept them."),
            "current_version": CURRENT_TERMS_VERSION,
        }), 409

    current_user.accepted_terms_at = datetime.utcnow()
    current_user.accepted_terms_version = CURRENT_TERMS_VERSION
    db.session.commit()

    if not current_user.approved:
        try:
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

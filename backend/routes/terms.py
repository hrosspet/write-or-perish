from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user
from backend.extensions import db
from datetime import datetime

terms_bp = Blueprint("terms_bp", __name__)

@terms_bp.route("/accept", methods=["POST"])
@login_required
def accept_terms():
    # Save the current UTC time – this can be compared with a version if you later change your terms.
    current_user.accepted_terms_at = datetime.utcnow()
    db.session.commit()
    return jsonify({
        "message": "Terms accepted",
        "accepted_terms_at": current_user.accepted_terms_at.isoformat()
    }), 200
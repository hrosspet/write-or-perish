from functools import wraps
from flask import Blueprint, request, jsonify, abort
from flask_login import login_required, current_user
from backend.models import User
from backend.extensions import db

admin_bp = Blueprint("admin_bp", __name__)

# Decorator to check that the current user is the admin (username: "hrosspet")
def admin_required(func):
    @wraps(func)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.username != "hrosspet":
            abort(403)  # Forbidden if not admin
        return func(*args, **kwargs)
    return decorated_function

@admin_bp.route("/users", methods=["GET"])
@login_required
@admin_required
def list_users():
    users = User.query.order_by(User.created_at.desc()).all()
    user_list = []
    for user in users:
        user_list.append({
            "id": user.id,
            "twitter_id": user.twitter_id,
            "username": user.username,
            "description": user.description,
            "created_at": user.created_at.isoformat(),
            "accepted_terms_at": user.accepted_terms_at.isoformat() if user.accepted_terms_at else None,
            "approved": user.approved,
            "email": user.email
        })
    return jsonify({"users": user_list}), 200

@admin_bp.route("/users/<int:user_id>/toggle", methods=["POST"])
@login_required
@admin_required
def toggle_user_status(user_id):
    user = User.query.get_or_404(user_id)
    user.approved = not user.approved  # Toggle the approved flag
    db.session.commit()
    return jsonify({"message": "User status updated", "approved": user.approved}), 200

@admin_bp.route("/users/<int:user_id>/update_email", methods=["PUT"])
@login_required
@admin_required
def update_user_email(user_id):
    data = request.get_json()
    email = data.get("email")
    if email is None:
        return jsonify({"error": "Email is required"}), 400
    user = User.query.get_or_404(user_id)
    user.email = email
    db.session.commit()
    return jsonify({"message": "Email updated", "email": user.email}), 200

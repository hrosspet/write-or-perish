import logging
from datetime import datetime, timedelta
from functools import wraps
from flask import Blueprint, request, jsonify, abort, current_app
from flask_login import login_required, current_user
from backend.models import User, APICostLog
from backend.extensions import db
from backend.utils.timefmt import iso_utc
from sqlalchemy import func
from backend.utils.magic_link import generate_magic_link_token, hash_token
from backend.utils.email import send_welcome_email

logger = logging.getLogger(__name__)

admin_bp = Blueprint("admin_bp", __name__)

# Decorator to check that the current user is the admin (placeholder check by username)
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

    # Aggregate total spending per user in a single query
    spending_rows = db.session.query(
        APICostLog.user_id,
        func.sum(APICostLog.cost_microdollars).label("total_microdollars")
    ).group_by(APICostLog.user_id).all()
    spending_map = {row.user_id: row.total_microdollars for row in spending_rows}

    user_list = []
    for user in users:
        total_microdollars = spending_map.get(user.id, 0) or 0
        user_list.append({
            "id": user.id,
            "twitter_id": user.twitter_id,
            "username": user.username,
            "description": user.description,
            "created_at": iso_utc(user.created_at),
            "accepted_terms_at": iso_utc(user.accepted_terms_at),
            "approved": user.approved,
            "email": user.email,
            "plan": user.plan,
            "deactivated_at": iso_utc(user.deactivated_at),
            "total_spending_usd": total_microdollars / 1_000_000,
        })
    return jsonify({
        "users": user_list,
        "allowed_plans": sorted(User.ALLOWED_PLANS)
    }), 200

@admin_bp.route("/users/<int:user_id>/toggle", methods=["POST"])
@login_required
@admin_required
def toggle_user_status(user_id):
    user = User.query.get_or_404(user_id)
    user.approved = not user.approved  # Toggle the approved flag
    if not user.approved:
        # Record deactivation time (terms acceptance fields are preserved for audit)
        user.deactivated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({"message": "User status updated", "approved": user.approved}), 200

@admin_bp.route("/users/<int:user_id>/update_email", methods=["PUT"])
@login_required
@admin_required
def update_user_email(user_id):
    data = request.get_json()
    email = data.get("email")
    if email is None:
        return jsonify({"error": "Email is required."}), 400
    user = User.query.get_or_404(user_id)
    user.email = email
    db.session.commit()
    return jsonify({"message": "Email updated", "email": user.email}), 200

@admin_bp.route("/users/<int:user_id>/update_plan", methods=["PUT"])
@login_required
@admin_required
def update_user_plan(user_id):
    data = request.get_json()
    plan = data.get("plan")
    if plan not in User.ALLOWED_PLANS:
        return jsonify({"error": f"Invalid plan. Allowed: {sorted(User.ALLOWED_PLANS)}"}), 400
    user = User.query.get_or_404(user_id)
    user.plan = plan
    db.session.commit()
    return jsonify({"message": "Plan updated", "plan": user.plan}), 200

# New endpoint: Whitelist a user by handle.
@admin_bp.route("/whitelist", methods=["POST"])
@login_required
@admin_required
def whitelist_user():
    data = request.get_json() or {}
    handle = data.get("handle", "").strip()
    if not handle:
        return jsonify({"error": "Handle is required."}), 400

    # Check if a user with that handle already exists.
    if User.query.filter_by(username=handle).first():
        return jsonify({"error": "User with that handle already exists."}), 400

    # Create a new user with the handle
    user = User(twitter_id=None, username=handle, approved=True)
    db.session.add(user)
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "DB error", "details": str(e)}), 500
    return jsonify({
        "message": "User whitelisted successfully.",
        "user": {
            "id": user.id,
            "username": user.username,
            "twitter_id": user.twitter_id,
            "approved": user.approved,
            "accepted_terms_at": user.accepted_terms_at,  # Will be null.
            "email": user.email
        }
    }), 201


@admin_bp.route("/users/<int:user_id>/activate_and_welcome", methods=["POST"])
@login_required
@admin_required
def activate_and_welcome(user_id):
    user = User.query.get_or_404(user_id)

    if not user.email:
        return jsonify({"error": "User has no email address. Add one first."}), 400

    # Approve the user
    user.approved = True
    db.session.commit()

    # Generate magic link pointing to /welcome (30-day expiry)
    welcome_max_age = 30 * 24 * 3600  # 30 days in seconds
    try:
        token = generate_magic_link_token(
            user.email, next_url="/welcome", max_age=welcome_max_age
        )
        token_h = hash_token(token)

        user.magic_link_token_hash = token_h
        user.magic_link_expires_at = (
            datetime.utcnow() + timedelta(seconds=welcome_max_age)
        )
        db.session.commit()

        backend_url = request.host_url.rstrip("/")
        magic_link_url = f"{backend_url}/auth/magic-link/verify?token={token}"

        send_welcome_email(user.email, magic_link_url)
    except Exception:
        logger.exception(
            f"Failed to send welcome email to user {user_id} ({user.email})"
        )
        return jsonify({
            "message": "User approved but welcome email failed to send.",
            "approved": True,
            "email_sent": False,
        }), 200

    return jsonify({
        "message": "User approved and welcome email sent.",
        "approved": True,
        "email_sent": True,
    }), 200


@admin_bp.route("/feedback", methods=["GET"])
@login_required
@admin_required
def list_feedback():
    """List user feedback submitted via the LLM tool (issue #158)."""
    from backend.models import UserFeedback

    status = request.args.get("status")
    query = UserFeedback.query
    if status:
        query = query.filter_by(status=status)
    items = query.order_by(UserFeedback.created_at.desc()).limit(500).all()

    return jsonify({"feedback": [
        {
            "id": f.id,
            "user_id": f.user_id,
            "username": f.user.username if f.user else None,
            "content": f.get_content(),
            "category": f.category,
            "source": f.source,
            "status": f.status,
            "created_at": iso_utc(f.created_at),
        }
        for f in items
    ]}), 200


@admin_bp.route("/feedback/<int:feedback_id>", methods=["PUT"])
@login_required
@admin_required
def update_feedback_status(feedback_id):
    from backend.models import UserFeedback

    feedback = UserFeedback.query.get_or_404(feedback_id)
    status = (request.get_json() or {}).get("status")
    if status not in ("new", "reviewed", "done"):
        return jsonify({"error": "Invalid status"}), 400
    feedback.status = status
    db.session.commit()
    return jsonify({"id": feedback.id, "status": feedback.status}), 200

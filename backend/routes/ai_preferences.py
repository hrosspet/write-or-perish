from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user
from backend.models import UserAIPreferences
from backend.extensions import db

ai_preferences_bp = Blueprint("ai_preferences", __name__)


@ai_preferences_bp.route("/", methods=["GET"])
@login_required
def get_ai_preferences():
    """Get the latest AI preferences for the current user."""
    prefs = UserAIPreferences.query.filter_by(
        user_id=current_user.id
    ).order_by(UserAIPreferences.created_at.desc()).first()

    if not prefs:
        return jsonify({"ai_preferences": None}), 200

    version_count = UserAIPreferences.query.filter_by(
        user_id=current_user.id
    ).count()

    return jsonify({
        "ai_preferences": {
            "id": prefs.id,
            "content": prefs.get_content(),
            "generated_by": prefs.generated_by,
            "tokens_used": prefs.tokens_used,
            "created_at": prefs.created_at.isoformat(),
            "privacy_level": prefs.privacy_level,
            "ai_usage": prefs.ai_usage,
            "version_number": version_count,
        }
    }), 200


@ai_preferences_bp.route("/", methods=["PUT"])
@login_required
def update_ai_preferences():
    """Create a new AI preferences version."""
    data = request.get_json()
    content = data.get("content")
    generated_by = data.get("generated_by", "user")

    if content is None:
        return jsonify({"error": "Content is required"}), 400
    if not content.strip():
        return jsonify({"error": "Content cannot be empty"}), 400

    prefs = UserAIPreferences(
        user_id=current_user.id,
        generated_by=generated_by,
        tokens_used=data.get("tokens_used", 0),
    )
    prefs.set_content(content)
    db.session.add(prefs)
    db.session.commit()

    version_count = UserAIPreferences.query.filter_by(
        user_id=current_user.id
    ).count()

    return jsonify({
        "ai_preferences": {
            "id": prefs.id,
            "content": prefs.get_content(),
            "generated_by": prefs.generated_by,
            "tokens_used": prefs.tokens_used,
            "created_at": prefs.created_at.isoformat(),
            "version_number": version_count,
        }
    }), 200


@ai_preferences_bp.route("/versions", methods=["GET"])
@login_required
def get_ai_preferences_versions():
    """List all AI preferences versions for the current user."""
    all_prefs = UserAIPreferences.query.filter_by(
        user_id=current_user.id
    ).order_by(UserAIPreferences.created_at.desc()).all()

    versions = []
    total = len(all_prefs)
    for i, prefs in enumerate(all_prefs):
        versions.append({
            "id": prefs.id,
            "generated_by": prefs.generated_by,
            "tokens_used": prefs.tokens_used,
            "created_at": prefs.created_at.isoformat(),
            "version_number": total - i,
        })

    return jsonify({"versions": versions}), 200


@ai_preferences_bp.route("/versions/<int:version_id>", methods=["GET"])
@login_required
def get_ai_preferences_version(version_id):
    """Get a specific AI preferences version's content."""
    prefs = UserAIPreferences.query.get_or_404(version_id)

    if prefs.user_id != current_user.id:
        return jsonify({"error": "Unauthorized"}), 403

    return jsonify({
        "ai_preferences": {
            "id": prefs.id,
            "content": prefs.get_content(),
            "generated_by": prefs.generated_by,
            "tokens_used": prefs.tokens_used,
            "created_at": prefs.created_at.isoformat(),
        }
    }), 200

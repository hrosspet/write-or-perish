import re

from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user
from backend.models import Node, User, UserProfile
from backend.extensions import db
from backend.utils.privacy import (
    accessible_nodes_filter, VALID_PRIVACY_LEVELS, VALID_AI_USAGE,
)
from backend.routes.terms import CURRENT_TERMS_VERSION

dashboard_bp = Blueprint("dashboard_bp", __name__)


def _terms_up_to_date(user):
    if user.accepted_terms_version != CURRENT_TERMS_VERSION:
        return False
    if user.deactivated_at and (
        not user.accepted_terms_at or user.deactivated_at > user.accepted_terms_at
    ):
        return False
    return True

def get_latest_profile(user):
    """Get the most recent profile for a user, or None if no profile exists."""
    profile = UserProfile.query.filter_by(user_id=user.id).order_by(UserProfile.created_at.desc()).first()
    if profile:
        return {
            "id": profile.id,
            "content": profile.get_content(),
            "generated_by": profile.generated_by,
            "tokens_used": profile.tokens_used,
            "created_at": profile.created_at.isoformat(),
            "source_tokens_used": profile.source_tokens_used,
            "source_data_cutoff": (
                profile.source_data_cutoff.isoformat()
                if profile.source_data_cutoff else None
            ),
            "generation_type": profile.generation_type,
        }
    return None


def _serialize_node_for_list(node):
    """Serialize a node for dashboard/feed list views."""
    # If this is a system prompt root, skip to the first child
    display_node = node
    prompt_key = None
    if node.user_prompt_id is not None:
        prompt_key = node.user_prompt.prompt_key if node.user_prompt else None
        first_child = Node.query.filter_by(parent_id=node.id).order_by(Node.created_at.asc()).first()
        if first_child:
            display_node = first_child

    content = display_node.get_content()
    preview = content[:200] + ("..." if len(content) > 200 else "")

    # Determine human owner username for LLM nodes
    human_owner_username = None
    if display_node.node_type == "llm" and display_node.human_owner_id:
        human_owner = User.query.get(display_node.human_owner_id)
        if human_owner:
            human_owner_username = human_owner.username

    return {
        "id": display_node.id,
        "preview": preview,
        "node_type": display_node.node_type,
        "child_count": len(node.children),
        "created_at": display_node.created_at.isoformat(),
        "pinned_at": node.pinned_at.isoformat() if node.pinned_at else None,
        "username": node.user.username if node.user else "Unknown",
        "human_owner_username": human_owner_username,
        "llm_model": display_node.llm_model,
        "has_original_audio": bool(display_node.audio_original_url or display_node.streaming_transcription),
        "prompt_key": prompt_key,
    }


# Dashboard endpoint: only return top-level nodes (nodes with no parent)
@dashboard_bp.route("/", methods=["GET"])
@login_required
def get_dashboard():
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 20, type=int)
    per_page = min(per_page, 100)

    # Pinned nodes for this user (separate from pagination)
    pinned_nodes = Node.query.filter(
        Node.pinned_by == current_user.id,
        Node.pinned_at.isnot(None)
    ).order_by(Node.pinned_at.desc()).all()
    pinned_list = [_serialize_node_for_list(n) for n in pinned_nodes]

    query = Node.query.filter_by(user_id=current_user.id, parent_id=None).order_by(Node.created_at.desc())
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    nodes_list = [_serialize_node_for_list(node) for node in pagination.items]
    # Determine if Voice Mode is enabled for this user (admin or paid plan)
    voice_mode_enabled = current_user.has_voice_mode
    dashboard = {
        "user": {
            "id": current_user.id,
            "username": current_user.username,
            "description": current_user.description,
            "accepted_terms_at": current_user.accepted_terms_at.isoformat() if current_user.accepted_terms_at else None,
            "terms_up_to_date": _terms_up_to_date(current_user),
            "approved": current_user.approved,
            "email": current_user.email,
            "is_admin": current_user.is_admin,
            "plan": current_user.plan,
            "voice_mode_enabled": voice_mode_enabled,
            "craft_mode": current_user.craft_mode,
            "preferred_model": current_user.preferred_model,
            "profile_generation_task_id": current_user.profile_generation_task_id,
            "default_privacy_level": current_user.default_privacy_level,
            "default_ai_usage": current_user.default_ai_usage,
        },
        "pinned_nodes": pinned_list,
        "nodes": nodes_list,
        "has_more": pagination.has_next,
        "page": page,
        "total_nodes": pagination.total,
        "latest_profile": get_latest_profile(current_user)
    }
    return jsonify(dashboard), 200


# Public view of any user's dashboard; no private stats provided.
@dashboard_bp.route("/<string:username>", methods=["GET"])
@login_required
def get_public_dashboard(username):
    # Lookup the user by their (unique) handle (username).
    user = User.query.filter_by(username=username).first_or_404()

    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 20, type=int)
    per_page = min(per_page, 100)

    # Pinned nodes for this user (filtered by accessibility)
    pinned_nodes = Node.query.filter(
        Node.pinned_by == user.id,
        Node.pinned_at.isnot(None),
        accessible_nodes_filter(Node, current_user.id)
    ).order_by(Node.pinned_at.desc()).all()
    pinned_list = [_serialize_node_for_list(n) for n in pinned_nodes]

    query = Node.query.filter(
        Node.user_id == user.id,
        Node.parent_id.is_(None),
        accessible_nodes_filter(Node, current_user.id)
    ).order_by(Node.created_at.desc())
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    nodes_list = [_serialize_node_for_list(node) for node in pagination.items]

    dashboard = {
        "user": {
            "id": user.id,
            "username": user.username,
            "description": user.description
        },
        "pinned_nodes": pinned_list,
        "nodes": nodes_list,
        "has_more": pagination.has_next,
        "page": page,
        "total_nodes": pagination.total,
        "latest_profile": get_latest_profile(user)
    }
    return jsonify(dashboard), 200


# New endpoint to update the user’s display handle and description.
@dashboard_bp.route("/user", methods=["PUT"])
@login_required
def update_user():
    data = request.get_json()
    new_username = data.get("username")
    new_description = data.get("description")
    new_email = data.get("email")

    if new_description and len(new_description) > 128:
        return jsonify({"error": "Description exceeds maximum length of 128 characters."}), 400

    if new_username:
        new_username = new_username.strip()
        if not new_username:
            return jsonify({"error": "Username cannot be empty."}), 400
        if len(new_username) > 64:
            return jsonify({"error": "Username must be 64 characters or fewer."}), 400
        if not re.fullmatch(r'[a-zA-Z0-9_]+', new_username):
            return jsonify({
                "error": "Username may only contain letters, numbers, and underscores."
            }), 400
        # Case-insensitive uniqueness check (exclude current user)
        existing = User.query.filter(
            db.func.lower(User.username) == new_username.lower(),
            User.id != current_user.id
        ).first()
        if existing:
            return jsonify({"error": "That username is already taken."}), 400
        current_user.username = new_username

    if new_description is not None:
        current_user.description = new_description
    if new_email is not None:
        current_user.email = new_email

    if "craft_mode" in data:
        current_user.craft_mode = bool(data["craft_mode"])

    if "preferred_model" in data:
        current_user.preferred_model = data["preferred_model"]

    if "default_privacy_level" in data:
        val = data["default_privacy_level"]
        if val not in VALID_PRIVACY_LEVELS:
            return jsonify({"error": f"Invalid privacy level: {val}"}), 400
        current_user.default_privacy_level = val

    if "default_ai_usage" in data:
        val = data["default_ai_usage"]
        if val not in VALID_AI_USAGE:
            return jsonify({"error": f"Invalid AI usage value: {val}"}), 400
        current_user.default_ai_usage = val

    try:
        db.session.commit()
        # Include voice mode feature flag and user plan in the response
        voice_mode_enabled = current_user.has_voice_mode
        return jsonify({
            "message": "Profile updated successfully.",
            "user": {
                "id": current_user.id,
                "username": current_user.username,
                "description": current_user.description,
                "email": current_user.email,
                "approved": current_user.approved,
                "accepted_terms_at": current_user.accepted_terms_at.isoformat() if current_user.accepted_terms_at else None,
                "terms_up_to_date": _terms_up_to_date(current_user),
                "is_admin": current_user.is_admin,
                "plan": current_user.plan,
                "voice_mode_enabled": voice_mode_enabled,
                "craft_mode": current_user.craft_mode,
                "preferred_model": current_user.preferred_model,
                "profile_generation_task_id": current_user.profile_generation_task_id,
                "default_privacy_level": current_user.default_privacy_level,
                "default_ai_usage": current_user.default_ai_usage,
            }
        }), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "Failed to update profile.", "details": str(e)}), 500

from flask import Blueprint, jsonify, request, current_app
from flask_login import login_required, current_user
from backend.models import Node, User, UserProfile
from backend.extensions import db
from backend.utils.timefmt import iso_utc, is_valid_timezone
from backend.utils.privacy import (
    accessible_nodes_filter, VALID_PRIVACY_LEVELS, VALID_AI_USAGE,
)
from backend.routes.terms import CURRENT_TERMS_VERSION
from backend.utils.reserved_usernames import validate_username
from backend.utils.spend import user_is_capped

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
            "created_at": iso_utc(profile.created_at),
            "source_tokens_used": profile.source_tokens_used,
            "source_data_cutoff": (
                iso_utc(profile.source_data_cutoff)
            ),
            "generation_type": profile.generation_type,
            # Whether this profile has generated TTS audio — drives the
            # "regenerate audio?" edit prompt (#66).
            "has_tts": bool(profile.audio_tts_url),
        }
    return None


def _serialize_node_for_list(node):
    """Serialize a node for dashboard/feed list views."""
    # If this is a system prompt root, skip to the first child
    display_node = node
    prompt_key = None
    if node.is_system_prompt:
        prompt = node.get_artifact("prompt")
        if prompt is None and node.user_prompt:
            prompt = node.user_prompt  # legacy fallback
        prompt_key = prompt.prompt_key if prompt else None
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
        "created_at": iso_utc(display_node.created_at),
        "pinned_at": iso_utc(node.pinned_at),
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
            "accepted_terms_at": iso_utc(current_user.accepted_terms_at),
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
            "timezone": current_user.timezone or "UTC",
            # Lets the client block cost actions (e.g. starting a long voice
            # recording) up front instead of after the fact (issue #85).
            "spend_blocked": user_is_capped(current_user),
            # Public side (#228): enabled = deployed (env) AND the user's
            # own opt-in — every frontend surface keys off this. available
            # = deployed only; it decides whether Account shows the toggle.
            "share_v1_enabled": bool(
                current_app.config.get("SHARE_V1", False)
                and current_user.public_sharing_enabled),
            "share_v1_available": bool(
                current_app.config.get("SHARE_V1", False)),
            "public_sharing_enabled": bool(
                current_user.public_sharing_enabled),
            # Archive search + saved references (#208): available = the
            # env killswitch is on (decides whether Account shows the
            # toggle); enabled = the user's own easter-egg opt-in.
            "external_content_available": bool(
                current_app.config.get("SEMANTIC_SEARCH_AGENTIC", True)),
            "external_content_enabled": bool(
                current_user.external_content_enabled),
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
        # Validates non-empty, length, allowed chars, reserved names, and
        # case-insensitive uniqueness (excluding the current user's own row).
        error = validate_username(new_username, exclude_user_id=current_user.id)
        if error:
            return jsonify({"error": error}), 400
        current_user.username = new_username

    if new_description is not None:
        current_user.description = new_description
    if new_email is not None:
        current_user.email = new_email

    if "craft_mode" in data:
        current_user.craft_mode = bool(data["craft_mode"])

    if "public_sharing_enabled" in data:
        current_user.public_sharing_enabled = bool(
            data["public_sharing_enabled"])

    if "external_content_enabled" in data:
        current_user.external_content_enabled = bool(
            data["external_content_enabled"])

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
                "accepted_terms_at": iso_utc(current_user.accepted_terms_at),
                "terms_up_to_date": _terms_up_to_date(current_user),
                "is_admin": current_user.is_admin,
                "plan": current_user.plan,
                "voice_mode_enabled": voice_mode_enabled,
                "craft_mode": current_user.craft_mode,
                "preferred_model": current_user.preferred_model,
                "profile_generation_task_id": current_user.profile_generation_task_id,
                "default_privacy_level": current_user.default_privacy_level,
                "default_ai_usage": current_user.default_ai_usage,
                "spend_blocked": user_is_capped(current_user),
                "share_v1_enabled": bool(
                    current_app.config.get("SHARE_V1", False)
                    and current_user.public_sharing_enabled),
                "share_v1_available": bool(
                    current_app.config.get("SHARE_V1", False)),
                "public_sharing_enabled": bool(
                    current_user.public_sharing_enabled),
                "external_content_available": bool(
                    current_app.config.get(
                        "SEMANTIC_SEARCH_AGENTIC", True)),
                "external_content_enabled": bool(
                    current_user.external_content_enabled),
                "timezone": current_user.timezone or "UTC",
            }
        }), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "Failed to update profile.", "details": str(e)}), 500


# Persist the browser-reported IANA timezone (e.g. "Europe/Prague"), used to
# render absolute local-time stamps in the LLM context (#130). Called by the
# frontend on session start when the detected timezone differs from the stored
# one. Fire-and-forget: invalid values are rejected rather than clobbering the
# stored timezone.
@dashboard_bp.route("/timezone", methods=["PATCH"])
@login_required
def update_timezone():
    data = request.get_json(silent=True) or {}
    tz_name = data.get("timezone")
    if not is_valid_timezone(tz_name):
        return jsonify({"error": "Invalid timezone."}), 400
    current_user.timezone = tz_name
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "Failed to update timezone.",
                        "details": str(e)}), 500
    return jsonify({"timezone": current_user.timezone}), 200

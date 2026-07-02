"""Alchemical Mode routes (dark behind ALCHEMY_V1).

The double gate, enforced here:
- /status and the Home card only ever OFFER the mode to users whose
  AlchemyState says the readiness check passed (our side).
- /opt-in requires the readiness gate to be open AND an explicit
  accept_risks acknowledgment (user side).
- /start requires both gates open, and attaches the hidden alchemy guide
  prompt (never the user-editable textmode prompt).
"""
from datetime import datetime

from flask import Blueprint, jsonify, request, current_app
from flask_login import login_required, current_user

from backend.models import (
    Node, AlchemySource, AlchemyState, User,
)
from backend.extensions import db
from backend.utils.privacy import validate_ai_usage
from backend.utils.llm_nodes import (
    create_llm_placeholder, pick_model_for_generation,
)
from backend.utils.context_artifacts import attach_context_artifacts

alchemy_bp = Blueprint("alchemy", __name__)

PROMPT_KEY = "alchemy"


def _enabled():
    return bool(current_app.config.get("ALCHEMY_V1", False))


def _state():
    return AlchemyState.query.filter_by(user_id=current_user.id).first()


@alchemy_bp.route("/status", methods=["GET"])
@login_required
def status():
    if not _enabled():
        return jsonify({"error": "Not found"}), 404
    state = _state()
    return jsonify({
        "status": state.status_for_user if state else None,
        "source_slug": state.source_slug if state else None,
    }), 200


@alchemy_bp.route("/sources", methods=["GET"])
@login_required
def sources():
    if not _enabled():
        return jsonify({"error": "Not found"}), 404
    rows = AlchemySource.query.order_by(AlchemySource.id.asc()).all()
    return jsonify({"sources": [{
        "slug": s.slug,
        "title": s.title,
        "description": s.description,
        "available": s.available,
    } for s in rows]}), 200


@alchemy_bp.route("/opt-in", methods=["POST"])
@login_required
def opt_in():
    """The user-side gate. Requires the readiness gate already open and an
    explicit acknowledgment of the risks."""
    if not _enabled():
        return jsonify({"error": "Not found"}), 404
    state = _state()
    if state is None or state.readiness_status != "ready":
        return jsonify({
            "error": "Alchemical Mode is not available for this account."
        }), 403
    if state.opted_in_at is not None:
        return jsonify({"error": "Already opted in."}), 409

    data = request.get_json() or {}
    if data.get("accept_risks") is not True:
        return jsonify({
            "error": "Explicit acknowledgment of the risks is required."
        }), 400
    source_slug = data.get("source_slug")
    source = AlchemySource.query.filter_by(slug=source_slug).first()
    if source is None or not source.available:
        return jsonify({"error": "Unknown or unavailable source."}), 400

    state.opted_in_at = datetime.utcnow()
    state.source_slug = source.slug
    db.session.commit()
    return jsonify({
        "status": "active",
        "source_slug": source.slug,
    }), 200


@alchemy_bp.route("/opt-out", methods=["POST"])
@login_required
def opt_out():
    """Stopping must always be at least as easy as starting."""
    if not _enabled():
        return jsonify({"error": "Not found"}), 404
    state = _state()
    if state is None or state.opted_in_at is None:
        return jsonify({"error": "Not opted in."}), 409
    state.opted_in_at = None
    state.source_slug = None
    db.session.commit()
    return jsonify({"status": "offered"}), 200


@alchemy_bp.route("/check-readiness", methods=["POST"])
@login_required
def trigger_readiness():
    """Admin-only manual trigger of the readiness check for a user.

    Deliberately not self-serve and not scheduled — rollout policy (who
    gets checked, when) is an open product decision."""
    if not _enabled():
        return jsonify({"error": "Not found"}), 404
    if not current_user.is_admin:
        return jsonify({"error": "Unauthorized"}), 403
    data = request.get_json() or {}
    user_id = data.get("user_id")
    if not user_id or not User.query.get(user_id):
        return jsonify({"error": "user_id is required"}), 400
    from backend.tasks.alchemy_readiness import check_user_readiness
    task = check_user_readiness.delay(user_id)
    return jsonify({"task_id": task.id}), 202


@alchemy_bp.route("/start", methods=["POST"])
@login_required
def start_session():
    """Start an alchemy conversation — mirrors /textmode/start, but the
    system node carries the HIDDEN alchemy guide prompt and both gates are
    checked. Subsequent turns ride the ordinary thread flow."""
    if not _enabled():
        return jsonify({"error": "Not found"}), 404
    state = _state()
    if (state is None or state.readiness_status != "ready"
            or state.opted_in_at is None):
        return jsonify({
            "error": "Alchemical Mode is not active for this account."
        }), 403

    data = request.get_json() or {}
    content = data.get("content")
    if not content or not content.strip():
        return jsonify({"error": "Content is required"}), 400
    from backend.utils.node_split import NODE_CHAR_CAP
    if len(content) > NODE_CHAR_CAP:
        return jsonify({
            "error": (f"Content exceeds the {NODE_CHAR_CAP:,}-character "
                      f"per-entry limit."),
            "char_cap": NODE_CHAR_CAP,
        }), 422

    ai_usage = data.get("ai_usage") or current_user.default_ai_usage
    if not validate_ai_usage(ai_usage) or ai_usage == "none":
        return jsonify({
            "error": "Alchemy requires ai_usage of 'chat' or 'train'",
        }), 400
    # Depth work stays private by default regardless of the user's default
    # privacy setting — sharing an alchemy thread is a deliberate act later.
    privacy_level = "private"

    model_id = data.get("model")
    if not model_id:
        model_id = pick_model_for_generation(None, current_user)
    if model_id not in current_app.config["SUPPORTED_MODELS"]:
        return jsonify({"error": f"Unsupported model: {model_id}"}), 400

    from backend.utils.prompts import get_user_prompt_record
    prompt_record = get_user_prompt_record(current_user.id, PROMPT_KEY)

    system_node = Node(
        user_id=current_user.id,
        human_owner_id=current_user.id,
        parent_id=None,
        node_type="user",
        privacy_level=privacy_level,
        ai_usage=ai_usage,
    )
    db.session.add(system_node)
    db.session.flush()
    attach_context_artifacts(
        system_node.id, current_user.id, prompt_record=prompt_record,
    )

    from backend.utils.tokens import approximate_token_count
    user_node = Node(
        user_id=current_user.id,
        human_owner_id=current_user.id,
        parent_id=system_node.id,
        node_type="user",
        privacy_level=privacy_level,
        ai_usage=ai_usage,
        token_count=approximate_token_count(content),
    )
    user_node.set_content(content)
    db.session.add(user_node)
    db.session.flush()

    from backend.utils.spend import user_is_capped
    if user_is_capped(current_user):
        db.session.commit()
        return jsonify({
            "conversation_id": system_node.id,
            "user_node_id": user_node.id,
            "spend_capped": True,
        }), 202

    llm_node, task_id = create_llm_placeholder(
        user_node.id, model_id, current_user.id,
        privacy_level=privacy_level,
        ai_usage=ai_usage,
        source_mode="textmode",
    )
    db.session.commit()
    return jsonify({
        "conversation_id": system_node.id,
        "user_node_id": user_node.id,
        "llm_node_id": llm_node.id,
        "task_id": task_id,
    }), 202

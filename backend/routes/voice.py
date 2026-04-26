from flask import Blueprint, jsonify, request, current_app
from flask_login import login_required, current_user
from backend.models import Node
from backend.extensions import db
from backend.utils.prompts import get_user_prompt_record
from backend.utils.llm_nodes import create_llm_placeholder
from backend.utils.placeholders import UserExportValidationError
from backend.utils.context_artifacts import attach_context_artifacts
from backend.utils.session_helpers import (
    ancestors_have_prompt, is_llm_node, create_llm_placeholder_node,
)

voice_bp = Blueprint("voice", __name__)

PROMPT_KEY = 'voice'
# Keys that share the unified agentic.txt template. Any of these counts
# as "an agentic prompt is already attached" when walking ancestry, so
# bridging a text thread into voice mode (or vice-versa) doesn't append
# a second prompt node.
AGENTIC_PROMPT_KEYS = ('voice', 'textmode')


@voice_bp.route("/from-node/<int:node_id>", methods=["POST"])
@login_required
def create_voice_from_node(node_id):
    """Start or resume a voice session from an existing node's thread."""
    node = Node.query.get(node_id)
    if not node:
        return jsonify({"error": "Node not found"}), 404
    if node.human_owner_id != current_user.id:
        return jsonify({"error": "Unauthorized"}), 403

    data = request.get_json() or {}
    model_id = data.get("model")
    if not model_id:
        model_id = current_app.config.get(
            "DEFAULT_LLM_MODEL", "claude-opus-4.5"
        )
    if model_id not in current_app.config["SUPPORTED_MODELS"]:
        return jsonify({"error": f"Unsupported model: {model_id}"}), 400

    # Inherit ai_usage from the target node
    ai_usage = node.ai_usage or current_user.default_ai_usage

    has_prompt = ancestors_have_prompt(node, current_user.id, AGENTIC_PROMPT_KEYS)
    is_llm = is_llm_node(node)

    if has_prompt and not is_llm:
        llm_node = create_llm_placeholder_node(
            node.id, model_id, current_user.id,
            ai_usage=ai_usage,
            source_mode='voice',
        )
        return jsonify({
            "mode": "processing",
            "llm_node_id": llm_node.id,
            "parent_id": llm_node.id,
            "fresh": True,
        }), 202

    if has_prompt and is_llm:
        return jsonify({
            "mode": "processing",
            "llm_node_id": node.id,
            "parent_id": node.id,
        }), 200

    if not has_prompt and not is_llm:
        prompt_record = get_user_prompt_record(current_user.id, PROMPT_KEY)
        system_node = Node(
            user_id=current_user.id,
            human_owner_id=current_user.id,
            parent_id=node.id,
            node_type="user",
            privacy_level="private",
            ai_usage=ai_usage,
        )
        db.session.add(system_node)
        db.session.flush()
        attach_context_artifacts(
            system_node.id, current_user.id, prompt_record=prompt_record,
        )

        llm_node = create_llm_placeholder_node(
            system_node.id, model_id, current_user.id,
            ai_usage=ai_usage,
            source_mode='voice',
        )
        return jsonify({
            "mode": "processing",
            "llm_node_id": llm_node.id,
            "parent_id": llm_node.id,
            "fresh": True,
        }), 202

    # not has_prompt and is_llm
    prompt_record = get_user_prompt_record(current_user.id, PROMPT_KEY)
    system_node = Node(
        user_id=current_user.id,
        human_owner_id=current_user.id,
        parent_id=node.id,
        node_type="user",
        privacy_level="private",
        ai_usage=ai_usage,
    )
    db.session.add(system_node)
    db.session.flush()
    attach_context_artifacts(
        system_node.id, current_user.id, prompt_record=prompt_record,
    )
    db.session.commit()

    return jsonify({
        "mode": "processing",
        "llm_node_id": node.id,
        "parent_id": system_node.id,
    }), 200


@voice_bp.route("/", methods=["POST"])
@login_required
def create_voice_session():
    """
    Start or continue a voice session.
    Body: { content: string, model?: string, parent_id?: int,
            session_id?: string }
    Without parent_id: creates system node (prompt) -> user node -> LLM node
    With parent_id: continues thread
    """
    data = request.get_json() or {}
    content = data.get("content")
    model_id = data.get("model")
    parent_id = data.get("parent_id")
    session_id = data.get("session_id")
    if not content or not content.strip():
        return jsonify({"error": "Content is required"}), 400

    if not model_id:
        model_id = current_app.config.get(
            "DEFAULT_LLM_MODEL", "claude-opus-4.5"
        )

    if model_id not in current_app.config["SUPPORTED_MODELS"]:
        return jsonify({"error": f"Unsupported model: {model_id}"}), 400

    if parent_id:
        parent_node = Node.query.get(parent_id)
        if not parent_node:
            return jsonify({"error": "Parent node not found"}), 404
        if parent_node.human_owner_id != current_user.id:
            return jsonify({"error": "Unauthorized"}), 403
        # Inherit ai_usage from parent node in the thread
        ai_usage = parent_node.ai_usage or current_user.default_ai_usage
        user_parent_id = parent_id
    else:
        ai_usage = data.get("ai_usage") or current_user.default_ai_usage
        prompt_record = get_user_prompt_record(current_user.id, PROMPT_KEY)
        system_node = Node(
            user_id=current_user.id,
            human_owner_id=current_user.id,
            parent_id=None,
            node_type="user",
            privacy_level="private",
            ai_usage=ai_usage,
        )
        db.session.add(system_node)
        db.session.flush()
        attach_context_artifacts(
            system_node.id, current_user.id, prompt_record=prompt_record,
        )
        user_parent_id = system_node.id

    from backend.utils.tokens import approximate_token_count
    user_node = Node(
        user_id=current_user.id,
        human_owner_id=current_user.id,
        parent_id=user_parent_id,
        node_type="user",
        privacy_level="private",
        ai_usage=ai_usage,
        token_count=approximate_token_count(content),
    )
    user_node.set_content(content)
    db.session.add(user_node)
    db.session.flush()

    if session_id:
        from backend.utils.audio_storage import (
            attach_streaming_audio_to_node,
        )
        attach_streaming_audio_to_node(
            session_id, user_node, current_user.id
        )

    try:
        llm_node, task_id = create_llm_placeholder(
            user_node.id, model_id, current_user.id,
            ai_usage=ai_usage,
            source_mode='voice',
        )
    except UserExportValidationError as e:
        return jsonify({"error": str(e)}), 400

    current_app.logger.info(
        f"Voice session: parent={user_parent_id}, "
        f"user={user_node.id}, llm={llm_node.id}, task={task_id}"
    )

    return jsonify({
        "parent_id": user_parent_id,
        "user_node_id": user_node.id,
        "llm_node_id": llm_node.id,
        "task_id": task_id,
    }), 202

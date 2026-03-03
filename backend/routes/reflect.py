from flask import Blueprint, jsonify, request, current_app
from flask_login import login_required, current_user
from backend.models import Node
from backend.extensions import db
from backend.utils.prompts import get_user_prompt_record
from backend.utils.llm_nodes import create_llm_placeholder

reflect_bp = Blueprint("reflect", __name__)

PROMPT_KEY = 'reflect'


def _ancestors_have_prompt(node, user_id, prompt_key):
    """Walk up ancestors and check if any node links to a UserPrompt with this key."""
    current = node
    while current:
        if (current.user_prompt_id is not None
                and current.user_prompt
                and current.user_prompt.prompt_key == prompt_key):
            return True
        if current.parent_id:
            current = Node.query.get(current.parent_id)
        else:
            break
    return False


def _is_llm_node(node):
    return node.node_type == 'llm' or bool(node.llm_model)


def _create_llm_placeholder(parent_node_id, model_id, requesting_user_id):
    """Create an LLM placeholder node and enqueue the generation task."""
    llm_node, _ = create_llm_placeholder(
        parent_node_id, model_id, requesting_user_id
    )
    return llm_node


@reflect_bp.route("/from-node/<int:node_id>", methods=["POST"])
@login_required
def create_reflect_from_node(node_id):
    """Start or resume a reflect session from an existing node's thread."""
    node = Node.query.get(node_id)
    if not node:
        return jsonify({"error": "Node not found"}), 404
    if node.user_id != current_user.id:
        # Also allow if this is an LLM node whose parent belongs to user
        parent = Node.query.get(node.parent_id) if node.parent_id else None
        if not parent or parent.user_id != current_user.id:
            return jsonify({"error": "Unauthorized"}), 403

    data = request.get_json() or {}
    model_id = data.get("model")
    if not model_id:
        model_id = current_app.config.get(
            "DEFAULT_LLM_MODEL", "claude-opus-4.5"
        )
    if model_id not in current_app.config["SUPPORTED_MODELS"]:
        return jsonify({"error": f"Unsupported model: {model_id}"}), 400

    has_prompt = _ancestors_have_prompt(node, current_user.id, PROMPT_KEY)
    is_llm = _is_llm_node(node)

    if has_prompt and not is_llm:
        # User node with existing prompt: create LLM child → processing
        llm_node = _create_llm_placeholder(
            node.id, model_id, current_user.id
        )
        return jsonify({
            "mode": "processing",
            "llm_node_id": llm_node.id,
        }), 202

    if has_prompt and is_llm:
        # LLM node with existing prompt: play back its TTS first
        return jsonify({
            "mode": "processing",
            "llm_node_id": node.id,
            "parent_id": node.id,
        }), 200

    if not has_prompt and not is_llm:
        # User node, no prompt: create system prompt as child, then LLM child
        prompt_record = get_user_prompt_record(current_user.id, PROMPT_KEY)
        system_node = Node(
            user_id=current_user.id,
            human_owner_id=current_user.id,
            parent_id=node.id,
            node_type="user",
            privacy_level="private",
            ai_usage="chat",
            user_prompt_id=prompt_record.id,
        )
        db.session.add(system_node)
        db.session.flush()

        llm_node = _create_llm_placeholder(
            system_node.id, model_id, current_user.id
        )
        return jsonify({
            "mode": "processing",
            "llm_node_id": llm_node.id,
        }), 202

    # not has_prompt and is_llm
    # LLM node, no prompt: create system prompt as child, then play back TTS
    prompt_record = get_user_prompt_record(current_user.id, PROMPT_KEY)
    system_node = Node(
        user_id=current_user.id,
        human_owner_id=current_user.id,
        parent_id=node.id,
        node_type="user",
        privacy_level="private",
        ai_usage="chat",
        user_prompt_id=prompt_record.id,
    )
    db.session.add(system_node)
    db.session.commit()

    return jsonify({
        "mode": "processing",
        "llm_node_id": node.id,
        "parent_id": system_node.id,
    }), 200


@reflect_bp.route("/", methods=["POST"])
@login_required
def create_reflect_session():
    """
    Start or continue a reflect session.
    Body: { content: string, model?: string, parent_id?: int,
            session_id?: string }
    Without parent_id: creates system node (prompt) -> user node -> LLM node
    With parent_id: continues thread — user node parented to parent_id -> LLM node
    session_id: optional streaming-transcription session whose audio
                chunks should be attached to the user node.
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
        # Continue an existing thread — parent the user node to the given node
        parent_node = Node.query.get(parent_id)
        if not parent_node:
            return jsonify({"error": "Parent node not found"}), 404
        user_parent_id = parent_id
    else:
        # New thread — create system node with reflect prompt
        prompt_record = get_user_prompt_record(current_user.id, PROMPT_KEY)
        system_node = Node(
            user_id=current_user.id,
            human_owner_id=current_user.id,
            parent_id=None,
            node_type="user",
            privacy_level="private",
            ai_usage="chat",
            user_prompt_id=prompt_record.id,
        )
        db.session.add(system_node)
        db.session.flush()
        user_parent_id = system_node.id

    # Create user node with transcribed content
    user_node = Node(
        user_id=current_user.id,
        human_owner_id=current_user.id,
        parent_id=user_parent_id,
        node_type="user",
        privacy_level="private",
        ai_usage="chat",
    )
    user_node.set_content(content)
    db.session.add(user_node)
    db.session.flush()

    # Attach original recording audio if session_id provided
    if session_id:
        from backend.utils.audio_storage import (
            attach_streaming_audio_to_node,
        )
        attach_streaming_audio_to_node(
            session_id, user_node, current_user.id
        )

    # 3. Create placeholder LLM node and enqueue task
    llm_node, task_id = create_llm_placeholder(
        user_node.id, model_id, current_user.id
    )

    current_app.logger.info(
        f"Reflect session: parent={user_parent_id}, "
        f"user={user_node.id}, llm={llm_node.id}, task={task_id}"
    )

    return jsonify({
        "parent_id": user_parent_id,
        "user_node_id": user_node.id,
        "llm_node_id": llm_node.id,
        "task_id": task_id,
    }), 202

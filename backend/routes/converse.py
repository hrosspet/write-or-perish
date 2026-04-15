from flask import Blueprint, jsonify, request, current_app
from flask_login import login_required, current_user
from backend.models import Node
from backend.extensions import db
from backend.utils.prompts import get_user_prompt_record
from backend.utils.llm_nodes import create_llm_placeholder
from backend.utils.context_artifacts import attach_context_artifacts

converse_bp = Blueprint("converse", __name__)

PROMPT_KEY = 'converse'


def _get_last_node_in_chain(system_node):
    """Walk down to the deepest child in the conversation chain."""
    current = system_node
    while True:
        children = Node.query.filter_by(parent_id=current.id).order_by(
            Node.created_at.desc()
        ).first()
        if children is None:
            return current
        current = children


def _serialize_message(node):
    """Serialize a node as a conversation message."""
    is_llm = node.node_type == "llm" or node.llm_model is not None
    return {
        "id": node.id,
        "role": "assistant" if is_llm else "user",
        "content": node.get_content(),
        "created_at": node.created_at.isoformat(),
        "llm_model": node.llm_model,
        "llm_task_status": node.llm_task_status,
    }


@converse_bp.route("/start", methods=["POST"])
@login_required
def start_conversation():
    """
    Start a new conversation.
    Body: { content: string, model?: string }
    """
    data = request.get_json() or {}
    content = data.get("content")
    model_id = data.get("model")
    ai_usage = data.get("ai_usage") or current_user.default_ai_usage

    if not content or not content.strip():
        return jsonify({"error": "Content is required"}), 400

    if not model_id:
        model_id = current_app.config.get(
            "DEFAULT_LLM_MODEL", "claude-opus-4.5"
        )

    if model_id not in current_app.config["SUPPORTED_MODELS"]:
        return jsonify({"error": f"Unsupported model: {model_id}"}), 400

    # 1. System node with converse prompt
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

    # 2. User message node
    from backend.utils.tokens import approximate_token_count
    user_node = Node(
        user_id=current_user.id,
        human_owner_id=current_user.id,
        parent_id=system_node.id,
        node_type="user",
        privacy_level="private",
        ai_usage=ai_usage,
        token_count=approximate_token_count(content),
    )
    user_node.set_content(content)
    db.session.add(user_node)
    db.session.flush()

    # 3. Placeholder LLM node and enqueue task
    llm_node, task_id = create_llm_placeholder(
        user_node.id, model_id, current_user.id,
        ai_usage=ai_usage,
    )

    return jsonify({
        "conversation_id": system_node.id,
        "user_node_id": user_node.id,
        "llm_node_id": llm_node.id,
        "task_id": task_id,
    }), 202


@converse_bp.route("/<int:conversation_id>/message", methods=["POST"])
@login_required
def add_message(conversation_id):
    """
    Add a message to an existing conversation.
    Body: { content: string, model?: string }
    """
    data = request.get_json() or {}
    content = data.get("content")
    model_id = data.get("model")
    if not content or not content.strip():
        return jsonify({"error": "Content is required"}), 400

    system_node = Node.query.get_or_404(conversation_id)
    if system_node.user_id != current_user.id:
        return jsonify({"error": "Unauthorized"}), 403

    if not model_id:
        model_id = current_app.config.get(
            "DEFAULT_LLM_MODEL", "claude-opus-4.5"
        )

    if model_id not in current_app.config["SUPPORTED_MODELS"]:
        return jsonify({"error": f"Unsupported model: {model_id}"}), 400

    # Find the last node in the chain and inherit its ai_usage
    last_node = _get_last_node_in_chain(system_node)
    ai_usage = last_node.ai_usage or current_user.default_ai_usage

    # Create user message node
    from backend.utils.tokens import approximate_token_count
    user_node = Node(
        user_id=current_user.id,
        human_owner_id=current_user.id,
        parent_id=last_node.id,
        node_type="user",
        privacy_level="private",
        ai_usage=ai_usage,
        token_count=approximate_token_count(content),
    )
    user_node.set_content(content)
    db.session.add(user_node)
    db.session.flush()

    # Create placeholder LLM node and enqueue task
    llm_node, task_id = create_llm_placeholder(
        user_node.id, model_id, current_user.id,
        ai_usage=ai_usage,
    )

    return jsonify({
        "user_node_id": user_node.id,
        "llm_node_id": llm_node.id,
        "task_id": task_id,
    }), 202


@converse_bp.route("/<int:conversation_id>", methods=["GET"])
@login_required
def get_conversation(conversation_id):
    """Get all messages in a conversation (excluding system prompt)."""
    system_node = Node.query.get_or_404(conversation_id)
    if system_node.user_id != current_user.id:
        return jsonify({"error": "Unauthorized"}), 403

    # Walk the chain from system node down, skip system node itself
    messages = []
    queue = [system_node]
    while queue:
        node = queue.pop(0)
        if node.id != system_node.id:
            messages.append(_serialize_message(node))
        children = Node.query.filter_by(parent_id=node.id).order_by(
            Node.created_at.asc()
        ).all()
        queue.extend(children)

    return jsonify({
        "conversation_id": conversation_id,
        "messages": messages,
    }), 200

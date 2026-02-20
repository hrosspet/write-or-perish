from flask import Blueprint, jsonify, request, current_app
from flask_login import login_required, current_user
from backend.models import Node, User
from backend.extensions import db
from backend.utils.prompts import get_user_prompt

converse_bp = Blueprint("converse", __name__)


def _get_converse_prompt():
    return get_user_prompt(current_user.id, 'converse')


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

    if not content or not content.strip():
        return jsonify({"error": "Content is required"}), 400

    if not model_id:
        model_id = current_app.config.get(
            "DEFAULT_LLM_MODEL", "claude-opus-4.5"
        )

    if model_id not in current_app.config["SUPPORTED_MODELS"]:
        return jsonify({"error": f"Unsupported model: {model_id}"}), 400

    # 1. System node with converse prompt
    system_node = Node(
        user_id=current_user.id,
        parent_id=None,
        node_type="user",
        privacy_level="private",
        ai_usage="chat",
    )
    system_node.set_content(_get_converse_prompt())
    db.session.add(system_node)
    db.session.flush()

    # 2. User message node
    user_node = Node(
        user_id=current_user.id,
        parent_id=system_node.id,
        node_type="user",
        privacy_level="private",
        ai_usage="chat",
    )
    user_node.set_content(content)
    db.session.add(user_node)
    db.session.flush()

    # 3. Placeholder LLM node
    llm_user = User.query.filter_by(username=model_id).first()
    if not llm_user:
        llm_user = User(twitter_id=f"llm-{model_id}", username=model_id)
        db.session.add(llm_user)
        db.session.flush()

    llm_node = Node(
        user_id=llm_user.id,
        parent_id=user_node.id,
        node_type="llm",
        llm_model=model_id,
        llm_task_status="pending",
        privacy_level="private",
        ai_usage="chat",
    )
    llm_node.set_content("[LLM response generation pending...]")
    db.session.add(llm_node)
    db.session.commit()

    # 4. Enqueue LLM completion
    from backend.tasks.llm_completion import generate_llm_response

    task = generate_llm_response.delay(
        user_node.id, llm_node.id, model_id, current_user.id
    )
    llm_node.llm_task_id = task.id
    db.session.commit()

    return jsonify({
        "conversation_id": system_node.id,
        "user_node_id": user_node.id,
        "llm_node_id": llm_node.id,
        "task_id": task.id,
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

    # Find the last node in the chain
    last_node = _get_last_node_in_chain(system_node)

    # Create user message node
    user_node = Node(
        user_id=current_user.id,
        parent_id=last_node.id,
        node_type="user",
        privacy_level="private",
        ai_usage="chat",
    )
    user_node.set_content(content)
    db.session.add(user_node)
    db.session.flush()

    # Create placeholder LLM node
    llm_user = User.query.filter_by(username=model_id).first()
    if not llm_user:
        llm_user = User(twitter_id=f"llm-{model_id}", username=model_id)
        db.session.add(llm_user)
        db.session.flush()

    llm_node = Node(
        user_id=llm_user.id,
        parent_id=user_node.id,
        node_type="llm",
        llm_model=model_id,
        llm_task_status="pending",
        privacy_level="private",
        ai_usage="chat",
    )
    llm_node.set_content("[LLM response generation pending...]")
    db.session.add(llm_node)
    db.session.commit()

    # Enqueue LLM completion
    from backend.tasks.llm_completion import generate_llm_response

    task = generate_llm_response.delay(
        user_node.id, llm_node.id, model_id, current_user.id
    )
    llm_node.llm_task_id = task.id
    db.session.commit()

    return jsonify({
        "user_node_id": user_node.id,
        "llm_node_id": llm_node.id,
        "task_id": task.id,
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

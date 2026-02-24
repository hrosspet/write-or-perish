from flask import Blueprint, jsonify, request, current_app
from flask_login import login_required, current_user
from backend.models import Node, User
from backend.extensions import db
from backend.utils.prompts import get_user_prompt
from backend.routes.reflect import (
    _ancestors_have_prompt, _is_llm_node, _create_llm_placeholder,
)

orient_bp = Blueprint("orient", __name__)

PROMPT_KEY = 'orient'


def _get_orient_prompt():
    return get_user_prompt(current_user.id, PROMPT_KEY)


@orient_bp.route("/", methods=["POST"])
@login_required
def create_orient_session():
    """
    Start or continue an orient session.
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
        # New thread — create system node with orient prompt
        system_node = Node(
            user_id=current_user.id,
            parent_id=None,
            node_type="user",
            privacy_level="private",
            ai_usage="chat",
        )
        system_node.set_content(_get_orient_prompt())
        db.session.add(system_node)
        db.session.flush()
        user_parent_id = system_node.id

    # User node with transcription
    user_node = Node(
        user_id=current_user.id,
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

    current_app.logger.info(
        f"Orient session: parent={user_parent_id}, "
        f"user={user_node.id}, llm={llm_node.id}, task={task.id}"
    )

    return jsonify({
        "parent_id": user_parent_id,
        "user_node_id": user_node.id,
        "llm_node_id": llm_node.id,
        "task_id": task.id,
    }), 202


@orient_bp.route("/from-node/<int:node_id>", methods=["POST"])
@login_required
def create_orient_from_node(node_id):
    """Start or resume an orient session from an existing node's thread."""
    node = Node.query.get(node_id)
    if not node:
        return jsonify({"error": "Node not found"}), 404
    if node.user_id != current_user.id:
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
        llm_node = _create_llm_placeholder(
            node.id, model_id, current_user.id
        )
        return jsonify({
            "mode": "processing",
            "llm_node_id": llm_node.id,
        }), 202

    if has_prompt and is_llm:
        return jsonify({
            "mode": "recording",
            "parent_id": node.id,
        }), 200

    if not has_prompt and not is_llm:
        system_node = Node(
            user_id=current_user.id,
            parent_id=node.id,
            node_type="user",
            privacy_level="private",
            ai_usage="chat",
        )
        system_node.set_content(_get_orient_prompt())
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
    system_node = Node(
        user_id=current_user.id,
        parent_id=node.id,
        node_type="user",
        privacy_level="private",
        ai_usage="chat",
    )
    system_node.set_content(_get_orient_prompt())
    db.session.add(system_node)
    db.session.commit()

    return jsonify({
        "mode": "recording",
        "parent_id": system_node.id,
    }), 200


@orient_bp.route("/<int:llm_node_id>/apply-todo", methods=["POST"])
@login_required
def apply_todo(llm_node_id):
    """
    Apply orient AI suggestions to the todo list.
    Kicks off a Celery task that merges the Orient update into the user's
    full todo via a second LLM call, then saves the result.
    """
    # Verify the LLM node exists and belongs to user's session
    llm_node = Node.query.get_or_404(llm_node_id)
    # Walk up to find the system node owner
    parent = llm_node.parent
    while parent and parent.parent:
        parent = parent.parent
    if not parent or parent.user_id != current_user.id:
        return jsonify({"error": "Unauthorized"}), 403

    from backend.tasks.orient_todo import apply_orient_todo
    task = apply_orient_todo.delay(llm_node_id, current_user.id)

    return jsonify({"task_id": task.id}), 202

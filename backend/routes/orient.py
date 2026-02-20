from flask import Blueprint, jsonify, request, current_app
from flask_login import login_required, current_user
from backend.models import Node, User, UserTodo
from backend.extensions import db
from backend.utils.prompts import get_user_prompt

orient_bp = Blueprint("orient", __name__)


def _get_orient_prompt():
    return get_user_prompt(current_user.id, 'orient')


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


@orient_bp.route("/<int:llm_node_id>/apply-todo", methods=["POST"])
@login_required
def apply_todo(llm_node_id):
    """
    Apply orient AI suggestions to the todo list.
    Body: { updated_content: string }
    Creates a new UserTodo version with generated_by='orient_session'.
    """
    data = request.get_json() or {}
    updated_content = data.get("updated_content")

    if not updated_content or not updated_content.strip():
        return jsonify({"error": "Updated content is required"}), 400

    # Verify the LLM node exists and belongs to user's session
    llm_node = Node.query.get_or_404(llm_node_id)
    # Walk up to find the system node owner
    parent = llm_node.parent
    while parent and parent.parent:
        parent = parent.parent
    if not parent or parent.user_id != current_user.id:
        return jsonify({"error": "Unauthorized"}), 403

    # Create new todo version
    todo = UserTodo(
        user_id=current_user.id,
        generated_by="orient_session",
        tokens_used=0,
    )
    todo.set_content(updated_content)
    db.session.add(todo)
    db.session.commit()

    version_count = UserTodo.query.filter_by(
        user_id=current_user.id
    ).count()

    return jsonify({
        "todo": {
            "id": todo.id,
            "content": todo.get_content(),
            "generated_by": todo.generated_by,
            "created_at": todo.created_at.isoformat(),
            "version_number": version_count,
        }
    }), 200

from flask import Blueprint, jsonify, request, current_app
from flask_login import login_required, current_user
from backend.models import Node, User, UserTodo
from backend.extensions import db
from pathlib import Path

orient_bp = Blueprint("orient", __name__)

PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "orient.txt"


def _get_orient_prompt():
    return PROMPT_PATH.read_text(encoding="utf-8")


@orient_bp.route("/", methods=["POST"])
@login_required
def create_orient_session():
    """
    Start an orient session.
    Body: { content: string, model?: string }
    Creates: system node (prompt with {user_profile} + {user_todo}) -> user node -> LLM node
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

    # 1. System node with orient prompt (has both {user_profile} and {user_todo})
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

    # 2. User node with transcription
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

    current_app.logger.info(
        f"Orient session created: system={system_node.id}, "
        f"user={user_node.id}, llm={llm_node.id}, task={task.id}"
    )

    return jsonify({
        "session_node_id": system_node.id,
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

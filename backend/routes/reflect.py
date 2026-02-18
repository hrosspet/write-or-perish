from flask import Blueprint, jsonify, request, current_app
from flask_login import login_required, current_user
from backend.models import Node, User
from backend.extensions import db
from pathlib import Path

reflect_bp = Blueprint("reflect", __name__)

PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "reflect.txt"


def _get_reflect_prompt():
    return PROMPT_PATH.read_text(encoding="utf-8")


@reflect_bp.route("/", methods=["POST"])
@login_required
def create_reflect_session():
    """
    Start a reflect session.
    Body: { content: string, model?: string }
    Creates: system node (prompt) -> user node (transcript) -> placeholder LLM node
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

    # 1. Create system node with reflect prompt
    system_node = Node(
        user_id=current_user.id,
        parent_id=None,
        node_type="user",
        privacy_level="private",
        ai_usage="chat",
    )
    system_node.set_content(_get_reflect_prompt())
    db.session.add(system_node)
    db.session.flush()

    # 2. Create user node with transcribed content
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

    # 3. Create placeholder LLM node
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

    # 4. Enqueue LLM completion task
    from backend.tasks.llm_completion import generate_llm_response

    task = generate_llm_response.delay(
        user_node.id, llm_node.id, model_id, current_user.id
    )
    llm_node.llm_task_id = task.id
    db.session.commit()

    current_app.logger.info(
        f"Reflect session created: system={system_node.id}, "
        f"user={user_node.id}, llm={llm_node.id}, task={task.id}"
    )

    return jsonify({
        "session_node_id": system_node.id,
        "user_node_id": user_node.id,
        "llm_node_id": llm_node.id,
        "task_id": task.id,
    }), 202

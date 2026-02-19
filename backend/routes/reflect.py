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
        user_parent_id = system_node.id

    # Create user node with transcribed content
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
        f"Reflect session: parent={user_parent_id}, "
        f"user={user_node.id}, llm={llm_node.id}, task={task.id}"
    )

    return jsonify({
        "parent_id": user_parent_id,
        "user_node_id": user_node.id,
        "llm_node_id": llm_node.id,
        "task_id": task.id,
    }), 202

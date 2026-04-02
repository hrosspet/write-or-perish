import json
from flask import Blueprint, jsonify, request, current_app
from flask_login import login_required, current_user
from backend.models import Node, Draft, UserTodo
from backend.extensions import db
from backend.utils.tool_meta import update_tool_meta

todo_bp = Blueprint("todo", __name__)


@todo_bp.route("/", methods=["GET"])
@login_required
def get_todo():
    """Get the latest todo version for the current user."""
    todo = UserTodo.query.filter_by(
        user_id=current_user.id
    ).order_by(UserTodo.created_at.desc()).first()

    if not todo:
        return jsonify({"todo": None}), 200

    # Count total versions for version number
    version_count = UserTodo.query.filter_by(
        user_id=current_user.id
    ).count()

    return jsonify({
        "todo": {
            "id": todo.id,
            "content": todo.get_content(),
            "generated_by": todo.generated_by,
            "tokens_used": todo.tokens_used,
            "created_at": todo.created_at.isoformat(),
            "privacy_level": todo.privacy_level,
            "ai_usage": todo.ai_usage,
            "version_number": version_count,
        }
    }), 200


@todo_bp.route("/", methods=["PATCH"])
@login_required
def patch_todo():
    """Update the latest todo version in-place (e.g. checkbox toggles)."""
    data = request.get_json()
    content = data.get("content")

    if content is None:
        return jsonify({"error": "Content is required"}), 400
    if not content.strip():
        return jsonify({"error": "Content cannot be empty"}), 400

    todo = UserTodo.query.filter_by(
        user_id=current_user.id
    ).order_by(UserTodo.created_at.desc()).first()

    if not todo:
        return jsonify({"error": "No todo exists to update"}), 404

    todo.set_content(content)
    db.session.commit()

    version_count = UserTodo.query.filter_by(
        user_id=current_user.id
    ).count()

    return jsonify({
        "todo": {
            "id": todo.id,
            "content": todo.get_content(),
            "generated_by": todo.generated_by,
            "tokens_used": todo.tokens_used,
            "created_at": todo.created_at.isoformat(),
            "version_number": version_count,
        }
    }), 200


@todo_bp.route("/", methods=["PUT"])
@login_required
def update_todo():
    """Create a new todo version."""
    data = request.get_json()
    content = data.get("content")
    generated_by = data.get("generated_by", "user")

    if content is None:
        return jsonify({"error": "Content is required"}), 400
    if not content.strip():
        return jsonify({"error": "Content cannot be empty"}), 400

    todo = UserTodo(
        user_id=current_user.id,
        generated_by=generated_by,
        tokens_used=data.get("tokens_used", 0),
    )
    todo.set_content(content)
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
            "tokens_used": todo.tokens_used,
            "created_at": todo.created_at.isoformat(),
            "version_number": version_count,
        }
    }), 200


@todo_bp.route("/versions", methods=["GET"])
@login_required
def get_todo_versions():
    """List all todo versions for the current user."""
    todos = UserTodo.query.filter_by(
        user_id=current_user.id
    ).order_by(UserTodo.created_at.desc()).all()

    versions = []
    total = len(todos)
    for i, todo in enumerate(todos):
        versions.append({
            "id": todo.id,
            "generated_by": todo.generated_by,
            "tokens_used": todo.tokens_used,
            "created_at": todo.created_at.isoformat(),
            "version_number": total - i,
        })

    return jsonify({"versions": versions}), 200


@todo_bp.route("/versions/<int:version_id>", methods=["GET"])
@login_required
def get_todo_version(version_id):
    """Get a specific todo version's content."""
    todo = UserTodo.query.get_or_404(version_id)

    if todo.user_id != current_user.id:
        return jsonify({"error": "Unauthorized"}), 403

    return jsonify({
        "todo": {
            "id": todo.id,
            "content": todo.get_content(),
            "generated_by": todo.generated_by,
            "tokens_used": todo.tokens_used,
            "created_at": todo.created_at.isoformat(),
        }
    }), 200


@todo_bp.route("/revert/<int:version_id>", methods=["POST"])
@login_required
def revert_todo(version_id):
    """Create a new todo version from a historical one."""
    old_todo = UserTodo.query.get_or_404(version_id)

    if old_todo.user_id != current_user.id:
        return jsonify({"error": "Unauthorized"}), 403

    new_todo = UserTodo(
        user_id=current_user.id,
        generated_by="revert",
        tokens_used=0,
    )
    # Copy the encrypted content directly
    new_todo.content = old_todo.content
    db.session.add(new_todo)
    db.session.commit()

    version_count = UserTodo.query.filter_by(
        user_id=current_user.id
    ).count()

    return jsonify({
        "todo": {
            "id": new_todo.id,
            "content": new_todo.get_content(),
            "generated_by": new_todo.generated_by,
            "created_at": new_todo.created_at.isoformat(),
            "version_number": version_count,
        }
    }), 200


def _find_pending_todo_draft(llm_node_id, user_id):
    """Find the todo_pending draft by walking ancestor chain from llm_node_id."""
    llm_node = Node.query.get(llm_node_id)
    if not llm_node:
        return None, None

    current_node = llm_node
    visited = set()
    while current_node and current_node.id not in visited:
        visited.add(current_node.id)
        draft = Draft.query.filter_by(
            user_id=user_id,
            parent_id=current_node.id,
            label='todo_pending',
        ).first()
        if draft:
            return draft, current_node
        current_node = current_node.parent
    return None, None


def _start_todo_merge(draft, llm_node, user_id, confirm_node_id=None):
    """Kick off async background todo merge. No visible nodes created.

    The merge runs entirely in a Celery task:
    1. Reads the update summary from the LLM node's content
    2. Gets the current user todo
    3. Calls LLM with orient_apply_todo prompt to merge
    4. Saves result as new UserTodo
    5. Updates tool_calls_meta on the originating LLM node

    Args:
        confirm_node_id: Optional ID of the node where the user confirmed
            (apply_todo_changes). If provided, its meta is also updated
            with the final outcome.
    """
    merge_model = llm_node.llm_model or current_app.config.get(
        "DEFAULT_LLM_MODEL", "claude-opus-4.5"
    )

    # Delete ALL pending todo drafts for this user (not just the one found)
    all_pending = Draft.query.filter_by(
        user_id=draft.user_id,
        label='todo_pending',
    ).all()
    for d in all_pending:
        db.session.delete(d)

    # Update tool_calls_meta on the LLM node to record apply started
    update_tool_meta(llm_node, "propose_todo", {
        "apply_status": "started",
    })

    # When confirmed via UI button (no separate confirmation node),
    # add an apply_todo_changes entry on the proposal node itself
    # so NodeDetail shows the confirmation action.
    if not confirm_node_id:
        confirm_node_id = llm_node.id
        meta = []
        if llm_node.tool_calls_meta:
            try:
                meta = json.loads(llm_node.tool_calls_meta)
            except (json.JSONDecodeError, TypeError):
                meta = []
        # Only add if not already present
        if not any(e.get("name") == "apply_todo_changes" for e in meta):
            meta.append({
                "name": "apply_todo_changes",
                "status": "success",
                "apply_status": "started",
            })
            llm_node.tool_calls_meta = json.dumps(meta)

    db.session.commit()

    from backend.tasks.voice_todo_merge import apply_voice_todo
    task = apply_voice_todo.delay(
        llm_node.id, merge_model, user_id, confirm_node_id
    )

    return task.id


@todo_bp.route("/apply-draft", methods=["POST"])
@login_required
def apply_todo_draft():
    """
    Apply a pending todo draft created by the Voice update_todo tool.
    Kicks off an async orient_apply_todo LLM merge to apply the
    proposed changes to the full todo list.
    """
    data = request.get_json() or {}
    llm_node_id = data.get("llm_node_id")

    if not llm_node_id:
        return jsonify({"error": "llm_node_id is required"}), 400

    draft, llm_node = _find_pending_todo_draft(llm_node_id, current_user.id)

    if not draft:
        return jsonify({"error": "No pending todo changes found"}), 404

    if draft.user_id != current_user.id:
        return jsonify({"error": "Unauthorized"}), 403

    task_id = _start_todo_merge(draft, llm_node, current_user.id)

    return jsonify({
        "status": "started",
        "task_id": task_id,
        "llm_node_id": llm_node.id,
    }), 202

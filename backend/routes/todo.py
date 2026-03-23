from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user
from backend.models import Node, Draft, UserTodo
from backend.extensions import db

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


@todo_bp.route("/apply-draft", methods=["POST"])
@login_required
def apply_todo_draft():
    """
    Apply a pending todo draft created by the Voice update_todo tool.
    Walks ancestor nodes from the given llm_node_id to find the
    todo_pending draft, creates a new UserTodo, and deletes the draft.
    """
    data = request.get_json() or {}
    llm_node_id = data.get("llm_node_id")

    if not llm_node_id:
        return jsonify({"error": "llm_node_id is required"}), 400

    llm_node = Node.query.get(llm_node_id)
    if not llm_node:
        return jsonify({"error": "Node not found"}), 404

    # Walk ancestor chain to find todo_pending draft
    draft = None
    current_node = llm_node
    visited = set()
    while current_node and current_node.id not in visited:
        visited.add(current_node.id)
        # Check children of this node for drafts too (summary node is
        # a child of the LLM node)
        child_draft = Draft.query.filter_by(
            user_id=current_user.id,
            label='todo_pending',
        ).filter(
            Draft.parent_id.in_(
                [c.id for c in current_node.children] + [current_node.id]
            )
        ).first()
        if child_draft:
            draft = child_draft
            break
        current_node = current_node.parent

    if not draft:
        return jsonify({"error": "No pending todo changes found"}), 404

    if draft.user_id != current_user.id:
        return jsonify({"error": "Unauthorized"}), 403

    # Create new UserTodo from draft
    new_todo = UserTodo(
        user_id=current_user.id,
        generated_by="voice_session",
        tokens_used=0,
    )
    new_todo.set_content(draft.get_content())
    db.session.add(new_todo)
    db.session.delete(draft)
    db.session.commit()

    return jsonify({
        "status": "applied",
        "todo_id": new_todo.id,
    }), 200

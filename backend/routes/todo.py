import hashlib
import json
from flask import Blueprint, jsonify, request, current_app
from flask_login import login_required, current_user
from backend.models import Node, Draft, UserTodo
from backend.extensions import db
from backend.utils.privacy import AI_ALLOWED
from backend.utils.timefmt import iso_utc
from backend.utils.tool_meta import update_tool_meta

todo_bp = Blueprint("todo", __name__)

# A PATCH refused because the list changed since the client loaded it
# (#430): the answer's "code".
TODO_CHANGED_CODE = "todo_changed"
TODO_CHANGED_MESSAGE = (
    "Your todo list changed since this page loaded, so this change wasn't "
    "saved.")


def todo_revision(todo):
    """Names the stored text of a todo version. It changes with every
    write, an in-place PATCH included: the column holds ciphertext with a
    fresh key and nonce per write (or, unencrypted, the text itself). Only
    the stored column is hashed, so nothing is decrypted. A client sends it
    back as base_revision on PATCH (#430)."""
    stored = f"{todo.id}:{todo.content or ''}"
    return hashlib.sha256(stored.encode("utf-8")).hexdigest()


def _todo_json(todo, version_number):
    return {
        "id": todo.id,
        "content": todo.get_content(),
        "generated_by": todo.generated_by,
        "tokens_used": todo.tokens_used,
        "created_at": iso_utc(todo.created_at),
        "privacy_level": todo.privacy_level,
        "ai_usage": todo.ai_usage,
        "version_number": version_number,
        "revision": todo_revision(todo),
    }


def _version_count(user_id):
    return UserTodo.query.filter_by(user_id=user_id).count()


@todo_bp.route("/", methods=["GET"])
@login_required
def get_todo():
    """Get the latest todo version for the current user."""
    todo = UserTodo.query.filter_by(
        user_id=current_user.id
    ).order_by(UserTodo.created_at.desc()).first()

    if not todo:
        return jsonify({"todo": None}), 200

    return jsonify({
        "todo": _todo_json(todo, _version_count(current_user.id))
    }), 200


@todo_bp.route("/", methods=["PATCH"])
@login_required
def patch_todo():
    """Update the latest todo version in place (checkbox toggles, the row
    "+", quick-add).

    ``base_revision`` (optional): the revision of the version the client
    applied its change to. When the latest version is no longer that text
    (a todo merge, or an edit on another device or tab, landed in between),
    nothing is written and the answer is 409 ``{"error", "code":
    "todo_changed", "todo": <the latest version>}``, so the client can
    apply its own change to the latest list and save again (#430).
    Without it the request overwrites the latest version as before; the
    iPhone app re-fetches the list right before it saves.
    """
    data = request.get_json() or {}
    content = data.get("content")
    base_revision = data.get("base_revision")

    if content is None:
        return jsonify({"error": "Content is required"}), 400
    if not content.strip():
        return jsonify({"error": "Content cannot be empty"}), 400

    # Locked until the commit, so two saves can't both pass the check
    # against the same text.
    todo = UserTodo.query.filter_by(
        user_id=current_user.id
    ).order_by(UserTodo.created_at.desc()).with_for_update().first()

    if not todo:
        return jsonify({"error": "No todo exists to update"}), 404

    if base_revision is not None and base_revision != todo_revision(todo):
        latest = _todo_json(todo, _version_count(current_user.id))
        db.session.rollback()
        return jsonify({
            "error": TODO_CHANGED_MESSAGE,
            "code": TODO_CHANGED_CODE,
            "todo": latest,
        }), 409

    todo.set_content(content)
    db.session.commit()

    return jsonify({
        "todo": _todo_json(todo, _version_count(current_user.id))
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
        # ai_usage follows the user's global default, not a hardcoded
        # 'chat' (#191); live-gated out of prompts if the user opts out.
        ai_usage=current_user.default_ai_usage,
    )
    todo.set_content(content)
    db.session.add(todo)
    db.session.commit()

    return jsonify({
        "todo": _todo_json(todo, _version_count(current_user.id))
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
            "created_at": iso_utc(todo.created_at),
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
            "created_at": iso_utc(todo.created_at),
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
        # A revert reproduces a prior version, so copy its ai_usage (#191).
        ai_usage=old_todo.ai_usage,
    )
    # Copy the encrypted content directly
    new_todo.content = old_todo.content
    db.session.add(new_todo)
    db.session.commit()

    return jsonify({
        "todo": _todo_json(new_todo, _version_count(current_user.id))
    }), 200


# A todo merge sends the newest todo list and the proposal it applies to a
# model (orient_apply_todo), so it runs only when AI may read both (Peter,
# 2026-10-01: content marked 'none' is never sent to a model). Checked
# before a merge starts (the apply-draft route, the apply_todo_changes
# tool) and again when it runs (voice_todo_merge._run_merge), since the
# list can change in between. The web and the iPhone app show the message
# where the apply failed (the route's "error", the task's apply_error).
TODO_MERGE_REFUSED_MESSAGE = (
    "Your todo list is set to AI usage None, so Loore keeps it away from "
    "AI and didn't change it.")
TODO_PROPOSAL_REFUSED_MESSAGE = (
    "This reply is set to AI usage None, so Loore keeps it away from AI "
    "and didn't apply its todo changes.")


def todo_merge_refusal(user_id, proposal_node):
    """Why a todo merge may not send its inputs to a model (a message for
    the user), or None: the proposal node whose text the merge applies, or
    the newest todo list, has an ai_usage AI may not read. No todo list
    yet does not refuse: the merge then starts one from the proposal."""
    if (proposal_node is not None
            and proposal_node.ai_usage not in AI_ALLOWED):
        return TODO_PROPOSAL_REFUSED_MESSAGE
    todo = UserTodo.query.filter_by(user_id=user_id).order_by(
        UserTodo.created_at.desc()
    ).first()
    if todo is not None and todo.ai_usage not in AI_ALLOWED:
        return TODO_MERGE_REFUSED_MESSAGE
    return None


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
        "DEFAULT_LLM_MODEL", "claude-opus-4.6"
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

    403 ``{"error", "code": "ai_usage_none"}`` when AI may not read the
    todo list or the proposal (todo_merge_refusal); nothing is started.
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

    # Nothing starts, and the proposal stays pending, where AI may not read
    # the todo list or the proposal.
    refusal = todo_merge_refusal(current_user.id, llm_node)
    if refusal is not None:
        from backend.utils.llm_nodes import AI_USAGE_NONE_CODE
        return jsonify({"error": refusal, "code": AI_USAGE_NONE_CODE}), 403

    task_id = _start_todo_merge(draft, llm_node, current_user.id)

    return jsonify({
        "status": "started",
        "task_id": task_id,
        "llm_node_id": llm_node.id,
    }), 202

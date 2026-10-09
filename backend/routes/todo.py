import json
from flask import Blueprint, jsonify, request, current_app
from flask_login import login_required, current_user
from backend.models import Node, Draft, UserTodo
from backend.extensions import db
from backend.utils.privacy import AI_ALLOWED
from backend.utils.timefmt import iso_utc
from backend.utils.tool_meta import get_tool_meta_entry

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
            "created_at": iso_utc(todo.created_at),
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
            "created_at": iso_utc(todo.created_at),
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
        # ai_usage follows the user's global default, not a hardcoded
        # 'chat' (#191); live-gated out of prompts if the user opts out.
        ai_usage=current_user.default_ai_usage,
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
            "created_at": iso_utc(todo.created_at),
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

    version_count = UserTodo.query.filter_by(
        user_id=current_user.id
    ).count()

    return jsonify({
        "todo": {
            "id": new_todo.id,
            "content": new_todo.get_content(),
            "generated_by": new_todo.generated_by,
            "created_at": iso_utc(new_todo.created_at),
            "version_number": version_count,
        }
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


# A second apply of a proposal whose merge is running (a double click, a
# second tab): the answer's "code". The web and iPhone cards then show the
# merge as started and follow it (#434).
TODO_MERGE_RUNNING_CODE = "todo_merge_started"
TODO_MERGE_RUNNING_MESSAGE = "These todo changes are already being applied."


def _todo_merge_running_response():
    return jsonify({
        "error": TODO_MERGE_RUNNING_MESSAGE,
        "code": TODO_MERGE_RUNNING_CODE,
    }), 409


def _todo_merge_running(node_id, user_id):
    """Whether the user's proposal node has a merge running."""
    node = Node.query.get(node_id)
    if node is None or node.user_id != user_id:
        return False
    entry = get_tool_meta_entry(node, "propose_todo")
    return bool(entry) and entry.get("apply_status") == "started"


def restore_todo_draft(proposal_node, user_id):
    """After a failed merge, make its proposal applicable again (#434): put
    back the pending draft its start removed, so the card's "Apply again",
    the apply-draft route and the voice apply_todo_changes tool all find
    it. Not when the user has another todo proposal pending: a merge's
    start removes every pending one, so that one is newer, and only one
    proposal is pending at a time. Returns whether the proposal is pending
    again. The caller commits."""
    if Draft.query.filter_by(user_id=user_id, label='todo_pending').first():
        return False
    draft = Draft(user_id=user_id, parent_id=proposal_node.id,
                  label='todo_pending')
    draft.set_content("")
    db.session.add(draft)
    return True


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

    Returns the task id, or None when another request already started a
    merge of this proposal (a double click, a second tab): nothing is
    started then.
    """
    merge_model = llm_node.llm_model or current_app.config.get(
        "DEFAULT_LLM_MODEL", "claude-opus-4.6"
    )

    # The draft says "this proposal can be applied now". Deleting it claims
    # the merge: of two requests that found it, only the one whose delete
    # removed the row goes on (the other's waits for that commit and then
    # matches nothing). A failed merge puts the draft back (#434).
    draft_id, owner_id = draft.id, draft.user_id
    claimed = Draft.query.filter_by(id=draft_id).delete()
    if not claimed:
        return None

    # Delete ALL other pending todo drafts for this user too
    Draft.query.filter_by(
        user_id=owner_id,
        label='todo_pending',
    ).delete()

    meta = []
    if llm_node.tool_calls_meta:
        try:
            meta = json.loads(llm_node.tool_calls_meta)
        except (json.JSONDecodeError, TypeError):
            meta = []
    # Record apply started on the proposal; a merge applied again after a
    # failure drops the old error.
    for entry in meta:
        if entry.get("name") == "propose_todo":
            entry["apply_status"] = "started"
            entry.pop("apply_error", None)
            entry.pop("retryable", None)
            break

    # When confirmed via UI button (no separate confirmation node),
    # the apply_todo_changes entry on the proposal node itself shows
    # the confirmation action in NodeDetail.
    if not confirm_node_id:
        confirm_node_id = llm_node.id
        confirm = next((e for e in meta
                        if e.get("name") == "apply_todo_changes"), None)
        if confirm is None:
            meta.append({
                "name": "apply_todo_changes",
                "status": "success",
                "apply_status": "started",
            })
        else:
            confirm["apply_status"] = "started"
            confirm.pop("apply_error", None)
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
        if _todo_merge_running(llm_node_id, current_user.id):
            return _todo_merge_running_response()
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
    if task_id is None:
        return _todo_merge_running_response()

    return jsonify({
        "status": "started",
        "task_id": task_id,
        "llm_node_id": llm_node.id,
    }), 202

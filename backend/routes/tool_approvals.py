"""The user's approval of write tool calls a reply made in a thread whose
history holds text they did not write (Peter, 2026-11-12; see
utils/tool_approvals).

GET  /api/tool-approvals?node=<id>   the waiting calls in that node's
                                     thread (the node, its ancestors and
                                     its replies), oldest first
POST /api/tool-approvals/<id>/approve  run the call now
POST /api/tool-approvals/<id>/reject   drop it; nothing changes

Each call is the user's own; another user's id answers 404, like one that
does not exist.
"""
from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

from backend.extensions import db
from backend.models import Draft, Node
from backend.utils.privacy import can_user_access_node
from backend.utils.tool_approvals import (
    TOOL_PENDING_LABEL, decide, pending_for_user, serialize_pending,
)

tool_approvals_bp = Blueprint("tool_approvals", __name__)


def thread_node_ids(node):
    """*node*, its ancestors and its replies at every depth: ids only,
    one query per level (the level walk node_settings uses)."""
    ids = {node.id}
    current = node.parent
    seen = {node.id}
    while current is not None and current.id not in seen:
        seen.add(current.id)
        ids.add(current.id)
        current = current.parent
    frontier = [node.id]
    while frontier:
        rows = db.session.query(Node.id).filter(
            Node.parent_id.in_(frontier)).all()
        frontier = [r[0] for r in rows if r[0] not in ids]
        ids.update(frontier)
    return ids


def _own_pending(draft_id):
    draft = db.session.get(Draft, draft_id)
    if (draft is None or draft.user_id != current_user.id
            or draft.label != TOOL_PENDING_LABEL):
        return None
    return draft


@tool_approvals_bp.route("", methods=["GET"])
@login_required
def list_pending():
    try:
        node_id = int(request.args.get("node", ""))
    except ValueError:
        return jsonify({"error": "node is required"}), 400
    node = db.session.get(Node, node_id)
    if node is None or not can_user_access_node(node, current_user.id):
        return jsonify({"error": "Node not found"}), 404
    return jsonify({
        "pending": pending_for_user(current_user.id, thread_node_ids(node)),
    }), 200


def _decide(draft_id, approve):
    draft = _own_pending(draft_id)
    if draft is None:
        return jsonify({"error": "Not found"}), 404
    turn_node_id = serialize_pending(draft, current_user.id)["turn_node_id"]
    anchor = db.session.get(Node, draft.parent_id)
    outcome, result = decide(draft, current_user.id, approve)
    db.session.commit()
    # The reply's other waiting calls, so the dialog stays in step.
    remaining = []
    if anchor is not None:
        remaining = [p for p in pending_for_user(
            current_user.id, thread_node_ids(anchor))
            if p["turn_node_id"] == turn_node_id]
    body = {"outcome": outcome, "pending": remaining}
    if outcome == "failed":
        body["error"] = result.get("error") or "Nothing was saved."
    return jsonify(body), 200


@tool_approvals_bp.route("/<int:draft_id>/approve", methods=["POST"])
@login_required
def approve(draft_id):
    return _decide(draft_id, True)


@tool_approvals_bp.route("/<int:draft_id>/reject", methods=["POST"])
@login_required
def reject(draft_id):
    return _decide(draft_id, False)

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
from flask import Blueprint, current_app, jsonify, request
from flask_login import current_user, login_required

from backend.extensions import db
from backend.models import Draft, Node
from backend.utils.privacy import can_user_access_node
from backend.utils.error_privacy import describe_exception
from backend.utils.tool_approvals import (
    claim, decide, pending_for_user, release, serialize_pending,
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


# The answer when the call can't run now; it still waits.
NOT_RUN_MESSAGE = ("Loore couldn't make this change. It still waits for "
                   "your approval.")


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
    # One decision per call: a second request for it (a double tap,
    # another device, a retry) finds it claimed and gets the answer for a
    # call already decided.
    if not claim(draft_id, current_user.id):
        return jsonify({"error": "Not found"}), 404
    draft = db.session.get(Draft, draft_id)
    turn_node_id = serialize_pending(draft, current_user.id)["turn_node_id"]
    anchor = db.session.get(Node, draft.parent_id)
    try:
        outcome, result = decide(draft, current_user.id, approve)
        if outcome == "gone":
            # The reply was deleted: as for a call that does not exist.
            db.session.commit()
            return jsonify({"error": "Not found"}), 404
        if outcome == "failed":
            db.session.rollback()
            release(draft_id)
        else:
            db.session.commit()
    except Exception as e:  # noqa: BLE001 - the call stays waiting
        db.session.rollback()
        current_app.logger.exception(
            "Tool approval %s could not be decided: %s", draft_id,
            describe_exception(e))
        release(draft_id)
        return jsonify({"error": NOT_RUN_MESSAGE}), 500
    # The reply's other waiting calls, so the dialog stays in step.
    remaining = []
    if anchor is not None:
        remaining = [p for p in pending_for_user(
            current_user.id, thread_node_ids(anchor))
            if p["turn_node_id"] == turn_node_id]
    body = {"outcome": outcome, "pending": remaining}
    if outcome == "failed":
        # Nothing was saved; the call still waits (approve again, or
        # reject it).
        body["error"] = result.get("error") or NOT_RUN_MESSAGE
    return jsonify(body), 200


@tool_approvals_bp.route("/<int:draft_id>/approve", methods=["POST"])
@login_required
def approve(draft_id):
    return _decide(draft_id, True)


@tool_approvals_bp.route("/<int:draft_id>/reject", methods=["POST"])
@login_required
def reject(draft_id):
    return _decide(draft_id, False)

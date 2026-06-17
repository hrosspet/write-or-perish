from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user
from backend.models import Node, Draft
from backend.extensions import db
from backend.utils.feedback import submit_feedback_from_node
from backend.utils.tool_meta import update_tool_meta

feedback_bp = Blueprint("feedback", __name__)


@feedback_bp.route("/submit", methods=["POST"])
@login_required
def submit():
    """Send the feedback proposed on a pending node to the Loore team.

    Mirrors /github/create-issue: the feedback text lives in the visible
    LLM node content (under ### Feedback); this confirms + persists it only
    when the user clicks Send. The user is the gate — feedback is never sent
    without this explicit action (or the equivalent apply_feedback tool)."""
    data = request.get_json() or {}
    llm_node_id = data.get("llm_node_id")

    if not llm_node_id:
        return jsonify({"error": "llm_node_id is required"}), 400

    llm_node = Node.query.get(llm_node_id)
    if not llm_node:
        return jsonify({"error": "Node not found"}), 404

    # Find the pending draft by walking the ancestor chain.
    draft = None
    current_node = llm_node
    visited = set()
    while current_node and current_node.id not in visited:
        visited.add(current_node.id)
        draft = Draft.query.filter_by(
            user_id=current_user.id,
            parent_id=current_node.id,
            label='feedback_pending',
        ).first()
        if draft:
            break
        current_node = current_node.parent

    if not draft:
        return jsonify({"error": "No pending feedback found"}), 404

    if draft.user_id != current_user.id:
        return jsonify({"error": "Unauthorized"}), 403

    origin_node = Node.query.get(draft.parent_id)
    if not origin_node:
        return jsonify({"error": "Origin node not found"}), 404

    feedback, err = submit_feedback_from_node(origin_node, current_user.id)
    if err:
        return jsonify({"error": err}), 400

    db.session.delete(draft)
    update_tool_meta(origin_node, "propose_feedback", {
        "apply_status": "completed",
        "feedback_id": feedback.id,
    })
    db.session.commit()

    return jsonify({
        "status": "completed",
        "feedback_id": feedback.id,
    }), 200

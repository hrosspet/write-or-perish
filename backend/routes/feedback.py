from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user
from backend.models import Node
from backend.extensions import db
from backend.utils.feedback import submit_feedback_from_node
from backend.utils.privacy import can_user_access_node
from backend.utils.proposals import find_own_pending_proposal
from backend.utils.tool_meta import update_tool_meta

feedback_bp = Blueprint("feedback", __name__)


@feedback_bp.route("/submit", methods=["POST"])
@login_required
def submit():
    """Send the feedback proposed on a pending node to the Loore team.

    Mirrors /github/create-issue: the feedback text lives in the visible
    LLM node content (under ### Feedback); this confirms + persists it only
    when the user clicks Send. The user is the gate — feedback is never sent
    without this explicit action (or the equivalent apply_feedback tool).

    404 unless the node, or the nearest node above it with a pending
    feedback draft, is the user's own live feedback proposal."""
    data = request.get_json() or {}
    llm_node_id = data.get("llm_node_id")

    if not llm_node_id:
        return jsonify({"error": "llm_node_id is required"}), 400

    llm_node = Node.query.get(llm_node_id)
    if not llm_node or not can_user_access_node(llm_node, current_user.id):
        return jsonify({"error": "Node not found"}), 404

    # The pending proposal at or above the node, walking up its ancestors:
    # only the user's own live proposal (utils/proposals).
    draft, origin_node = find_own_pending_proposal(
        llm_node, current_user.id, 'feedback_pending')
    if not draft:
        return jsonify({"error": "No pending feedback found"}), 404

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

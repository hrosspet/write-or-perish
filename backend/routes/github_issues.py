from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user
from backend.models import Node
from backend.utils.client_platform import request_client
from backend.utils.issue_proposals import file_proposed_issues
from backend.utils.privacy import can_user_access_node
from backend.utils.proposals import find_own_pending_proposal
from backend.utils.tool_meta import get_tool_meta_entry

github_bp = Blueprint("github", __name__)


@github_bp.route("/create-issue", methods=["POST"])
@login_required
def create_issue():
    """Create the GitHub issue(s) of a pending proposal.

    404 unless the node, or the nearest node above it with a pending issue
    draft, is the user's own live issue proposal: nothing is read or sent
    to GitHub then."""
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
        llm_node, current_user.id, 'github_issue_pending')
    if not draft:
        return jsonify({"error": "No pending GitHub issue found"}), 404

    # Each issue in the reply is filed on its own: ``issue_index`` (0-based,
    # content order) files one; ``all`` files every issue not filed yet.
    # With neither, only the last issue is filed: what clients that don't
    # know about several issues (the iPhone app, old tabs) have always got.
    issue_index = data.get("issue_index")
    if issue_index is not None:
        try:
            issue_index = int(issue_index)
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid issue_index"}), 400

    filed_now, total, error, status = file_proposed_issues(
        origin_node, draft, current_user.username, request_client(),
        index=issue_index, all_unfiled=bool(data.get("all")))
    if error:
        return jsonify({"error": error}), status

    first = filed_now[0]
    entry = get_tool_meta_entry(origin_node, "propose_github_issue") or {}
    return jsonify({
        "status": entry.get("apply_status", "completed"),
        # The first issue filed by this call, for clients that know one.
        "issue_url": first["issue_url"],
        "issue_number": first["issue_number"],
        "issues": filed_now,
        "total": total,
    }), 200

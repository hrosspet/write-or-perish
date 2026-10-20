from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user
from backend.models import Node
from backend.extensions import db
from backend.utils.client_platform import request_client
from backend.utils.github import create_github_issue
from backend.utils.privacy import can_user_access_node
from backend.utils.proposals import find_own_pending_proposal
from backend.utils.tool_meta import parse_github_issue, update_tool_meta

github_bp = Blueprint("github", __name__)


@github_bp.route("/create-issue", methods=["POST"])
@login_required
def create_issue():
    """Create a GitHub issue from a pending Voice proposal.

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

    # Parse issue from the originating LLM node content
    issue_data = parse_github_issue(origin_node.get_content() or "")
    if not issue_data.get("title"):
        return jsonify({"error": "Could not parse issue from proposal"}), 400

    category = issue_data.get("category", "enhancement")
    if category not in ("bug", "feature", "enhancement"):
        category = "enhancement"

    try:
        gh_result = create_github_issue(
            title=issue_data["title"],
            description=issue_data.get("description", ""),
            category=category,
            username=current_user.username,
            # The app the user tapped "Create issue" in.
            platform=request_client(),
        )
    except (ValueError, RuntimeError) as e:
        return jsonify({"error": str(e)}), 500

    # Clean up draft
    db.session.delete(draft)

    # Update tool_calls_meta on the origin node
    update_tool_meta(origin_node, "propose_github_issue", {
        "apply_status": "completed",
        "issue_url": gh_result["url"],
        "issue_number": gh_result["number"],
    })

    db.session.commit()

    return jsonify({
        "status": "completed",
        "issue_url": gh_result["url"],
        "issue_number": gh_result["number"],
    }), 200

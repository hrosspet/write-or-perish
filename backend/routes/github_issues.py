from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user
from backend.models import Node, Draft
from backend.extensions import db
from backend.utils.github import create_github_issue
from backend.utils.tool_meta import parse_github_issue, update_tool_meta

github_bp = Blueprint("github", __name__)


@github_bp.route("/create-issue", methods=["POST"])
@login_required
def create_issue():
    """Create a GitHub issue from a pending Voice proposal."""
    data = request.get_json() or {}
    llm_node_id = data.get("llm_node_id")

    if not llm_node_id:
        return jsonify({"error": "llm_node_id is required"}), 400

    # Find the pending draft by walking ancestor chain
    llm_node = Node.query.get(llm_node_id)
    if not llm_node:
        return jsonify({"error": "Node not found"}), 404

    draft = None
    current_node = llm_node
    visited = set()
    while current_node and current_node.id not in visited:
        visited.add(current_node.id)
        draft = Draft.query.filter_by(
            user_id=current_user.id,
            parent_id=current_node.id,
            label='github_issue_pending',
        ).first()
        if draft:
            break
        current_node = current_node.parent

    if not draft:
        return jsonify({"error": "No pending GitHub issue found"}), 404

    if draft.user_id != current_user.id:
        return jsonify({"error": "Unauthorized"}), 403

    # Parse issue from the originating LLM node content
    origin_node = Node.query.get(draft.parent_id)
    if not origin_node:
        return jsonify({"error": "Origin node not found"}), 404

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
        )
    except (ValueError, RuntimeError) as e:
        return jsonify({"error": str(e)}), 500

    # Clean up draft
    db.session.delete(draft)

    # Update tool_calls_meta on the origin node
    update_tool_meta(origin_node, "create_github_issue", {
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

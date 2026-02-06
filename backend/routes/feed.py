from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user
from backend.models import Node
from backend.extensions import db
from backend.utils.privacy import accessible_nodes_filter

feed_bp = Blueprint("feed_bp", __name__)

@feed_bp.route("/feed", methods=["GET"])
@login_required
def get_feed():
    """
    Returns top-level nodes (nodes that have no parent) as the global feed.
    Only shows public nodes and the current user's own nodes.
    Supports pagination via ?page=1&per_page=20 query params.
    """
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 20, type=int)
    per_page = min(per_page, 100)  # cap max page size

    query = Node.query.filter(
        Node.parent_id.is_(None),
        accessible_nodes_filter(Node, current_user.id)
    ).order_by(Node.created_at.desc())
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    def make_preview(text, length=200):
        return text[:length] + ("..." if len(text) > length else "")

    nodes_list = []
    for node in pagination.items:
        nodes_list.append({
            "id": node.id,
            "preview": make_preview(node.get_content()),
            "node_type": node.node_type,
            "child_count": len(node.children),
            "created_at": node.created_at.isoformat(),
            "username": node.user.username if node.user else "Unknown"
        })

    return jsonify({
        "nodes": nodes_list,
        "has_more": pagination.has_next,
        "page": page,
        "total": pagination.total,
    }), 200
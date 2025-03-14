from flask import Blueprint, jsonify
from flask_login import login_required
from backend.models import Node
from backend.extensions import db

feed_bp = Blueprint("feed_bp", __name__)

@feed_bp.route("/feed", methods=["GET"])
@login_required
def get_feed():
    """
    Returns top-level nodes (nodes that have no parent) as the global feed.
    Each node is returned with a preview (first 200 characters), node type, child count,
    creation time, and the author's username.
    """
    # Query for nodes that are top-level (i.e. no parent_id).
    top_nodes = Node.query.filter(Node.parent_id.is_(None)).order_by(Node.created_at.desc()).all()

    # Helper function to create a preview string.
    def make_preview(text, length=200):
        return text[:length] + ("..." if len(text) > length else "")
    
    # Build list of node previews
    nodes_list = []
    for node in top_nodes:
        nodes_list.append({
            "id": node.id,
            "preview": make_preview(node.content),
            "node_type": node.node_type,
            "child_count": len(node.children),
            "created_at": node.created_at.isoformat(),
            # Assuming that the node's relationship to User is set up correctly.
            "username": node.user.username if node.user else "Unknown"
        })

    return jsonify({"nodes": nodes_list}), 200
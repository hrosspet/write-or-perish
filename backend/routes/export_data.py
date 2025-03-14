from flask import Blueprint, jsonify
from flask_login import login_required, current_user
from backend.models import Node, NodeVersion
from backend.extensions import db

export_bp = Blueprint("export_bp", __name__)

# Export all of the current user’s data (in JSON format for MVP).
@export_bp.route("/export", methods=["GET"])
@login_required
def export_data():
    user_data = {
        "user": {
            "id": current_user.id,
            "twitter_id": current_user.twitter_id,
            "username": current_user.username,
            "description": current_user.description,
            "created_at": current_user.created_at.isoformat(),
        },
        "nodes": [],
        "versions": []
    }
    nodes = Node.query.filter_by(user_id=current_user.id).all()
    for node in nodes:
        user_data["nodes"].append({
            "id": node.id,
            "content": node.content,
            "node_type": node.node_type,
            "parent_id": node.parent_id,
            "linked_node_id": node.linked_node_id,
            "token_count": node.token_count,
            "created_at": node.created_at.isoformat(),
            "updated_at": node.updated_at.isoformat()
        })
    versions = NodeVersion.query.join(Node, Node.id == NodeVersion.node_id).filter(Node.user_id == current_user.id).all()
    for version in versions:
        user_data["versions"].append({
            "id": version.id,
            "node_id": version.node_id,
            "content": version.content,
            "timestamp": version.timestamp.isoformat()
        })
    return jsonify(user_data), 200

# Delete all of the current user’s data from our app.
@export_bp.route("/delete_my_data", methods=["DELETE"])
@login_required
def delete_my_data():
    try:
        # Delete all node versions first, then nodes.
        NodeVersion.query.filter(
            NodeVersion.node_id.in_(db.session.query(Node.id).filter_by(user_id=current_user.id))
        ).delete(synchronize_session=False)
        Node.query.filter_by(user_id=current_user.id).delete(synchronize_session=False)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "Error deleting data", "details": str(e)}), 500
    return jsonify({"message": "All your app data has been deleted."}), 200

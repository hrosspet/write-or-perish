from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user
from backend.models import Node, User
from backend.extensions import db
from datetime import date

dashboard_bp = Blueprint("dashboard_bp", __name__)

def get_daily_tokens(user):
    today = date.today()
    tokens = db.session.query(db.func.sum(Node.distributed_tokens)).filter(
        Node.user_id == user.id,
        db.func.date(Node.created_at) == today
    ).scalar()
    return tokens or 0

def get_total_tokens(user):
    tokens = db.session.query(db.func.sum(Node.distributed_tokens)).filter(
        Node.user_id == user.id
    ).scalar()
    return tokens or 0

def get_global_tokens():
    tokens = db.session.query(db.func.sum(Node.distributed_tokens)).scalar()
    return tokens or 0


# Dashboard endpoint: only return top-level nodes (nodes with no parent)
@dashboard_bp.route("/", methods=["GET"])
@login_required
def get_dashboard():
    user_nodes = Node.query.filter_by(user_id=current_user.id, parent_id=None).order_by(Node.created_at.desc()).all()
    nodes_list = []
    for node in user_nodes:
        preview = node.content[:200] + ("..." if len(node.content) > 200 else "")
        nodes_list.append({
            "id": node.id,
            "preview": preview,
            "node_type": node.node_type,
            "child_count": len(node.children),
            "created_at": node.created_at.isoformat(),
            "username": node.user.username
        })
    dashboard = {
        "user": {
            "id": current_user.id,
            "username": current_user.username,
            "description": current_user.description,
            "accepted_terms_at": current_user.accepted_terms_at.isoformat() if current_user.accepted_terms_at else None,
            "approved": current_user.approved,
            "email": current_user.email
        },
        "stats": {
            "daily_tokens": get_daily_tokens(current_user),
            "total_tokens": get_total_tokens(current_user),
            "global_tokens": get_global_tokens(),
            "target_daily_tokens": 1000000  # the 1M tokens/day collective goal
        },
        "nodes": nodes_list
    }
    return jsonify(dashboard), 200


# Public view of any user’s dashboard; no private stats provided.
@dashboard_bp.route("/<string:username>", methods=["GET"])
@login_required
def get_public_dashboard(username):
    # Lookup the user by their (unique) handle (username).
    user = User.query.filter_by(username=username).first_or_404()
    
    # Get top-level nodes for the user.
    nodes = Node.query.filter_by(user_id=user.id, parent_id=None)\
                      .order_by(Node.created_at.desc()).all()
    nodes_list = []
    for node in nodes:
        preview = node.content[:200] + ("..." if len(node.content) > 200 else "")
        nodes_list.append({
            "id": node.id,
            "preview": preview,
            "node_type": node.node_type,
            "child_count": len(node.children),
            "created_at": node.created_at.isoformat(),
            "username": node.user.username
        })

    # Calculate token stats just as in the private dashboard.
    stats = {
        "daily_tokens": get_daily_tokens(user),
        "total_tokens": get_total_tokens(user),
        "global_tokens": get_global_tokens(),
        "target_daily_tokens": 1000000  # the 1M tokens/day target
    }
    
    dashboard = {
        "user": {
            "id": user.id,
            "username": user.username,
            "description": user.description
        },
        "stats": stats,
        "nodes": nodes_list
    }
    return jsonify(dashboard), 200


# New endpoint to update the user’s display handle and description.
@dashboard_bp.route("/user", methods=["PUT"])
@login_required
def update_user():
    data = request.get_json()
    new_username = data.get("username")
    new_description = data.get("description")
    new_email = data.get("email")

    if new_description and len(new_description) > 128:
        return jsonify({"error": "Description exceeds maximum length of 128 characters."}), 400

    if new_username:
        current_user.username = new_username
    if new_description is not None:
        current_user.description = new_description
    if new_email is not None:
        current_user.email = new_email

    try:
        db.session.commit()
        return jsonify({
            "message": "Profile updated successfully.",
            "user": {
                "id": current_user.id,
                "username": current_user.username,
                "description": current_user.description,
                "email": current_user.email,
                "approved": current_user.approved,
                "accepted_terms_at": current_user.accepted_terms_at.isoformat() if current_user.accepted_terms_at else None,
                "email": current_user.email
            }
        }), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "Failed to update profile.", "details": str(e)}), 500

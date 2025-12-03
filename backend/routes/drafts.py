from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user
from backend.models import Draft, Node
from backend.extensions import db

drafts_bp = Blueprint("drafts_bp", __name__)


@drafts_bp.route("/", methods=["GET"])
@login_required
def get_draft():
    """
    Get a draft for the current user.
    Query params:
      - node_id: If editing an existing node (optional)
      - parent_id: If creating a new node under a parent (optional)

    Returns the draft if found, or 404 if no draft exists.
    Drafts are private - only the owner can access them.
    """
    node_id = request.args.get("node_id", type=int)
    parent_id = request.args.get("parent_id", type=int)

    # Build query for the user's draft matching the context
    query = Draft.query.filter_by(user_id=current_user.id)

    if node_id:
        # Editing an existing node
        query = query.filter_by(node_id=node_id)
    else:
        # Creating a new node (possibly under a parent)
        query = query.filter_by(node_id=None)
        if parent_id:
            query = query.filter_by(parent_id=parent_id)
        else:
            query = query.filter_by(parent_id=None)

    draft = query.first()

    if not draft:
        return jsonify({"error": "No draft found"}), 404

    return jsonify({
        "id": draft.id,
        "content": draft.content,
        "node_id": draft.node_id,
        "parent_id": draft.parent_id,
        "created_at": draft.created_at.isoformat() + "Z",
        "updated_at": draft.updated_at.isoformat() + "Z"
    }), 200


@drafts_bp.route("/", methods=["POST"])
@login_required
def save_draft():
    """
    Create or update a draft for the current user.
    Body:
      - content: The draft content (required)
      - node_id: If editing an existing node (optional)
      - parent_id: If creating a new node under a parent (optional)

    If a draft already exists for this context, it will be updated.
    Drafts are private - only the owner can access them.
    """
    data = request.get_json() or {}
    content = data.get("content", "")
    node_id = data.get("node_id")
    parent_id = data.get("parent_id")

    # Validate node_id if provided - user must own the node they're editing
    if node_id:
        node = Node.query.get(node_id)
        if not node:
            return jsonify({"error": "Node not found"}), 404
        if node.user_id != current_user.id:
            return jsonify({"error": "Not authorized to edit this node"}), 403

    # Validate parent_id if provided - parent must exist
    if parent_id:
        parent = Node.query.get(parent_id)
        if not parent:
            return jsonify({"error": "Parent node not found"}), 404

    # Find existing draft for this context
    query = Draft.query.filter_by(user_id=current_user.id)

    if node_id:
        query = query.filter_by(node_id=node_id)
    else:
        query = query.filter_by(node_id=None)
        if parent_id:
            query = query.filter_by(parent_id=parent_id)
        else:
            query = query.filter_by(parent_id=None)

    draft = query.first()

    if draft:
        # Update existing draft
        draft.content = content
    else:
        # Create new draft
        draft = Draft(
            user_id=current_user.id,
            node_id=node_id,
            parent_id=parent_id,
            content=content
        )
        db.session.add(draft)

    db.session.commit()

    # Refresh to get the updated timestamp from database
    db.session.refresh(draft)

    updated_at_str = draft.updated_at.isoformat() + "Z"
    print(f"[DEBUG] Returning updated_at: {updated_at_str}")

    return jsonify({
        "id": draft.id,
        "content": draft.content,
        "node_id": draft.node_id,
        "parent_id": draft.parent_id,
        "created_at": draft.created_at.isoformat() + "Z",
        "updated_at": updated_at_str
    }), 200


@drafts_bp.route("/", methods=["DELETE"])
@login_required
def delete_draft():
    """
    Delete a draft for the current user.
    Query params:
      - node_id: If editing an existing node (optional)
      - parent_id: If creating a new node under a parent (optional)

    Called when user saves their work or explicitly discards the draft.
    Drafts are private - only the owner can delete them.
    """
    node_id = request.args.get("node_id", type=int)
    parent_id = request.args.get("parent_id", type=int)

    # Build query for the user's draft matching the context
    query = Draft.query.filter_by(user_id=current_user.id)

    if node_id:
        query = query.filter_by(node_id=node_id)
    else:
        query = query.filter_by(node_id=None)
        if parent_id:
            query = query.filter_by(parent_id=parent_id)
        else:
            query = query.filter_by(parent_id=None)

    draft = query.first()

    if not draft:
        return jsonify({"error": "No draft found"}), 404

    db.session.delete(draft)
    db.session.commit()

    return jsonify({"message": "Draft deleted"}), 200

from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user
from backend.models import Node, User
from backend.extensions import db
from backend.utils.privacy import accessible_nodes_filter
from sqlalchemy import or_, func

feed_bp = Blueprint("feed_bp", __name__)

@feed_bp.route("/feed", methods=["GET"])
@login_required
def get_feed():
    """
    Returns the current user's personal log: their own top-level and
    pinned nodes.  Supports pagination via ?page=1&per_page=20.
    """
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 20, type=int)
    per_page = min(per_page, 100)  # cap max page size

    query = Node.query.filter(
        or_(Node.parent_id.is_(None), Node.pinned_at.isnot(None)),
        or_(
            Node.user_id == current_user.id,
            Node.human_owner_id == current_user.id,
        ),
    ).order_by(func.coalesce(Node.pinned_at, Node.created_at).desc())
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    def make_preview(text, length=200):
        return text[:length] + ("..." if len(text) > length else "")

    # Map each row's root id to the most-recently-updated descendant the
    # current user can access. Drives the "click → newest node" jump on
    # Log cards. One recursive CTE per page (no N+1).
    root_ids = [n.id for n in pagination.items]
    newest_map = {}
    if root_ids:
        anchor = db.session.query(
            Node.id.label("id"),
            Node.updated_at.label("updated_at"),
            Node.id.label("root_id"),
        ).filter(Node.id.in_(root_ids)).cte(name="subtree", recursive=True)

        child = db.aliased(Node, flat=True)
        recursive = db.session.query(
            child.id,
            child.updated_at,
            anchor.c.root_id,
        ).join(anchor, child.parent_id == anchor.c.id).filter(
            accessible_nodes_filter(child, current_user.id)
        )
        subtree = anchor.union_all(recursive)

        rows = (
            db.session.query(subtree.c.root_id, subtree.c.id)
            .order_by(subtree.c.root_id, subtree.c.updated_at.desc())
            .distinct(subtree.c.root_id)
            .all()
        )
        newest_map = {root_id: nid for root_id, nid in rows}

    nodes_list = []
    for node in pagination.items:
        # If this is a system prompt root, skip to the first child
        display_node = node
        prompt_key = None
        if node.is_system_prompt:
            prompt = node.get_artifact("prompt")
            if prompt is None and node.user_prompt:
                prompt = node.user_prompt  # legacy fallback
            prompt_key = prompt.prompt_key if prompt else None
            first_child = Node.query.filter_by(parent_id=node.id).order_by(Node.created_at.asc()).first()
            if first_child:
                display_node = first_child

        # Determine human owner username for LLM nodes
        human_owner_username = None
        if display_node.node_type == "llm" and display_node.human_owner_id:
            human_owner = User.query.get(display_node.human_owner_id)
            if human_owner:
                human_owner_username = human_owner.username

        nodes_list.append({
            "id": display_node.id,
            "newest_node_id": newest_map.get(node.id, display_node.id),
            "preview": make_preview(display_node.get_content()),
            "node_type": display_node.node_type,
            "child_count": len(node.children),
            "created_at": display_node.created_at.isoformat(),
            "pinned_at": node.pinned_at.isoformat() if node.pinned_at else None,
            "username": node.user.username if node.user else "Unknown",
            "human_owner_username": human_owner_username,
            "llm_model": display_node.llm_model,
            "has_original_audio": bool(display_node.audio_original_url or display_node.streaming_transcription),
            "prompt_key": prompt_key,
        })

    return jsonify({
        "nodes": nodes_list,
        "has_more": pagination.has_next,
        "page": page,
        "total": pagination.total,
    }), 200
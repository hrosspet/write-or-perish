from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user
from backend.models import Node, User
from backend.extensions import db
from backend.utils.privacy import (
    accessible_nodes_filter, accessible_nodes_filter_ignoring_deleted,
)
from sqlalchemy import and_, or_, func

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

    # §4a Case 2: a soft-deleted thread root whose subtree still has an
    # alive accessible descendant must still surface in Log — otherwise
    # the live descendants disappear (no other entry point exists for
    # the owner). The recursive CTE below maps each accessible node to
    # its root and yields the set of roots whose subtree has at least
    # one alive accessible node.
    anchor = db.session.query(
        Node.id.label("id"),
        Node.deleted_at.label("deleted_at"),
        Node.id.label("root_id"),
    ).filter(
        Node.parent_id.is_(None),
        or_(
            Node.user_id == current_user.id,
            Node.human_owner_id == current_user.id,
        ),
    ).cte(name="user_thread_subtree", recursive=True)

    descendant = db.aliased(Node, flat=True)
    recursive = db.session.query(
        descendant.id,
        descendant.deleted_at,
        anchor.c.root_id,
    ).join(anchor, descendant.parent_id == anchor.c.id).filter(
        # Walk through tombstones so the alive_roots check below can find
        # alive descendants buried under one or more deleted ancestors.
        # The outer alive_roots_subq filter on subtree.deleted_at IS NULL
        # is what classifies which rows count as "alive descendant" —
        # this filter just controls which descendants the walk reaches.
        accessible_nodes_filter_ignoring_deleted(descendant, current_user.id),
    )
    subtree_cte = anchor.union_all(recursive)

    # Root IDs with at least one alive node in their subtree (the root
    # itself counts if alive; otherwise an accessible alive descendant).
    alive_roots_subq = (
        db.session.query(subtree_cte.c.root_id)
        .filter(subtree_cte.c.deleted_at.is_(None))
        .distinct()
        .subquery()
    )

    query = Node.query.filter(
        or_(Node.parent_id.is_(None), Node.pinned_at.isnot(None)),
        or_(
            Node.user_id == current_user.id,
            Node.human_owner_id == current_user.id,
        ),
        or_(
            # Alive (any kind: alive root or alive pinned non-root).
            Node.deleted_at.is_(None),
            # §4a Case 2: soft-deleted thread root whose subtree still
            # has an alive accessible descendant. Pinned non-roots that
            # are soft-deleted stay hidden — this branch only relaxes
            # the rule for thread roots.
            and_(
                Node.parent_id.is_(None),
                Node.id.in_(db.session.query(alive_roots_subq)),
            ),
        ),
    ).order_by(func.coalesce(Node.pinned_at, Node.created_at).desc())
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    def make_preview(text, length=200):
        return text[:length] + ("..." if len(text) > length else "")

    # Map each row's root id to the most-recently-updated descendant the
    # current user can access. Drives the "click → newest node" jump on
    # Log cards AND the §4a Case 2 preview swap (when the root is
    # soft-deleted, the card surfaces a live descendant). The recursive
    # arm walks through tombstones so a live grandchild buried under
    # deleted ancestors is still reachable; the outer query then
    # filters by deleted_at IS NULL so the navigation target itself is
    # always alive.
    root_ids = [n.id for n in pagination.items]
    newest_map = {}
    if root_ids:
        anchor = db.session.query(
            Node.id.label("id"),
            Node.updated_at.label("updated_at"),
            Node.deleted_at.label("deleted_at"),
            Node.id.label("root_id"),
        ).filter(Node.id.in_(root_ids)).cte(name="subtree", recursive=True)

        child = db.aliased(Node, flat=True)
        recursive = db.session.query(
            child.id,
            child.updated_at,
            child.deleted_at,
            anchor.c.root_id,
        ).join(anchor, child.parent_id == anchor.c.id).filter(
            accessible_nodes_filter_ignoring_deleted(child, current_user.id),
        )
        subtree = anchor.union_all(recursive)

        rows = (
            db.session.query(subtree.c.root_id, subtree.c.id)
            .filter(subtree.c.deleted_at.is_(None))
            .order_by(subtree.c.root_id, subtree.c.updated_at.desc())
            .distinct(subtree.c.root_id)
            .all()
        )
        newest_map = {root_id: nid for root_id, nid in rows}

    nodes_list = []
    for node in pagination.items:
        # Display-swap order:
        #   1. System prompt root → first alive child for the preview.
        #   2. Soft-deleted root with alive descendants (§4a Case 2) →
        #      newest_map's accessible descendant for the preview, since
        #      the root itself has no content to show.
        # `thread_root_id` always points at the actual root so the
        # frontend kebab targets the right node for delete.
        display_node = node
        prompt_key = None
        if node.is_system_prompt:
            prompt = node.get_artifact("prompt")
            if prompt is None and node.user_prompt:
                prompt = node.user_prompt  # legacy fallback
            prompt_key = prompt.prompt_key if prompt else None
            first_child = (
                Node.query
                .filter_by(parent_id=node.id)
                .filter(Node.deleted_at.is_(None))
                .order_by(Node.created_at.asc())
                .first()
            )
            if first_child:
                display_node = first_child
        elif node.deleted_at is not None:
            # §4a Case 2: surface a live descendant as the card preview.
            # newest_map is computed via `accessible_nodes_filter`, which
            # only returns alive accessible descendants — exactly what
            # we want here.
            newest_id = newest_map.get(node.id)
            if newest_id and newest_id != node.id:
                display_node = Node.query.get(newest_id) or node

        # Determine human owner username for LLM nodes
        human_owner_username = None
        if display_node.node_type == "llm" and display_node.human_owner_id:
            human_owner = User.query.get(display_node.human_owner_id)
            if human_owner:
                human_owner_username = human_owner.username

        # Count only alive children — tombstones don't contribute to the
        # visible reply count.
        alive_child_count = sum(
            1 for c in node.children if c.deleted_at is None
        )

        nodes_list.append({
            "id": display_node.id,
            "thread_root_id": node.id,
            "newest_node_id": newest_map.get(node.id, display_node.id),
            "preview": make_preview(display_node.get_content()),
            "node_type": display_node.node_type,
            "child_count": alive_child_count,
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
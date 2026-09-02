from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user
from backend.models import Node, User
from backend.extensions import db
from backend.utils.privacy import (
    PrivacyLevel,
    accessible_nodes_filter, accessible_nodes_filter_ignoring_deleted,
)
from backend.utils.timefmt import iso_utc
from backend.utils.encryption import prefetch_deks
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
            # Alive and NOT public (#228): the Log is the private diary —
            # public writing lives on the public page and in the Commons,
            # and public roots here would be duplicate echoes of the
            # private threads they were extracted from.
            and_(
                Node.deleted_at.is_(None),
                Node.privacy_level != PrivacyLevel.PUBLIC.value,
            ),
            # §4a Case 2: soft-deleted thread root whose subtree still
            # has an alive accessible descendant — ANY privacy, incl.
            # public: a deleted public root leaves the Commons feed, so
            # the Log is the owner's only entry point to what's still
            # alive underneath. Pinned non-roots that are soft-deleted
            # stay hidden — this branch only relaxes the rule for roots.
            and_(
                Node.parent_id.is_(None),
                Node.deleted_at.isnot(None),
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

    # Phase 1 — pick each card's display node without decrypting anything,
    # with the per-card lookups batched (they were one query per card):
    #   1. System prompt root → first alive child for the preview.
    #   2. Soft-deleted root with alive descendants (§4a Case 2) →
    #      newest_map's accessible descendant for the preview, since
    #      the root itself has no content to show.
    # `thread_root_id` always points at the actual root so the
    # frontend kebab targets the right node for delete.
    items = list(pagination.items)
    sys_root_ids = [n.id for n in items if n.is_system_prompt]
    first_child_map = {}
    if sys_root_ids:
        for c in (
            Node.query
            .filter(Node.parent_id.in_(sys_root_ids), Node.deleted_at.is_(None))
            .order_by(Node.created_at.asc())
            .all()
        ):
            first_child_map.setdefault(c.parent_id, c)
    # newest_map is computed via `accessible_nodes_filter`, which only
    # returns alive accessible descendants — exactly what Case 2 wants.
    newest_needed = [
        newest_map[n.id] for n in items
        if n.deleted_at is not None and not n.is_system_prompt
        and newest_map.get(n.id) and newest_map[n.id] != n.id
    ]
    newest_nodes = (
        {n.id: n for n in Node.query.filter(Node.id.in_(newest_needed)).all()}
        if newest_needed else {}
    )
    # Count only alive children — tombstones don't contribute to the
    # visible reply count.
    alive_child_counts = dict(
        db.session.query(Node.parent_id, func.count(Node.id))
        .filter(Node.parent_id.in_(root_ids), Node.deleted_at.is_(None))
        .group_by(Node.parent_id).all()
    ) if root_ids else {}

    cards = []
    for node in items:
        display_node = node
        prompt_key = None
        if node.is_system_prompt:
            prompt = node.get_artifact("prompt")
            if prompt is None and node.user_prompt:
                prompt = node.user_prompt  # legacy fallback
            prompt_key = prompt.prompt_key if prompt else None
            display_node = first_child_map.get(node.id, node)
        elif node.deleted_at is not None:
            display_node = newest_nodes.get(newest_map.get(node.id), node)
        cards.append((node, display_node, prompt_key))

    # Phase 2 — one concurrent KMS batch for every preview on the page.
    # Decrypting inside the loop cost a cold worker ~80 ms per card, in
    # sequence (~1.6 s for a page of 20).
    prefetch_deks(display_node.content for _, display_node, _ in cards)

    # Phase 3 — serialize (previews are cache hits now).
    nodes_list = []
    for node, display_node, prompt_key in cards:
        # Determine human owner username for LLM nodes
        human_owner_username = None
        if display_node.node_type == "llm" and display_node.human_owner_id:
            human_owner = User.query.get(display_node.human_owner_id)
            if human_owner:
                human_owner_username = human_owner.username

        nodes_list.append({
            "id": display_node.id,
            "thread_root_id": node.id,
            "newest_node_id": newest_map.get(node.id, display_node.id),
            "preview": make_preview(display_node.get_content()),
            "node_type": display_node.node_type,
            "child_count": alive_child_counts.get(node.id, 0),
            "created_at": iso_utc(display_node.created_at),
            "pinned_at": iso_utc(node.pinned_at),
            "username": node.user.username if node.user else "Unknown",
            "human_owner_username": human_owner_username,
            "llm_model": display_node.llm_model,
            "origin": display_node.origin,
            "has_original_audio": bool(display_node.audio_original_url or display_node.streaming_transcription),
            "prompt_key": prompt_key,
        })

    return jsonify({
        "nodes": nodes_list,
        "has_more": pagination.has_next,
        "page": page,
        "total": pagination.total,
    }), 200
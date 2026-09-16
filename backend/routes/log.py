from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user
from backend.models import Node, User, Thread, NodeContextArtifact, UserPrompt
from backend.extensions import db
from backend.utils.privacy import (
    PrivacyLevel, accessible_nodes_filter_ignoring_deleted,
)
from backend.utils.timefmt import iso_utc
from backend.utils.encryption import prefetch_deks
from sqlalchemy import and_, or_, func
from sqlalchemy.orm.attributes import set_committed_value

log_bp = Blueprint("log_bp", __name__)


def _preload_context_artifacts(nodes):
    """Fill `context_artifacts` on each node from one query.

    Both `Node.is_system_prompt` and `Node.get_content()` read that
    relationship, and the default lazy load is one query per node — a
    page of 20 cards paid 20+ queries. Nodes with no rows get an empty
    list so the lazy loader never fires for them either.
    """
    pending = [n for n in nodes if "context_artifacts" not in n.__dict__]
    if not pending:
        return
    by_node = {}
    for row in NodeContextArtifact.query.filter(
        NodeContextArtifact.node_id.in_([n.id for n in pending])
    ).all():
        by_node.setdefault(row.node_id, []).append(row)
    for n in pending:
        set_committed_value(n, "context_artifacts", by_node.get(n.id, []))


@log_bp.route("/log", methods=["GET"])
@login_required
def get_log():
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
    ).order_by(
        func.coalesce(Node.pinned_at, Node.created_at).desc(),
        # Tiebreak: imports stamp many roots with the same second, and
        # LIMIT/OFFSET over a non-total order can repeat one row on two
        # pages and skip another.
        Node.id.desc(),
    )
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
    # frontend kebab targets the right node for rename and delete.
    items = list(pagination.items)

    # Pinned replies are rows too; their thread root is up the parent
    # chain, not the row itself. One recursive walk for the page.
    thread_root_of = {n.id: n.id for n in items}
    pinned_reply_ids = [n.id for n in items if n.parent_id is not None]
    if pinned_reply_ids:
        up = db.session.query(
            Node.id.label("start_id"),
            Node.id.label("id"),
            Node.parent_id.label("parent_id"),
        ).filter(Node.id.in_(pinned_reply_ids)).cte(name="ancestors", recursive=True)
        parent = db.aliased(Node, flat=True)
        up = up.union_all(
            db.session.query(up.c.start_id, parent.id, parent.parent_id)
            .join(up, parent.id == up.c.parent_id)
        )
        for start_id, root_id in (
            db.session.query(up.c.start_id, up.c.id)
            .filter(up.c.parent_id.is_(None)).all()
        ):
            thread_root_of[start_id] = root_id
    thread_root_ids = list(set(thread_root_of.values()))

    # Which rows are system prompts. The stamped column answers for
    # current roots; roots from before the column only carry a prompt
    # link, so load the page's links in one query and their UserPrompt
    # rows in another (into the identity map, so a later
    # `get_content()` on such a root is a no-query hit too).
    _preload_context_artifacts(items)
    prompt_key_of = {n.id: n.prompt_key for n in items if n.prompt_key}
    legacy_link_of = {
        n.id: n.get_artifact_id("prompt") for n in items if not n.prompt_key
    }
    legacy_prompt_ids = {pid for pid in legacy_link_of.values() if pid}
    if legacy_prompt_ids:
        key_of_prompt = {
            p.id: p.prompt_key
            for p in UserPrompt.query.filter(UserPrompt.id.in_(legacy_prompt_ids)).all()
        }
        for node_id, prompt_id in legacy_link_of.items():
            if prompt_id in key_of_prompt:
                prompt_key_of[node_id] = key_of_prompt[prompt_id]

    sys_root_ids = [n.id for n in items if n.id in prompt_key_of]
    first_child_map = {}
    if sys_root_ids:
        for c in (
            Node.query
            .filter(Node.parent_id.in_(sys_root_ids), Node.deleted_at.is_(None))
            .order_by(Node.created_at.asc())
            .all()
        ):
            first_child_map.setdefault(c.parent_id, c)
    # newest_map's walk uses `accessible_nodes_filter_ignoring_deleted`
    # so it can pass through tombstones; the outer
    # `subtree.c.deleted_at.is_(None)` filter is what keeps deleted nodes
    # out of the result. Do not drop that filter as redundant.
    newest_needed = [
        newest_map[n.id] for n in items
        if n.deleted_at is not None and n.id not in prompt_key_of
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

    # User-given thread names live in the thread table, keyed by root;
    # one query for the page.
    thread_rows = {
        t.root_node_id: t
        for t in Thread.query.filter(Thread.root_node_id.in_(thread_root_ids)).all()
    } if thread_root_ids else {}

    cards = []
    for node in items:
        display_node = node
        prompt_key = prompt_key_of.get(node.id)
        if prompt_key is not None:
            display_node = first_child_map.get(node.id, node)
        elif node.deleted_at is not None:
            display_node = newest_nodes.get(newest_map.get(node.id), node)
        cards.append((node, display_node, prompt_key))

    # Phase 2 — one concurrent KMS batch for every preview (and thread
    # name) on the page. Decrypting inside the loop cost a cold worker
    # ~80 ms per card, in sequence (~1.6 s for a page of 20).
    # `get_content()` also checks each display node for a linked prompt;
    # feed that from one query rather than one per card.
    _preload_context_artifacts([display_node for _, display_node, _ in cards])
    prefetch_deks(
        [display_node.content for _, display_node, _ in cards]
        + [t.name for t in thread_rows.values()]
    )

    # Phase 3 — serialize (previews are cache hits now).
    nodes_list = []
    for node, display_node, prompt_key in cards:
        thread_root_id = thread_root_of[node.id]
        # Determine human owner username for LLM nodes
        human_owner_username = None
        if display_node.node_type == "llm" and display_node.human_owner_id:
            human_owner = User.query.get(display_node.human_owner_id)
            if human_owner:
                human_owner_username = human_owner.username

        nodes_list.append({
            "id": display_node.id,
            "thread_root_id": thread_root_id,
            "newest_node_id": newest_map.get(node.id, display_node.id),
            # Keyed by the root (the thread), never by the display node.
            "thread_name": (
                thread_rows[thread_root_id].get_name()
                if thread_root_id in thread_rows else None
            ),
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
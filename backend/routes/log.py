from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user
from backend.models import Node, User, Thread, NodeContextArtifact, UserPrompt
from backend.extensions import db
from backend.utils.privacy import (
    PrivacyLevel, accessible_nodes_filter_ignoring_deleted,
)
from backend.utils.timefmt import iso_utc
from backend.utils.encryption import prefetch_deks
from backend.utils.thread_tree import thread_root_of
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
    pinned nodes.  Paginated via ?page=1&per_page=20, or ?offset=N
    (the frontend sends how many cards it holds, so a card removed or
    added client-side doesn't shift the next page).
    """
    page = max(request.args.get("page", 1, type=int), 1)
    per_page = request.args.get("per_page", 20, type=int)
    per_page = max(1, min(per_page, 100))  # cap max page size
    offset = request.args.get("offset", type=int)
    if offset is None or offset < 0:
        offset = (page - 1) * per_page

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
    items = query.offset(offset).limit(per_page + 1).all()
    has_more = len(items) > per_page
    items = items[:per_page]
    total = query.order_by(None).count()

    def make_preview(text, length=200):
        return text[:length] + ("..." if len(text) > length else "")

    # Pinned replies are rows too; their thread root is up the parent
    # chain. The card targets that root (rename, delete, thread name)
    # only when the root is the user's own: a reply pinned in someone
    # else's thread keeps targeting the reply, and the other user's
    # (private, encrypted) thread name never reaches this user's Log.
    thread_root_of_row = {n.id: n.id for n in items}
    pinned_reply_ids = [n.id for n in items if n.parent_id is not None]
    if pinned_reply_ids:
        root_of = thread_root_of(pinned_reply_ids)
        own_roots = {
            rid for (rid,) in db.session.query(Node.id).filter(
                Node.id.in_(set(root_of.values())),
                or_(
                    Node.user_id == current_user.id,
                    Node.human_owner_id == current_user.id,
                ),
            ).all()
        }
        for reply_id, root_id in root_of.items():
            if root_id in own_roots:
                thread_root_of_row[reply_id] = root_id
    thread_root_ids = list(set(thread_root_of_row.values()))

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

    # One walk down from every row over the descendants the user can
    # access, then two picks per row from it (window functions, so the
    # SQL is the same on Postgres and the sqlite tests):
    #   newest_map: the most recently updated alive node — the "click →
    #     newest node" jump, and the §4a Case 2 preview swap when the
    #     row is a soft-deleted root with alive descendants.
    #   first_alive_map: for system-prompt roots, the shallowest, then
    #     oldest alive descendant — the entry the card is titled by and
    #     previews from. Direct children first (the first alive entry, as
    #     before); when none is alive (the entries were deleted "this
    #     node only", or before the delete dialog offered the prompt too)
    #     the first alive grandchild, typically the AI reply, instead of
    #     the prompt text.
    # The recursive arm walks through tombstones so an alive grandchild
    # buried under deleted ancestors is still reachable; the outer
    # `deleted_at IS NULL` filter is what keeps deleted nodes out. Do not
    # drop that filter as redundant.
    root_ids = [n.id for n in items]
    newest_map = {}
    first_alive_map = {}
    sys_root_ids = [n.id for n in items if n.id in prompt_key_of]
    if root_ids:
        anchor = db.session.query(
            Node.id.label("id"),
            Node.updated_at.label("updated_at"),
            Node.created_at.label("created_at"),
            Node.deleted_at.label("deleted_at"),
            Node.id.label("root_id"),
            db.literal(0).label("depth"),
        ).filter(Node.id.in_(root_ids)).cte(name="subtree", recursive=True)

        child = db.aliased(Node, flat=True)
        recursive = db.session.query(
            child.id,
            child.updated_at,
            child.created_at,
            child.deleted_at,
            anchor.c.root_id,
            (anchor.c.depth + 1).label("depth"),
        ).join(anchor, child.parent_id == anchor.c.id).filter(
            accessible_nodes_filter_ignoring_deleted(child, current_user.id),
        )
        subtree = anchor.union_all(recursive)

        def first_per_root(order_by, only_root_ids=None):
            ranked = db.session.query(
                subtree.c.root_id,
                subtree.c.id,
                func.row_number().over(
                    partition_by=subtree.c.root_id, order_by=order_by,
                ).label("rn"),
            ).filter(subtree.c.deleted_at.is_(None))
            if only_root_ids is not None:
                ranked = ranked.filter(
                    subtree.c.root_id.in_(only_root_ids),
                    subtree.c.id != subtree.c.root_id,
                )
            ranked = ranked.subquery()
            return dict(
                db.session.query(ranked.c.root_id, ranked.c.id)
                .filter(ranked.c.rn == 1).all()
            )

        newest_map = first_per_root(
            (subtree.c.updated_at.desc(), subtree.c.id.desc()),
        )
        if sys_root_ids:
            first_alive_map = first_per_root(
                (subtree.c.depth.asc(), subtree.c.created_at.asc(), subtree.c.id.asc()),
                only_root_ids=sys_root_ids,
            )

    # Phase 1 — pick each card's display node without decrypting anything:
    #   1. System prompt root → its first alive entry (first_alive_map);
    #      with nothing alive underneath, the root itself.
    #   2. Soft-deleted root with alive descendants (§4a Case 2) →
    #      newest_map's accessible descendant, since the root itself has
    #      no content to show.
    # `thread_root_id` points at the actual root (when it is the user's
    # own) so the frontend kebab targets the right node for rename and
    # delete.
    display_ids = set()
    for n in items:
        if n.id in prompt_key_of:
            display_ids.add(first_alive_map.get(n.id, n.id))
        elif n.deleted_at is not None:
            display_ids.add(newest_map.get(n.id, n.id))
    display_needed = [nid for nid in display_ids if nid not in root_ids]
    display_nodes = (
        {n.id: n for n in Node.query.filter(Node.id.in_(display_needed)).all()}
        if display_needed else {}
    )
    by_id = {n.id: n for n in items}
    by_id.update(display_nodes)
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
            display_node = by_id.get(first_alive_map.get(node.id), node)
        elif node.deleted_at is not None:
            display_node = by_id.get(newest_map.get(node.id), node)
        cards.append((node, display_node, prompt_key))

    # Phase 2 — one concurrent KMS batch for every preview (and thread
    # name) on the page. Decrypting inside the loop cost a cold worker
    # ~80 ms per card, in sequence (~1.6 s for a page of 20).
    # `get_content()` also checks each display node for a linked prompt;
    # supply that from one query rather than one per card. A display
    # node WITH a linked prompt (a session with nothing alive under its
    # root) reads the UserPrompt's content, so those rows are loaded
    # here and their ciphertext joins the batch.
    _preload_context_artifacts([display_node for _, display_node, _ in cards])
    prompt_id_of_display = {
        display_node.id: display_node.get_artifact_id("prompt")
        for _, display_node, _ in cards
    }
    prompt_ids = {pid for pid in prompt_id_of_display.values() if pid}
    prompts = {
        p.id: p for p in UserPrompt.query.filter(UserPrompt.id.in_(prompt_ids)).all()
    } if prompt_ids else {}
    ciphertexts = []
    for _, display_node, _ in cards:
        prompt = prompts.get(prompt_id_of_display.get(display_node.id))
        ciphertexts.append(prompt.content if prompt else display_node.content)
    prefetch_deks(ciphertexts + [t.name for t in thread_rows.values()])

    # Phase 3 — serialize (previews are cache hits now).
    nodes_list = []
    for node, display_node, prompt_key in cards:
        thread_root_id = thread_root_of_row[node.id]
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
        "has_more": has_more,
        "page": page,
        "offset": offset,
        "total": total,
    }), 200

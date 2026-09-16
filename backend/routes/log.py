from datetime import datetime

from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user
from backend.models import Node, User, Thread, NodeContextArtifact, UserPrompt
from backend.extensions import db
from backend.utils.privacy import PrivacyLevel
from backend.utils.timefmt import iso_utc
from backend.utils.encryption import prefetch_deks
from backend.utils.thread_tree import (
    thread_root_of, subtree_walk, alive_child_counts,
)
from sqlalchemy import and_, or_, func, case
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


# Log rows are ordered by (pinned_at or created_at, id), newest first. A
# page's cursor names the last row of that order, so the next page is
# "everything after it": rows removed (a delete, here or in another tab)
# or added since don't shift what comes next, unlike an offset.
_SORT_KEY = func.coalesce(Node.pinned_at, Node.created_at)


def _cursor_of(node):
    return f"{(node.pinned_at or node.created_at).isoformat()}|{node.id}"


def _parse_cursor(raw):
    """(sort timestamp, id) from a cursor string; ValueError if malformed."""
    ts, _, node_id = raw.rpartition("|")
    return datetime.fromisoformat(ts), int(node_id)


@log_bp.route("/log", methods=["GET"])
@login_required
def get_log():
    """
    Returns the current user's personal log: their own top-level and
    pinned nodes.  Pages continue from `?cursor=` (the previous
    response's `next_cursor`); `?page=N&per_page=M` still works for a
    fixed window.
    """
    page = max(request.args.get("page", 1, type=int), 1)
    per_page = request.args.get("per_page", 20, type=int)
    per_page = max(1, min(per_page, 100))  # cap max page size
    cursor = request.args.get("cursor") or None
    if cursor is not None:
        try:
            cursor_ts, cursor_id = _parse_cursor(cursor)
        except ValueError:
            return jsonify({"error": "Invalid cursor"}), 400

    # §4a Case 2: a soft-deleted thread root whose subtree still has an
    # alive accessible descendant must still surface in Log — otherwise
    # the live descendants disappear (no other entry point exists for
    # the owner). One walk (thread_tree.subtree_walk, the shared
    # definition of what is left under a root) seeded from the user's
    # *deleted* roots only — alive roots need no check, and on a heavy
    # importer they are the whole corpus — yields the roots with at
    # least one alive accessible node underneath. The seeds are deleted,
    # so depth-0 rows never pass the alive filter.
    deleted_roots = db.session.query(Node.id).filter(
        Node.parent_id.is_(None),
        Node.deleted_at.isnot(None),
        or_(
            Node.user_id == current_user.id,
            Node.human_owner_id == current_user.id,
        ),
    )
    deleted_walk = subtree_walk(
        deleted_roots, current_user.id, name="deleted_root_walk",
    )
    alive_roots = (
        db.session.query(deleted_walk.c.root_id)
        .filter(deleted_walk.c.deleted_at.is_(None))
        .distinct()
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
                Node.id.in_(alive_roots),
            ),
        ),
    ).order_by(
        _SORT_KEY.desc(),
        # Tiebreak: imports stamp many roots with the same second, and
        # LIMIT/OFFSET over a non-total order can repeat one row on two
        # pages and skip another.
        Node.id.desc(),
    )
    if cursor is not None:
        query = query.filter(or_(
            _SORT_KEY < cursor_ts,
            and_(_SORT_KEY == cursor_ts, Node.id < cursor_id),
        ))
    else:
        query = query.offset((page - 1) * per_page)
    items = query.limit(per_page + 1).all()
    has_more = len(items) > per_page
    items = items[:per_page]
    next_cursor = _cursor_of(items[-1]) if items and has_more else None

    def make_preview(text, length=200):
        return text[:length] + ("..." if len(text) > length else "")

    # Pinned replies are rows too; their thread root is up the parent
    # chain. The card targets that root (rename, delete, thread name)
    # only when the root is the user's own: a reply pinned in someone
    # else's thread keeps targeting the reply, and the other user's
    # (private, encrypted) thread name never reaches this user's Log.
    # Such a card can't be renamed either (only a root can be named),
    # which `can_rename` tells the kebab.
    thread_root_of_row = {n.id: n.id for n in items}
    own_root_ids = {n.id for n in items if n.parent_id is None}
    pinned_reply_ids = [n.id for n in items if n.parent_id is not None]
    if pinned_reply_ids:
        root_of = thread_root_of(pinned_reply_ids)
        own_root_ids.update(
            rid for (rid,) in db.session.query(Node.id).filter(
                Node.id.in_(set(root_of.values())),
                or_(
                    Node.user_id == current_user.id,
                    Node.human_owner_id == current_user.id,
                ),
            ).all()
        )
        for reply_id, root_id in root_of.items():
            if root_id in own_root_ids:
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
    # access (thread_tree.subtree_walk — the same definition of "what is
    # left under a root" the delete dialog uses), then two picks per row
    # from that single result, as two window columns of one SELECT:
    #   newest_map: the most recently updated alive node — the "click →
    #     newest node" jump, and the §4a Case 2 preview swap when the
    #     row is a soft-deleted root with alive descendants.
    #   first_alive_map: for system-prompt roots, the shallowest, then
    #     oldest alive descendant — the entry the card is titled by and
    #     previews from. Direct children first (the first alive entry, as
    #     before); when none is alive (the entries were deleted "this
    #     node only", or before the delete dialog offered the prompt too)
    #     the first alive grandchild, typically the AI reply, instead of
    #     the prompt text. The root sorts last, so it is picked only when
    #     nothing else is alive.
    # The walk steps through tombstones so an alive grandchild buried
    # under deleted ancestors is still reachable; the `deleted_at IS
    # NULL` filter here is what keeps deleted nodes out. Do not drop that
    # filter as redundant.
    root_ids = [n.id for n in items]
    newest_map = {}
    first_alive_map = {}
    if root_ids:
        walk = subtree_walk(root_ids, current_user.id)
        ranked = db.session.query(
            walk.c.root_id,
            walk.c.id,
            func.row_number().over(
                partition_by=walk.c.root_id,
                order_by=(walk.c.updated_at.desc(), walk.c.id.desc()),
            ).label("rn_newest"),
            func.row_number().over(
                partition_by=walk.c.root_id,
                order_by=(
                    case((walk.c.depth == 0, 1), else_=0).asc(),
                    walk.c.depth.asc(), walk.c.created_at.asc(), walk.c.id.asc(),
                ),
            ).label("rn_first"),
        ).filter(walk.c.deleted_at.is_(None)).subquery()
        for root_id, node_id, rn_newest, rn_first in db.session.query(
            ranked.c.root_id, ranked.c.id, ranked.c.rn_newest, ranked.c.rn_first,
        ).filter(or_(ranked.c.rn_newest == 1, ranked.c.rn_first == 1)).all():
            if rn_newest == 1:
                newest_map[root_id] = node_id
            if rn_first == 1 and node_id != root_id:
                first_alive_map[root_id] = node_id

    # Phase 1 — pick each card's display node without decrypting anything:
    #   1. System prompt root → its first alive entry (first_alive_map);
    #      with nothing alive underneath, the root itself.
    #   2. Soft-deleted root with alive descendants (§4a Case 2) →
    #      newest_map's accessible descendant, since the root itself has
    #      no content to show.
    # A node is one card at most: when a root's display node is itself a
    # pinned row of this page (an entry pinned in its own session), the
    # row that sorts first keeps the card and the other is skipped.
    # `thread_root_id` points at the actual root (when it is the user's
    # own) so the frontend kebab targets the right node for rename and
    # delete.
    display_id_of = {}
    shown = set()
    for n in items:
        if n.id in prompt_key_of:
            display_id = first_alive_map.get(n.id, n.id)
        elif n.deleted_at is not None:
            display_id = newest_map.get(n.id, n.id)
        else:
            display_id = n.id
        if display_id in shown:
            continue
        shown.add(display_id)
        display_id_of[n.id] = display_id
    display_needed = [did for did in set(display_id_of.values()) if did not in root_ids]
    display_nodes = (
        {n.id: n for n in Node.query.filter(Node.id.in_(display_needed)).all()}
        if display_needed else {}
    )
    by_id = {n.id: n for n in items}
    by_id.update(display_nodes)
    cards = [
        (node, by_id.get(display_id_of[node.id], node), prompt_key_of.get(node.id))
        for node in items if node.id in display_id_of
    ]
    # The reply count is the displayed node's (alive replies only —
    # tombstones don't contribute), so a card titled by an entry, or by
    # the AI reply left after the entry was deleted, counts that node's
    # replies rather than the root's.
    child_counts = alive_child_counts(d.id for _, d, _ in cards)

    # User-given thread names live in the thread table, keyed by root;
    # one query for the page.
    thread_rows = {
        t.root_node_id: t
        for t in Thread.query.filter(Thread.root_node_id.in_(thread_root_ids)).all()
    } if thread_root_ids else {}

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

    # The card's names are the *display* node's: when a deleted root
    # falls back to a descendant, that may be someone else's public
    # reply, and their words must not appear under the owner's name.
    # One query for the page's authors and human owners.
    user_ids = set()
    for _, display_node, _ in cards:
        user_ids.add(display_node.user_id)
        if display_node.node_type == "llm":
            user_ids.add(display_node.human_owner_id)
    user_ids.discard(None)
    username_of = {
        u.id: u.username
        for u in User.query.filter(User.id.in_(user_ids)).all()
    } if user_ids else {}

    # Phase 3 — serialize (previews are cache hits now).
    nodes_list = []
    for node, display_node, prompt_key in cards:
        thread_root_id = thread_root_of_row[node.id]
        human_owner_username = (
            username_of.get(display_node.human_owner_id)
            if display_node.node_type == "llm" else None
        )

        nodes_list.append({
            "id": display_node.id,
            "thread_root_id": thread_root_id,
            "newest_node_id": newest_map.get(node.id, display_node.id),
            # Keyed by the root (the thread), never by the display node.
            "thread_name": (
                thread_rows[thread_root_id].get_name()
                if thread_root_id in thread_rows else None
            ),
            "can_rename": thread_root_id in own_root_ids,
            "preview": make_preview(display_node.get_content()),
            "node_type": display_node.node_type,
            "child_count": child_counts.get(display_node.id, 0),
            "created_at": iso_utc(display_node.created_at),
            "pinned_at": iso_utc(node.pinned_at),
            "username": username_of.get(display_node.user_id, "Unknown"),
            "human_owner_username": human_owner_username,
            "llm_model": display_node.llm_model,
            "origin": display_node.origin,
            "has_original_audio": bool(display_node.audio_original_url or display_node.streaming_transcription),
            "prompt_key": prompt_key,
        })

    return jsonify({
        "nodes": nodes_list,
        "has_more": has_more,
        "next_cursor": next_cursor,
        "page": page,
    }), 200

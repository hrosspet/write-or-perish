from flask import Blueprint, jsonify, Response, request, current_app
from flask_login import login_required, current_user
from backend.models import (
    Node, NodeVersion, UserProfile, UserPrompt, UserTodo,
    UserAIPreferences,
)
from backend.extensions import db
from backend.utils.tokens import approximate_token_count, get_model_context_window
from backend.utils.privacy import AI_ALLOWED, accessible_nodes_filter, can_user_access_node
from backend.utils.quotes import (
    resolve_quotes, has_quotes, ExportQuoteResolver, resolve_quotes_for_export
)
from sqlalchemy import func, or_
from sqlalchemy.orm import subqueryload
from datetime import datetime
import os

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
            "content": node.get_content(),
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
            "content": version.get_content(),
            "timestamp": version.timestamp.isoformat()
        })
    return jsonify(user_data), 200


def _node_author_label(node):
    """Return a compact author label like 'User (alice)' or 'AI (claude-opus-4.6)'."""
    if node.node_type == "llm":
        return f"AI ({node.llm_model})" if node.llm_model else "AI (unknown)"
    author = node.user.username if node.user else "Unknown"
    return f"User ({author})"


def _render_inaccessible_node(
    node, index_path, processed_nodes, filter_ai_usage,
    user_id, created_before, embedded_quotes, included_ids,
    ai_blocked_ids,
):
    """Emit a privacy placeholder for an inaccessible node and recurse
    into its children (which may themselves be accessible)."""
    depth = len(index_path.split('.'))
    header = "#" * min(depth + 1, 6)
    ts = node.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
    result = (
        f"{header} [{index_path}] {_node_author_label(node)}"
        f" - {ts}\n"
        f"[Content not accessible — private node by another user]\n\n"
    )

    processed_nodes.add(node.id)
    children = node.children
    if filter_ai_usage:
        children = [c for c in children if c.ai_usage in AI_ALLOWED]
    if created_before:
        children = [c for c in children if c.created_at < created_before]
    if included_ids is not None:
        children = [c for c in children if c.id in included_ids]
    children = sorted(children, key=lambda c: c.created_at)

    for j, gc in enumerate(children):
        gc_index = f"{index_path}.{j+1}"
        result += format_node_tree(
            gc, index_path=gc_index,
            processed_nodes=processed_nodes,
            filter_ai_usage=filter_ai_usage,
            user_id=user_id,
            created_before=created_before,
            embedded_quotes=embedded_quotes,
            included_ids=included_ids,
            ai_blocked_ids=ai_blocked_ids,
        )
    return result


def format_node_tree(
    node,
    index_path="1",
    processed_nodes=None,
    filter_ai_usage=False,
    user_id=None,
    created_before=None,
    embedded_quotes=None,
    included_ids=None,
    ai_blocked_ids=None
):
    """
    Recursively format a node and its descendants into a human-readable tree structure
    using Markdown headers. Uses depth-first traversal for maximum readability.

    Args:
        node: The Node object to format
        index_path: The hierarchical index (e.g., "1.1.2")
        processed_nodes: Set of node IDs already processed (to avoid infinite loops)
        filter_ai_usage: If True, only include child nodes where ai_usage is 'chat' or 'train'
        user_id: User ID for resolving {quote:ID} placeholders (for access checks)
        created_before: Optional datetime. If provided, only include child nodes created before
                       this timestamp.
        embedded_quotes: Optional dict from ExportQuoteResolver mapping
                        node_id -> {quoted_id -> content}. When provided, uses smart
                        quote resolution that embeds only when needed.
        included_ids: Optional set of node IDs included in the export. Used with
                     embedded_quotes for reference-based resolution.

    Returns:
        str: Formatted text representation of the node tree
    """
    if processed_nodes is None:
        processed_nodes = set()

    # Avoid infinite loops from circular references
    if node.id in processed_nodes:
        return ""
    processed_nodes.add(node.id)

    # Calculate depth from index_path (e.g., "1.2.3" -> depth 3)
    depth = len(index_path.split('.'))

    # Format the node header using Markdown headers (max 6 levels, then stay at 6)
    header_level = min(depth + 1, 6)  # +1 because thread title uses #
    header_prefix = "#" * header_level

    timestamp = node.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")

    # Build the node text - no content indentation for token efficiency
    result = f"{header_prefix} [{index_path}] {_node_author_label(node)} - {timestamp}\n"

    # System prompt nodes: emit reference instead of full content
    # Check new artifact system first, then legacy FK
    prompt = node.get_artifact("prompt")
    if prompt is not None:
        version_num = UserPrompt.query.filter(
            UserPrompt.user_id == prompt.user_id,
            UserPrompt.prompt_key == prompt.prompt_key,
            UserPrompt.created_at <= prompt.created_at,
        ).count()
        result += f"[System Prompt: {prompt.title} v{version_num} (ref #{prompt.id})]\n"

        # Emit profile/todo artifact refs if present
        profile = node.get_artifact("profile")
        if profile is not None:
            profile_ver = UserProfile.query.filter(
                UserProfile.user_id == profile.user_id,
                UserProfile.created_at <= profile.created_at,
            ).count()
            result += f"[User Profile v{profile_ver} (ref #{profile.id})]\n"

        todo = node.get_artifact("todo")
        if todo is not None:
            todo_ver = UserTodo.query.filter(
                UserTodo.user_id == todo.user_id,
                UserTodo.created_at <= todo.created_at,
            ).count()
            result += f"[User TODO v{todo_ver} (ref #{todo.id})]\n"

        ai_prefs = node.get_artifact("ai_preferences")
        if ai_prefs is not None:
            ai_prefs_ver = UserAIPreferences.query.filter(
                UserAIPreferences.user_id == ai_prefs.user_id,
                UserAIPreferences.created_at <= ai_prefs.created_at,
            ).count()
            result += f"[AI Preferences v{ai_prefs_ver} (ref #{ai_prefs.id})]\n"

        result += "\n"

        # Process children
        children = node.children
        if filter_ai_usage:
            children = [c for c in children if c.ai_usage in AI_ALLOWED]
        if created_before:
            children = [c for c in children if c.created_at < created_before]
        if included_ids is not None:
            children = [c for c in children if c.id in included_ids]
        children = sorted(children, key=lambda c: c.created_at)

        for i, child in enumerate(children):
            child_index = f"{index_path}.{i+1}"
            if len(children) > 1 and i > 0:
                result += "---\n**BRANCH**\n---\n\n"

            if user_id and not can_user_access_node(child, user_id):
                result += _render_inaccessible_node(
                    child, child_index, processed_nodes,
                    filter_ai_usage, user_id, created_before,
                    embedded_quotes, included_ids, ai_blocked_ids,
                )
                continue

            result += format_node_tree(
                child, index_path=child_index,
                processed_nodes=processed_nodes,
                filter_ai_usage=filter_ai_usage,
                user_id=user_id,
                created_before=created_before,
                embedded_quotes=embedded_quotes,
                included_ids=included_ids,
                ai_blocked_ids=ai_blocked_ids
            )
        return result

    # Resolve {quote:ID} placeholders in content
    content = node.get_content()
    if has_quotes(content):
        if embedded_quotes is not None:
            # Use smart resolution from ExportQuoteResolver
            content = resolve_quotes_for_export(
                content, node.id, embedded_quotes, user_id,
                ai_blocked_ids=ai_blocked_ids
            )
        elif user_id:
            # Fallback to simple resolution (depth 1)
            content, _ = resolve_quotes(content, user_id, for_llm=False, max_depth=1)

    result += content
    result += "\n\n"

    # Process children (depth-first traversal)
    # Filter children by AI usage if requested (for AI profile generation)
    # Also filter by created_before if specified
    children = node.children
    if filter_ai_usage:
        children = [c for c in children if c.ai_usage in AI_ALLOWED]
    if created_before:
        children = [c for c in children if c.created_at < created_before]

    # If we have included_ids, filter to only include nodes in the export
    if included_ids is not None:
        children = [c for c in children if c.id in included_ids]

    children = sorted(children, key=lambda c: c.created_at)

    for i, child in enumerate(children):
        child_index = f"{index_path}.{i+1}"

        # Mark branches (when there are multiple children)
        if len(children) > 1 and i > 0:
            result += "---\n**BRANCH**\n---\n\n"

        if user_id and not can_user_access_node(child, user_id):
            result += _render_inaccessible_node(
                child, child_index, processed_nodes,
                filter_ai_usage, user_id, created_before,
                embedded_quotes, included_ids, ai_blocked_ids,
            )
            continue

        result += format_node_tree(
            child,
            index_path=child_index,
            processed_nodes=processed_nodes,
            filter_ai_usage=filter_ai_usage,
            user_id=user_id,
            created_before=created_before,
            embedded_quotes=embedded_quotes,
            included_ids=included_ids,
            ai_blocked_ids=ai_blocked_ids
        )

    return result


def _collect_all_nodes_in_tree(node, filter_ai_usage=False, created_before=None,
                               collected=None, user_id=None):
    """
    Recursively collect all accessible nodes in a tree.

    Args:
        node: Root node of the tree
        filter_ai_usage: If True, only include nodes where ai_usage is 'chat' or 'train'
        created_before: Optional datetime filter
        collected: Set of already collected node IDs (to avoid duplicates)
        user_id: If provided, skip inaccessible nodes but still recurse
                into their children (which may be accessible).

    Returns:
        List of Node objects in the tree
    """
    if collected is None:
        collected = set()

    if node.id in collected:
        return []

    collected.add(node.id)

    # Only include the node itself if it's accessible (or no user_id check)
    if user_id and not can_user_access_node(node, user_id):
        result = []
    else:
        result = [node]

    children = node.children
    if filter_ai_usage:
        children = [c for c in children if c.ai_usage in AI_ALLOWED]
    if created_before:
        children = [c for c in children if c.created_at < created_before]

    for child in children:
        result.extend(_collect_all_nodes_in_tree(
            child, filter_ai_usage, created_before, collected, user_id
        ))

    return result


def _select_incremental_rows(user_id, filter_ai_usage=False,
                             created_before=None, created_after=None):
    """Return rows for the incremental-export scope.

    Used when build_user_export_content is called with `created_after`.
    Returns a list of namedtuple-like rows with .id, .parent_id,
    .created_at, .token_count for nodes in the target user's
    "conversational scope":

      - Anchors: target's own or addressed nodes
        (`user_id == uid OR human_owner_id == uid`) that pass the
        usual filters (accessible, ai_usage, created_before/after).
      - Climb up from anchors: include parents that pass the same
        filters. Stops when an ancestor fails (typically pre-cutoff).
      - Climb down from anchors: include descendants that pass the
        same filters.

    Foreign post-cutoff ancestors that pass `accessible_nodes_filter`
    are included (the conversation the target is responding to).
    Foreign siblings of anchors are NOT included (they are neither
    ancestors nor descendants of any anchor).

    Implementation note: iterates BFS in Python over indexed SQL
    queries (one per depth layer in each direction). Typical
    user-tree depths are <10, so this is a small number of fast
    queries. Could be folded into a single recursive CTE if profiling
    ever shows it matters; kept iterative for portability between
    PostgreSQL and the SQLite test database.
    """
    cols = (Node.id, Node.parent_id, Node.created_at, Node.token_count)

    def _general_filter():
        clauses = [accessible_nodes_filter(Node, user_id)]
        if filter_ai_usage:
            clauses.append(Node.ai_usage.in_(AI_ALLOWED))
        if created_before is not None:
            clauses.append(Node.created_at < created_before)
        if created_after is not None:
            clauses.append(Node.created_at > created_after)
        return clauses

    # 1. Anchors: target-owned/addressed AND pass general filters.
    anchor_rows = db.session.query(*cols).filter(
        or_(Node.user_id == user_id, Node.human_owner_id == user_id),
        *_general_filter(),
    ).all()

    rows_by_id = {r.id: r for r in anchor_rows}

    # 2. Climb UP from anchors. Each iteration: fetch parents matching
    #    filters that we haven't seen yet. Stop when no new parents.
    pending_parent_ids = {
        r.parent_id for r in anchor_rows if r.parent_id is not None
    } - rows_by_id.keys()
    while pending_parent_ids:
        parent_rows = db.session.query(*cols).filter(
            Node.id.in_(pending_parent_ids),
            *_general_filter(),
        ).all()
        next_pending = set()
        for pr in parent_rows:
            if pr.id in rows_by_id:
                continue
            rows_by_id[pr.id] = pr
            if pr.parent_id is not None:
                next_pending.add(pr.parent_id)
        pending_parent_ids = next_pending - rows_by_id.keys()

    # 3. Climb DOWN from anchors. Each iteration: fetch children whose
    #    parent is in the current frontier and that pass filters.
    frontier_ids = {r.id for r in anchor_rows}
    while frontier_ids:
        child_rows = db.session.query(*cols).filter(
            Node.parent_id.in_(frontier_ids),
            *_general_filter(),
        ).all()
        next_frontier = set()
        for cr in child_rows:
            if cr.id in rows_by_id:
                continue
            rows_by_id[cr.id] = cr
            next_frontier.add(cr.id)
        frontier_ids = next_frontier

    return list(rows_by_id.values())


def _preselect_node_ids(user_id, budget, filter_ai_usage=False,
                        created_before=None, created_after=None,
                        chronological_order=False):
    """Select node IDs that fit within a token budget using a SQL window function.

    Uses a recursive CTE to find all descendants of the user's top-level
    threads, then filters to accessible nodes only.  Returns a list of
    node IDs ordered by created_at (direction controlled by
    chronological_order).  No nodes are loaded or decrypted.
    """
    # Recursive CTE: all node IDs in the user's thread trees
    base = db.session.query(Node.id).filter(
        Node.user_id == user_id,
        Node.parent_id.is_(None),
    ).cte(name="thread_nodes", recursive=True)

    thread_child = db.aliased(Node, flat=True)
    recursive = db.session.query(thread_child.id).join(
        base, thread_child.parent_id == base.c.id
    )
    thread_cte = base.union_all(recursive)

    sort_order = (
        Node.created_at.asc() if chronological_order
        else Node.created_at.desc()
    )
    cumul = func.sum(Node.token_count).over(
        order_by=sort_order
    ).label("cumul")

    inner = db.session.query(Node.id, cumul).filter(
        Node.id.in_(db.session.query(thread_cte.c.id)),
        accessible_nodes_filter(Node, user_id),
    )
    if filter_ai_usage:
        inner = inner.filter(Node.ai_usage.in_(AI_ALLOWED))
    if created_before:
        inner = inner.filter(Node.created_at < created_before)
    if created_after:
        inner = inner.filter(Node.created_at > created_after)

    inner = inner.subquery()

    return [
        row[0] for row in
        db.session.query(inner.c.id).filter(
            inner.c.cumul - func.coalesce(
                Node.token_count, 0) < budget
        ).join(Node, Node.id == inner.c.id).all()
    ]


def get_raw_data_date_range(user_id, max_tokens=10000, created_before=None):
    """Return (earliest, latest) created_at for the pre-selected raw data window.

    This is an approximation: the resolver may pull in older nodes as quote
    dependencies, but using those dates would be misleading (e.g. a quote
    from years back doesn't mean the raw data "covers" that period).  The
    pre-selection window reflects the actual recent writing period.

    Runs only the SQL window function — no nodes are loaded or decrypted.
    Returns (earliest, latest, total_tokens) or (None, None, 0).
    """
    budget = max_tokens - 100  # header/footer reserve
    selected_ids = _preselect_node_ids(
        user_id, budget, filter_ai_usage=True,
        created_before=created_before, chronological_order=False,
    )
    if not selected_ids:
        return None, None, 0

    row = db.session.query(
        func.min(Node.created_at), func.max(Node.created_at),
        func.coalesce(func.sum(Node.token_count), 0),
    ).filter(Node.id.in_(selected_ids)).one()
    return row[0], row[1], row[2]


def _entry_point_top_level_started(node, user_id):
    """Walk node.parent until parent_id IS NULL; return that root's
    created_at IFF the root is accessible to user_id. Falls back to
    None if the walk hits a missing parent, a cycle, or an
    inaccessible root — the caller renders a generic preamble
    without the date in that case.

    Checking accessibility prevents leaking the start-date of a
    private foreign thread whose root was excluded by
    accessible_nodes_filter during the CTE walk.

    Used only by the incremental export. O(depth) ORM lookups per
    entry point — N+1 potential, fine for typical thread depths
    (≤ ~10) and small entry-point counts. If that ever proves too
    expensive, batch the ancestor lookup with a single recursive
    CTE keyed by entry id.
    """
    cur = node
    seen = set()
    while cur is not None and cur.parent_id is not None:
        if cur.id in seen:  # defensive: avoid cycles
            return None
        seen.add(cur.id)
        cur = cur.parent
    if cur is None:
        return None
    if not can_user_access_node(cur, user_id):
        return None
    return cur.created_at


PREAMBLE_PREFIX = "> [Continuation of thread"


def _format_preamble(top_started_at):
    """Render the entry-point preamble. If we know when the thread
    started, include the date so the summarizer can distinguish
    multiple older-thread continuations."""
    if top_started_at is not None:
        date_str = top_started_at.strftime("%Y-%m-%d")
        return (
            f"{PREAMBLE_PREFIX} started {date_str} — earlier context "
            f"in this thread is not shown.]"
        )
    return (
        f"{PREAMBLE_PREFIX} — earlier context in this thread is not shown.]"
    )


def _build_user_export_incremental(
    user, max_tokens, filter_ai_usage, created_before, created_after,
    chronological_order, return_metadata, collapse_artifacts,
):
    """Incremental export path (created_after is set). See
    `build_user_export_content` docstring for behavior. The CTE row
    set defines `included_ids`; entry points are CTE rows whose
    parent is not in scope (preamble emitted if parent exists).
    """
    cte_rows = _select_incremental_rows(
        user.id, filter_ai_usage=filter_ai_usage,
        created_before=created_before, created_after=created_after,
    )
    cte_row_ids = {r.id for r in cte_rows}

    if not cte_rows:
        return None

    # Variables for smart quote resolution (used when max_tokens is set).
    embedded_quotes = None
    ai_blocked_ids = None
    resolver = None
    selected_ids = None
    # `render_included_ids` is what `format_node_tree` filters children
    # by. In the no-budget case it equals the CTE set. In the budgeted
    # case it equals the resolver's set (which may include quoted
    # pre-cutoff nodes for embedding).
    render_included_ids = set(cte_row_ids)

    if max_tokens:
        header_footer_tokens = 100
        budget = max_tokens - header_footer_tokens

        # Apply budget windowing to CTE rows. Semantics differ slightly
        # from `_preselect_node_ids`'s SQL window: this loop stops
        # BEFORE a row that would overshoot the budget (strict fit),
        # while the SQL version includes the overshooting row (its
        # predicate is `cumul - token_count < budget`, which is true
        # for the row that straddles the boundary). The strict-fit
        # variant keeps budgeted recent-context calls closer to the
        # caller's stated ceiling and is simpler to reason about.
        # Intentional difference; if callers ever need the old
        # overshoot semantic, add a flag.
        ordered = sorted(
            cte_rows,
            key=lambda r: r.created_at,
            reverse=not chronological_order,
        )
        cumulative = 0
        selected_ids = []
        for r in ordered:
            tk = r.token_count or 0
            if cumulative + tk > budget and cumulative > 0:
                break
            selected_ids.append(r.id)
            cumulative += tk

        if not selected_ids:
            return None

        selected_nodes = (
            Node.query
            .filter(Node.id.in_(selected_ids))
            .options(subqueryload(Node.context_artifacts))
            .all()
        )
        selected_nodes.sort(
            key=lambda n: n.created_at,
            reverse=not chronological_order,
        )

        resolver = ExportQuoteResolver(
            user.id, budget,
            filter_ai_usage=filter_ai_usage,
            chronological=chronological_order
        )
        for node in selected_nodes:
            content = node.get_content()
            node_artifacts = [
                (a.artifact_type, a.artifact_id)
                for a in node.context_artifacts
            ]
            prompt = node.get_artifact("prompt")
            if prompt is not None:
                version_num = UserPrompt.query.filter(
                    UserPrompt.user_id == prompt.user_id,
                    UserPrompt.prompt_key == prompt.prompt_key,
                    UserPrompt.created_at <= prompt.created_at,
                ).count()
                resolver.add_node(
                    node_id=node.id,
                    created_at=node.created_at,
                    content=content,
                    token_count=node.token_count,
                    user_prompt_id=prompt.id,
                    prompt_content=content,
                    prompt_label=f"{prompt.title} v{version_num}",
                    artifacts=node_artifacts,
                )
            else:
                resolver.add_node(
                    node_id=node.id,
                    created_at=node.created_at,
                    content=content,
                    token_count=node.token_count,
                    artifacts=node_artifacts,
                )

        resolver.resolve()
        included_ids, embedded_quotes, ai_blocked_ids = (
            resolver.get_resolution_result()
        )
        render_included_ids = included_ids

    # Entry points. Iterate CTE rows so quoted-pre-cutoff embeds added
    # by the resolver can never be entry candidates. Parent membership
    # is checked against `render_included_ids` so a budget-ejected
    # parent correctly leaves its surviving child as an entry point.
    entry_rows = [
        r for r in cte_rows
        if r.id in render_included_ids
        and (r.parent_id is None
             or r.parent_id not in render_included_ids)
    ]
    entry_rows.sort(
        key=lambda r: r.created_at,
        reverse=not chronological_order,
    )

    if not entry_rows:
        return None

    entry_nodes_by_id = {
        n.id: n for n in Node.query.filter(
            Node.id.in_([r.id for r in entry_rows])
        ).all()
    }
    entry_nodes = [
        entry_nodes_by_id[r.id]
        for r in entry_rows
        if r.id in entry_nodes_by_id
    ]

    # Build the export content using Markdown format
    export_lines = []
    export_lines.append("# Loore - Thread Export")
    export_lines.append("")
    export_lines.append(f"**User:** {user.username}")
    export_lines.append(
        f"**Export Date:** "
        f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}"
    )
    export_lines.append(f"**Entry Points:** {len(entry_nodes)}")
    if max_tokens:
        export_lines.append(f"*(Limited to ~{max_tokens:,} tokens)*")
    export_lines.append("")
    export_lines.append("---")
    export_lines.append("")

    # Artifact preambles. Mirrors the legacy path's behavior but driven
    # by entry_nodes instead of top_level_nodes.
    if collapse_artifacts:
        pass
    elif resolver is not None:
        preamble = resolver.get_artifacts_preamble()
        if preamble:
            export_lines.append(preamble)
            export_lines.append("---")
            export_lines.append("")

    for thread_num, entry in enumerate(entry_nodes, 1):
        if entry.parent_id is not None:
            top_started = _entry_point_top_level_started(entry, user.id)
            export_lines.append(_format_preamble(top_started))
            export_lines.append("")

        export_lines.append(f"# Thread {thread_num}")
        export_lines.append(
            f"**Started:** "
            f"{entry.created_at.strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )
        export_lines.append("")

        thread_text = format_node_tree(
            entry,
            index_path=str(thread_num),
            filter_ai_usage=filter_ai_usage,
            user_id=user.id,
            created_before=created_before,
            embedded_quotes=embedded_quotes,
            included_ids=render_included_ids,
            ai_blocked_ids=ai_blocked_ids,
        )
        export_lines.append(thread_text)
        export_lines.append("---")
        export_lines.append("")

    export_lines.append("*End of Export*")
    content = "\n".join(export_lines)

    if return_metadata:
        # Use CTE rows for metadata. They already have created_after
        # applied in SQL.
        timestamps = [r.created_at for r in cte_rows]
        latest_ts = max(timestamps) if timestamps else None
        earliest_ts = min(timestamps) if timestamps else None
        return {
            "content": content,
            "token_count": approximate_token_count(content),
            "latest_node_created_at": latest_ts,
            "earliest_node_created_at": earliest_ts,
            "node_count": len(cte_rows),
            "node_ids": cte_row_ids,
        }

    return content


def build_user_export_content(
    user, max_tokens=None, filter_ai_usage=False,
    created_before=None, created_after=None,
    chronological_order=False, return_metadata=False,
    collapse_artifacts=False
):
    """
    Core export logic: Build a human-readable text export of threads for a user.

    Two modes:
      - **Legacy (default, `created_after is None`)**: includes top-level
        threads owned by the user (`Node.user_id == user.id`,
        `parent_id IS NULL`) and walks their accessible descendants.
        Used for user-facing data export and full-archive profile gen.
      - **Incremental (`created_after is not None`)**: includes the user's
        post-cutoff "anchors" (own or addressed nodes) plus their accessible
        post-cutoff ancestors and descendants. Foreign post-cutoff ancestors
        the target replied to are pulled in (conversational context);
        foreign siblings the target never engaged with are excluded.
        Renders entry points (a node in scope whose parent is not in scope)
        with a short preamble when the entry point sits beneath a pre-cutoff
        / out-of-scope parent. Used by `recent_context` and iterative
        profile regen.

    When `max_tokens` is specified, uses `ExportQuoteResolver` for smart
    quote resolution. The resolver may pull in pre-cutoff quoted nodes for
    embedding; those never become entry points (entry-point membership is
    decided from the CTE rows, not from the resolver's mutated set).

    Args:
        user: User object to export threads for
        max_tokens: Optional maximum token count. If provided, only includes
                   most recent threads that fit within this limit, and uses
                   smart quote resolution.
        filter_ai_usage: If True, only include nodes where ai_usage is
                        'chat' or 'train'. Use True for AI profile
                        generation, False for user data export.
        created_before: Optional datetime. If provided, only includes
                       nodes created before this timestamp.
        created_after: Optional datetime. If provided, switches to the
                      incremental mode described above.
        chronological_order: If True and max_tokens is set, select oldest
                           nodes first (for iterative profile building).
        return_metadata: If True, return a dict with `content`,
                        `token_count`, `latest_node_created_at`,
                        `earliest_node_created_at`, `node_count`, and
                        `node_ids` (set of in-scope node IDs).
        collapse_artifacts: If True, suppress full artifact preambles
                           (artifact ID refs are still inlined).

    Returns:
        str or dict: Formatted export content (or None if no threads found).
                    If return_metadata=True, returns the dict described above.
    """
    if created_after is not None:
        return _build_user_export_incremental(
            user, max_tokens=max_tokens, filter_ai_usage=filter_ai_usage,
            created_before=created_before, created_after=created_after,
            chronological_order=chronological_order,
            return_metadata=return_metadata,
            collapse_artifacts=collapse_artifacts,
        )

    # Legacy path: top-level threads owned by the user.
    query = Node.query.filter_by(
        user_id=user.id,
        parent_id=None
    )

    # Only filter by AI usage if requested (for AI profile generation)
    if filter_ai_usage:
        query = query.filter(Node.ai_usage.in_(AI_ALLOWED))

    # Filter by creation timestamp if specified (for {user_export} context
    # limiting). `created_after` is intercepted by the dispatcher above and
    # is always None on the legacy path; we leave the parameter in the
    # signature for backward compatibility with callers that pass it.
    if created_before:
        query = query.filter(Node.created_at < created_before)

    all_top_level_nodes = query.order_by(Node.created_at.desc()).all()

    if not all_top_level_nodes:
        return None

    # Variables for smart quote resolution (used when max_tokens is specified)
    embedded_quotes = None
    included_ids = None
    ai_blocked_ids = None
    resolver = None
    selected_ids = None

    # If max_tokens is specified, use ExportQuoteResolver for smart quote handling
    if max_tokens:
        # Reserve tokens for header and footer
        header_footer_tokens = 100
        budget = max_tokens - header_footer_tokens

        # Pre-select node IDs via SQL window function (no loading/decryption)
        selected_ids = _preselect_node_ids(
            user.id, budget, filter_ai_usage=filter_ai_usage,
            created_before=created_before, created_after=created_after,
            chronological_order=chronological_order,
        )

        # Load selected Node objects with context_artifacts eager-loaded
        selected_nodes = (
            Node.query
            .filter(Node.id.in_(selected_ids))
            .options(subqueryload(Node.context_artifacts))
            .all()
        )
        # Sort in Python to match the desired order
        selected_nodes.sort(
            key=lambda n: n.created_at,
            reverse=not chronological_order,
        )

        # Create resolver with adjusted token budget
        resolver = ExportQuoteResolver(
            user.id, budget,
            filter_ai_usage=filter_ai_usage,
            chronological=chronological_order
        )

        # Add only the pre-selected nodes to the resolver (decrypts here)
        for node in selected_nodes:
            content = node.get_content()
            # Collect artifact tuples from join table
            node_artifacts = [
                (a.artifact_type, a.artifact_id)
                for a in node.context_artifacts
            ]
            prompt = node.get_artifact("prompt")
            if prompt is not None:
                version_num = UserPrompt.query.filter(
                    UserPrompt.user_id == prompt.user_id,
                    UserPrompt.prompt_key == prompt.prompt_key,
                    UserPrompt.created_at <= prompt.created_at,
                ).count()
                resolver.add_node(
                    node_id=node.id,
                    created_at=node.created_at,
                    content=content,
                    token_count=node.token_count,
                    user_prompt_id=prompt.id,
                    prompt_content=content,
                    prompt_label=f"{prompt.title} v{version_num}",
                    artifacts=node_artifacts,
                )
            else:
                resolver.add_node(
                    node_id=node.id,
                    created_at=node.created_at,
                    content=content,
                    token_count=node.token_count,
                    artifacts=node_artifacts,
                )

        # Run the resolution algorithm
        resolver.resolve()

        # Get the results
        included_ids, embedded_quotes, ai_blocked_ids = resolver.get_resolution_result()

        # Filter top-level nodes to only those with content in the export
        top_level_nodes = [n for n in all_top_level_nodes if n.id in included_ids]
        # Reverse to get chronological order (oldest to newest among selected)
        top_level_nodes.reverse()
    else:
        # No limit - use all threads in chronological order
        top_level_nodes = list(reversed(all_top_level_nodes))

    if not top_level_nodes:
        return None

    # Build the export content using Markdown format
    export_lines = []
    export_lines.append("# Loore - Thread Export")
    export_lines.append("")
    export_lines.append(f"**User:** {user.username}")
    export_lines.append(f"**Export Date:** {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    export_lines.append(f"**Total Threads:** {len(all_top_level_nodes)}")
    if max_tokens and len(top_level_nodes) < len(all_top_level_nodes):
        export_lines.append(f"**Included Threads (most recent):** {len(top_level_nodes)}")
        export_lines.append(f"*(Limited to ~{max_tokens:,} tokens)*")
    export_lines.append("")
    export_lines.append("---")
    export_lines.append("")

    # Emit artifact preambles (prompts, profiles, todos) if any are referenced
    if collapse_artifacts:
        pass  # Skip full artifact content; inline refs are still emitted
    elif resolver is not None:
        preamble = resolver.get_artifacts_preamble()
        if preamble:
            export_lines.append(preamble)
            export_lines.append("---")
            export_lines.append("")
    elif not max_tokens:
        # Full export: collect all unique artifact versions
        prompt_versions = {}
        profile_versions = {}
        todo_versions = {}
        ai_prefs_versions = {}
        for top_node in top_level_nodes:
            tree_nodes = _collect_all_nodes_in_tree(
                top_node, filter_ai_usage, created_before,
                user_id=user.id,
            )
            for n in tree_nodes:
                # Prompt artifacts
                prompt = n.get_artifact("prompt")
                if prompt is not None and prompt.id not in prompt_versions:
                    vnum = UserPrompt.query.filter(
                        UserPrompt.user_id == prompt.user_id,
                        UserPrompt.prompt_key == prompt.prompt_key,
                        UserPrompt.created_at <= prompt.created_at,
                    ).count()
                    prompt_versions[prompt.id] = {
                        "title": prompt.title,
                        "version": vnum,
                        "content": n.get_content(),
                    }
                # Profile artifacts
                profile = n.get_artifact("profile")
                if profile is not None and profile.id not in profile_versions:
                    pver = UserProfile.query.filter(
                        UserProfile.user_id == profile.user_id,
                        UserProfile.created_at <= profile.created_at,
                    ).count()
                    profile_versions[profile.id] = {
                        "version": pver,
                        "content": profile.get_content(),
                    }
                # Todo artifacts
                todo = n.get_artifact("todo")
                if todo is not None and todo.id not in todo_versions:
                    tver = UserTodo.query.filter(
                        UserTodo.user_id == todo.user_id,
                        UserTodo.created_at <= todo.created_at,
                    ).count()
                    todo_versions[todo.id] = {
                        "version": tver,
                        "content": todo.get_content(),
                    }
                # AI preferences artifacts
                ai_prefs = n.get_artifact("ai_preferences")
                if ai_prefs is not None and ai_prefs.id not in ai_prefs_versions:
                    aver = UserAIPreferences.query.filter(
                        UserAIPreferences.user_id == ai_prefs.user_id,
                        UserAIPreferences.created_at <= ai_prefs.created_at,
                    ).count()
                    ai_prefs_versions[ai_prefs.id] = {
                        "version": aver,
                        "content": ai_prefs.get_content(),
                    }

        has_any_preamble = (
            prompt_versions or profile_versions or todo_versions
            or ai_prefs_versions
        )
        if prompt_versions:
            export_lines.append("## System Prompts Referenced\n")
            sorted_pids = sorted(prompt_versions)
            for i, pid in enumerate(sorted_pids):
                pv = prompt_versions[pid]
                export_lines.append(
                    f"### {pv['title']} v{pv['version']} (ref #{pid})\n"
                )
                export_lines.append(pv["content"])
                export_lines.append("")
                if i < len(sorted_pids) - 1:
                    export_lines.append("===\n")
        if profile_versions:
            if prompt_versions:
                export_lines.append("===\n")
            export_lines.append("## User Profiles Referenced\n")
            sorted_pids = sorted(profile_versions)
            for i, pid in enumerate(sorted_pids):
                pv = profile_versions[pid]
                export_lines.append(
                    f"### User Profile v{pv['version']} (ref #{pid})\n"
                )
                export_lines.append(pv["content"])
                export_lines.append("")
                if i < len(sorted_pids) - 1:
                    export_lines.append("===\n")
        if todo_versions:
            if prompt_versions or profile_versions:
                export_lines.append("===\n")
            export_lines.append("## User TODOs Referenced\n")
            sorted_tids = sorted(todo_versions)
            for i, tid in enumerate(sorted_tids):
                tv = todo_versions[tid]
                export_lines.append(
                    f"### User TODO v{tv['version']} (ref #{tid})\n"
                )
                export_lines.append(tv["content"])
                export_lines.append("")
                if i < len(sorted_tids) - 1:
                    export_lines.append("===\n")
        if ai_prefs_versions:
            if prompt_versions or profile_versions or todo_versions:
                export_lines.append("===\n")
            export_lines.append("## AI Preferences Referenced\n")
            sorted_aids = sorted(ai_prefs_versions)
            for i, aid in enumerate(sorted_aids):
                av = ai_prefs_versions[aid]
                export_lines.append(
                    f"### AI Preferences v{av['version']} (ref #{aid})\n"
                )
                export_lines.append(av["content"])
                export_lines.append("")
                if i < len(sorted_aids) - 1:
                    export_lines.append("===\n")
        if has_any_preamble:
            export_lines.append("===")
            export_lines.append("")

    # Process each thread
    for thread_num, node in enumerate(top_level_nodes, 1):
        export_lines.append(f"# Thread {thread_num}")
        export_lines.append(f"**Started:** {node.created_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        export_lines.append("")

        # Format the entire thread tree (depth-first traversal)
        thread_text = format_node_tree(
            node,
            index_path=str(thread_num),
            filter_ai_usage=filter_ai_usage,
            user_id=user.id,
            created_before=created_before,
            embedded_quotes=embedded_quotes,
            included_ids=included_ids,
            ai_blocked_ids=ai_blocked_ids
        )
        export_lines.append(thread_text)

        export_lines.append("---")
        export_lines.append("")

    # Add footer
    export_lines.append("*End of Export*")

    content = "\n".join(export_lines)

    if return_metadata:
        # Date range from the pre-selected window (not inflated by
        # quote dependencies the resolver may have pulled in).
        if selected_ids:
            row = db.session.query(
                func.min(Node.created_at), func.max(Node.created_at),
                func.count(Node.id),
            ).filter(Node.id.in_(selected_ids)).one()
            earliest_ts, latest_ts, node_count = row
            node_ids = set(selected_ids)
        else:
            # No max_tokens path — scan all included nodes
            meta_nodes = []
            for top_node in top_level_nodes:
                meta_nodes.extend(_collect_all_nodes_in_tree(
                    top_node, filter_ai_usage, created_before,
                    user_id=user.id,
                ))
            latest_ts = max(
                (n.created_at for n in meta_nodes), default=None
            )
            earliest_ts = min(
                (n.created_at for n in meta_nodes), default=None
            )
            node_count = len(meta_nodes)
            node_ids = {n.id for n in meta_nodes}
        return {
            "content": content,
            "token_count": approximate_token_count(content),
            "latest_node_created_at": latest_ts,
            "earliest_node_created_at": earliest_ts,
            "node_count": node_count,
            "node_ids": node_ids,
        }

    return content

@export_bp.route("/export/threads", methods=["GET"])
@login_required
def export_threads():
    """
    Export all threads originated by the current user in a human-readable text format.

    This includes:
    - All top-level nodes created by the user
    - All descendants of those nodes (including AI replies)
    - Properly formatted with hierarchical structure showing branches
    """
    # Use the core export logic
    export_content = build_user_export_content(current_user)

    if not export_content:
        return Response(
            "No threads found to export.",
            mimetype="text/plain",
            headers={
                "Content-Disposition": f'attachment; filename="write-or-perish-export-{datetime.utcnow().strftime("%Y%m%d-%H%M%S")}.txt"'
            }
        )

    # Return as downloadable text file
    filename = f"write-or-perish-export-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.txt"
    return Response(
        export_content,
        mimetype="text/plain",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        }
    )

# approximate_token_count is imported from backend.utils.tokens

@export_bp.route("/export/estimate_profile_tokens", methods=["POST"])
@login_required
def estimate_profile_tokens():
    """
    Estimate the number of tokens that would be used for profile generation.
    Returns the estimate without actually calling the LLM.

    Request body:
        {
            "model": "gpt-5" | "claude-sonnet-4.5" | etc.
        }

    Returns:
        {
            "estimated_tokens": 12345,
            "model": "gpt-5",
            "has_content": true
        }
    """
    # Get and validate the model from request body
    data = request.get_json() or {}
    model_id = data.get("model")

    if not model_id:
        model_id = current_app.config.get("DEFAULT_LLM_MODEL", "claude-opus-4.5")

    # Validate model is supported
    if model_id not in current_app.config["SUPPORTED_MODELS"]:
        return jsonify({
            "error": f"Unsupported model: {model_id}",
            "supported_models": list(current_app.config["SUPPORTED_MODELS"].keys())
        }), 400

    # Load the prompt template to calculate its token overhead
    prompt_template_path = os.path.join(
        current_app.root_path,
        "prompts",
        "profile_generation.txt"
    )

    try:
        with open(prompt_template_path, "r", encoding="utf-8") as f:
            prompt_template = f.read()
    except FileNotFoundError:
        current_app.logger.error(f"Prompt template not found at {prompt_template_path}")
        return jsonify({
            "error": "Profile generation prompt template not found"
        }), 500

    # Build full export (no token limit) — the task will retry if too long
    user_export = build_user_export_content(current_user, max_tokens=None, filter_ai_usage=True)

    if not user_export:
        return jsonify({
            "estimated_tokens": 0,
            "model": model_id,
            "has_content": False,
            "error": "No writing found to analyze."
        }), 200

    # Replace the placeholder with actual user export
    final_prompt = prompt_template.replace("{user_export}", user_export)

    # Estimate tokens, capped at model's context window
    estimated_tokens = approximate_token_count(final_prompt)
    context_window = get_model_context_window(model_id)
    estimated_tokens = min(estimated_tokens, context_window)

    return jsonify({
        "estimated_tokens": estimated_tokens,
        "model": model_id,
        "has_content": True
    }), 200

@export_bp.route("/export/update_profile", methods=["POST"])
@login_required
def update_profile():
    """
    Unified endpoint for initial profile generation and incremental updates.

    Finds the latest profile; if one exists, dispatches an incremental update.
    Otherwise dispatches initial generation (possibly iterative).

    Request body:
        { "model": "claude-opus-4.6" }  (optional)

    Returns:
        { "task_id": "...", "status": "pending", "is_update": bool }
    """
    from backend.tasks.exports import update_user_profile

    data = request.get_json() or {}
    model_id = data.get("model")
    force_full_regen = data.get("force_full_regen", False)

    if not model_id:
        model_id = current_app.config.get(
            "DEFAULT_LLM_MODEL", "claude-opus-4.6"
        )

    if force_full_regen:
        from backend.extensions import db as _db_flag
        current_user.profile_needs_full_regen = True
        _db_flag.session.commit()

    if model_id not in current_app.config["SUPPORTED_MODELS"]:
        return jsonify({
            "error": f"Unsupported model: {model_id}",
            "supported_models": list(
                current_app.config["SUPPORTED_MODELS"].keys()
            )
        }), 400

    # Check concurrency guard
    if current_user.profile_generation_task_id:
        from backend.tasks.exports import _is_task_stale
        if _is_task_stale(current_user):
            current_user.profile_generation_task_id = None
            current_user.profile_generation_task_dispatched_at = None
            db.session.commit()
        else:
            return jsonify({
                "task_id": current_user.profile_generation_task_id,
                "status": "already_running",
                "is_update": False,
            }), 200

    # Quick check for any writing
    has_threads = Node.query.filter_by(
        user_id=current_user.id, parent_id=None
    ).first() is not None
    if not has_threads:
        return jsonify({
            "error": "No writing found to analyze."
        }), 400

    # Find latest non-integration profile
    latest_profile = UserProfile.query.filter(
        UserProfile.user_id == current_user.id,
        UserProfile.generation_type != 'integration'
    ).order_by(UserProfile.created_at.desc()).first()

    # Determine if full regen is needed
    needs_full_regen = False
    if latest_profile:
        if current_user.profile_needs_full_regen:
            needs_full_regen = True
        elif latest_profile.source_data_cutoff is None:
            # Old profile without cutoff metadata — must regenerate
            needs_full_regen = True

    prev_id = None if needs_full_regen else (
        latest_profile.id if latest_profile else None
    )
    is_update = prev_id is not None

    task = update_user_profile.delay(current_user.id, model_id, prev_id)

    # Set concurrency guard
    from backend.extensions import db as _db
    current_user.profile_generation_task_id = task.id
    current_user.profile_generation_task_dispatched_at = datetime.utcnow()
    _db.session.commit()

    current_app.logger.info(
        f"Enqueued profile {'update' if is_update else 'generation'} "
        f"task {task.id} for user {current_user.id}"
    )

    return jsonify({
        "task_id": task.id,
        "status": "pending",
        "is_update": is_update,
    }), 202


@export_bp.route("/export/integrate_profile", methods=["POST"])
@login_required
def integrate_profile():
    """
    Manually trigger profile integration: collect all iterative/update
    profile versions and integrate them into a single unified profile.

    Request body:
        { "model": "claude-opus-4.6" }  (optional)

    Returns:
        { "task_id": "...", "status": "pending" }
    """
    from backend.tasks.exports import integrate_user_profile

    data = request.get_json() or {}
    model_id = data.get("model")
    if not model_id:
        model_id = current_app.config.get(
            "DEFAULT_LLM_MODEL", "claude-opus-4.6"
        )

    if model_id not in current_app.config["SUPPORTED_MODELS"]:
        return jsonify({
            "error": f"Unsupported model: {model_id}",
            "supported_models": list(
                current_app.config["SUPPORTED_MODELS"].keys()
            )
        }), 400

    # Check concurrency guard
    if current_user.profile_generation_task_id:
        from backend.tasks.exports import _is_task_stale
        if _is_task_stale(current_user):
            current_user.profile_generation_task_id = None
            current_user.profile_generation_task_dispatched_at = None
            db.session.commit()
        else:
            return jsonify({
                "task_id": current_user.profile_generation_task_id,
                "status": "already_running",
            }), 200

    # Find latest non-integration profile that has iterative parents
    latest_profile = UserProfile.query.filter(
        UserProfile.user_id == current_user.id,
        UserProfile.generation_type != 'integration'
    ).order_by(UserProfile.created_at.desc()).first()

    if not latest_profile:
        return jsonify({"error": "No profile found to integrate."}), 400

    task = integrate_user_profile.delay(
        current_user.id, model_id, latest_profile.id
    )

    from backend.extensions import db as _db
    current_user.profile_generation_task_id = task.id
    current_user.profile_generation_task_dispatched_at = datetime.utcnow()
    _db.session.commit()

    current_app.logger.info(
        f"Enqueued profile integration task {task.id} "
        f"for user {current_user.id}"
    )

    return jsonify({
        "task_id": task.id,
        "status": "pending",
    }), 202


@export_bp.route("/export/generate_profile", methods=["POST"])
@login_required
def generate_profile():
    """
    Generate a comprehensive user profile using an LLM to analyze all of the user's writing.
    Uses the same export logic as /export/threads via build_user_export_content().

    Request body:
        {
            "model": "gpt-5" | "claude-sonnet-4.5" | etc.
        }

    Returns:
        {
            "profile": "The generated profile text...",
            "model_used": "gpt-5",
            "tokens_used": 12345
        }
    """
    from backend.llm_providers import LLMProvider

    # Get and validate the model from request body
    data = request.get_json() or {}
    model_id = data.get("model")

    if not model_id:
        model_id = current_app.config.get("DEFAULT_LLM_MODEL", "claude-opus-4.5")

    # Validate model is supported
    if model_id not in current_app.config["SUPPORTED_MODELS"]:
        return jsonify({
            "error": f"Unsupported model: {model_id}",
            "supported_models": list(current_app.config["SUPPORTED_MODELS"].keys())
        }), 400

    # Quick check if user has any writing to analyze
    has_threads = Node.query.filter_by(user_id=current_user.id, parent_id=None).first() is not None
    if not has_threads:
        return jsonify({
            "error": "No writing found to analyze. Please create some threads first."
        }), 400

    # Enqueue async profile generation task
    from backend.tasks.exports import generate_user_profile

    task = generate_user_profile.delay(current_user.id, model_id)

    current_app.logger.info(f"Enqueued profile generation task {task.id} for user {current_user.id}")

    return jsonify({
        "message": "Profile generation started",
        "task_id": task.id,
        "status": "pending"
    }), 202


@export_bp.route("/export/profile-status/<task_id>", methods=["GET"])
@login_required
def get_profile_status(task_id):
    """Get the status of a profile generation task."""
    from backend.celery_app import celery
    from backend.models import UserProfile

    task = celery.AsyncResult(task_id)

    # Get task state and info
    state = task.state

    # Celery returns PENDING for unknown/expired task IDs. If the DB no
    # longer lists this task as the active generation, the task already
    # finished — treat it as completed so the frontend stops polling.
    if state == 'PENDING' and current_user.profile_generation_task_id != task_id:
        latest = (UserProfile.query
                  .filter_by(user_id=current_user.id)
                  .order_by(UserProfile.created_at.desc())
                  .first())
        profile_data = None
        if latest:
            profile_data = {
                "id": latest.id,
                "content": latest.get_content(),
                "generated_by": latest.generated_by,
                "tokens_used": latest.tokens_used,
                "created_at": latest.created_at.isoformat()
            }
        return jsonify({
            "task_id": task_id,
            "status": "completed",
            "progress": 100,
            "message": "Profile generation complete",
            "error": None,
            "profile": profile_data
        }), 200

    # task.info can be a dict (for PROGRESS state) or an exception (for FAILURE)
    # or None/other for PENDING states
    info = {}
    error_message = None
    if isinstance(task.info, dict):
        info = task.info
    elif isinstance(task.info, Exception):
        error_message = str(task.info)

    # For failed tasks, try to get the traceback
    if state == 'FAILURE':
        error_message = error_message or str(task.info) if task.info else "Unknown error"
        current_app.logger.error(f"Profile generation task {task_id} failed: {error_message}")

    # If task completed, fetch the profile from database
    profile_data = None
    if state == 'SUCCESS' and task.result:
        result = task.result if isinstance(task.result, dict) else {}
        profile_id = result.get('profile_id')
        if profile_id:
            profile = UserProfile.query.get(profile_id)
            if profile and profile.user_id == current_user.id:
                profile_data = {
                    "id": profile.id,
                    "content": profile.get_content(),
                    "generated_by": profile.generated_by,
                    "tokens_used": profile.tokens_used,
                    "created_at": profile.created_at.isoformat()
                }

    # Map Celery states to frontend-expected statuses
    status_map = {
        'PENDING': 'pending',
        'STARTED': 'processing',
        'PROGRESS': 'progress',
        'SUCCESS': 'completed',
        'FAILURE': 'failed',
        'REVOKED': 'failed',
    }
    frontend_status = status_map.get(state, state.lower())

    return jsonify({
        "task_id": task_id,
        "status": frontend_status,
        "progress": info.get('progress', 0),
        "message": info.get('status', ''),
        "error": error_message,
        "profile": profile_data
    }), 200


# Delete all of the current user's data from our app.
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

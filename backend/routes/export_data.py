from flask import Blueprint, jsonify, Response, request, current_app
from flask_login import login_required, current_user
from backend.models import (
    Node, NodeVersion, UserProfile, UserPrompt, UserTodo,
    UserAIPreferences,
)
from backend.extensions import db
from backend.utils.tokens import approximate_token_count, get_model_context_window
from backend.utils.quotes import (
    resolve_quotes, has_quotes, ExportQuoteResolver, resolve_quotes_for_export
)
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

    author = node.user.username if node.user else "Unknown"
    node_type_display = "AI" if node.node_type == "llm" else "User"
    if node.node_type == "llm" and node.llm_model:
        node_type_display = f"AI ({node.llm_model})"

    timestamp = node.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")

    # Build the node text - no content indentation for token efficiency
    result = f"{header_prefix} [{index_path}] {node_type_display} ({author}) - {timestamp}\n"

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
            children = [c for c in children if c.ai_usage in ['chat', 'train']]
        if created_before:
            children = [c for c in children if c.created_at < created_before]
        if included_ids is not None:
            children = [c for c in children if c.id in included_ids]
        children = sorted(children, key=lambda c: c.created_at)

        for i, child in enumerate(children):
            child_index = f"{index_path}.{i+1}"
            if len(children) > 1 and i > 0:
                result += "---\n**BRANCH**\n---\n\n"
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
        children = [c for c in children if c.ai_usage in ['chat', 'train']]
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


def _collect_all_nodes_in_tree(node, filter_ai_usage=False, created_before=None, collected=None):
    """
    Recursively collect all nodes in a tree.

    Args:
        node: Root node of the tree
        filter_ai_usage: If True, only include nodes where ai_usage is 'chat' or 'train'
        created_before: Optional datetime filter
        collected: Set of already collected node IDs (to avoid duplicates)

    Returns:
        List of Node objects in the tree
    """
    if collected is None:
        collected = set()

    if node.id in collected:
        return []

    collected.add(node.id)
    result = [node]

    children = node.children
    if filter_ai_usage:
        children = [c for c in children if c.ai_usage in ['chat', 'train']]
    if created_before:
        children = [c for c in children if c.created_at < created_before]

    for child in children:
        result.extend(_collect_all_nodes_in_tree(
            child, filter_ai_usage, created_before, collected
        ))

    return result


def build_user_export_content(
    user, max_tokens=None, filter_ai_usage=False,
    created_before=None, created_after=None,
    chronological_order=False, return_metadata=False,
    collapse_artifacts=False
):
    """
    Core export logic: Build a human-readable text export of all threads for a given user.

    When max_tokens is specified, uses the ExportQuoteResolver for smart quote resolution
    that ensures quoted content is available even when the export is truncated.

    Args:
        user: User object to export threads for
        max_tokens: Optional maximum token count. If provided, only includes most recent
                   threads that fit within this limit, and uses smart quote resolution.
        filter_ai_usage: If True, only include nodes where ai_usage is 'chat' or 'train'.
                        Use True for AI profile generation, False for user data export.
        created_before: Optional datetime. If provided, only includes threads (and nodes within
                       threads) created before this timestamp.
        created_after: Optional datetime. If provided, only includes nodes created at or
                      after this timestamp. Used for incremental profile updates.
        chronological_order: If True and max_tokens is set, select oldest nodes first
                           (for iterative profile building).
        return_metadata: If True, return a dict with content, token_count,
                        latest_node_created_at, and node_count instead of just the string.

    Returns:
        str or dict: Formatted export content (or None if no threads found).
                    If return_metadata=True, returns dict with keys:
                    content, token_count, latest_node_created_at, node_count
    """
    # Get all top-level nodes (threads) created by the user, ordered by most recent first
    query = Node.query.filter_by(
        user_id=user.id,
        parent_id=None
    )

    # Only filter by AI usage if requested (for AI profile generation)
    if filter_ai_usage:
        query = query.filter(Node.ai_usage.in_(['chat', 'train']))

    # Filter by creation timestamp if specified (for {user_export} context limiting)
    if created_before:
        query = query.filter(Node.created_at < created_before)

    if created_after:
        query = query.filter(Node.created_at > created_after)

    all_top_level_nodes = query.order_by(Node.created_at.desc()).all()

    if not all_top_level_nodes:
        return None

    # Variables for smart quote resolution (used when max_tokens is specified)
    embedded_quotes = None
    included_ids = None
    ai_blocked_ids = None
    resolver = None

    # If max_tokens is specified, use ExportQuoteResolver for smart quote handling
    if max_tokens:
        # Collect ALL nodes from all threads
        all_nodes = []
        for top_node in all_top_level_nodes:
            all_nodes.extend(_collect_all_nodes_in_tree(
                top_node, filter_ai_usage, created_before
            ))

        # Filter by created_after at the node level too
        if created_after:
            all_nodes = [n for n in all_nodes if n.created_at > created_after]

        # Sort: chronological (oldest first) for iterative building,
        # or reverse-chronological (newest first) for normal truncation
        all_nodes.sort(
            key=lambda n: n.created_at,
            reverse=not chronological_order
        )

        # Reserve tokens for header and footer
        header_footer_tokens = 100

        # Create resolver with adjusted token budget
        resolver = ExportQuoteResolver(
            user.id, max_tokens - header_footer_tokens,
            filter_ai_usage=filter_ai_usage,
            chronological=chronological_order
        )

        # Add all nodes to the resolver
        for node in all_nodes:
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
                top_node, filter_ai_usage, created_before
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
        # Determine latest node timestamp and node count from included nodes
        if included_ids is not None:
            if chronological_order:
                # When chronological, use only directly-selected entries
                # (not embedded dependencies) to avoid inflating the cutoff
                # with timestamps from newer nodes that were pulled in as
                # quote dependencies.
                included_entries = resolver.get_included_entries()
                meta_nodes = [
                    n for n in all_nodes
                    if any(e.node_id == n.id for e in included_entries)
                ]
            else:
                meta_nodes = [
                    n for n in all_nodes if n.id in included_ids
                ]
        else:
            meta_nodes = []
            for top_node in top_level_nodes:
                meta_nodes.extend(_collect_all_nodes_in_tree(
                    top_node, filter_ai_usage, created_before
                ))
            if created_after:
                meta_nodes = [
                    n for n in meta_nodes if n.created_at > created_after
                ]
        latest_ts = max(
            (n.created_at for n in meta_nodes), default=None
        )
        return {
            "content": content,
            "token_count": approximate_token_count(content),
            "latest_node_created_at": latest_ts,
            "node_count": len(meta_nodes),
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

"""
Celery task for asynchronous LLM completion.
"""
import json
import re
from celery import Task
from celery.utils.log import get_task_logger
from datetime import datetime
from urllib.parse import parse_qs

from backend.celery_app import celery, flask_app
from backend.models import (
    Node, User, UserProfile, UserRecentContext, UserTodo, APICostLog,
    UserAIPreferences, Draft,
)
from backend.extensions import db
from backend.llm_providers import LLMProvider, PromptTooLongError
from backend.utils.tokens import (
    approximate_token_count, reduce_export_tokens, format_date_metadata,
)
from backend.utils.quotes import resolve_quotes, has_quotes
from backend.utils.api_keys import determine_api_key_type, get_api_keys_for_usage
from backend.utils.cost import calculate_llm_cost_microdollars
from backend.utils.tool_meta import update_tool_meta, parse_github_issue
from backend.utils.privacy import AI_ALLOWED

logger = get_task_logger(__name__)

# Pattern for detecting {user_export} with optional URL-style params.
#
# Syntax: {user_export} or {user_export?param=value&param2=value2}
#
# Supported params:
#   keep=oldest  - When truncating to fit the context window, keep the oldest
#                  threads instead of the newest (default). Useful for tasks
#                  that need early/foundational writing.
#   keep=newest  - Explicit default: keep the newest threads when truncating.
#
USER_EXPORT_PATTERN = re.compile(r"\{user_export(\?[^}]*)?\}")
# Placeholder for injecting user's AI-generated profile into messages
USER_PROFILE_PLACEHOLDER = "{user_profile}"
# Placeholder for injecting user's todo list into messages
USER_TODO_PLACEHOLDER = "{user_todo}"
# Placeholders for recent context (summary + raw data since last summary)
USER_RECENT_PLACEHOLDER = "{user_recent}"
USER_RECENT_RAW_PLACEHOLDER = "{user_recent_raw}"
# Placeholder for AI interaction preferences
USER_AI_PREFERENCES_PLACEHOLDER = "{user_ai_preferences}"

# ── Voice tool definitions ──────────────────────────────────────────────

VOICE_TOOLS = [
    {
        "name": "apply_todo_changes",
        "description": (
            "Apply previously proposed todo changes to the user's actual "
            "todo list. Call this ONLY when the user explicitly confirms "
            "they want to apply the changes (e.g. 'ok apply those "
            "changes', 'yes update my todo', 'go ahead'). Do not call "
            "proactively. Do not call if changes were already applied "
            "(check the context notes for apply status). "
            "Always produce a text response — the user needs to hear "
            "what happened as they are interacting via voice."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "apply_github_issue",
        "description": (
            "Create the previously proposed GitHub issue. Call this "
            "ONLY when the user explicitly confirms they want to create "
            "the issue (e.g. 'yes create it', 'go ahead', 'file that'). "
            "Do not call proactively. Do not call if the issue was "
            "already created (check the context notes for apply status). "
            "Always produce a text response — the user needs to hear "
            "what happened as they are interacting via voice."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "update_ai_preferences",
        "description": (
            "Update the user's AI interaction preferences. Call this when "
            "the user expresses how they want AI to interact with them — "
            "tone, style, boundaries, topics to avoid, interaction "
            "patterns. Examples: 'don't bring up family unless I do', "
            "'be more direct', 'keep todo updates concise'. The "
            "updated_preferences should be the FULL updated text (not "
            "just the diff), incorporating the new preference into the "
            "existing ones. Always produce a text response — the user "
            "needs to hear what happened as they are interacting via voice."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "updated_preferences": {
                    "type": "string",
                    "description": (
                        "The complete updated AI interaction preferences "
                        "as markdown text. Incorporate the new preference "
                        "into the existing text. Keep it concise."
                    ),
                },
            },
            "required": ["updated_preferences"],
        },
    },
]


def get_user_ai_preferences_content(user_id):
    """Get the most recent AI preferences if AI usage is permitted."""
    prefs = UserAIPreferences.query.filter_by(user_id=user_id).order_by(
        UserAIPreferences.created_at.desc()
    ).first()
    if prefs and prefs.ai_usage == "chat":
        return prefs.get_content()
    return None


def _is_voice_prompt(node_chain):
    """Check if this conversation has a voice prompt in its chain."""
    for node in node_chain:
        prompt = node.get_artifact("prompt") if hasattr(node, 'get_artifact') else None
        if prompt is not None and prompt.prompt_key == 'voice':
            return True
    return False


def _find_pending_todo_draft(node_chain, user_id):
    """Walk the node chain to find a pending todo draft."""
    for node in reversed(node_chain):
        draft = Draft.query.filter_by(
            parent_id=node.id,
            user_id=user_id,
            label='todo_pending',
        ).first()
        if draft:
            return draft
    return None


def _find_pending_github_issue_draft(node_chain, user_id):
    """Walk the node chain to find a pending GitHub issue draft."""
    for node in reversed(node_chain):
        draft = Draft.query.filter_by(
            parent_id=node.id,
            user_id=user_id,
            label='github_issue_pending',
        ).first()
        if draft:
            return draft
    return None


def _detect_todo_proposal(text):
    """Check if LLM text contains todo proposal headings.

    Requires at least one task-specific heading (Completed, New Tasks,
    Priority). A standalone ### Note is not enough — it's too generic.
    """
    if not text:
        return False
    headings = [h.lower() for h in re.findall(
        r'^###\s+(.+)', text, re.MULTILINE)]
    task_keywords = {'completed', 'new task', 'new tasks', 'priority'}
    return any(
        any(kw in h for kw in task_keywords) for h in headings
    )


def _detect_github_issue_proposal(text):
    """Check if LLM text contains GitHub issue proposal headings."""
    if not text:
        return False
    headings = [h.lower() for h in re.findall(
        r'^###\s+(.+)', text, re.MULTILINE)]
    has_title = any(
        'issue title' in h or h.strip() == 'title' for h in headings)
    has_desc = any('description' in h for h in headings)
    return has_title and has_desc


def _scan_proposal_statuses(node_chain):
    """Walk all nodes and collect proposal status notes to inject.

    Refreshes each node from the DB (the merge task may have updated
    tool_calls_meta asynchronously). Returns (notes_list, nodes_to_mark)
    where nodes_to_mark is a list of (node, tool_name) tuples whose
    status_reported flag should be set after a successful LLM call.
    """
    notes = []
    to_mark = []
    for node in node_chain:
        if not node.tool_calls_meta:
            continue
        # Refresh from DB to pick up async merge updates
        db.session.refresh(node)
        if not node.tool_calls_meta:
            continue
        try:
            meta = json.loads(node.tool_calls_meta)
        except (json.JSONDecodeError, TypeError):
            continue
        for entry in meta:
            name = entry.get("name")
            status = entry.get("apply_status")
            reported = entry.get("status_reported")
            if name not in ("propose_todo", "propose_github_issue"):
                continue
            tag = ("todo-proposal" if name == "propose_todo"
                   else "issue-proposal")
            if status == "started":
                notes.append(
                    f"[{tag}:{node.id}: merge in progress.]"
                )
            elif status == "completed" and not reported:
                notes.append(
                    f"[{tag}:{node.id}: applied successfully.]"
                )
                to_mark.append((node, name))
            elif status == "failed" and not reported:
                err = entry.get("apply_error", "unknown error")
                notes.append(
                    f"[{tag}:{node.id}: failed — {err}.]"
                )
                to_mark.append((node, name))
    return notes, to_mark


def _mark_status_reported(to_mark):
    """Set status_reported=true on proposal entries after LLM success."""
    for node, tool_name in to_mark:
        try:
            meta = json.loads(node.tool_calls_meta)
            for entry in meta:
                if entry.get("name") == tool_name:
                    entry["status_reported"] = True
            node.tool_calls_meta = json.dumps(meta)
        except (json.JSONDecodeError, TypeError):
            pass


def _supersede_old_proposals(node_chain, tool_name, exclude_node_id):
    """Mark old pending_approval proposals as superseded.

    Only supersedes entries matching *tool_name* (scoped by type).
    Proposals already in 'started' or later states are left alone.
    """
    for node in node_chain:
        if node.id == exclude_node_id or not node.tool_calls_meta:
            continue
        try:
            meta = json.loads(node.tool_calls_meta)
        except (json.JSONDecodeError, TypeError):
            continue
        changed = False
        for entry in meta:
            if (entry.get("name") == tool_name
                    and entry.get("apply_status") == "pending_approval"):
                entry["apply_status"] = "superseded"
                changed = True
        if changed:
            node.tool_calls_meta = json.dumps(meta)


def _auto_create_drafts(llm_text, llm_node, node_chain, user_id):
    """Auto-detect proposals in LLM text and create draft flags.

    Returns list of auto-created draft metadata (same shape as tool results)
    so the frontend can show accept buttons.
    """
    results = []

    if _detect_todo_proposal(llm_text):
        # Skip if a draft already exists on this node (e.g. from a
        # stale update_todo tool call that was also processed)
        already = Draft.query.filter_by(
            parent_id=llm_node.id, user_id=user_id,
            label='todo_pending',
        ).first()
        if not already:
            existing = _find_pending_todo_draft(node_chain, user_id)
            draft = Draft(
                user_id=user_id,
                parent_id=llm_node.id,
                label='todo_pending',
            )
            draft.set_content("")
            db.session.add(draft)
            db.session.flush()
            if existing:
                db.session.delete(existing)
                db.session.flush()
            # Supersede old pending_approval update_todo proposals
            _supersede_old_proposals(
                node_chain, "propose_todo", llm_node.id)
            results.append({
                "name": "propose_todo",
                "status": "success",
                "draft_id": draft.id,
                "apply_status": "pending_approval",
            })

    if _detect_github_issue_proposal(llm_text):
        already = Draft.query.filter_by(
            parent_id=llm_node.id, user_id=user_id,
            label='github_issue_pending',
        ).first()
        if not already:
            existing = _find_pending_github_issue_draft(
                node_chain, user_id)
            draft = Draft(
                user_id=user_id,
                parent_id=llm_node.id,
                label='github_issue_pending',
            )
            draft.set_content("")
            db.session.add(draft)
            db.session.flush()
            if existing:
                db.session.delete(existing)
                db.session.flush()
            # Supersede old pending_approval issue proposals
            _supersede_old_proposals(
                node_chain, "propose_github_issue", llm_node.id)
            results.append({
                "name": "propose_github_issue",
                "status": "success",
                "draft_id": draft.id,
                "apply_status": "pending_approval",
            })

    return results


def _execute_tool_calls(tool_calls, llm_node, node_chain, user_id):
    """Execute tool calls and return metadata list."""
    tool_results = []

    for tc in tool_calls:
        name = tc["name"]
        inp = tc.get("input", {})
        result = {"name": name, "input": inp}

        try:
            if name == "apply_todo_changes":
                draft = _find_pending_todo_draft(node_chain, user_id)
                if not draft:
                    result["status"] = "error"
                    result["error"] = "No pending todo changes found"
                else:
                    # Kick off async background merge using the
                    # proposal node (where the draft originated)
                    from backend.routes.todo import (
                        _start_todo_merge,
                    )
                    proposal_node = Node.query.get(draft.parent_id)
                    task_id = _start_todo_merge(
                        draft, proposal_node or llm_node, user_id
                    )
                    result["status"] = "success"
                    result["apply_task_id"] = task_id

            elif name == "apply_github_issue":
                draft = _find_pending_github_issue_draft(
                    node_chain, user_id
                )
                if not draft:
                    result["status"] = "error"
                    result["error"] = "No pending GitHub issue found"
                else:
                    # Find the LLM node that proposed the issue
                    origin_node = Node.query.get(draft.parent_id)
                    if not origin_node:
                        result["status"] = "error"
                        result["error"] = "Origin node not found"
                    else:
                        issue_data = parse_github_issue(
                            origin_node.get_content() or ""
                        )
                        if not issue_data.get("title"):
                            result["status"] = "error"
                            result["error"] = (
                                "Could not parse issue from proposal"
                            )
                        else:
                            from backend.utils.github import (
                                create_github_issue,
                            )
                            user = User.query.get(user_id)
                            username = (
                                user.username if user else str(user_id)
                            )
                            category = issue_data.get(
                                "category", "enhancement"
                            )
                            if category not in (
                                "bug", "feature", "enhancement"
                            ):
                                category = "enhancement"
                            gh_result = create_github_issue(
                                title=issue_data["title"],
                                description=issue_data.get(
                                    "description", ""
                                ),
                                category=category,
                                username=username,
                            )
                            # Clean up draft
                            db.session.delete(draft)
                            db.session.flush()
                            # Update origin node meta
                            update_tool_meta(
                                origin_node,
                                "propose_github_issue",
                                {
                                    "apply_status": "completed",
                                    "issue_url": gh_result["url"],
                                    "issue_number": gh_result["number"],
                                },
                            )
                            result["status"] = "success"
                            result["issue_url"] = gh_result["url"]
                            result["issue_number"] = gh_result["number"]

            elif name == "update_ai_preferences":
                prefs = UserAIPreferences(
                    user_id=user_id,
                    generated_by=llm_node.llm_model or "voice_session",
                    tokens_used=0,
                )
                prefs.set_content(inp["updated_preferences"])
                db.session.add(prefs)
                db.session.flush()
                result["status"] = "success"
                result["preferences_id"] = prefs.id

            elif name in ("propose_todo", "propose_github_issue"):
                # These are no longer tools — proposals are auto-detected
                # from headings. Ignore gracefully if LLM still calls them.
                result["status"] = "ignored"
            else:
                result["status"] = "error"
                result["error"] = f"Unknown tool: {name}"

        except Exception as e:
            logger.error(f"Tool execution error for {name}: {e}",
                         exc_info=True)
            result["status"] = "error"
            result["error"] = str(e)

        tool_results.append(result)

    return tool_results


def parse_placeholder_params(match_str):
    """Parse URL-style params from a placeholder like {user_export?keep=oldest}."""
    if '?' in match_str:
        qs = match_str.split('?', 1)[1].rstrip('}')
        return {k: v[0] for k, v in parse_qs(qs).items()}
    return {}


def build_user_export_content(user, max_tokens=None, filter_ai_usage=False,
                              created_before=None, chronological_order=False):
    """Import the actual implementation from export_data routes."""
    from backend.routes.export_data import build_user_export_content as _build
    return _build(user, max_tokens, filter_ai_usage, created_before,
                  chronological_order=chronological_order)


def get_user_profile_content(user_id):
    """
    Get the most recent user profile if AI usage is permitted.

    Returns the UserProfile object if ai_usage is 'chat' or 'train',
    otherwise returns None.
    """
    profile = UserProfile.query.filter_by(user_id=user_id).order_by(
        UserProfile.created_at.desc()
    ).first()

    if profile and profile.ai_usage in AI_ALLOWED:
        return profile
    return None


def get_user_todo_content(user_id):
    """
    Get the most recent user todo content if AI usage is permitted.

    Returns the todo content if ai_usage permits AI access,
    otherwise returns None.
    """
    todo = UserTodo.query.filter_by(user_id=user_id).order_by(
        UserTodo.created_at.desc()
    ).first()

    if todo and todo.ai_usage in AI_ALLOWED:
        return todo.get_content()
    return None


def get_user_recent_content(user_id):
    """Get the latest recent context summary for a user.

    Filters by profile_id matching the current profile so old summaries
    (from before a profile update) are not returned.
    """
    profile = UserProfile.query.filter_by(user_id=user_id).filter(
        UserProfile.ai_usage.in_(AI_ALLOWED)
    ).order_by(UserProfile.created_at.desc()).first()

    profile_id = profile.id if profile else None

    q = UserRecentContext.query.filter_by(user_id=user_id)
    if profile_id is not None:
        q = q.filter_by(profile_id=profile_id)
    else:
        q = q.filter(UserRecentContext.profile_id.is_(None))

    rc = q.order_by(UserRecentContext.created_at.desc()).first()
    if rc and rc.ai_usage in AI_ALLOWED:
        return rc
    return None


def get_user_recent_raw_content(user_id, created_before=None):
    """Get the most recent ~10K tokens of raw user writing.

    Always returns a fixed window of the last ~10K tokens regardless of
    when the recent context summary was last generated.  This guarantees
    a consistent window of high-fidelity recent context.

    Args:
        created_before: Upper bound timestamp. Nodes created at/after this
            time are excluded to avoid duplicating current session context.

    Returns:
        dict with keys (content, earliest, latest) or None if no data.
    """
    from backend.routes.export_data import (
        build_user_export_content as _build_export,
    )
    user = User.query.get(user_id)
    if not user:
        return None

    result = _build_export(
        user,
        max_tokens=10000,
        filter_ai_usage=True,
        created_before=created_before,
        chronological_order=False,
        return_metadata=True,
    )
    if not result:
        return None

    return {
        "content": result["content"],
        "earliest": result.get("earliest_node_created_at"),
        "latest": result.get("latest_node_created_at"),
        "token_count": result.get("token_count", 0),
    }


class LLMCompletionTask(Task):
    """Custom task class with error handling."""

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        """Called when task fails."""
        # In the new scheme, the llm_node_id is the second argument
        llm_node_id = args[1] if len(args) > 1 else None
        if llm_node_id:
            with flask_app.app_context():
                node = Node.query.get(llm_node_id)
                if node:
                    node.llm_task_status = 'failed'
                    # Store error message if not already set
                    if not node.llm_task_error:
                        node.llm_task_error = str(exc)
                    db.session.commit()
                    logger.error(f"LLM completion failed for node {llm_node_id}: {exc}")


@celery.task(base=LLMCompletionTask, bind=True)
def generate_llm_response(self, parent_node_id: int, llm_node_id: int, model_id: str, user_id: int):
    """
    Asynchronously generate an LLM response and update a placeholder node.

    Args:
        parent_node_id: ID of the parent node to respond to.
        llm_node_id: ID of the placeholder 'llm' node to update.
        model_id: Model identifier (e.g., "gpt-5", "claude-sonnet-4.5").
        user_id: ID of the user requesting the completion.
    """
    logger.info(f"Starting LLM completion task for parent {parent_node_id}, updating node {llm_node_id}, model={model_id}")

    with flask_app.app_context():
        parent_node = Node.query.get(parent_node_id)
        llm_node = Node.query.get(llm_node_id)

        if not parent_node:
            raise ValueError(f"Parent node {parent_node_id} not found")
        if not llm_node:
            raise ValueError(f"LLM node {llm_node_id} not found")

        # Update status on the new llm_node
        llm_node.llm_task_status = 'processing'
        llm_node.llm_task_progress = 10
        db.session.commit()

        try:
            # ... (The rest of the logic remains largely the same, but updates llm_node)

            # Step 1: Build the chain of nodes for context
            self.update_state(state='PROGRESS', meta={'progress': 20, 'status': 'Building context'})
            llm_node.llm_task_progress = 20
            db.session.commit()

            node_chain = []
            current = parent_node
            while current:
                node_chain.insert(0, current)
                current = current.parent

            # Step 2: Build messages array
            self.update_state(state='PROGRESS', meta={'progress': 30, 'status': 'Preparing messages'})
            llm_node.llm_task_progress = 30
            db.session.commit()

            # Check if any node contains the {user_export} placeholder
            # Find the first node containing it to use its timestamp as cutoff
            export_node = None
            export_placeholder_match = None
            for node in node_chain:
                node_content = node.get_content()
                if node_content:
                    m = USER_EXPORT_PATTERN.search(node_content)
                    if m:
                        export_node = node
                        export_placeholder_match = m.group(0)
                        break
            needs_export = export_node is not None
            export_params = parse_placeholder_params(export_placeholder_match) if export_placeholder_match else {}
            user_export_content = None

            # Check if any node contains the {user_profile} placeholder
            needs_profile = any(
                USER_PROFILE_PLACEHOLDER in node.get_content()
                for node in node_chain if node.get_content()
            )
            user_profile_content = None

            # Check if any node contains the {user_todo} placeholder
            needs_todo = any(
                USER_TODO_PLACEHOLDER in node.get_content()
                for node in node_chain if node.get_content()
            )
            user_todo_content = None

            # Check if any node contains {user_recent} or {user_recent_raw}
            needs_recent = any(
                USER_RECENT_PLACEHOLDER in node.get_content()
                for node in node_chain if node.get_content()
            )
            # Find the node containing {user_recent_raw} to use its
            # created_at as an upper bound (avoid duplicating session context)
            recent_raw_node = None
            for node in node_chain:
                nc = node.get_content()
                if nc and USER_RECENT_RAW_PLACEHOLDER in nc:
                    recent_raw_node = node
                    break
            needs_recent_raw = recent_raw_node is not None
            user_recent_content = None
            user_recent_raw_content = None

            # Check if any node contains {user_ai_preferences}
            needs_ai_prefs = any(
                USER_AI_PREFERENCES_PLACEHOLDER in node.get_content()
                for node in node_chain if node.get_content()
            )
            user_ai_preferences_content = None

            # Check if any node contains {quote:ID} placeholders
            needs_quotes = any(
                has_quotes(node.get_content())
                for node in node_chain if node.get_content()
            )
            if needs_quotes:
                logger.info("Detected {quote:ID} placeholders in conversation chain")

            if needs_profile:
                profile_obj = get_user_profile_content(user_id)
                if profile_obj:
                    # Metadata already baked into stored content
                    user_profile_content = profile_obj.get_content()

            if needs_todo:
                user_todo_content = get_user_todo_content(user_id)

            if needs_recent:
                rc = get_user_recent_content(user_id)
                if rc:
                    # Metadata already baked into stored content
                    user_recent_content = rc.get_content()

            if needs_recent_raw:
                raw_cutoff = recent_raw_node.created_at if recent_raw_node else None
                raw_result = get_user_recent_raw_content(
                    user_id, created_before=raw_cutoff
                )
                if raw_result:
                    # Raw data is dynamic — add metadata on the fly
                    user_recent_raw_content = format_date_metadata(
                        covers_start=raw_result.get("earliest"),
                        covers_end=raw_result.get("latest"),
                        tokens=raw_result.get("token_count"),
                    ) + raw_result["content"]

            if needs_ai_prefs:
                user_ai_preferences_content = get_user_ai_preferences_content(user_id)

            # Detect if this is a voice session (enables tools)
            is_voice = _is_voice_prompt(node_chain)
            voice_tools = VOICE_TOOLS if is_voice else None

            # Check for pending drafts and inject context notes
            pending_draft_note = None
            if is_voice:
                pending = _find_pending_todo_draft(node_chain, user_id)
                if pending:
                    pending_draft_note = (
                        "[Note: there are pending todo changes awaiting "
                        "confirmation. The user can say 'apply the "
                        "changes' to confirm.]"
                    )
                pending_issue = _find_pending_github_issue_draft(
                    node_chain, user_id
                )
                if pending_issue:
                    issue_note = (
                        "[Note: there is a proposed GitHub issue awaiting "
                        "confirmation. The user can say 'yes create it' "
                        "or 'file that issue' to confirm.]"
                    )
                    if pending_draft_note:
                        pending_draft_note += "\n" + issue_note
                    else:
                        pending_draft_note = issue_note

            # Scan all proposals across the chain for status injection.
            # Refreshes from DB to pick up async merge updates.
            proposal_notes = []
            proposal_to_mark = []
            if is_voice:
                proposal_notes, proposal_to_mark = (
                    _scan_proposal_statuses(node_chain)
                )

            # Inject status for non-proposal tools (only ai_preferences)
            prev_tool_note = None
            if is_voice and len(node_chain) >= 2:
                prev_llm = None
                for prev_node in reversed(node_chain):
                    if (prev_node.node_type == "llm"
                            or prev_node.llm_model is not None):
                        prev_llm = prev_node
                        break
                if prev_llm and prev_llm.tool_calls_meta:
                    try:
                        prev_meta = json.loads(prev_llm.tool_calls_meta)
                        for m in prev_meta:
                            if m.get("name") == "update_ai_preferences":
                                prev_tool_note = (
                                    "[AI preferences were updated.]"
                                )
                                break
                    except (json.JSONDecodeError, KeyError):
                        pass

            if needs_export:
                max_export_tokens = None  # Send entire archive; let retry loop converge

            # Determine which API key to use based on ai_usage settings
            key_type = determine_api_key_type(node_chain, logger=logger)
            api_keys = get_api_keys_for_usage(flask_app.config, key_type)

            model_config = flask_app.config["SUPPORTED_MODELS"][model_id]
            provider = model_config["provider"]
            api_model = model_config["api_model"]

            if provider == "anthropic" and not api_keys["anthropic"]:
                raise ValueError("Anthropic API key is not configured.")
            elif provider == "openai" and not api_keys["openai"]:
                raise ValueError("OpenAI API key is not configured.")

            MAX_RETRIES = 3
            for attempt in range(MAX_RETRIES + 1):
                if needs_export:
                    user = User.query.get(user_id)
                    if user and max_export_tokens != 0:
                        # Use the timestamp of the node containing {user_export} as cutoff
                        # to only include archive data created before that node
                        created_before = export_node.created_at if export_node else None
                        chronological = export_params.get('keep') == 'oldest'
                        user_export_content = build_user_export_content(
                            user,
                            max_tokens=max_export_tokens,
                            filter_ai_usage=True,
                            created_before=created_before,
                            chronological_order=chronological
                        )
                        logger.info(f"Built user export for {user_id}: {len(user_export_content or '')} chars, ~{approximate_token_count(user_export_content or '')} tokens, cutoff={created_before} (attempt {attempt + 1})")

                messages = []
                for node in node_chain:
                    author = node.user.username if node.user else "Unknown"
                    is_llm_node = node.node_type == "llm" or (node.llm_model is not None)
                    node_content = node.get_content()

                    if is_llm_node:
                        role = "assistant"
                        message_text = node_content
                        # Tag proposals with node ID for tracking
                        if is_voice and node.tool_calls_meta:
                            try:
                                tcm = json.loads(node.tool_calls_meta)
                                for entry in tcm:
                                    ename = entry.get("name")
                                    if ename == "propose_todo":
                                        message_text += (
                                            f"\n\n[todo-proposal:"
                                            f"{node.id}]"
                                        )
                                    elif ename == "propose_github_issue":
                                        message_text += (
                                            f"\n\n[issue-proposal:"
                                            f"{node.id}]"
                                        )
                            except (json.JSONDecodeError, TypeError):
                                pass
                    else:
                        role = "user"
                        message_text = f"author {author}: {node_content}"
                        # Replace {user_export} placeholder if present
                        if user_export_content and export_placeholder_match:
                            message_text = message_text.replace(
                                export_placeholder_match,
                                user_export_content
                            )
                        # Replace {user_profile} placeholder if present
                        if USER_PROFILE_PLACEHOLDER in message_text:
                            message_text = message_text.replace(
                                USER_PROFILE_PLACEHOLDER,
                                user_profile_content or ""
                            )
                        # Replace {user_todo} placeholder if present
                        if USER_TODO_PLACEHOLDER in message_text:
                            message_text = message_text.replace(
                                USER_TODO_PLACEHOLDER,
                                user_todo_content or ""
                            )
                        # Replace {user_recent} placeholder if present
                        if USER_RECENT_PLACEHOLDER in message_text:
                            message_text = message_text.replace(
                                USER_RECENT_PLACEHOLDER,
                                user_recent_content or ""
                            )
                        # Replace {user_recent_raw} placeholder if present
                        if USER_RECENT_RAW_PLACEHOLDER in message_text:
                            message_text = message_text.replace(
                                USER_RECENT_RAW_PLACEHOLDER,
                                user_recent_raw_content or ""
                            )
                        # Replace {user_ai_preferences} placeholder
                        if USER_AI_PREFERENCES_PLACEHOLDER in message_text:
                            message_text = message_text.replace(
                                USER_AI_PREFERENCES_PLACEHOLDER,
                                user_ai_preferences_content or ""
                            )
                        # Resolve {quote:ID} placeholders if present
                        if needs_quotes and has_quotes(message_text):
                            message_text, resolved_ids = resolve_quotes(message_text, user_id, for_llm=True)
                            if resolved_ids:
                                logger.info(f"Resolved quotes for node IDs: {resolved_ids}")

                    messages.append({
                        "role": role,
                        "content": [{"type": "text", "text": message_text}]
                    })

                # Inject voice context notes as a final user message
                voice_notes = (
                    proposal_notes
                    + [n for n in [prev_tool_note, pending_draft_note] if n]
                )
                if is_voice and voice_notes:
                    injected_text = "\n".join(voice_notes)
                    logger.debug(f"Voice context injection for node {llm_node_id}: {injected_text}")
                    messages.append({
                        "role": "user",
                        "content": [{"type": "text", "text": injected_text}]
                    })

                # Step 3: Call LLM API
                self.update_state(state='PROGRESS', meta={'progress': 40, 'status': 'Generating response'})
                llm_node.llm_task_progress = 40
                db.session.commit()

                # Log total context being sent
                total_content = "".join(m["content"][0]["text"] for m in messages if m.get("content"))
                estimated_tokens = approximate_token_count(total_content)
                logger.info(f"Calling LLM API: model_id={model_id}, api_model={api_model}, provider={provider}, key_type={key_type}, estimated_tokens={estimated_tokens}, total_chars={len(total_content)}")

                try:
                    response = LLMProvider.get_completion(
                        model_id, messages, api_keys,
                        tools=voice_tools,
                    )
                    break  # Success
                except PromptTooLongError as e:
                    if attempt == MAX_RETRIES or not needs_export:
                        raise
                    max_export_tokens = reduce_export_tokens(
                        max_export_tokens, e.actual_tokens, e.max_tokens,
                        export_content=user_export_content
                    )
                    logger.warning(
                        f"Prompt too long ({e.actual_tokens} > {e.max_tokens}), "
                        f"retrying with max_export_tokens={max_export_tokens} "
                        f"(attempt {attempt + 2}/{MAX_RETRIES + 1})"
                    )
            llm_text = response["content"]
            total_tokens = response["total_tokens"]
            input_tokens = response.get("input_tokens", 0)
            output_tokens = response.get("output_tokens", 0)
            response_tool_calls = response.get("tool_calls", [])
            response_truncated = response.get("truncated", False)

            logger.info(f"LLM response generated: {len(llm_text)} chars, {total_tokens} tokens, {len(response_tool_calls)} tool calls")

            # Step 4: Log API cost
            self.update_state(state='PROGRESS', meta={'progress': 90, 'status': 'Logging cost'})
            llm_node.llm_task_progress = 90
            db.session.commit()

            cost = calculate_llm_cost_microdollars(model_id, input_tokens, output_tokens)
            cost_log = APICostLog(
                user_id=user_id,
                model_id=model_id,
                request_type="conversation",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_microdollars=cost,
            )
            db.session.add(cost_log)

            # Step 5: Update the placeholder LLM node with the response
            self.update_state(state='PROGRESS', meta={'progress': 95, 'status': 'Finalizing'})
            llm_node.set_content(llm_text)
            llm_node.token_count = output_tokens or approximate_token_count(llm_text)

            # Step 5b: Execute tool calls if any
            tool_meta = None
            if response_tool_calls:
                logger.info(f"Executing {len(response_tool_calls)} tool calls")
                tool_results = _execute_tool_calls(
                    response_tool_calls, llm_node, node_chain, user_id
                )
                if response_truncated:
                    for tr in tool_results:
                        tr["response_truncated"] = True
                tool_meta = tool_results

            # Step 5c: Auto-detect proposals in LLM text (Voice sessions)
            if is_voice:
                auto_drafts = _auto_create_drafts(
                    llm_text, llm_node, node_chain, user_id
                )
                if auto_drafts:
                    if tool_meta is None:
                        tool_meta = []
                    tool_meta.extend(auto_drafts)

            if tool_meta:
                llm_node.tool_calls_meta = json.dumps(tool_meta)

            # Mark proposal statuses as reported (deferred until
            # after LLM success so they re-inject on retry)
            if proposal_to_mark:
                _mark_status_reported(proposal_to_mark)

            llm_node.llm_task_status = 'completed'
            llm_node.llm_task_progress = 100
            db.session.commit()

            logger.info(f"LLM completion successful, updated node {llm_node.id}")

            result = {
                'parent_node_id': parent_node_id,
                'llm_node_id': llm_node.id,
                'status': 'completed',
                'total_tokens': total_tokens,
            }
            if tool_meta:
                result['tool_calls_meta'] = tool_meta
            return result

        except Exception as e:
            error_message = str(e)
            logger.error(f"LLM completion error for node {llm_node_id}: {error_message}", exc_info=True)
            llm_node.llm_task_status = 'failed'
            llm_node.llm_task_error = error_message
            db.session.commit()
            raise

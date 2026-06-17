"""
Celery task for asynchronous LLM completion.
"""
import json
import re
from celery import Task
from celery.utils.log import get_task_logger
from datetime import datetime, timezone

from backend.celery_app import celery, flask_app
from backend.models import (
    Node, User, UserProfile, UserRecentContext, UserTodo, APICostLog,
    UserAIPreferences, UserArtifact, UserFeedback, Draft,
    NodeContextArtifact,
)
from backend.extensions import db
from backend.llm_providers import LLMProvider, PromptTooLongError
from backend.utils.tokens import (
    approximate_token_count, reduce_export_tokens, format_date_metadata,
)
from backend.utils.quotes import resolve_quotes, has_quotes
from backend.utils.timefmt import local_stamp
from backend.utils.api_keys import determine_api_key_type, get_api_keys_for_usage
from backend.utils.cost import calculate_llm_cost_microdollars
from backend.utils.tool_meta import update_tool_meta, parse_github_issue
from backend.utils.privacy import AI_ALLOWED
from backend.utils.placeholders import (
    USER_EXPORT_PATTERN,
    parse_placeholder_params,
    parse_max_export_tokens,
    warn_unknown_user_export_keys,
)

logger = get_task_logger(__name__)

# See backend/utils/placeholders.py for USER_EXPORT_PATTERN and the
# {user_export} placeholder syntax/modifier reference.

# Placeholder for injecting user's AI-generated profile into messages
USER_PROFILE_PLACEHOLDER = "{user_profile}"
# Placeholder for injecting user's todo list into messages
USER_TODO_PLACEHOLDER = "{user_todo}"
# Placeholders for recent context (summary + raw data since last summary)
USER_RECENT_PLACEHOLDER = "{user_recent}"
USER_RECENT_RAW_PLACEHOLDER = "{user_recent_raw}"
# Placeholder for AI interaction preferences
USER_AI_PREFERENCES_PLACEHOLDER = "{user_ai_preferences}"
# Placeholders for user artifacts — LLM memory/scratchpad + index (#158)
USER_MEMORY_PLACEHOLDER = "{user_memory}"
USER_SCRATCHPAD_PLACEHOLDER = "{user_scratchpad}"
USER_ARTIFACTS_INDEX_PLACEHOLDER = "{user_artifacts_index}"

# Within-turn retrieval loop (#158, text mode only). When the model calls one
# of these tools, the retrieved content is injected back into the message
# stream and the model is re-called so it answers WITH the content in the same
# turn — instead of the cross-turn injection done by _scan_proposal_statuses.
# read_artifact pulls a UserArtifact by kind; read_todo pulls the user's todo
# list (its own model, no longer shown inline — #158 Slice 3).
RETRIEVAL_TOOLS = {"read_artifact", "read_todo"}
# Max number of interim retrieval round-trips before the model must answer.
MAX_RETRIEVAL_ROUNDS = 2

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
        "name": "update_artifact",
        "description": (
            "Create or update one of the user's artifacts — persistent "
            "documents that survive across sessions. Built-in kinds: "
            "'memory' (durable facts and observations about the user "
            "worth remembering long-term — write here whenever you learn "
            "something that future sessions should know) and 'scratchpad' "
            "(your working notes for ongoing threads of work). You can "
            "also create new kinds for the user on request (e.g. "
            "'reading-list'). The updated_content must be the FULL new "
            "text of the artifact (not a diff) — it replaces the previous "
            "version, and old versions stay in history. Call proactively "
            "for memory-worthy facts; no confirmation is needed. Always "
            "produce a text response alongside the call."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "description": (
                        "Artifact slug: 'memory', 'scratchpad', or a "
                        "short lowercase dash-separated name for a "
                        "custom artifact."
                    ),
                },
                "updated_content": {
                    "type": "string",
                    "description": (
                        "The complete new artifact text as markdown. "
                        "Carry forward everything still relevant from "
                        "the current version."
                    ),
                },
                "title": {
                    "type": "string",
                    "description": (
                        "Display title. Only needed when creating a new "
                        "custom artifact."
                    ),
                },
                "description": {
                    "type": "string",
                    "description": (
                        "One-line summary of what this artifact is for, "
                        "shown in the index/nav. Set it when creating a new "
                        "kind; for built-in kinds and updates you can omit "
                        "it (the existing/default description is kept)."
                    ),
                },
            },
            "required": ["kind", "updated_content"],
        },
    },
    {
        "name": "read_artifact",
        "description": (
            "Request the full content of one of the user's artifacts "
            "listed in the artifacts index. The content is injected back "
            "into your context within the same turn and you answer with "
            "it. Use this only for artifacts in the index — anything "
            "already shown inline in your context is always present and "
            "should not be read. Tell the user you're pulling it up. Always "
            "produce a text response alongside the call."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "description": "Slug of the artifact to read.",
                },
            },
            "required": ["kind"],
        },
    },
    {
        "name": "read_todo",
        "description": (
            "Pull the user's current todo list into context. The todo "
            "list is listed in the artifacts index (as 'todo') rather than "
            "shown inline, so read it on demand whenever the conversation "
            "turns to their tasks, plans, or priorities — and before "
            "proposing todo changes, so your proposal builds on what's "
            "actually there. Like read_artifact, the content is injected "
            "back into your context within the same turn and you answer "
            "with it, so tell the user you're pulling it up. Always produce "
            "a text response alongside the call."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "submit_feedback",
        "description": (
            "Submit feedback about Loore itself to its creators on the "
            "user's behalf. Call this when the user expresses feedback "
            "about the product — praise, frustration, confusion, ideas — "
            "and either asks you to pass it on or agrees when you offer. "
            "For concrete bugs or feature requests prefer proposing a "
            "GitHub issue instead; feedback is for everything that "
            "doesn't fit an issue. Quote or faithfully summarize the "
            "user's own words. Always produce a text response alongside "
            "the call."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": (
                        "The feedback text, faithful to what the user "
                        "expressed."
                    ),
                },
                "category": {
                    "type": "string",
                    "enum": ["praise", "frustration", "idea", "other"],
                    "description": "Best-fit category.",
                },
            },
            "required": ["content"],
        },
    },
]


_ARTIFACT_KIND_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,47}$')

# UserArtifact kinds that are injected INLINE in the agentic prompt (each has
# its own placeholder/tag) and therefore must be EXCLUDED from the artifacts
# index — otherwise they'd appear both inline and in the index. Single source
# of truth for both index-exclusion spots in get_user_artifacts_context.
# (#202 intentions joins this when it lands — it has an ambient
# {user_intentions} placeholder too.)
ALWAYS_INLINE_KINDS = ("memory", "scratchpad", "ai_preferences")

# Kinds that are NOT UserArtifacts — they're separate single-row models or
# system-managed, so update_artifact must refuse them. Without this guard the
# model could create a UserArtifact named e.g. "todo" that shadows and
# silently diverges from the real UserTodo. Each value is a message pointing
# the model at the correct path. (ai_preferences is NOT here — Slice 5 folded
# it into the UserArtifact model, so it's a normal writable artifact now.)
RESERVED_ARTIFACT_KINDS = {
    "todo": ("Todo changes go through proposals — put them under the "
             "### Completed / ### New Tasks / ### Priority Order headings in "
             "your reply; don't write the todo with update_artifact."),
    "profile": ("The profile is system-generated and read-only — there is no "
                "tool to edit it."),
    "recent_context": "Recent context is system-generated and can't be edited.",
}


def _todo_index_line(user_id, pinned_node=None):
    """Index line for the user's todo list, or None if it's AI-blocked.

    Todo is its own model (not a UserArtifact) and since #158 Slice 3 is no
    longer shown inline — it appears in the index and is pulled on demand via
    read_todo. Resolved from the session's pinned snapshot, falling back to
    latest. A todo the user opted out of AI access is omitted entirely; an
    absent or empty todo is still listed (as ``(empty)``) so the model knows
    the surface exists. No open/priority counts (noise).
    """
    todo = pinned_node.get_artifact("todo") if pinned_node else None
    if todo is None:
        todo = UserTodo.query.filter_by(user_id=user_id).order_by(
            UserTodo.created_at.desc()
        ).first()
    if todo is not None and todo.ai_usage not in AI_ALLOWED:
        return None  # privacy: user opted this todo out of AI access
    content = (todo.get_content() if todo else "") or ""
    if content.strip():
        suffix = f"(~{approximate_token_count(content)} tokens)"
    else:
        suffix = "(empty)"
    return (f"- todo — the user's running task list; pull it with "
            f"read_todo {suffix}")


def get_user_artifacts_context(user_id, pinned_node=None):
    """Resolve user artifacts for the agentic prompt (#158).

    Returns (memory_content, scratchpad_content, index_text). Pinned to the
    per-session snapshot recorded on *pinned_node* (#191 semantics), falling
    back to latest for legacy nodes. ai_usage is re-checked on resolved rows.
    The todo list (its own model) is listed in the index too, pulled via
    read_todo (#158 Slice 3).
    """
    artifacts = pinned_node.get_user_artifacts() if pinned_node else {}
    if not artifacts:
        artifacts = UserArtifact.latest_per_kind(user_id)
    artifacts = {
        kind: a for kind, a in artifacts.items()
        if a.ai_usage in AI_ALLOWED
    }

    memory = artifacts.get("memory")
    scratchpad = artifacts.get("scratchpad")
    memory_content = memory.get_content() if memory else ""
    scratchpad_content = scratchpad.get_content() if scratchpad else ""

    index_lines = []
    # Todo first — it's a curated logistics surface, not a freeform artifact.
    todo_line = _todo_index_line(user_id, pinned_node=pinned_node)
    if todo_line is not None:
        index_lines.append(todo_line)
    present = set()
    for kind, artifact in sorted(artifacts.items()):
        if kind in ALWAYS_INLINE_KINDS:
            continue
        present.add(kind)
        tokens = approximate_token_count(artifact.get_content() or "")
        desc = (artifact.description or "").strip()
        desc_part = f": {desc}" if desc else ""
        index_lines.append(
            f"- {kind} — \"{artifact.title}\"{desc_part} (~{tokens} tokens)")
    # Built-in default kinds with no content yet still belong in the index so
    # the model knows they exist and writes to them (e.g. predictions)
    # rather than creating a duplicate custom artifact.
    for kind, title in UserArtifact.DEFAULT_KINDS.items():
        if kind in ALWAYS_INLINE_KINDS or kind in present:
            continue
        desc = (UserArtifact.DEFAULT_DESCRIPTIONS.get(kind) or "").strip()
        desc_part = f": {desc}" if desc else ""
        index_lines.append(f"- {kind} — \"{title}\"{desc_part} (empty)")
    index_text = "\n".join(index_lines) if index_lines else "(none)"
    return memory_content, scratchpad_content, index_text


def get_user_ai_preferences_content(user_id, pinned_node=None):
    """Resolve the AI preferences for an LLM prompt.

    Since #158 Slice 5, AI preferences are a ``UserArtifact`` kind
    ('ai_preferences'). Resolution prefers the node's pinned snapshot (#191),
    then the latest, with a legacy ``UserAIPreferences`` fallback for the
    expand-contract transition (removed in #219). ai_usage is re-checked on
    the resolved row so a mid-session opt-out is honored. Order:

      1. the node's pinned user_artifact (new nodes) — its snapshot;
      2. the node's legacy ai_preferences pin (pre-Slice-5 nodes) — its
         snapshot, so a continued old thread keeps its pinned version
         instead of drifting to latest (matches the export/inline display);
      3. the latest UserArtifact (a node with no ai_preferences pin);
      4. the latest legacy UserAIPreferences (pre-backfill / unpinned).
    """
    if pinned_node is not None:
        art = pinned_node.get_user_artifacts().get("ai_preferences")
        if art is not None and art.ai_usage in AI_ALLOWED:
            return art.get_content()
        prefs = pinned_node.get_artifact("ai_preferences")
        if prefs is not None and prefs.ai_usage in AI_ALLOWED:
            return prefs.get_content()

    art = UserArtifact.latest_for(user_id, "ai_preferences")
    if art is not None and art.ai_usage in AI_ALLOWED:
        return art.get_content()

    prefs = UserAIPreferences.query.filter_by(user_id=user_id).order_by(
        UserAIPreferences.created_at.desc()
    ).first()
    if prefs and prefs.ai_usage in AI_ALLOWED:
        return prefs.get_content()
    return None


def _get_previous_source_mode(node_chain):
    """Find the source_mode of the most recent LLM node in the chain.

    Returns None if no previous LLM node has a stored source_mode
    (i.e. this is the first agentic turn in the thread).
    """
    for node in reversed(node_chain):
        if not node.tool_calls_meta:
            continue
        try:
            meta = json.loads(node.tool_calls_meta)
        except (json.JSONDecodeError, TypeError):
            continue
        for entry in meta:
            if entry.get("name") == "_mode":
                return entry.get("source_mode")
    return None


def _is_agentic_prompt(node_chain):
    """Check if this conversation has an agentic prompt (voice/textmode)."""
    for node in node_chain:
        prompt = node.get_artifact("prompt") if hasattr(node, 'get_artifact') else None
        if prompt is not None and prompt.prompt_key in ('voice', 'textmode'):
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


def _retrieval_injection_text(tr):
    """Build the context-injection string for a successful retrieval tool
    result (read_artifact / read_todo), re-resolving the content fresh from
    the encrypted row — content is never stored in tool_calls_meta. Re-checks
    ai_usage so a mid-session opt-out is honored. Returns None if the
    artifact is gone or no longer AI-readable.
    """
    name = tr.get("name")
    if name == "read_artifact":
        artifact = UserArtifact.query.get(tr.get("artifact_id"))
        if artifact is None or artifact.ai_usage not in AI_ALLOWED:
            return None
        return (f"[Contents of artifact '{tr.get('kind', '?')}' you "
                f"requested:\n{artifact.get_content()}]")
    if name == "read_todo":
        todo = UserTodo.query.get(tr.get("todo_id"))
        if todo is None or todo.ai_usage not in AI_ALLOWED:
            return None
        return f"[Your current todo list:\n{todo.get_content()}]"
    return None


def _retrieval_pin(tr):
    """(artifact_type, artifact_id) to pin on the interim node for a
    successful retrieval result, so the export references the exact version
    read. Returns (None, None) for unrecognized/unpinnable results."""
    if tr.get("name") == "read_artifact":
        return ("user_artifact", tr.get("artifact_id"))
    if tr.get("name") == "read_todo":
        return ("todo", tr.get("todo_id"))
    return (None, None)


def _scan_proposal_statuses(node_chain):
    """Walk all nodes and collect proposal/tool status notes to inject.

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
            reported = entry.get("status_reported")

            if name in ("propose_todo", "propose_github_issue"):
                status = entry.get("apply_status")
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

            elif name == "update_artifact" and not reported:
                if entry.get("status") == "success":
                    kind = entry.get("kind", "?")
                    verb = ("created" if entry.get("created")
                            else "updated")
                    notes.append(f"[Artifact '{kind}' was {verb}.]")
                to_mark.append((node, name))

            elif name in RETRIEVAL_TOOLS and not reported:
                # Cross-turn delivery (voice / single-shot): the retrieval
                # ran last turn; inject the content now. Text mode resolves
                # these within the same turn (the loop marks them reported),
                # so this path only fires for the single-shot modes.
                if entry.get("status") == "success":
                    # Re-resolved fresh from the encrypted row — content is
                    # never stored in tool_calls_meta.
                    text = _retrieval_injection_text(entry)
                    if text is not None:
                        notes.append(text)
                else:
                    err = entry.get("error", "unknown error")
                    notes.append(f"[{name} failed — {err}]")
                to_mark.append((node, name))

            elif name == "submit_feedback" and not reported:
                if entry.get("status") == "success":
                    notes.append(
                        "[Feedback was submitted to the Loore team.]")
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
                        draft, proposal_node or llm_node, user_id,
                        confirm_node_id=llm_node.id,
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

            elif name == "update_artifact":
                kind = (inp.get("kind") or "").strip().lower()
                if kind in RESERVED_ARTIFACT_KINDS:
                    result["status"] = "error"
                    result["error"] = RESERVED_ARTIFACT_KINDS[kind]
                elif not _ARTIFACT_KIND_RE.match(kind):
                    result["status"] = "error"
                    result["error"] = (
                        f"Invalid artifact kind: {kind!r}. Use a short "
                        "lowercase slug (letters, digits, dashes)."
                    )
                else:
                    previous = UserArtifact.latest_for(user_id, kind)
                    title = (inp.get("title") or "").strip()
                    if not title:
                        title = (
                            previous.title if previous
                            else UserArtifact.DEFAULT_KINDS.get(
                                kind, kind.replace("-", " ").title())
                        )
                    # Description: explicit value wins; else carry forward the
                    # previous version's; else the built-in default for the
                    # kind. Mirrors the REST route so AI writes don't leave a
                    # null description (which blocked editing in the UI).
                    description = (inp.get("description") or "").strip()
                    if not description:
                        description = (
                            previous.description if previous
                            else UserArtifact.DEFAULT_DESCRIPTIONS.get(kind)
                        )
                    artifact = UserArtifact(
                        user_id=user_id,
                        kind=kind,
                        title=title[:128],
                        description=description,
                        generated_by=llm_node.llm_model or "agentic_session",
                        tokens_used=0,
                    )
                    artifact.set_content(inp["updated_content"])
                    db.session.add(artifact)
                    db.session.flush()
                    result["status"] = "success"
                    result["artifact_id"] = artifact.id
                    result["kind"] = kind
                    result["created"] = previous is None

            elif name == "read_artifact":
                kind = (inp.get("kind") or "").strip().lower()
                artifact = UserArtifact.latest_for(user_id, kind)
                if artifact is None or artifact.ai_usage not in AI_ALLOWED:
                    result["status"] = "error"
                    result["error"] = f"No readable artifact of kind {kind!r}"
                else:
                    # Content is NOT stored in tool meta (plaintext column)
                    # — the retrieval loop / _scan_proposal_statuses
                    # re-resolves it from the encrypted row and injects it.
                    result["status"] = "success"
                    result["artifact_id"] = artifact.id
                    result["kind"] = kind

            elif name == "read_todo":
                # Todo is its own model (not a UserArtifact), pulled on
                # demand since it's no longer shown inline (#158 Slice 3).
                todo = UserTodo.query.filter_by(user_id=user_id).order_by(
                    UserTodo.created_at.desc()
                ).first()
                if todo is None or todo.ai_usage not in AI_ALLOWED:
                    result["status"] = "error"
                    result["error"] = "The todo list is empty or unavailable."
                else:
                    # Content re-resolved from the encrypted row at injection
                    # time; only the row id sits in tool meta.
                    result["status"] = "success"
                    result["todo_id"] = todo.id

            elif name == "submit_feedback":
                category = inp.get("category") or "other"
                if category not in ("praise", "frustration", "idea", "other"):
                    category = "other"
                feedback = UserFeedback(
                    user_id=user_id,
                    category=category,
                    source="llm",
                )
                feedback.set_content(inp["content"])
                db.session.add(feedback)
                db.session.flush()
                result["status"] = "success"
                result["feedback_id"] = feedback.id

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


def build_user_export_content(user, max_tokens=None, filter_ai_usage=False,
                              created_before=None, chronological_order=False,
                              include_strategy="authored_threads"):
    """Import the actual implementation from export_data routes."""
    from backend.routes.export_data import build_user_export_content as _build
    return _build(user, max_tokens, filter_ai_usage, created_before,
                  chronological_order=chronological_order,
                  include_strategy=include_strategy)


def get_user_profile_content(user_id, pinned_node=None):
    """
    Resolve the user profile for an LLM prompt.

    Returns the UserProfile object if ai_usage is 'chat' or 'train',
    otherwise None. If *pinned_node* carries a recorded version
    (NodeContextArtifact, written by ``attach_context_artifacts`` at session
    start — see #191), that exact version is used: the same one the data
    export references, so the session sees one coherent point-in-time
    snapshot instead of re-fetching "latest now" each turn. Falls back to
    the latest when the node has no binding (e.g. legacy nodes). ai_usage is
    re-checked on the resolved row so a mid-session opt-out is honored.
    """
    profile = pinned_node.get_artifact("profile") if pinned_node else None
    if profile is None:
        profile = UserProfile.query.filter_by(user_id=user_id).order_by(
            UserProfile.created_at.desc()
        ).first()

    if profile and profile.ai_usage in AI_ALLOWED:
        return profile
    return None


def get_user_todo_content(user_id, pinned_node=None):
    """
    Resolve the user todo content for an LLM prompt.

    Returns the todo content if ai_usage permits AI access, otherwise None.
    Prefers the version recorded on *pinned_node* (see #191), falling back to
    the latest when the node has no binding. ai_usage is re-checked on the
    resolved row so a mid-session opt-out is honored.
    """
    todo = pinned_node.get_artifact("todo") if pinned_node else None
    if todo is None:
        todo = UserTodo.query.filter_by(user_id=user_id).order_by(
            UserTodo.created_at.desc()
        ).first()

    if todo and todo.ai_usage in AI_ALLOWED:
        return todo.get_content()
    return None


def get_user_recent_content(user_id, pinned_node=None):
    """Resolve the recent context summary for an LLM prompt.

    Prefers the version recorded on *pinned_node* (see #191) — the same one
    the data export references — so the session sees a coherent snapshot.
    Falls back (legacy nodes) to the latest summary for the *current*
    profile, so summaries from before a profile update are not returned.
    ai_usage is re-checked on the resolved row so a mid-session opt-out is
    honored.
    """
    rc = pinned_node.get_artifact("recent_context") if pinned_node else None
    if rc is None:
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
def generate_llm_response(self, parent_node_id: int, llm_node_id: int, model_id: str, user_id: int, source_mode: str = None):
    """
    Asynchronously generate an LLM response and update a placeholder node.

    Args:
        parent_node_id: ID of the parent node to respond to.
        llm_node_id: ID of the placeholder 'llm' node to update.
        model_id: Model identifier (e.g., "gpt-5", "claude-sonnet-4.5").
        user_id: ID of the user requesting the completion.
        source_mode: 'voice' or 'textmode' — which mode triggered this call.
    """
    logger.info(f"Starting LLM completion task for parent {parent_node_id}, updating node {llm_node_id}, model={model_id}")

    with flask_app.app_context():
        parent_node = Node.query.get(parent_node_id)
        llm_node = Node.query.get(llm_node_id)

        if not parent_node:
            raise ValueError(f"Parent node {parent_node_id} not found")
        if not llm_node:
            raise ValueError(f"LLM node {llm_node_id} not found")

        from backend.utils.spend import user_is_capped
        if user_is_capped(user_id):
            logger.warning(
                "User %s is spend-capped; skipping LLM completion", user_id)
            llm_node.llm_task_status = 'failed'
            db.session.commit()
            return

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

            # All placeholder-detection passes below skip soft-deleted
            # ancestors — those are scrubbed in the message-build loop
            # below, so any placeholders inside their (still-in-DB during
            # grace) content would be acting on content the user has
            # asked to delete.
            def _alive(n):
                return n.deleted_at is None and n.get_content()

            # Check if any node contains the {user_export} placeholder
            # Find the first node containing it to use its timestamp as cutoff
            export_node = None
            export_placeholder_match = None
            for node in node_chain:
                if not _alive(node):
                    continue
                m = USER_EXPORT_PATTERN.search(node.get_content())
                if m:
                    export_node = node
                    export_placeholder_match = m.group(0)
                    break
            needs_export = export_node is not None
            export_params = parse_placeholder_params(export_placeholder_match) if export_placeholder_match else {}
            user_export_content = None

            # Every context artifact is pinned to a per-session snapshot.
            # The node carrying a placeholder also carries a
            # NodeContextArtifact row recording the exact artifact version
            # that was current when it was created (written by
            # attach_context_artifacts for agentic system nodes, and by
            # sync_context_artifacts for ad-hoc placeholders in user
            # messages). We resolve each artifact from that recorded version
            # — the same source of truth the data export reads — so the
            # whole system prefix presents one coherent point-in-time view
            # for the life of the session: logically consistent within a
            # session and byte-identical across turns (a pre-condition for
            # prompt caching, #187). Before #191, profile / recent-context /
            # todo / AI-prefs were re-fetched "latest now" each turn and
            # drifted mid-thread; only the 10k-raw window was pinned (and it
            # still is, via recent_raw_node.created_at — it's a rolling
            # token window, not a single versioned row).
            def _placeholder_node(placeholder):
                for n in node_chain:
                    if _alive(n) and placeholder in n.get_content():
                        return n
                return None

            profile_node = _placeholder_node(USER_PROFILE_PLACEHOLDER)
            needs_profile = profile_node is not None
            user_profile_content = None

            todo_node = _placeholder_node(USER_TODO_PLACEHOLDER)
            needs_todo = todo_node is not None
            user_todo_content = None

            recent_node = _placeholder_node(USER_RECENT_PLACEHOLDER)
            needs_recent = recent_node is not None
            recent_raw_node = _placeholder_node(USER_RECENT_RAW_PLACEHOLDER)
            needs_recent_raw = recent_raw_node is not None
            user_recent_content = None
            user_recent_raw_content = None

            ai_prefs_node = _placeholder_node(USER_AI_PREFERENCES_PLACEHOLDER)
            needs_ai_prefs = ai_prefs_node is not None
            user_ai_preferences_content = None

            # User artifacts (#158) — memory/scratchpad content + index.
            # All three placeholders resolve from one pinned snapshot.
            artifacts_node = (
                _placeholder_node(USER_MEMORY_PLACEHOLDER)
                or _placeholder_node(USER_SCRATCHPAD_PLACEHOLDER)
                or _placeholder_node(USER_ARTIFACTS_INDEX_PLACEHOLDER)
            )
            needs_artifacts = artifacts_node is not None
            user_memory_content = None
            user_scratchpad_content = None
            user_artifacts_index = None

            # Check if any node contains {quote:ID} placeholders
            needs_quotes = any(
                has_quotes(node.get_content())
                for node in node_chain if _alive(node)
            )
            if needs_quotes:
                logger.info("Detected {quote:ID} placeholders in conversation chain")

            if needs_profile:
                profile_obj = get_user_profile_content(
                    user_id, pinned_node=profile_node
                )
                if profile_obj:
                    # Metadata already baked into stored content
                    user_profile_content = profile_obj.get_content()

            if needs_todo:
                user_todo_content = get_user_todo_content(
                    user_id, pinned_node=todo_node
                )

            if needs_recent:
                rc = get_user_recent_content(
                    user_id, pinned_node=recent_node
                )
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
                user_ai_preferences_content = get_user_ai_preferences_content(
                    user_id, pinned_node=ai_prefs_node
                )

            if needs_artifacts:
                (user_memory_content, user_scratchpad_content,
                 user_artifacts_index) = get_user_artifacts_context(
                    user_id, pinned_node=artifacts_node
                )

            # Detect if this is an agentic session (enables tools)
            is_agentic = _is_agentic_prompt(node_chain)
            agentic_tools = VOICE_TOOLS if is_agentic else None

            # Check for pending drafts and inject context notes
            pending_draft_note = None
            if is_agentic:
                pending = _find_pending_todo_draft(node_chain, user_id)
                if pending:
                    pending_draft_note = (
                        f"[todo-proposal:{pending.parent_id}: pending "
                        f"confirmation. The user can say 'apply the "
                        f"changes' to confirm.]"
                    )
                pending_issue = _find_pending_github_issue_draft(
                    node_chain, user_id
                )
                if pending_issue:
                    issue_note = (
                        f"[issue-proposal:{pending_issue.parent_id}: "
                        f"pending confirmation. The user can say "
                        f"'yes create it' or 'file that issue' "
                        f"to confirm.]"
                    )
                    if pending_draft_note:
                        pending_draft_note += "\n" + issue_note
                    else:
                        pending_draft_note = issue_note

            # Scan all proposals across the chain for status injection.
            # Refreshes from DB to pick up async merge updates.
            proposal_notes = []
            proposal_to_mark = []
            if is_agentic:
                proposal_notes, proposal_to_mark = (
                    _scan_proposal_statuses(node_chain)
                )

            if needs_export:
                # Defense-in-depth log only: validate_user_export_placeholders
                # runs upstream in create_llm_placeholder and aborts the
                # request before this task is dispatched. Reaching this
                # branch with unknown keys would mean someone bypassed
                # the factory — log so it's visible, but proceed
                # (engaged_threads + None budget is the safe default).
                warn_unknown_user_export_keys(
                    export_params,
                    user_id=user_id,
                    placeholder=export_placeholder_match,
                    log=logger,
                )
                # None = full archive; the retry loop converges if the
                # prompt overflows. A user-supplied max_export_tokens
                # becomes the initial budget instead.
                max_export_tokens = parse_max_export_tokens(
                    export_params.get('max_export_tokens'),
                    user_id=user_id,
                    placeholder=export_placeholder_match,
                    log=logger,
                )

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
                            chronological_order=chronological,
                            include_strategy="engaged_threads",
                        )
                        requested_max = export_params.get('max_export_tokens')
                        logger.info(
                            f"Built user export for {user_id}: "
                            f"{len(user_export_content or '')} chars, "
                            f"~{approximate_token_count(user_export_content or '')} tokens, "
                            f"cutoff={created_before}, "
                            f"strategy=engaged_threads, "
                            f"requested_max={requested_max} "
                            f"(attempt {attempt + 1})"
                        )

                # Track which heavy-context placeholders have been replaced
                # so subsequent occurrences get emptied (dedup).
                # user_todo and user_ai_preferences are NOT deduped — a
                # re-injected placeholder repeats the same pinned snapshot
                # (since #191 all artifacts resolve to the version recorded
                # on the node, not a re-fetched "latest").
                replaced_profile = False
                replaced_recent = False
                replaced_recent_raw = False

                # Temporal grounding (#130): every message is prefixed with an
                # absolute local-time stamp derived from the node's updated_at
                # (falling back to created_at), rendered in the conversation
                # owner's timezone. The model infers "now" from the most recent
                # message's stamp — no separate "Today is X" anchor, no
                # relative phrasing.
                _owner = User.query.get(user_id)
                user_tz = (_owner.timezone if _owner and _owner.timezone
                           else "UTC")

                messages = []
                for node in node_chain:
                    author = node.user.username if node.user else "Unknown"
                    is_llm_node = node.node_type == "llm" or (node.llm_model is not None)
                    time_prefix = local_stamp(node.updated_at or node.created_at, user_tz)

                    # Soft-deleted ancestor: scrub content so the AI doesn't
                    # ingest deleted user data. We still include the node so
                    # the conversation structure is preserved (better than
                    # an unexplained gap, which tends to make models try to
                    # "fill in" what's missing).
                    if node.deleted_at is not None:
                        message_text = (
                            f"{time_prefix} "
                            "[Earlier message in this thread was deleted "
                            "by the author]"
                        )
                        role = "assistant" if is_llm_node else "user"
                        messages.append({"role": role, "content": message_text})
                        continue

                    node_content = node.get_content()

                    if is_llm_node:
                        role = "assistant"
                        message_text = f"{time_prefix} {node_content}"
                        # Tag proposals with node ID for tracking
                        if is_agentic and node.tool_calls_meta:
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
                        message_text = (
                            f"{time_prefix} author {author}: {node_content}"
                        )
                        # Replace {user_export} placeholder if present
                        if user_export_content and export_placeholder_match:
                            message_text = message_text.replace(
                                export_placeholder_match,
                                user_export_content
                            )
                        # Replace {user_profile} — first occurrence
                        # gets content, subsequent get emptied (dedup)
                        if USER_PROFILE_PLACEHOLDER in message_text:
                            if not replaced_profile:
                                message_text = message_text.replace(
                                    USER_PROFILE_PLACEHOLDER,
                                    user_profile_content or ""
                                )
                                replaced_profile = True
                            else:
                                message_text = message_text.replace(
                                    USER_PROFILE_PLACEHOLDER,
                                    "(see profile above)"
                                )
                        # Replace {user_todo} — pinned snapshot (no dedup)
                        if USER_TODO_PLACEHOLDER in message_text:
                            message_text = message_text.replace(
                                USER_TODO_PLACEHOLDER,
                                user_todo_content or ""
                            )
                        # Replace {user_recent} — first occurrence only
                        if USER_RECENT_PLACEHOLDER in message_text:
                            if not replaced_recent:
                                message_text = message_text.replace(
                                    USER_RECENT_PLACEHOLDER,
                                    user_recent_content or ""
                                )
                                replaced_recent = True
                            else:
                                message_text = message_text.replace(
                                    USER_RECENT_PLACEHOLDER,
                                    "(see recent context above)"
                                )
                        # Replace {user_recent_raw} — first occurrence only
                        if USER_RECENT_RAW_PLACEHOLDER in message_text:
                            if not replaced_recent_raw:
                                message_text = message_text.replace(
                                    USER_RECENT_RAW_PLACEHOLDER,
                                    user_recent_raw_content or ""
                                )
                                replaced_recent_raw = True
                            else:
                                message_text = message_text.replace(
                                    USER_RECENT_RAW_PLACEHOLDER,
                                    "(see recent raw data above)"
                                )
                        # Replace {user_ai_preferences} — pinned snapshot
                        if USER_AI_PREFERENCES_PLACEHOLDER in message_text:
                            message_text = message_text.replace(
                                USER_AI_PREFERENCES_PLACEHOLDER,
                                user_ai_preferences_content or ""
                            )
                        # Replace artifact placeholders — pinned snapshot
                        if USER_MEMORY_PLACEHOLDER in message_text:
                            message_text = message_text.replace(
                                USER_MEMORY_PLACEHOLDER,
                                user_memory_content or ""
                            )
                        if USER_SCRATCHPAD_PLACEHOLDER in message_text:
                            message_text = message_text.replace(
                                USER_SCRATCHPAD_PLACEHOLDER,
                                user_scratchpad_content or ""
                            )
                        if USER_ARTIFACTS_INDEX_PLACEHOLDER in message_text:
                            message_text = message_text.replace(
                                USER_ARTIFACTS_INDEX_PLACEHOLDER,
                                user_artifacts_index or "(none)"
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

                # Inject agentic context notes as a final user message
                agentic_notes = (
                    proposal_notes
                    + ([pending_draft_note] if pending_draft_note else [])
                )
                # Inject mode indicator only when the mode changes
                # (or on the first turn to establish the initial mode).
                if is_agentic and source_mode:
                    prev_mode = _get_previous_source_mode(node_chain)
                    if prev_mode != source_mode:
                        mode_labels = {
                            'voice': (
                                "[Mode: Voice. The user is speaking "
                                "and hears your response via TTS. "
                                "Keep responses concise and natural "
                                "for listening.]"
                            ),
                            'textmode': (
                                "[Mode: Text. The user is typing and "
                                "reads your response. Use formatting "
                                "(markdown, lists, headers) freely.]"
                            ),
                        }
                        mode_note = mode_labels.get(source_mode)
                        if mode_note:
                            agentic_notes.append(mode_note)
                if is_agentic and agentic_notes:
                    # Synthetic system-side note injected after the latest real
                    # message; stamp it with "now" so the model's most-recent
                    # time anchor stays consistent with the rest of the thread.
                    now_prefix = local_stamp(
                        datetime.now(timezone.utc), user_tz
                    )
                    injected_text = f"{now_prefix} " + "\n".join(agentic_notes)
                    logger.debug(f"Agentic context injection for node {llm_node_id}: {injected_text}")
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
                        tools=agentic_tools,
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
            # ── Helpers shared by the single-shot and retrieval paths ──────

            def _log_api_cost(resp):
                """Log an APICostLog row for one model call. Every model call
                costs money — the retrieval loop logs once per round."""
                in_toks = resp.get("input_tokens", 0)
                out_toks = resp.get("output_tokens", 0)
                cost = calculate_llm_cost_microdollars(
                    model_id, in_toks, out_toks)
                db.session.add(APICostLog(
                    user_id=user_id,
                    model_id=model_id,
                    request_type="conversation",
                    input_tokens=in_toks,
                    output_tokens=out_toks,
                    cost_microdollars=cost,
                ))

            def _finalize(target_node, resp):
                """Write *resp* as the final answer on *target_node* using the
                EXACT existing single-shot finalize behavior (cost log, Race B
                guard, content, tool calls, proposal auto-detect, _mode meta,
                status_reported marking, completed status, commit).

                Returns the result dict, or a 'cancelled' dict if the node was
                soft-deleted mid-generation.
                """
                f_llm_text = resp["content"]
                f_total_tokens = resp["total_tokens"]
                f_tool_calls = resp.get("tool_calls", [])
                f_truncated = resp.get("truncated", False)

                logger.info(
                    f"LLM response generated: {len(f_llm_text)} chars, "
                    f"{f_total_tokens} tokens, {len(f_tool_calls)} tool calls")

                # Step 4: Log API cost
                self.update_state(state='PROGRESS', meta={
                    'progress': 90, 'status': 'Logging cost'})
                target_node.llm_task_progress = 90
                db.session.commit()
                _log_api_cost(resp)

                # Step 5: Update the placeholder LLM node with the response
                self.update_state(state='PROGRESS', meta={
                    'progress': 95, 'status': 'Finalizing'})

                # Race B guard: re-fetch the target node and bail if it was
                # soft-deleted while we were generating. Without this we'd
                # write generated content into a tombstone-bound node and waste
                # both the compute and the storage until the 30-day wipe.
                db.session.refresh(target_node)
                if target_node.deleted_at is not None:
                    logger.warning(
                        "LLM target node %s was soft-deleted mid-generation; "
                        "discarding response", target_node.id,
                    )
                    target_node.llm_task_status = 'cancelled'
                    target_node.llm_task_progress = 100
                    db.session.commit()
                    return {
                        'parent_node_id': parent_node_id,
                        'llm_node_id': target_node.id,
                        'status': 'cancelled',
                        'reason': 'target_soft_deleted',
                    }

                target_node.set_content(f_llm_text)
                # chars/4, NOT the provider's output_tokens: Node.token_count
                # is the platform's stable information-content measure (gates,
                # chunk windowing, balance decisions all sum it). Provider
                # tokenizer counts drift upward across model generations
                # (~1.5x chars/4 already), which skewed every window that
                # mixed LLM replies with chars/4-counted user text. Real
                # token usage lives in APICostLog.
                target_node.token_count = approximate_token_count(f_llm_text)

                # Step 5b: Execute tool calls if any
                f_tool_meta = None
                if f_tool_calls:
                    logger.info(
                        f"Executing {len(f_tool_calls)} tool calls")
                    tool_results = _execute_tool_calls(
                        f_tool_calls, target_node, node_chain, user_id
                    )
                    if f_truncated:
                        for tr in tool_results:
                            tr["response_truncated"] = True
                    f_tool_meta = tool_results

                # Step 5c: Auto-detect proposals in LLM text (agentic)
                if is_agentic:
                    auto_drafts = _auto_create_drafts(
                        f_llm_text, target_node, node_chain, user_id
                    )
                    if auto_drafts:
                        if f_tool_meta is None:
                            f_tool_meta = []
                        f_tool_meta.extend(auto_drafts)

                # Store source_mode for future mode-switch detection
                if is_agentic and source_mode:
                    if f_tool_meta is None:
                        f_tool_meta = []
                    f_tool_meta.append({
                        "name": "_mode",
                        "source_mode": source_mode,
                    })

                if f_tool_meta:
                    target_node.tool_calls_meta = json.dumps(f_tool_meta)

                # Mark proposal statuses as reported (deferred until
                # after LLM success so they re-inject on retry)
                if proposal_to_mark:
                    _mark_status_reported(proposal_to_mark)

                target_node.llm_task_status = 'completed'
                target_node.llm_task_progress = 100
                db.session.commit()

                logger.info(
                    f"LLM completion successful, updated node "
                    f"{target_node.id}")

                f_result = {
                    'parent_node_id': parent_node_id,
                    'llm_node_id': target_node.id,
                    'status': 'completed',
                    'total_tokens': f_total_tokens,
                }
                if f_tool_meta:
                    f_result['tool_calls_meta'] = f_tool_meta
                return f_result

            # ── Within-turn retrieval loop gating (#158) ───────────────────
            # Both agentic modes (text + voice) run the bounded within-turn
            # retrieval loop so read_artifact/read_todo resolve same-turn. Any
            # other (non-agentic) caller — source_mode None — stays single-shot.
            if source_mode not in ("textmode", "voice"):
                # Single-shot: one model call, one node, no within-turn loop.
                return _finalize(llm_node, response)

            # ── Bounded within-turn retrieval loop (#158) ──────────────────
            # When the model calls a retrieval tool, execute it, inject the
            # retrieved content back into `messages` as a plain user message,
            # finalize the current node as an interim step, create a
            # continuation node, and re-call the model so it answers WITH the
            # content in the same turn.
            rounds_done = 0
            current_node = llm_node
            while True:
                response_tool_calls = response.get("tool_calls", [])
                retrieval_calls = [
                    tc for tc in response_tool_calls
                    if tc["name"] in RETRIEVAL_TOOLS
                ]

                # Race B guard on the node we're about to write to.
                db.session.refresh(current_node)
                if current_node.deleted_at is not None:
                    logger.warning(
                        "LLM target node %s was soft-deleted mid-generation; "
                        "discarding response", current_node.id,
                    )
                    current_node.llm_task_status = 'cancelled'
                    current_node.llm_task_progress = 100
                    db.session.commit()
                    return {
                        'parent_node_id': parent_node_id,
                        'llm_node_id': current_node.id,
                        'status': 'cancelled',
                        'reason': 'target_soft_deleted',
                    }

                # INTERIM step: retrieval requested and budget remaining.
                if retrieval_calls and rounds_done < MAX_RETRIEVAL_ROUNDS:
                    interim_text = response.get("content") or ""
                    interim_truncated = response.get("truncated", False)

                    # Cost for THIS model call (every call costs).
                    _log_api_cost(response)

                    # Execute ALL tool calls on the interim node.
                    tool_results = _execute_tool_calls(
                        response_tool_calls, current_node, node_chain,
                        user_id,
                    )
                    if interim_truncated:
                        for tr in tool_results:
                            tr["response_truncated"] = True

                    # Build the injection text for each retrieval call,
                    # re-resolving content from the encrypted row, pinning the
                    # exact version read to the interim node (faithful export),
                    # and marking each handled here so the cross-turn scan
                    # won't double-inject. Errors and vanished artifacts are
                    # surfaced too, so the model isn't re-called blind.
                    injection_strings = []
                    for tr in tool_results:
                        if tr.get("name") not in RETRIEVAL_TOOLS:
                            continue
                        tr["status_reported"] = True
                        if tr.get("status") == "success":
                            text = _retrieval_injection_text(tr)
                            if text is not None:
                                injection_strings.append(text)
                                a_type, a_id = _retrieval_pin(tr)
                                if a_type and a_id:
                                    db.session.add(NodeContextArtifact(
                                        node_id=current_node.id,
                                        artifact_type=a_type,
                                        artifact_id=a_id,
                                    ))
                            else:
                                injection_strings.append(
                                    f"[{tr.get('name')}: the requested item "
                                    f"is no longer available.]")
                        else:
                            injection_strings.append(
                                f"[{tr.get('name')} failed — "
                                f"{tr.get('error', 'unknown error')}]")

                    # Finalize the interim node (NO _mode, NO proposal
                    # auto-detect — that belongs to the final answer only).
                    current_node.set_content(
                        interim_text or "(looking that up…)")
                    current_node.token_count = approximate_token_count(
                        interim_text or "(looking that up…)")
                    current_node.tool_calls_meta = json.dumps(tool_results)
                    current_node.llm_task_status = 'completed'
                    current_node.llm_task_progress = 100

                    # Create the continuation node that will hold the answer.
                    llm_user = User.query.filter_by(
                        username=model_id).first()
                    continuation = Node(
                        user_id=llm_user.id,
                        parent_id=current_node.id,
                        human_owner_id=user_id,
                        node_type="llm",
                        llm_model=model_id,
                        llm_task_status='processing',
                        privacy_level=current_node.privacy_level,
                        ai_usage=current_node.ai_usage,
                    )
                    continuation.set_content(
                        "[LLM response generation pending...]")
                    continuation.token_count = approximate_token_count(
                        "[LLM response generation pending...]")
                    db.session.add(continuation)
                    db.session.flush()
                    current_node.continuation_node_id = continuation.id
                    db.session.commit()

                    # Inject the assistant turn + retrieved content into the
                    # message stream and re-call so the model answers WITH it.
                    messages.append({
                        "role": "assistant",
                        "content": [{
                            "type": "text",
                            "text": interim_text or "(looking that up…)",
                        }],
                    })
                    messages.append({
                        "role": "user",
                        "content": [{
                            "type": "text",
                            "text": "\n\n".join(injection_strings),
                        }],
                    })

                    current_node = continuation
                    rounds_done += 1

                    self.update_state(state='PROGRESS', meta={
                        'progress': 40,
                        'status': 'Retrieving and continuing'})
                    # Continuation call: plain call (no PromptTooLong retry).
                    # The base messages already had any export reduction
                    # applied on the first call; the injection adds only an
                    # artifact body, which is small relative to the export.
                    response = LLMProvider.get_completion(
                        model_id, messages, api_keys,
                        tools=agentic_tools,
                    )
                    continue

                # FINAL answer: no retrieval (or budget exhausted).
                return _finalize(current_node, response)

        except Exception as e:
            error_message = str(e)
            logger.error(f"LLM completion error for node {llm_node_id}: {error_message}", exc_info=True)
            llm_node.llm_task_status = 'failed'
            llm_node.llm_task_error = error_message
            db.session.commit()
            raise

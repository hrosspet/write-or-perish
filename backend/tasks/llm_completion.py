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
    UserArtifact, Draft,
    NodeContextArtifact,
)
from backend.extensions import db
from backend.llm_providers import LLMProvider, PromptTooLongError
from backend.utils.tokens import (
    approximate_token_count, reduce_export_tokens, format_date_metadata,
)
from backend.utils.quotes import resolve_quotes, has_quotes, find_quote_ids
from backend.utils.node_split import NODE_CHAR_CAP
from backend.utils.timefmt import local_stamp, strip_edge_timestamps
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
USER_INTENTIONS_PLACEHOLDER = "{user_intentions}"
USER_ARTIFACTS_INDEX_PLACEHOLDER = "{user_artifacts_index}"

# Within-turn retrieval loop (#158, text mode only). When the model calls one
# of these tools, the retrieved content is injected back into the message
# stream and the model is re-called so it answers WITH the content in the same
# turn — instead of the cross-turn injection done by _scan_proposal_statuses.
# read_artifact pulls a UserArtifact by kind; read_todo pulls the user's todo
# list (its own model, no longer shown inline — #158 Slice 3); semantic_search
# pulls relevant snippets from the user's own archive by meaning (#155).
RETRIEVAL_TOOLS = {"read_artifact", "read_todo", "semantic_search",
                   "read_source"}
# Max number of interim retrieval round-trips before the model must answer.
MAX_RETRIEVAL_ROUNDS = 5
# Max new {quote:ID} pulls resolved per loop round (a 1h sharing can be huge;
# this bounds how much full-node content the model can inject in one step).
MAX_QUOTE_PULLS_PER_ROUND = 5
# Depth for resolving loop-pulled {quote:ID}. 1 = the pulled node's OWN content
# only; any {quote:ID} nested inside it stays as a placeholder the model can
# pull explicitly next round. Recursive resolution (the default depth 3) inlines
# cross-quoted nodes once PER PATH, which for a densely cross-referenced archive
# blows the context up combinatorially (saw a 2.77M-token prompt). The model
# already saw previews and chose specific nodes; give it exactly those.
QUOTE_PULL_DEPTH = 1
# Hard char caps so a single huge node or a pathological pull can never overflow
# the window on the (otherwise unguarded) continuation call. Tied to the
# per-node content cap the app already enforces on writes
# (NODE_CHAR_CAP = 100k): the combined pulled content is bounded to one node's
# worth (legacy/imported nodes can exceed the write cap, so this still bites),
# and the whole per-round injection — quote pulls + search previews + an
# artifact/todo read — to twice that. ~4 chars/token → ~25k / ~50k tokens.
MAX_QUOTE_PULL_CHARS = NODE_CHAR_CAP
MAX_RETRIEVAL_INJECTION_CHARS = NODE_CHAR_CAP * 2

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
            "something that future sessions should know), 'scratchpad' "
            "(your working notes for ongoing threads of work), and "
            "'intentions' (the user's long-running aspirations you help "
            "them clarify and track — see the Intentions section of your "
            "instructions). You can "
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
        "name": "semantic_search",
        "description": (
            "Search the user's own archive — all their past entries plus "
            "your past replies — by meaning, not keywords. Use it when the "
            "conversation touches something they've likely written about "
            "before but that isn't in your current context, so you can "
            "ground your response in what they've actually said rather than "
            "guessing. Pass a focused natural-language query describing what "
            "to look for. You get back short PREVIEWS of the top matches (with "
            "node ids), same turn — these are for triage, not the full text. "
            "To read a match in full, reference it as {quote:<id>} in your "
            "reply; the full entry comes back the next step (and the quote "
            "shows the user which entry you pulled). You can refine and search "
            "again if the previews miss, and you can search and quote in the "
            "same step. Tell the user you're checking their archive; don't "
            "search for things already in your context. Always produce a text "
            "response alongside the call."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "What to look for — the meaning you're after, "
                        "phrased like a sentence the user might have written "
                        "about it (this is semantic similarity, not keyword "
                        "match, so describe the idea, not search terms). "
                        "Search one topic at a time."
                    ),
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "apply_feedback",
        "description": (
            "Send the previously proposed feedback to the Loore team. Call "
            "this ONLY when the user explicitly confirms they want it sent "
            "(e.g. 'yes send it', 'go ahead', 'pass that on'). Do not call "
            "proactively. Do not call if the feedback was already sent "
            "(check the context notes for apply status). To PROPOSE feedback "
            "in the first place, write it under a `### Feedback` heading "
            "instead (see the submit-feedback guidance) — that shows the user "
            "what you'd send and gives them a Send button. "
            "Always produce a text response — the user needs to hear what "
            "happened as they are interacting via voice."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "read_source",
        "description": (
            "Alchemy sessions only. Retrieve passages from the user's "
            "selected source material relevant to a query — where the "
            "source speaks to what the user is currently working with. "
            "Returns the most relevant passages within the same turn. "
            "Tell the user you're consulting the source. Do not call "
            "this outside an alchemy session — it will error."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "What to look for in the source — a theme, "
                        "question, or the user's current state, in plain "
                        "language."
                    ),
                },
            },
            "required": ["query"],
        },
    },
]


def gated_voice_tools(config):
    """The agentic voice tool list, with ``semantic_search`` filtered out
    unless ``SEMANTIC_SEARCH_AGENTIC`` is enabled for this environment
    (#155 dark-ship).

    Shared by BOTH generation and the #187 pre-warm so their tool prefix is
    byte-identical: tools sit at the front of the Anthropic cache prefix, so
    any divergence here silently busts the whole cache — the warm writes a
    tool set generation never reads (observed as read=0 with the warm
    keeping semantic_search while generation dropped it)."""
    dropped = set()
    if not config.get("SEMANTIC_SEARCH_AGENTIC", False):
        dropped.add("semantic_search")
    if not config.get("ALCHEMY_V1", False):
        dropped.add("read_source")
    if not dropped:
        return VOICE_TOOLS
    return [t for t in VOICE_TOOLS if t.get("name") not in dropped]


_ARTIFACT_KIND_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,47}$')

# UserArtifact kinds that are injected INLINE in the agentic prompt (each has
# its own placeholder/tag) and therefore must be EXCLUDED from the artifacts
# index — otherwise they'd appear both inline and in the index. Single source
# of truth for both index-exclusion spots in get_user_artifacts_context.
# intentions (#150/#202) has an ambient {user_intentions} placeholder, so it
# belongs here too.
ALWAYS_INLINE_KINDS = UserArtifact.INLINE_KINDS

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
    intentions = artifacts.get("intentions")
    memory_content = memory.get_content() if memory else ""
    scratchpad_content = scratchpad.get_content() if scratchpad else ""
    intentions_content = intentions.get_content() if intentions else ""

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
    return memory_content, scratchpad_content, intentions_content, index_text


def get_user_ai_preferences_content(user_id, pinned_node=None):
    """Resolve the AI preferences for an LLM prompt.

    Since #158 Slice 5, AI preferences are a ``UserArtifact`` kind
    ('ai_preferences'): the node's pinned snapshot (#191) if present, else the
    latest. ai_usage is re-checked on the resolved row so a mid-session
    opt-out is honored. (The pre-fold UserAIPreferences fallback was removed
    once the backfill migrates rows + node pins; the table is dropped in
    #219.)
    """
    if pinned_node is not None:
        art = pinned_node.get_user_artifacts().get("ai_preferences")
        if art is not None and art.ai_usage in AI_ALLOWED:
            return art.get_content()
    art = UserArtifact.latest_for(user_id, "ai_preferences")
    if art is not None and art.ai_usage in AI_ALLOWED:
        return art.get_content()
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


def _find_pending_feedback_draft(node_chain, user_id):
    """Walk the node chain to find a pending feedback draft."""
    for node in reversed(node_chain):
        draft = Draft.query.filter_by(
            parent_id=node.id,
            user_id=user_id,
            label='feedback_pending',
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


def _detect_feedback_proposal(text):
    """Check if LLM text contains a feedback proposal heading.

    A single exact ``### Feedback`` heading marks the block the AI proposes to
    send to the team. Exact match (not substring) keeps it from firing on
    incidental prose like '### Feedback I've heard'."""
    if not text:
        return False
    headings = [h.strip().lower() for h in re.findall(
        r'^###\s+(.+)', text, re.MULTILINE)]
    return any(h == 'feedback' for h in headings)


def _retrieval_injection_text(tr):
    """Build the context-injection string for a successful retrieval tool
    result (read_artifact / read_todo / semantic_search), re-resolving the
    content fresh from the source row — content is never stored in
    tool_calls_meta. Re-checks ai_usage so a mid-session opt-out is honored.
    Returns None if nothing is (still) available.
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
    if name == "semantic_search":
        # Re-resolve each matched node from its id (content never stored in
        # meta); re-check ai_usage + soft-delete at injection time. These are
        # PREVIEWS for triage — to read one in full, the model references it as
        # {quote:<id>} (resolved by the loop's quote step).
        matches = tr.get("matches") or []
        lines = []
        for m in matches:
            node = Node.query.get(m.get("node_id"))
            if (node is None or node.deleted_at is not None
                    or node.ai_usage not in AI_ALLOWED):
                continue
            text = (node.get_content() or "").strip()
            if not text:
                continue
            snippet = text[:800] + ("…" if len(text) > 800 else "")
            stamp = (node.created_at.strftime("%Y-%m-%d")
                     if node.created_at else "?")
            score = m.get("score")
            pct = f" · {round(score * 100)}%" if score is not None else ""
            lines.append(f"- node {node.id} · {stamp}{pct}: {snippet}")
        if not lines:
            return None
        return (
            "[Semantic search results for "
            f"\"{tr.get('query', '')}\" — previews only; to read one in full, "
            "reference it as {quote:<id>}. Use only if actually relevant:\n"
            + "\n".join(lines) + "]")
    if name == "read_source":
        from backend.models import AlchemySourceChunk
        chunks = tr.get("chunks") or []
        parts = []
        for c in chunks:
            chunk = AlchemySourceChunk.query.get(c.get("chunk_id"))
            if chunk is None:
                continue
            text = (chunk.content or "").strip()
            if not text:
                continue
            heading = f" — {chunk.heading}" if chunk.heading else ""
            parts.append(f"--- passage {chunk.idx}{heading} ---\n{text}")
        if not parts:
            return None
        return (
            f"[Source passages for \"{tr.get('query', '')}\" "
            f"(from {tr.get('source_slug', 'the selected source')}):\n\n"
            + "\n\n".join(parts) + "]")
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

            if name in ("propose_todo", "propose_github_issue",
                        "propose_feedback"):
                status = entry.get("apply_status")
                tag = {
                    "propose_todo": "todo-proposal",
                    "propose_github_issue": "issue-proposal",
                    "propose_feedback": "feedback-proposal",
                }[name]
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

    if _detect_feedback_proposal(llm_text):
        already = Draft.query.filter_by(
            parent_id=llm_node.id, user_id=user_id,
            label='feedback_pending',
        ).first()
        if not already:
            existing = _find_pending_feedback_draft(node_chain, user_id)
            draft = Draft(
                user_id=user_id,
                parent_id=llm_node.id,
                label='feedback_pending',
            )
            draft.set_content("")
            db.session.add(draft)
            db.session.flush()
            if existing:
                db.session.delete(existing)
                db.session.flush()
            # Supersede old pending_approval feedback proposals
            _supersede_old_proposals(
                node_chain, "propose_feedback", llm_node.id)
            results.append({
                "name": "propose_feedback",
                "status": "success",
                "draft_id": draft.id,
                "apply_status": "pending_approval",
            })

    return results


# Tool-input keys that carry free-text user content. These are stripped
# before the input is persisted to the plaintext ``tool_calls_meta`` column:
# the content is already stored encrypted in its own row (UserArtifact /
# UserFeedback / Draft), and a plaintext copy on the node would defeat
# encryption-at-rest. Nothing reads ``input`` back out of tool_calls_meta
# (the cross-turn scan, the frontend, and exports all key on other fields),
# so redaction is lossless for every consumer.
_REDACTED_INPUT_KEYS = {"content", "updated_content"}


def _redact_tool_input(inp):
    """Return a copy of a tool-call input with free-text content removed,
    safe to persist to the plaintext tool_calls_meta column."""
    if not isinstance(inp, dict):
        return inp
    return {
        k: ("[redacted]" if k in _REDACTED_INPUT_KEYS else v)
        for k, v in inp.items()
    }


def _execute_tool_calls(tool_calls, llm_node, node_chain, user_id):
    """Execute tool calls and return metadata list."""
    tool_results = []

    for tc in tool_calls:
        name = tc["name"]
        inp = tc.get("input", {})
        # Persisted to plaintext tool_calls_meta — redact free-text content
        # (handlers below read the live ``inp``, which keeps the real values).
        result = {"name": name, "input": _redact_tool_input(inp)}

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

            elif name == "semantic_search":
                # Search the user's own archive by meaning (#155). Embeds the
                # model's query, ranks NodeEmbedding rows (excluding nodes
                # already in this thread), and stores only matched node ids +
                # scores — snippet content is re-resolved at injection time
                # (never in plaintext tool_calls_meta), like read_artifact.
                query = (inp.get("query") or "").strip()
                if not query:
                    result["status"] = "error"
                    result["error"] = "semantic_search needs a query."
                else:
                    from backend.utils.api_keys import get_openai_chat_key
                    from backend.utils.embeddings import (
                        retrieve_relevant_snippets,
                    )
                    rag_key = get_openai_chat_key(flask_app.config)
                    if not rag_key:
                        result["status"] = "error"
                        result["error"] = (
                            "Semantic search is unavailable right now.")
                    else:
                        chain_ids = [n.id for n in node_chain]
                        snippets = retrieve_relevant_snippets(
                            user_id, query[:4000], chain_ids, rag_key,
                            k=flask_app.config.get("RAG_TOP_K", 4),
                            min_score=flask_app.config.get(
                                "RAG_MIN_SCORE", 0.35),
                        )
                        result["status"] = "success"
                        result["query"] = query
                        result["matches"] = [
                            {"node_id": nid, "score": round(score, 4)}
                            for nid, _created, _snip, score in snippets
                        ]

            elif name == "read_source":
                # Alchemy: retrieve passages from the user's selected source
                # (docs/FOUR-FEATURE-ECOSYSTEM.md, "Alchemical Mode"). Only
                # chunk ids + scores are stored in meta; passage text is
                # re-resolved at injection time like the other retrievals.
                query = (inp.get("query") or "").strip()
                from backend.models import AlchemyState, AlchemySource
                alchemy_state = AlchemyState.query.filter_by(
                    user_id=user_id).first()
                if not query:
                    result["status"] = "error"
                    result["error"] = "read_source needs a query."
                elif (alchemy_state is None
                        or alchemy_state.opted_in_at is None
                        or not alchemy_state.source_slug):
                    result["status"] = "error"
                    result["error"] = (
                        "No alchemy source is active for this user — "
                        "read_source only works in alchemy sessions.")
                else:
                    source = AlchemySource.query.filter_by(
                        slug=alchemy_state.source_slug).first()
                    if source is None or not source.available:
                        result["status"] = "error"
                        result["error"] = "The selected source is empty."
                    else:
                        from backend.utils.api_keys import (
                            get_openai_chat_key,
                        )
                        from backend.utils.alchemy_sources import (
                            search_source_chunks,
                        )
                        emb_key = get_openai_chat_key(flask_app.config)
                        if not emb_key:
                            result["status"] = "error"
                            result["error"] = (
                                "Source retrieval is unavailable right "
                                "now.")
                        else:
                            matches = search_source_chunks(
                                source.id, query[:4000], emb_key,
                                user_id=user_id,
                                k=flask_app.config.get(
                                    "ALCHEMY_TOP_K", 3),
                            )
                            result["status"] = "success"
                            result["query"] = query
                            result["source_slug"] = source.slug
                            result["chunks"] = [
                                {"chunk_id": cid, "score": round(sc, 4)}
                                for cid, sc in matches
                            ]

            elif name == "apply_feedback":
                # Send the feedback the AI proposed under a ### Feedback
                # heading — mirrors apply_github_issue. Gated on a pending
                # draft, so it can't self-confirm in the same turn it
                # proposed (the draft is auto-created after tool execution).
                draft = _find_pending_feedback_draft(node_chain, user_id)
                if not draft:
                    result["status"] = "error"
                    result["error"] = "No pending feedback found"
                else:
                    origin_node = Node.query.get(draft.parent_id)
                    if not origin_node:
                        result["status"] = "error"
                        result["error"] = "Origin node not found"
                    else:
                        from backend.utils.feedback import (
                            submit_feedback_from_node,
                        )
                        feedback, err = submit_feedback_from_node(
                            origin_node, user_id
                        )
                        if err:
                            result["status"] = "error"
                            result["error"] = err
                        else:
                            db.session.delete(draft)
                            db.session.flush()
                            update_tool_meta(
                                origin_node,
                                "propose_feedback",
                                {
                                    "apply_status": "completed",
                                    "feedback_id": feedback.id,
                                },
                            )
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


def render_system_message(system_node, user_id):
    """Render the system node's full message text exactly as the
    generation loop would (#192/#187).

    Used by the finalize pre-warm so the provider-cache warm and the
    real generation share byte-identical prefixes: the result is stored
    in the #192 Redis cache, and generation prefers those cached bytes.
    Only valid for prompts without volatile placeholders ({user_export},
    {quote:..}) — callers must check first.
    """
    owner = User.query.get(user_id)
    user_tz = owner.timezone if owner and owner.timezone else "UTC"
    author = system_node.user.username if system_node.user else "Unknown"
    time_prefix = local_stamp(
        system_node.updated_at or system_node.created_at, user_tz)
    text = f"{time_prefix} author {author}: {system_node.get_content()}"

    if USER_PROFILE_PLACEHOLDER in text:
        profile_obj = get_user_profile_content(
            user_id, pinned_node=system_node)
        text = text.replace(
            USER_PROFILE_PLACEHOLDER,
            profile_obj.get_content() if profile_obj else "")
    if USER_TODO_PLACEHOLDER in text:
        text = text.replace(
            USER_TODO_PLACEHOLDER,
            get_user_todo_content(user_id, pinned_node=system_node) or "")
    if USER_RECENT_PLACEHOLDER in text:
        rc = get_user_recent_content(user_id, pinned_node=system_node)
        text = text.replace(
            USER_RECENT_PLACEHOLDER, rc.get_content() if rc else "")
    if USER_RECENT_RAW_PLACEHOLDER in text:
        raw_result = get_user_recent_raw_content(
            user_id, created_before=system_node.created_at)
        raw_text = ""
        if raw_result:
            raw_text = format_date_metadata(
                covers_start=raw_result.get("earliest"),
                covers_end=raw_result.get("latest"),
                tokens=raw_result.get("token_count"),
            ) + raw_result["content"]
        text = text.replace(USER_RECENT_RAW_PLACEHOLDER, raw_text)
    if USER_AI_PREFERENCES_PLACEHOLDER in text:
        text = text.replace(
            USER_AI_PREFERENCES_PLACEHOLDER,
            get_user_ai_preferences_content(
                user_id, pinned_node=system_node) or "")
    if (USER_MEMORY_PLACEHOLDER in text
            or USER_SCRATCHPAD_PLACEHOLDER in text
            or USER_INTENTIONS_PLACEHOLDER in text
            or USER_ARTIFACTS_INDEX_PLACEHOLDER in text):
        memory, scratchpad, intentions, index = get_user_artifacts_context(
            user_id, pinned_node=system_node)
        text = text.replace(USER_MEMORY_PLACEHOLDER, memory or "")
        text = text.replace(USER_SCRATCHPAD_PLACEHOLDER, scratchpad or "")
        text = text.replace(USER_INTENTIONS_PLACEHOLDER, intentions or "")
        text = text.replace(
            USER_ARTIFACTS_INDEX_PLACEHOLDER, index or "(none)")
    return text


@celery.task(name='backend.tasks.llm_completion.prewarm_anthropic_cache')
def prewarm_anthropic_cache(system_node_id, user_id, model_id,
                            transcript_so_far=None, recording_stamp_iso=None):
    """Pre-warm the Anthropic prompt cache during voice finalize (#187).

    Fired at the START of finalize, overlapping the trailing-batch
    transcription. Renders the system prefix (storing it in the #192 cache
    so generation reuses the exact bytes), then sends a max_tokens=1 request
    with cache breakpoints. Generation reads the warmed entries at 0.1x.

    Two modes:
    * **Fresh thread** (``transcript_so_far`` given) — warms the system
      block AND the transcript-so-far block; generation prefills only the
      final batch.
    * **Ongoing thread, >5-min recording** (``transcript_so_far`` omitted) —
      warms the **system block only**. The prior conversation sits between
      system and the transcript, so the two-block transcript warm doesn't
      transfer; the full-prefix warm is tracked in #224.

    Every failure path is silent — a missed warm just means today's
    uncached behavior.
    """
    with flask_app.app_context():
        try:
            system_node = Node.query.get(system_node_id)
            if system_node is None:
                return {"status": "skipped", "reason": "no_system_node"}
            sys_content = system_node.get_content() or ""
            if USER_EXPORT_PATTERN.search(sys_content)                     or has_quotes(sys_content):
                return {"status": "skipped", "reason": "volatile_prompt"}

            model_config = flask_app.config["SUPPORTED_MODELS"].get(model_id)
            if not model_config or model_config["provider"] != "anthropic":
                return {"status": "skipped", "reason": "not_anthropic"}

            from backend.utils.prompt_cache import (
                get_cached_render, store_render,
            )
            sys_text = get_cached_render(flask_app.config, system_node)
            if sys_text is None:
                sys_text = render_system_message(system_node, user_id)
                store_render(flask_app.config, system_node, sys_text)

            key_type = determine_api_key_type([system_node], logger=logger)
            api_keys = get_api_keys_for_usage(flask_app.config, key_type)
            if not api_keys.get("anthropic"):
                return {"status": "skipped", "reason": "no_key"}

            # Always warm the system block (the big stable prefix). On a fresh
            # thread also warm the transcript-so-far as a second block; on an
            # ongoing thread warm system-only (#224).
            warm_messages = [
                {"role": "user", "content": [
                    {"type": "text", "text": sys_text,
                     "cache_control": {"type": "ephemeral"}},
                ]},
            ]
            if transcript_so_far and recording_stamp_iso:
                owner = User.query.get(user_id)
                user_tz = (owner.timezone if owner and owner.timezone
                           else "UTC")
                author = owner.username if owner else "Unknown"
                stamp = local_stamp(
                    datetime.fromisoformat(recording_stamp_iso), user_tz)
                warm_messages.append({"role": "user", "content": [
                    {"type": "text",
                     "text": f"{stamp} author {author}: {transcript_so_far}",
                     "cache_control": {"type": "ephemeral"}},
                ]})

            response = LLMProvider._call_anthropic(
                model_config["api_model"],
                warm_messages,
                api_keys["anthropic"],
                max_tokens=1,
                # SAME gated tool list generation uses, or the cached tool
                # prefix won't match and generation can't read the warm (#187).
                tools=gated_voice_tools(flask_app.config),
            )
            cache_write = response.get("cache_creation_input_tokens", 0)
            cost = calculate_llm_cost_microdollars(
                model_id, response.get("input_tokens", 0),
                response.get("output_tokens", 0),
                cache_read_tokens=response.get(
                    "cache_read_input_tokens", 0),
                cache_write_tokens=cache_write,
            )
            db.session.add(APICostLog(
                user_id=user_id,
                model_id=model_id,
                request_type="cache_warm",
                input_tokens=response.get("input_tokens", 0) + cache_write,
                output_tokens=response.get("output_tokens", 0),
                cache_read_tokens=response.get("cache_read_input_tokens", 0),
                cache_write_tokens=cache_write,
                cost_microdollars=cost,
            ))
            db.session.commit()
            logger.info("Cache pre-warm wrote %d tokens (node %s)",
                        cache_write, system_node_id)
            return {"status": "ok", "cache_write_tokens": cache_write}
        except Exception:
            logger.warning("Cache pre-warm failed; generation proceeds "
                           "uncached", exc_info=True)
            return {"status": "failed"}


@celery.task(base=LLMCompletionTask, bind=True)
def generate_llm_response(self, parent_node_id: int, llm_node_id: int, model_id: str, user_id: int, source_mode: str = None, cache_split_offset: int = None):
    """
    Asynchronously generate an LLM response and update a placeholder node.

    Args:
        parent_node_id: ID of the parent node to respond to.
        llm_node_id: ID of the placeholder 'llm' node to update.
        model_id: Model identifier (e.g., "gpt-5", "claude-sonnet-4.5").
        user_id: ID of the user requesting the completion.
        source_mode: 'voice' or 'textmode' — which mode triggered this call.
        cache_split_offset: char offset splitting the latest voice
            transcript into [already-warmed prefix][final batch] blocks
            for provider prompt caching (#187). None = no split.
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
            # #192: the system node's rendered text is byte-identical
            # across turns (#191 pinning), so render once and reuse via
            # Redis. A hit also lets us skip the heavy artifact fetches
            # when their placeholders live only in the system prompt.
            from backend.utils.prompt_cache import (
                get_cached_render, store_render,
            )
            system_node = next(
                (n for n in node_chain
                 if n.deleted_at is None and n.has_artifact("prompt")),
                None)
            system_render_cacheable = False
            cached_system_render = None
            if system_node is not None:
                _sys_text = system_node.get_content() or ""
                # Exclude prompts carrying per-call volatile placeholders.
                system_render_cacheable = (
                    not USER_EXPORT_PATTERN.search(_sys_text)
                    and not has_quotes(_sys_text)
                )
                if system_render_cacheable:
                    cached_system_render = get_cached_render(
                        flask_app.config, system_node)

            def _placeholder_node(placeholder):
                for n in node_chain:
                    if cached_system_render is not None and n is system_node:
                        continue  # already rendered — no fetch needed
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
                or _placeholder_node(USER_INTENTIONS_PLACEHOLDER)
                or _placeholder_node(USER_ARTIFACTS_INDEX_PLACEHOLDER)
            )
            needs_artifacts = artifacts_node is not None
            user_memory_content = None
            user_scratchpad_content = None
            user_intentions_content = None
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
                 user_intentions_content,
                 user_artifacts_index) = get_user_artifacts_context(
                    user_id, pinned_node=artifacts_node
                )

            # Detect if this is an agentic session (enables tools)
            is_agentic = _is_agentic_prompt(node_chain)
            # semantic_search ships DARK (#155): unless enabled for this
            # environment, gated_voice_tools drops it from the exposed tool
            # list so the model never sees it. (Manual Cmd+K search is
            # unaffected — separate HTTP endpoint.) The pre-warm uses the SAME
            # helper so the cached tool prefix matches (#187). agentic_search_on
            # also gates the within-turn {quote:ID} pull-in-full below.
            agentic_search_on = flask_app.config.get(
                "SEMANTIC_SEARCH_AGENTIC", False)
            agentic_tools = (
                gated_voice_tools(flask_app.config) if is_agentic else None)

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
                replaced_export = False  # #139: first occurrence only

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
                system_msg_index = None
                last_assistant_index = None
                latest_user_msg_index = None
                for node in node_chain:
                    author = node.user.username if node.user else "Unknown"
                    is_llm_node = node.node_type == "llm" or (node.llm_model is not None)
                    time_prefix = local_stamp(node.updated_at or node.created_at, user_tz)

                    # #192: reuse the cached system-prompt render verbatim.
                    if (node is system_node
                            and cached_system_render is not None):
                        system_msg_index = len(messages)
                        messages.append({
                            "role": "user",
                            "content": [{"type": "text",
                                         "text": cached_system_render}],
                        })
                        continue

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
                                    elif ename == "propose_feedback":
                                        message_text += (
                                            f"\n\n[feedback-proposal:"
                                            f"{node.id}]"
                                        )
                            except (json.JSONDecodeError, TypeError):
                                pass
                    else:
                        role = "user"
                        message_text = (
                            f"{time_prefix} author {author}: {node_content}"
                        )
                        # Replace {user_export} — first occurrence gets
                        # the archive, repeats get a stub (#139): the
                        # export is the heaviest placeholder and
                        # duplicating it doubles prompt cost.
                        if user_export_content and export_placeholder_match:
                            if export_placeholder_match in message_text:
                                if not replaced_export:
                                    message_text = message_text.replace(
                                        export_placeholder_match,
                                        user_export_content, 1
                                    )
                                    replaced_export = True
                                message_text = message_text.replace(
                                    export_placeholder_match,
                                    "(see archive above)"
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
                        if USER_INTENTIONS_PLACEHOLDER in message_text:
                            message_text = message_text.replace(
                                USER_INTENTIONS_PLACEHOLDER,
                                user_intentions_content or ""
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

                    if (node.id == parent_node_id and not is_llm_node
                            and cache_split_offset
                            and node.deleted_at is None):
                        # #187: two-block transcript split. Block A is the
                        # prefix the finalize pre-warm already cached
                        # (byte-identical incl. the recording-start stamp);
                        # block B is the final transcribed batch.
                        head_len = len(message_text) - len(node_content) \
                            + cache_split_offset
                        if 0 < head_len < len(message_text):
                            latest_user_msg_index = len(messages)
                            messages.append({
                                "role": role,
                                "content": [
                                    {"type": "text",
                                     "text": message_text[:head_len]},
                                    {"type": "text",
                                     "text": message_text[head_len:]},
                                ],
                            })
                            continue

                    if node.id == parent_node_id and not is_llm_node:
                        latest_user_msg_index = len(messages)
                    if node is system_node:
                        system_msg_index = len(messages)
                        if (system_render_cacheable
                                and cached_system_render is None
                                and attempt == 0):
                            store_render(
                                flask_app.config, system_node, message_text)
                    if role == "assistant":
                        last_assistant_index = len(messages)
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
                # Semantic archive retrieval is now a within-turn loop tool
                # (`semantic_search`, see RETRIEVAL_TOOLS) the model calls when
                # it judges the archive is relevant — replacing the old
                # always-on proactive notes-channel injection (#155 → #196
                # loop). Nothing to inject here.

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

                # #187: provider-side prompt caching (Anthropic). Mark
                # cache breakpoints: one on the rendered system prompt
                # (the big stable prefix) and one on the last assistant
                # reply (caches the whole prior conversation). Everything
                # after the last breakpoint — the new user message and the
                # volatile notes — prefills fresh each turn. Byte-identity
                # of the prefix across turns is guaranteed by #191/#192.
                if provider == "anthropic" and is_agentic:
                    for idx in (system_msg_index, last_assistant_index,
                                latest_user_msg_index):
                        if idx is None:
                            continue
                        blocks = messages[idx].get("content")
                        if isinstance(blocks, list) and blocks:
                            blocks[-1]["cache_control"] = {
                                "type": "ephemeral"}

                # Step 3: Call LLM API
                self.update_state(state='PROGRESS', meta={'progress': 40, 'status': 'Generating response'})
                llm_node.llm_task_progress = 40
                db.session.commit()

                # Log total context being sent
                total_content = "".join(m["content"][0]["text"] for m in messages if m.get("content"))
                estimated_tokens = approximate_token_count(total_content)
                logger.info(f"Calling LLM API: model_id={model_id}, api_model={api_model}, provider={provider}, key_type={key_type}, estimated_tokens={estimated_tokens}, total_chars={len(total_content)}")

                try:
                    # #189: stable per-thread key improves OpenAI's
                    # automatic prefix-cache routing.
                    thread_root_id = (node_chain[0].id if node_chain
                                      else parent_node_id)
                    response = LLMProvider.get_completion(
                        model_id, messages, api_keys,
                        tools=agentic_tools,
                        prompt_cache_key=f"loore-t{thread_root_id}",
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
                costs money — the retrieval loop logs once per round.

                Cache-aware (#187): cache reads price at 0.1x and writes at
                1.25x; the logged input_tokens is the full prompt size
                (uncached + cached reads + cache writes) for visibility, while
                pricing is already accounted in the cost above.

                OpenAI (#189): cached_tokens is the cached SUBSET of
                input_tokens (auto prefix-cache) — billed at the model's
                cached_input_multiplier, fixing the prior full-price
                over-count."""
                in_toks = resp.get("input_tokens", 0)
                out_toks = resp.get("output_tokens", 0)
                cache_read_toks = resp.get("cache_read_input_tokens", 0)
                cache_write_toks = resp.get(
                    "cache_creation_input_tokens", 0)
                cached_input_toks = resp.get("cached_tokens", 0)
                cost = calculate_llm_cost_microdollars(
                    model_id, in_toks, out_toks,
                    cache_read_tokens=cache_read_toks,
                    cache_write_tokens=cache_write_toks,
                    cached_input_tokens=cached_input_toks,
                )
                if cache_read_toks or cache_write_toks:
                    logger.info(
                        "Prompt cache usage: read=%d write=%d uncached=%d",
                        cache_read_toks, cache_write_toks, in_toks)
                if cached_input_toks:
                    logger.info(
                        "OpenAI prompt cache: %d/%d input tokens cached",
                        cached_input_toks, in_toks)
                db.session.add(APICostLog(
                    user_id=user_id,
                    model_id=model_id,
                    request_type="conversation",
                    # input_tokens = full prompt size. For Anthropic, in_toks
                    # is the uncached portion so read+write complete it; for
                    # OpenAI in_toks is already the full prompt (read/write 0).
                    input_tokens=(in_toks + cache_read_toks
                                  + cache_write_toks),
                    output_tokens=out_toks,
                    # cache_read_tokens = input SERVED from cache, unified
                    # across providers: Anthropic cache reads + OpenAI
                    # cached_tokens (one of the two is always 0). Drives the
                    # admin hit-rate (served / full prompt input).
                    cache_read_tokens=(cache_read_toks + cached_input_toks),
                    cache_write_tokens=cache_write_toks,
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
                # #179: strip hallucinated context-timestamp echoes from the
                # response edges before anything stores or speaks the text.
                f_scrubbed = strip_edge_timestamps(f_llm_text)
                if f_scrubbed != f_llm_text:
                    logger.info(
                        "Stripped edge timestamp(s) from LLM response "
                        "(model=%s, node=%s)", model_id, target_node.id)
                    f_llm_text = f_scrubbed
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
            # Quote pulls already grounded this turn, and nodes already in the
            # thread (their content is in `messages`) — neither should trigger
            # a re-loop when the model references them.
            resolved_quote_ids = set()
            chain_ids_set = {n.id for n in node_chain}
            while True:
                response_tool_calls = response.get("tool_calls", [])
                retrieval_calls = [
                    tc for tc in response_tool_calls
                    if tc["name"] in RETRIEVAL_TOOLS
                ]
                # New {quote:ID} the model emitted to pull a node in full this
                # turn (e.g. a semantic_search hit). A search call and quote
                # pulls can coexist in one round — both get fulfilled and the
                # model sees everything next round. Capped per round.
                resp_text = response.get("content") or ""
                # The within-turn {quote:ID} pull-in-full is the second half of
                # agentic semantic search, so it's gated with it (#155 dark).
                # Without it, a model-emitted {quote:ID} for an out-of-chain
                # node is left as-is (resolved at chain build like on main),
                # not pulled mid-turn — keeping prod behavior unchanged.
                new_quote_ids = [
                    q for q in find_quote_ids(resp_text)
                    if q not in resolved_quote_ids and q not in chain_ids_set
                ][:MAX_QUOTE_PULLS_PER_ROUND] if agentic_search_on else []

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

                # INTERIM step: a retrieval tool call and/or a full-node quote
                # pull was requested, and budget remains.
                if (retrieval_calls or new_quote_ids) and \
                        rounds_done < MAX_RETRIEVAL_ROUNDS:
                    # #179: scrub timestamp echoes from interim text too —
                    # it's stored on the interim node and spoken in voice.
                    interim_text = strip_edge_timestamps(resp_text)
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

                    # Resolve newly-referenced {quote:ID} to full content via
                    # the existing quote machinery (permission + ai_usage='none'
                    # checks built in). The {quote:ID} stays in the interim
                    # node's text → renders for the user + flows to exports.
                    if new_quote_ids:
                        placeholder = "\n".join(
                            "{quote:%d}" % q for q in new_quote_ids)
                        # Depth 1: the pulled nodes' own content only — nested
                        # {quote:ID} stay as placeholders the model can pull
                        # next round. Prevents the combinatorial expansion that
                        # produced a 2.77M-token prompt.
                        q_text, _ = resolve_quotes(
                            placeholder, user_id, for_llm=True,
                            max_depth=QUOTE_PULL_DEPTH)
                        if len(q_text) > MAX_QUOTE_PULL_CHARS:
                            q_text = (q_text[:MAX_QUOTE_PULL_CHARS]
                                      + "\n[…pulled content truncated to fit "
                                      "the context window…]")
                        if q_text and q_text.strip():
                            label = ("entries" if len(new_quote_ids) > 1
                                     else "entry")
                            injection_strings.append(
                                f"[Full content of the {label} you pulled "
                                f"up:\n{q_text}]")
                        resolved_quote_ids.update(new_quote_ids)
                    if not injection_strings:
                        injection_strings.append(
                            "[The requested items were unavailable.]")

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
                    injection_text = "\n\n".join(injection_strings)
                    # Final defense: even with per-pull caps, several pulls plus
                    # the previews could grow large. Cap the whole round's
                    # injection so the continuation call can't overflow.
                    if len(injection_text) > MAX_RETRIEVAL_INJECTION_CHARS:
                        injection_text = (
                            injection_text[:MAX_RETRIEVAL_INJECTION_CHARS]
                            + "\n[…retrieved content truncated to fit the "
                            "context window…]")
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
                            "text": injection_text,
                        }],
                    })

                    current_node = continuation
                    rounds_done += 1

                    self.update_state(state='PROGRESS', meta={
                        'progress': 40,
                        'status': 'Retrieving and continuing'})
                    # Continuation call. The base messages already had export
                    # reduction applied on the first call, and the injection is
                    # char-capped above — but accumulated rounds plus a near-full
                    # base can still tip over the window. Rather than fail the
                    # whole turn (the old behavior — a raw multi-million-token
                    # PromptTooLong surfaced to the user), drop this round's
                    # injection and let the model answer with what it has.
                    try:
                        response = LLMProvider.get_completion(
                            model_id, messages, api_keys,
                            tools=agentic_tools,
                            # #189: same per-thread key as the first call so
                            # loop continuations route to the same OpenAI cache.
                            prompt_cache_key=f"loore-t{thread_root_id}",
                        )
                    except PromptTooLongError:
                        logger.warning(
                            "Continuation prompt too long after retrieval "
                            "round %d; dropping the injection and answering "
                            "without it", rounds_done,
                        )
                        messages[-1] = {
                            "role": "user",
                            "content": [{
                                "type": "text",
                                "text": (
                                    "[The retrieved content was too large to "
                                    "fit in context. Answer with what you "
                                    "already have, and let the user know you "
                                    "could only partially load the entries "
                                    "you referenced.]"),
                            }],
                        }
                        response = LLMProvider.get_completion(
                            model_id, messages, api_keys,
                            tools=agentic_tools,
                            # #189: same per-thread key as the first call so
                            # loop continuations route to the same OpenAI cache.
                            prompt_cache_key=f"loore-t{thread_root_id}",
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

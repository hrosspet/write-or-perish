"""
Celery task for asynchronous LLM completion.
"""
import difflib
import json
import re
import time
from celery import Task
from celery.utils.log import get_task_logger
from datetime import datetime, timezone

from backend.celery_app import celery, flask_app
from backend.models import (
    Node, User, UserProfile, UserRecentContext, UserTodo, APICostLog,
    UserArtifact, Draft,
    NodeContextArtifact, ExternalItem,
)
from backend.extensions import db
from backend.llm_providers import LLMProvider, PromptTooLongError
from backend.utils.tokens import (
    approximate_token_count, reduce_export_tokens, format_date_metadata,
)
from backend.utils.quotes import (
    resolve_quotes, has_quotes,
    resolve_ext_quotes, has_ext_quotes, find_ext_quote_ids,
)
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
# Flag-conditional guidance for Upload v1 (SHARE_V1) — placeholder, text,
# and gate live in backend.utils.share_guidance so the node display can
# mirror the substitution without importing this Celery module. Substituted
# identically in render_system_message AND the generation loop (both paths
# must stay byte-identical for the prompt cache; the gate is constant per
# user per environment, so each render is stable).
from backend.utils.share_guidance import (  # noqa: E402
    SHARE_GUIDANCE_PLACEHOLDER, SHARE_GUIDANCE_TEXT, share_enabled_for_user,
)
from backend.utils.external_guidance import (
    EXTERNAL_GUIDANCE_PLACEHOLDER, EXTERNAL_GUIDANCE_TEXT,
    external_content_enabled_for_user,
)


def _share_enabled_for_user(user_id):
    return share_enabled_for_user(flask_app.config, user_id)


def _external_enabled_for_user(user_id):
    return external_content_enabled_for_user(flask_app.config, user_id)
USER_ARTIFACTS_INDEX_PLACEHOLDER = "{user_artifacts_index}"

# Within-turn retrieval loop (#158, text mode only). When the model calls one
# of these tools, the retrieved content is injected back into the message
# stream and the model is re-called so it answers WITH the content in the same
# turn — instead of the cross-turn injection done by _scan_proposal_statuses.
# read_artifact pulls a UserArtifact by kind; read_todo pulls the user's todo
# list (its own model, no longer shown inline — #158 Slice 3); semantic_search
# pulls relevant snippets from the user's own archive by meaning (#155).
RETRIEVAL_TOOLS = {"read_artifact", "read_todo", "semantic_search",
                   "read_full"}
# Max number of interim tool round-trips before the model must answer.
# The within-turn loop continues after EVERY tool call (retrieval AND action
# tools like update_artifact) — models follow the standard tool-use contract
# and expect a result round; ending the turn on an action call made them
# leave one-line preambles ("noting this in memory") as the whole answer.
MAX_RETRIEVAL_ROUNDS = 5

# Retry schedule for the loop's CONTINUATION calls: sleep lengths between
# attempts (len == number of retries). A transient provider error (overload,
# timeout) used to kill the whole turn, stranding the continuation node at
# 'processing' forever. Module-level so tests can zero the delays.
CONTINUATION_RETRY_DELAYS = (5, 15)


def _dispatch_voice_tts(node_id, user_id):
    """Enqueue TTS generation for a finalized voice-turn node.

    Voice turns dispatch TTS per node at that node's OWN finalization —
    the interim step's audio must be playable while the continuation call
    is still generating (it can run for minutes after a long sharing).
    A chained-after-the-task dispatch (the old design) blocked the interim
    audio behind the whole turn.

    Dispatch failures are logged, never raised: a missing TTS is
    recoverable (the SSE stream reports no chunks and the frontend's 60s
    safety net kicks in), a failed LLM turn is not.
    """
    try:
        import os
        import pathlib
        from backend.tasks.tts import generate_tts_audio
        audio_root = str(pathlib.Path(
            os.environ.get("AUDIO_STORAGE_PATH", "data/audio")).resolve())
        generate_tts_audio.delay(
            node_id, audio_root, requesting_user_id=user_id)
    except Exception:
        logger.exception(
            "Failed to dispatch voice TTS for node %s", node_id)


# ── Relative quote labels ────────────────────────────────────────────────
# semantic_search previews tag each match with a short label ([A], [B], …)
# and the model quotes by label ({quote:A}). The server canonicalizes
# labels to absolute {quote:<node_id>} / {quote_ext:<item_id>} markers
# BEFORE the text is stored or scanned for pulls — the stored lore is
# self-contained, and the model never copies a multi-digit id (which it
# reliably typos; labels it doesn't).
_LABEL_QUOTE_RE = re.compile(r'\{quote:([A-Za-z]{1,2})\}')


def _quote_label(n):
    """0 → A, 1 → B, … 25 → Z, 26 → AA, 27 → AB, …"""
    label = ""
    n += 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        label = chr(ord("A") + rem) + label
    return label


def _canonicalize_quote_labels(text, quote_labels):
    """Rewrite {quote:<label>} markers to absolute references using this
    turn's label map ({label: ("node"|"external", id)}). Unknown labels are
    left as-is (they render as literal text — visible, not wrong)."""
    if not text or not quote_labels:
        return text

    def repl(match):
        target = quote_labels.get(match.group(1).upper())
        if target is None:
            return match.group(0)
        kind, ref_id = target
        if kind == "external":
            return "{quote_ext:%d}" % ref_id
        return "{quote:%d}" % ref_id

    return _LABEL_QUOTE_RE.sub(repl, text)


def _bump_surfaced_references(text, user_id, already_bumped):
    """Eagerly record surfacing history for every {quote_ext:ID} in *text*
    (content is encrypted at rest, so this can't be derived later). Bumps
    each item at most once per turn via *already_bumped*. Committed by the
    caller's surrounding commit."""
    ids = [i for i in find_ext_quote_ids(text) if i not in already_bumped]
    if not ids:
        return
    items = ExternalItem.query.filter(
        ExternalItem.id.in_(ids),
        ExternalItem.user_id == user_id,
    ).all()
    now = datetime.utcnow()
    for item in items:
        item.surfaced_count = (item.surfaced_count or 0) + 1
        item.last_surfaced_at = now
        already_bumped.add(item.id)
# Depth for resolving read_full content. 1 = the read item's OWN content
# only; any {quote:ID} nested inside it stays as a placeholder the model can
# read explicitly next round. Recursive resolution (the default depth 3)
# inlines cross-quoted nodes once PER PATH, which for a densely
# cross-referenced archive blows the context up combinatorially (saw a
# 2.77M-token prompt). The model saw previews and chose ONE item; give it
# exactly that.
QUOTE_PULL_DEPTH = 1
# Hard char caps so a single huge node or a pathological read can never
# overflow the window on the (otherwise unguarded) continuation call. Tied
# to the per-node content cap the app already enforces on writes
# (NODE_CHAR_CAP = 100k): a read_full result is bounded to one node's worth
# (legacy/imported nodes can exceed the write cap, so this still bites),
# and the whole per-round injection — reads + search previews + an
# artifact/todo read — to twice that. ~4 chars/token → ~25k / ~50k tokens.
MAX_QUOTE_PULL_CHARS = NODE_CHAR_CAP
MAX_RETRIEVAL_INJECTION_CHARS = NODE_CHAR_CAP * 2
# Threshold for the within-turn echo of an update_artifact write: changes
# come back as a unified diff, but past this size the rewrite was
# substantial and the full new text is clearer (and usually shorter than
# its diff), so the echo switches to the full content. Creations always
# echo in full. The echo itself is uncapped — the round-level
# MAX_RETRIEVAL_INJECTION_CHARS above is the final defense.
ARTIFACT_ECHO_DIFF_THRESHOLD_CHARS = 4000

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
            "'reading-list'). Write with `edits` (targeted exact-match "
            "replacements against the current version — cheaper and "
            "faster) for small changes, or `updated_content` (the FULL "
            "new text) when creating or rewriting heavily. Either way a "
            "new version is stored and old versions stay in history. "
            "Call proactively for memory-worthy facts; no confirmation "
            "is needed. Always produce a text response alongside the "
            "call."
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
                        "Required when creating; for updates prefer "
                        "`edits` unless most of the text changes. Carry "
                        "forward everything still relevant from the "
                        "current version."
                    ),
                },
                "edits": {
                    "type": "array",
                    "description": (
                        "Targeted replacements applied in order to the "
                        "current version — use for small changes instead "
                        "of resending the full text. Each old_text must "
                        "match the current content exactly (whitespace "
                        "included) and occur exactly once; the whole call "
                        "fails cleanly if any edit doesn't apply."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "old_text": {
                                "type": "string",
                                "description": (
                                    "Exact text to replace (must occur "
                                    "exactly once in the current version)."
                                ),
                            },
                            "new_text": {
                                "type": "string",
                                "description": "The replacement text.",
                            },
                        },
                        "required": ["old_text", "new_text"],
                    },
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
            "required": ["kind"],
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
            "your past replies — AND their saved external references "
            "(imported tweets and bookmarks) by meaning, not keywords. Use "
            "it when the conversation touches something they've likely "
            "written about before, or when they ask about something they "
            "saved ('I bookmarked something about this'), or when a saved "
            "reference would genuinely serve the current thread. Pass a "
            "focused natural-language query describing what to look for. "
            "You get back short PREVIEWS of the top matches, each tagged "
            "with a label like [A], same turn — previews are for TRIAGE. "
            "Before quoting or building your answer on a match whose "
            "preview is marked truncated, read it first with the read_full "
            "tool; then give your final reply. To QUOTE a match in that "
            "reply, reference it by its label, e.g. {quote:A} — the user "
            "sees the full quoted entry or reference card (verbatim, with "
            "attribution); the marker is presentation only and ends your "
            "lookup. When you quote, say "
            "in your own words why it's relevant to what the user is "
            "saying right now — the quote plus your reasoning is the "
            "response, not a link dump. Reference previews show how often "
            "each was already surfaced; weigh that yourself — re-quoting "
            "something recently shown needs a good reason. You can refine "
            "and search again if the previews miss, and quoting nothing is "
            "always fine. Labels are your private triage handles: use them "
            "only inside {quote:...} markers — in prose, refer to items by "
            "author or content ('the @visa thread'), never by label. "
            "Tell the user you're checking their archive; "
            "don't search for things already in your context. Always "
            "produce a text response alongside the call."
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
        "name": "read_full",
        "description": (
            "Read one semantic_search match in full — pass its label from "
            "this turn's search results (e.g. 'B'), or a numeric archive "
            "entry id from your context. Use it BEFORE quoting or building "
            "your answer on anything longer than its preview (previews "
            "mark themselves truncated). The full text comes back the next "
            "step; then write your final reply — with {quote:<label>} if "
            "you want the user to see it quoted. Always produce a text "
            "response alongside the call."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": (
                        "A label from this turn's search results ('A', "
                        "'B', …) or a numeric archive entry id."
                    ),
                },
            },
            "required": ["ref"],
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
        "name": "apply_share",
        "description": (
            "Save the previously proposed share as a PRIVATE draft on the "
            "user's Share page. Call this ONLY when the user explicitly "
            "confirms they want it saved (e.g. 'yes save that', 'add it to "
            "my shares'). Do not call proactively. Do not call if the share "
            "was already saved (check the context notes for apply status). "
            "Saving does NOT publish — the user publishes from their Share "
            "page as a separate action. To PROPOSE a share in the first "
            "place, write it under a `### Share` heading instead (see the "
            "share guidance) — that shows the user exactly what would be "
            "saved and gives them a Save button. Always produce a text "
            "response — the user needs to hear what happened as they are "
            "interacting via voice."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]


def gated_voice_tools(config, search_enabled=False):
    """The agentic voice tool list, with gated tools filtered out:
    ``semantic_search`` unless *search_enabled* (per-user opt-in resolved
    via _external_enabled_for_user) and ``apply_share`` behind ``SHARE_V1``
    (Upload v1).

    Shared by BOTH generation and the #187 pre-warm so their tool prefix is
    byte-identical: tools sit at the front of the Anthropic cache prefix, so
    any divergence here silently busts the whole cache — the warm writes a
    tool set generation never reads (observed as read=0 with the warm
    keeping semantic_search while generation dropped it). Both callers must
    resolve *search_enabled* for the SAME user."""
    dropped = set()
    if not search_enabled:
        dropped.add("semantic_search")
        dropped.add("read_full")
    if not config.get("SHARE_V1", False):
        dropped.add("apply_share")
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
    read_todo, which pins the version it read to the fetching node. System
    nodes therefore no longer pin a todo at session start; only legacy nodes
    (or custom prompts embedding {user_todo}) carry one, and it's honored
    when present, falling back to latest otherwise.
    A todo the user opted out of AI access is omitted entirely; an
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


def _find_pending_share_draft(node_chain, user_id):
    """Walk the node chain to find a pending share draft (SHARE_V1)."""
    for node in reversed(node_chain):
        draft = Draft.query.filter_by(
            parent_id=node.id,
            user_id=user_id,
            label='share_pending',
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


def _detect_share_proposal(text):
    """Check if LLM text contains a share proposal heading (SHARE_V1).

    A single exact ``### Share`` heading marks the block the AI proposes to
    save to the user's Share page. Exact match (not substring) keeps it from
    firing on incidental prose like '### Shared context'."""
    if not text:
        return False
    headings = [h.strip().lower() for h in re.findall(
        r'^###\s+(.+)', text, re.MULTILINE)]
    return any(h == 'share' for h in headings)


def _retrieval_injection_text(tr, with_labels=False):
    """Build the context-injection string for a successful retrieval tool
    result (read_artifact / read_todo / semantic_search), re-resolving the
    content fresh from the source row — content is never stored in
    tool_calls_meta. Re-checks ai_usage so a mid-session opt-out is honored.
    Returns None if nothing is (still) available.

    *with_labels* renders each search match's short quote label ([A], [B])
    and tells the model to quote by label — only valid within the turn that
    assigned the labels (the loop). Cross-turn re-injection passes False and
    falls back to id-based quoting, since the label map died with its turn.
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
    if name == "read_full":
        # Re-resolve via the quote machinery (permission + ai_usage checks
        # built in; depth 1 — nested {quote:ID} stay as placeholders).
        kind, ref_id = tr.get("kind"), tr.get("ref_id")
        reader_id = tr.get("user_id")
        if ref_id is None or reader_id is None:
            return None
        if kind == "external":
            q_text, resolved = resolve_ext_quotes(
                "{quote_ext:%d}" % ref_id, reader_id, for_llm=True)
        else:
            q_text, resolved = resolve_quotes(
                "{quote:%d}" % ref_id, reader_id, for_llm=True,
                max_depth=QUOTE_PULL_DEPTH)
        if not resolved:
            return None
        if len(q_text) > MAX_QUOTE_PULL_CHARS:
            q_text = (q_text[:MAX_QUOTE_PULL_CHARS]
                      + "\n[…content truncated to fit the context window…]")
        what = "reference" if kind == "external" else "entry"
        return (f"[Full content of the {what} you asked to read "
                f"([{tr.get('ref', '?')}]):\n{q_text}]")
    if name == "semantic_search":
        # Re-resolve each matched node from its id (content never stored in
        # meta); re-check ai_usage + soft-delete at injection time. These are
        # PREVIEWS for triage — to read one in full, the model quotes it
        # (resolved by the loop's quote step).
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
            snippet = text[:800] + (
                "… (preview truncated)" if len(text) > 800 else "")
            stamp = (node.created_at.strftime("%Y-%m-%d")
                     if node.created_at else "?")
            score = m.get("score")
            pct = f" · {round(score * 100)}%" if score is not None else ""
            tag = (f"[{m['label']}] " if with_labels and m.get("label")
                   else "")
            lines.append(
                f"- {tag}entry {node.id} · {stamp}{pct}: {snippet}")
        # Saved external references (imported tweets/bookmarks), with their
        # surfacing history as visible metadata — the model weighs
        # repetition itself; there is no hardcoded cooldown.
        for m in (tr.get("ext_matches") or []):
            item = ExternalItem.query.get(m.get("item_id"))
            if item is None:
                continue
            text = (item.get_content() or "").strip()
            if not text:
                continue
            snippet = text[:800] + (
                "… (preview truncated)" if len(text) > 800 else "")
            stamp = (item.posted_at.strftime("%Y-%m-%d")
                     if item.posted_at else "?")
            score = m.get("score")
            pct = f" · {round(score * 100)}%" if score is not None else ""
            surfaced = ""
            if item.surfaced_count:
                last = (item.last_surfaced_at.strftime("%Y-%m-%d")
                        if item.last_surfaced_at else "?")
                surfaced = (f" · already surfaced {item.surfaced_count}x"
                            f" (last {last})")
            tag = (f"[{m['label']}] " if with_labels and m.get("label")
                   else f"reference {item.id} · ")
            author = (f"@{item.author_handle}" if item.author_handle
                      else item.source)
            lines.append(
                f"- {tag}saved reference by {author} · "
                f"{stamp}{pct}{surfaced}: {snippet}")
        if not lines:
            return None
        if with_labels:
            hint = (
                "previews, for triage. Read anything marked '(preview "
                "truncated)' with read_full before quoting or building on "
                "it. To QUOTE one in your final reply, reference it by its "
                "label, e.g. {quote:A} — it renders for the user as the "
                "full quoted entry or reference card (presentation only; "
                "it fetches nothing). Quote at most a couple, only what's "
                "actually relevant — quoting nothing is fine. In prose, "
                "refer to items by author or content, not by label")
        else:
            hint = (
                "previews only; to read an entry in full, reference it as "
                "{quote:<id>}, a saved reference as {quote_ext:<id>}. Use "
                "only if actually relevant")
        return (
            "[Semantic search results for "
            f"\"{tr.get('query', '')}\" — {hint}:\n"
            + "\n".join(lines) + "]")
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


# Spoken-friendly phrases for text-less tool rounds (voice reads the
# interim aloud). update_artifact is handled separately (names the kind).
ACTION_TOOL_LABELS = {
    "apply_todo_changes": "updating your todo list",
    "apply_github_issue": "filing the issue",
    "apply_feedback": "sending your feedback",
    "apply_share": "saving the share draft",
}


def _join_natural(items):
    """["a"] → "a"; ["a", "b"] → "a and b"; ["a", "b", "c"] → "a, b and c"."""
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _interim_fallback_text(tool_calls):
    """Fallback interim text for a TEXT-LESS tool round, naming what is
    actually happening — "(updating your memory and intentions…)" — instead
    of a generic "(on it…)". The model is prompted to write its own
    one-sentence acknowledgment alongside tool calls; this only covers the
    rounds where it didn't. Pure-retrieval rounds keep the established
    "(looking that up…)" byte-for-byte."""
    kinds = []
    actions = []
    has_retrieval = False
    for tc in tool_calls:
        name = tc.get("name")
        if name in RETRIEVAL_TOOLS:
            has_retrieval = True
        elif name == "update_artifact":
            kind = (tc.get("input", {}).get("kind") or "").strip().lower()
            kind = kind.replace("-", " ").replace("_", " ") or "an artifact"
            if kind not in kinds:
                kinds.append(kind)
        else:
            label = (ACTION_TOOL_LABELS.get(name)
                     or (name or "working").replace("_", " "))
            if label not in actions:
                actions.append(label)
    parts = []
    if kinds:
        parts.append("updating your " + _join_natural(kinds))
    parts.extend(actions)
    if not parts:
        return "(looking that up…)" if has_retrieval else "(on it…)"
    if has_retrieval:
        parts.append("looking that up")
    return "(" + ", ".join(parts) + "…)"


def _artifact_update_echo(tr):
    """Within-turn echo of what an update_artifact call actually WROTE,
    injected into the continuation call: a unified diff against the
    previous version (full text for creations). Without it the model has
    no access to its own write — the tool arguments are dropped from the
    injected assistant turn and the inline copy in the system context
    predates the update — so it couldn't build on what it just wrote
    (e.g. actually ask a question it noted in the artifact).

    Content is re-resolved from the encrypted rows via the stored ids and
    NEVER persisted to plaintext tool_calls_meta. Returns None for
    non-update or failed entries and for no-op writes."""
    if tr.get("name") != "update_artifact" or tr.get("status") != "success":
        return None
    artifact = UserArtifact.query.get(tr.get("artifact_id"))
    if artifact is None:
        return None
    new_text = artifact.get_content() or ""
    kind = tr.get("kind")
    prev_id = tr.get("previous_artifact_id")
    if prev_id is None:
        return f"[You created '{kind}' with this content:\n{new_text}]"
    previous = UserArtifact.query.get(prev_id)
    old_text = (previous.get_content() or "") if previous else ""
    # Drop the ---/+++ file headers; keep the @@ hunks.
    diff_lines = list(difflib.unified_diff(
        old_text.splitlines(), new_text.splitlines(), lineterm="", n=1))[2:]
    diff = "\n".join(diff_lines)
    if not diff:
        return None
    if len(diff) > ARTIFACT_ECHO_DIFF_THRESHOLD_CHARS:
        # A rewrite this heavy reads clearer (and usually shorter) in full.
        return (f"[Your rewrite of '{kind}' was substantial — its full "
                f"new content:\n{new_text}]")
    return (f"[Your changes to '{kind}' — the copy shown in your context "
            f"predates this update:\n{diff}]")


def _action_result_text(tr):
    """One-line result string injected back to the model for a NON-retrieval
    tool result, so the loop can continue after action tools too: the model
    sees its side effect land (or fail) before writing the answer the user
    actually reads/hears."""
    name = tr.get("name")
    status = tr.get("status")
    if status == "success":
        if name == "update_artifact":
            verb = "created" if tr.get("created") else "updated"
            return f"[update_artifact: artifact '{tr.get('kind')}' {verb}.]"
        if name == "apply_todo_changes":
            return ("[apply_todo_changes: merge started in the background; "
                    "it will finish shortly.]")
        if name == "apply_github_issue":
            return (f"[apply_github_issue: issue "
                    f"#{tr.get('issue_number')} created — "
                    f"{tr.get('issue_url')}]")
        if name == "apply_feedback":
            return "[apply_feedback: feedback sent to the team.]"
        if name == "apply_share":
            return "[apply_share: share saved as a private draft.]"
        return f"[{name}: done.]"
    if status == "ignored":
        return (f"[{name}: not a tool — write the proposal as a heading "
                f"in your reply instead.]")
    return f"[{name} failed — {tr.get('error', 'unknown error')}]"


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
                        "propose_feedback", "propose_share"):
                status = entry.get("apply_status")
                tag = {
                    "propose_todo": "todo-proposal",
                    "propose_github_issue": "issue-proposal",
                    "propose_feedback": "feedback-proposal",
                    "propose_share": "share-proposal",
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

    # Share proposals only exist where SHARE_V1 is enabled — with the flag
    # off the prompt guidance isn't injected either, so stray ### Share
    # headings in prose must not grow a Save button that would 404.
    if (_share_enabled_for_user(user_id)
            and _detect_share_proposal(llm_text)):
        already = Draft.query.filter_by(
            parent_id=llm_node.id, user_id=user_id,
            label='share_pending',
        ).first()
        if not already:
            existing = _find_pending_share_draft(node_chain, user_id)
            draft = Draft(
                user_id=user_id,
                parent_id=llm_node.id,
                label='share_pending',
            )
            draft.set_content("")
            db.session.add(draft)
            db.session.flush()
            if existing:
                db.session.delete(existing)
                db.session.flush()
            # Supersede old pending_approval share proposals
            _supersede_old_proposals(
                node_chain, "propose_share", llm_node.id)
            results.append({
                "name": "propose_share",
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
_REDACTED_INPUT_KEYS = {"content", "updated_content", "edits"}


def _resolve_artifact_write(inp, previous):
    """Resolve the new full text for an update_artifact call.

    Two write modes: `updated_content` (full replacement — required for
    creation, right for heavy rewrites) or `edits` (targeted exact-match
    {old_text, new_text} replacements against the current version —
    cheaper and faster for small changes). If both are passed the full
    text wins (it's already complete). Edits are all-or-nothing: any
    failing edit aborts the call before anything is written, with an
    error that steers the model to fix the anchor or fall back to
    updated_content.

    Returns (new_text, None) on success or (None, error_message).
    """
    updated_content = inp.get("updated_content")
    if updated_content:
        return updated_content, None
    edits = inp.get("edits")
    if not edits:
        return None, ("Pass updated_content (the full new text) or edits "
                      "(a list of {old_text, new_text} replacements).")
    if previous is None:
        return None, ("No existing artifact to edit — pass "
                      "updated_content with the full text to create it.")
    text = previous.get_content() or ""
    for i, edit in enumerate(edits):
        if not isinstance(edit, dict):
            return None, f"edits[{i}] is not an object."
        old = edit.get("old_text") or ""
        new = edit.get("new_text")
        if not old:
            return None, (f"edits[{i}].old_text is empty — every edit "
                          "needs the exact text to replace.")
        if new is None:
            return None, f"edits[{i}].new_text is missing."
        count = text.count(old)
        if count == 0:
            return None, (
                f"edits[{i}].old_text was not found in the current "
                f"version. Match the current text exactly (whitespace "
                f"included), or send updated_content with the full new "
                f"text instead.")
        if count > 1:
            return None, (
                f"edits[{i}].old_text matches {count} places — include "
                f"more surrounding context so it's unique.")
        text = text.replace(old, new, 1)
    return text, None


def _redact_tool_input(inp):
    """Return a copy of a tool-call input with free-text content removed,
    safe to persist to the plaintext tool_calls_meta column."""
    if not isinstance(inp, dict):
        return inp
    return {
        k: ("[redacted]" if k in _REDACTED_INPUT_KEYS else v)
        for k, v in inp.items()
    }


def _execute_tool_calls(tool_calls, llm_node, node_chain, user_id,
                        quote_labels=None):
    """Execute tool calls and return metadata list. *quote_labels* is the
    turn's label map ({label: ("node"|"external", id)}) so read_full can
    resolve a search-result label; None outside the retrieval loop."""
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
                    new_text, write_err = _resolve_artifact_write(
                        inp, previous)
                    if write_err:
                        result["status"] = "error"
                        result["error"] = write_err
                    else:
                        title = (inp.get("title") or "").strip()
                        if not title:
                            title = (
                                previous.title if previous
                                else UserArtifact.DEFAULT_KINDS.get(
                                    kind, kind.replace("-", " ").title())
                            )
                        # Description: explicit value wins; else carry
                        # forward the previous version's; else the built-in
                        # default for the kind. Mirrors the REST route so AI
                        # writes don't leave a null description (which
                        # blocked editing in the UI).
                        description = (inp.get("description") or "").strip()
                        if not description:
                            description = (
                                previous.description if previous
                                else UserArtifact.DEFAULT_DESCRIPTIONS.get(
                                    kind)
                            )
                        artifact = UserArtifact(
                            user_id=user_id,
                            kind=kind,
                            title=title[:128],
                            description=description,
                            generated_by=(llm_node.llm_model
                                          or "agentic_session"),
                            tokens_used=0,
                        )
                        artifact.set_content(new_text)
                        db.session.add(artifact)
                        db.session.flush()
                        result["status"] = "success"
                        result["artifact_id"] = artifact.id
                        result["kind"] = kind
                        result["created"] = previous is None
                        # Id only (content stays encrypted-at-rest): lets
                        # the within-turn loop re-resolve both versions and
                        # echo a diff of this write back to the model.
                        result["previous_artifact_id"] = (
                            previous.id if previous else None)

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
                        embed_texts,
                        retrieve_relevant_snippets,
                        retrieve_relevant_references,
                    )
                    rag_key = get_openai_chat_key(flask_app.config)
                    if not rag_key:
                        result["status"] = "error"
                        result["error"] = (
                            "Semantic search is unavailable right now.")
                    else:
                        chain_ids = [n.id for n in node_chain]
                        query_vector = embed_texts(
                            [query[:4000]], rag_key, user_id=user_id,
                            request_type="embedding_query",
                        )[0]
                        snippets = retrieve_relevant_snippets(
                            user_id, query[:4000], chain_ids, rag_key,
                            k=flask_app.config.get("RAG_TOP_K", 4),
                            min_score=flask_app.config.get(
                                "RAG_MIN_SCORE", 0.35),
                            query_vector=query_vector,
                        )
                        # Saved external references (tweets/bookmarks) rank
                        # against the same query embedding — one embed call
                        # covers both corpora.
                        references = retrieve_relevant_references(
                            user_id, query_vector,
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
                        result["ext_matches"] = [
                            {"item_id": r["item_id"],
                             "score": round(r["score"], 4)}
                            for r in references
                        ]

            elif name == "read_full":
                # Query intent, made explicit (#208): resolve a search
                # label (this turn's map) or a numeric archive-entry id.
                # Only the target's identity is stored in meta — content is
                # re-resolved at injection time like every retrieval tool.
                ref = (inp.get("ref") or "").strip()
                target = None
                if quote_labels and ref.upper() in quote_labels:
                    target = quote_labels[ref.upper()]
                elif ref.isdigit():
                    target = ("node", int(ref))
                if target is None:
                    result["status"] = "error"
                    result["error"] = (
                        f"Unknown reference {ref!r} — pass a label from "
                        "this turn's search results or a numeric entry id.")
                else:
                    result["status"] = "success"
                    result["kind"], result["ref_id"] = target
                    result["ref"] = ref
                    # Injection re-resolves content with permission checks,
                    # which need the requesting user (also cross-turn).
                    result["user_id"] = user_id
                    # Display metadata for the Actions-taken chip: the user
                    # never sees search labels, so the chip links to the
                    # actual content instead (external URL / entry node).
                    if target[0] == "external":
                        item = ExternalItem.query.get(target[1])
                        if item is not None:
                            result["url"] = item.url
                            result["author_handle"] = item.author_handle

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

            elif name == "apply_share":
                # Save the share the AI proposed under a ### Share heading
                # as a PRIVATE draft — mirrors apply_feedback. Saving never
                # publishes; publication is a separate action on the Share
                # page. Gated on a pending draft, so it can't self-confirm
                # in the same turn it proposed.
                draft = _find_pending_share_draft(node_chain, user_id)
                if not draft:
                    result["status"] = "error"
                    result["error"] = "No pending share found"
                else:
                    origin_node = Node.query.get(draft.parent_id)
                    if not origin_node:
                        result["status"] = "error"
                        result["error"] = "Origin node not found"
                    else:
                        from backend.utils.share import (
                            save_share_draft_from_node,
                        )
                        share, err = save_share_draft_from_node(
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
                                "propose_share",
                                {
                                    "apply_status": "completed",
                                    "share_id": share.id,
                                },
                            )
                            result["status"] = "success"
                            result["share_id"] = share.id

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
                # Skip nodes already finalized: when a CONTINUATION call
                # fails mid-loop, args[1] is the turn's FIRST node — a
                # completed interim step whose status must survive (the
                # in-flight continuation node is failed by the task body).
                if node and node.llm_task_status not in (
                        'completed', 'cancelled'):
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
    if SHARE_GUIDANCE_PLACEHOLDER in text:
        text = text.replace(
            SHARE_GUIDANCE_PLACEHOLDER,
            SHARE_GUIDANCE_TEXT
            if _share_enabled_for_user(user_id) else "")
    if EXTERNAL_GUIDANCE_PLACEHOLDER in text:
        text = text.replace(
            EXTERNAL_GUIDANCE_PLACEHOLDER,
            EXTERNAL_GUIDANCE_TEXT
            if _external_enabled_for_user(user_id) else "")
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
                # prefix won't match and generation can't read the warm (#187)
                # — including the per-user search opt-in.
                tools=gated_voice_tools(
                    flask_app.config,
                    search_enabled=_external_enabled_for_user(user_id)),
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

        # The node currently being generated: advances to each continuation
        # node inside the tool loop, so the failure handler below marks the
        # node actually in flight (not an already-completed interim step).
        current_node = llm_node

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
            # semantic_search is per-user opt-in (#208): the Account toggle
            # (external_content_enabled) under the env killswitch. (Manual
            # Cmd+K search is unaffected — separate HTTP endpoint.) The
            # pre-warm resolves the SAME per-user flag so the cached tool
            # prefix matches (#187). agentic_search_on also gates the
            # within-turn quote pull-in-full below.
            agentic_search_on = _external_enabled_for_user(user_id)
            agentic_tools = (
                gated_voice_tools(flask_app.config,
                                  search_enabled=agentic_search_on)
                if is_agentic else None)

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
                                    elif ename == "propose_share":
                                        message_text += (
                                            f"\n\n[share-proposal:"
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
                        # Replace {share_guidance} — flag-conditional, must
                        # mirror render_system_message byte-for-byte
                        if SHARE_GUIDANCE_PLACEHOLDER in message_text:
                            message_text = message_text.replace(
                                SHARE_GUIDANCE_PLACEHOLDER,
                                SHARE_GUIDANCE_TEXT
                                if _share_enabled_for_user(user_id)
                                else ""
                            )
                        # Mirrors render_system_message byte-for-byte
                        # (#187 cache prefix), like {share_guidance}.
                        if EXTERNAL_GUIDANCE_PLACEHOLDER in message_text:
                            message_text = message_text.replace(
                                EXTERNAL_GUIDANCE_PLACEHOLDER,
                                EXTERNAL_GUIDANCE_TEXT
                                if _external_enabled_for_user(user_id)
                                else ""
                            )
                        # Resolve {quote:ID} placeholders if present
                        if needs_quotes and has_quotes(message_text):
                            message_text, resolved_ids = resolve_quotes(message_text, user_id, for_llm=True)
                            if resolved_ids:
                                logger.info(f"Resolved quotes for node IDs: {resolved_ids}")
                        # Resolve {quote_ext:ID} (saved references) inline —
                        # small, non-recursive, owner-only.
                        if has_ext_quotes(message_text):
                            message_text, _ext_ids = resolve_ext_quotes(
                                message_text, user_id, for_llm=True)

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

            # Turn-scoped relative-quote state. quote_labels maps a short
            # label ("A") to ("node"|"external", id) — assigned when search
            # results are labeled, consumed by canonicalization wherever the
            # model's text is stored. bumped_ext_ids guards the eager
            # surfacing-history bump to once per item per turn. Initialized
            # HERE (not in the loop) because _finalize also runs on the
            # single-shot path that never enters the loop.
            quote_labels = {}
            bumped_ext_ids = set()

            def _finalize(target_node, resp):
                """Write *resp* as the final answer on *target_node* using the
                EXACT existing single-shot finalize behavior (cost log, Race B
                guard, content, tool calls, proposal auto-detect, _mode meta,
                status_reported marking, completed status, commit).

                Returns the result dict, or a 'cancelled' dict if the node was
                soft-deleted mid-generation.
                """
                # Canonicalize relative quote labels ({quote:A}) to absolute
                # markers first — everything downstream (storage, exports,
                # rendering, TTS) sees only absolute references.
                f_llm_text = _canonicalize_quote_labels(
                    resp["content"], quote_labels)
                _bump_surfaced_references(
                    f_llm_text, user_id, bumped_ext_ids)
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
                        f_tool_calls, target_node, node_chain, user_id,
                        quote_labels=quote_labels,
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
                # Voice: mark TTS pending in the SAME commit as completion so
                # the frontend's POST /tts (fired the moment it polls
                # 'completed') sees the in-flight promise and doesn't
                # double-enqueue.
                f_dispatch_tts = (source_mode == "voice"
                                  and not target_node.audio_tts_url)
                if f_dispatch_tts:
                    target_node.tts_task_status = 'pending'
                db.session.commit()
                if f_dispatch_tts:
                    _dispatch_voice_tts(target_node.id, user_id)

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

            # ── Bounded within-turn tool loop (#158) ───────────────────────
            # When the model calls ANY tool, execute it, inject the result
            # back into `messages` as a plain user message (full content for
            # retrieval tools, a one-line status for action tools), finalize
            # the current node as an interim step, create a continuation
            # node, and re-call the model so it answers WITH the results in
            # the same turn. This matches the tool-use contract the model
            # expects — ending the turn on an action call left one-line
            # preambles ("noting this in memory") as the whole answer.
            rounds_done = 0
            while True:
                response_tool_calls = response.get("tool_calls", [])
                # Quote markers ({quote:...} / {quote_ext:...}) are PURE
                # PRESENTATION — they render as cards and never trigger a
                # round. Query intent is a separate explicit act: the
                # read_full tool (in RETRIEVAL_TOOLS above). Labels
                # canonicalize here so the stored interim text is absolute.
                # (The old emit-a-marker-to-pull mechanic conflated the two
                # intents and duplicated nodes when the model quoted inside
                # a complete answer — staging pass, 2026-07-03.)
                resp_text = _canonicalize_quote_labels(
                    response.get("content") or "", quote_labels)

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

                # INTERIM step: a tool call was made and budget remains.
                if response_tool_calls and rounds_done < MAX_RETRIEVAL_ROUNDS:
                    # Text-less rounds get a spoken-friendly placeholder
                    # naming what's happening (voice reads it aloud).
                    interim_fallback = _interim_fallback_text(
                        response_tool_calls)
                    # #179: scrub timestamp echoes from interim text too —
                    # it's stored on the interim node and spoken in voice.
                    interim_text = strip_edge_timestamps(resp_text)
                    interim_truncated = response.get("truncated", False)
                    # Interim text is stored (and rendered) — record any
                    # references it already quotes.
                    _bump_surfaced_references(
                        interim_text, user_id, bumped_ext_ids)

                    # Cost for THIS model call (every call costs).
                    _log_api_cost(response)

                    # Execute ALL tool calls on the interim node.
                    tool_results = _execute_tool_calls(
                        response_tool_calls, current_node, node_chain,
                        user_id, quote_labels=quote_labels,
                    )
                    if interim_truncated:
                        for tr in tool_results:
                            tr["response_truncated"] = True

                    # Assign this turn's quote labels to fresh search results
                    # BEFORE building injections — the previews render them
                    # and canonicalization consumes them. Labels continue
                    # across rounds (A..Z, AA..) so two searches in one turn
                    # can't collide.
                    for tr in tool_results:
                        if (tr.get("name") != "semantic_search"
                                or tr.get("status") != "success"):
                            continue
                        for m in tr.get("matches") or []:
                            label = _quote_label(len(quote_labels))
                            m["label"] = label
                            quote_labels[label] = ("node", m["node_id"])
                        for m in tr.get("ext_matches") or []:
                            label = _quote_label(len(quote_labels))
                            m["label"] = label
                            quote_labels[label] = ("external", m["item_id"])

                    # Build the injection text for each tool call: retrieval
                    # tools re-resolve content from the encrypted row and pin
                    # the exact version read to the interim node (faithful
                    # export); action tools inject a one-line result. Every
                    # entry is marked handled here so the cross-turn scan
                    # won't double-inject. Errors and vanished artifacts are
                    # surfaced too, so the model isn't re-called blind.
                    injection_strings = []
                    for tr in tool_results:
                        tr["status_reported"] = True
                        if tr.get("name") not in RETRIEVAL_TOOLS:
                            injection_strings.append(_action_result_text(tr))
                            # Echo what an artifact write actually changed
                            # (diff vs. the previous version) so the model
                            # can build on its own edit in the answer.
                            echo = _artifact_update_echo(tr)
                            if echo:
                                injection_strings.append(echo)
                        elif tr.get("status") == "success":
                            text = _retrieval_injection_text(
                                tr, with_labels=True)
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

                    if not injection_strings:
                        injection_strings.append(
                            "[The requested items were unavailable.]")

                    # Finalize the interim node (NO _mode, NO proposal
                    # auto-detect — that belongs to the final answer only).
                    current_node.set_content(
                        interim_text or interim_fallback)
                    current_node.token_count = approximate_token_count(
                        interim_text or interim_fallback)
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
                    # Voice: the interim step is now final content — kick off
                    # its TTS immediately so the user can play it while the
                    # continuation call below is still generating. Status is
                    # set in the same commit as completion (see _finalize).
                    interim_dispatch_tts = (source_mode == "voice"
                                            and not current_node.audio_tts_url)
                    if interim_dispatch_tts:
                        current_node.tts_task_status = 'pending'
                    db.session.commit()
                    if interim_dispatch_tts:
                        _dispatch_voice_tts(current_node.id, user_id)

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
                            "text": interim_text or interim_fallback,
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

                    def _call_continuation():
                        try:
                            return LLMProvider.get_completion(
                                model_id, messages, api_keys,
                                tools=agentic_tools,
                                # #189: same per-thread key as the first call
                                # so loop continuations route to the same
                                # OpenAI cache.
                                prompt_cache_key=f"loore-t{thread_root_id}",
                            )
                        except PromptTooLongError:
                            logger.warning(
                                "Continuation prompt too long after tool "
                                "round %d; dropping the injection and "
                                "answering without it", rounds_done,
                            )
                            messages[-1] = {
                                "role": "user",
                                "content": [{
                                    "type": "text",
                                    "text": (
                                        "[The retrieved content was too large "
                                        "to fit in context. Answer with what "
                                        "you already have, and let the user "
                                        "know you could only partially load "
                                        "the entries you referenced.]"),
                                }],
                            }
                            return LLMProvider.get_completion(
                                model_id, messages, api_keys,
                                tools=agentic_tools,
                                # #189: same per-thread key as the first call
                                # so loop continuations route to the same
                                # OpenAI cache.
                                prompt_cache_key=f"loore-t{thread_root_id}",
                            )

                    # Transient provider errors (overload, timeout) used to
                    # propagate here and kill the turn, stranding this
                    # continuation node at 'processing' forever. Retry a
                    # couple of times; a terminal failure raises and the
                    # task-level handler fails THIS node (not the completed
                    # interim).
                    response = None
                    for retry_delay in (*CONTINUATION_RETRY_DELAYS, None):
                        try:
                            response = _call_continuation()
                            break
                        except Exception as cont_exc:
                            if retry_delay is None:
                                raise
                            logger.warning(
                                "Continuation call failed (%s); retrying "
                                "in %ss", cont_exc, retry_delay)
                            time.sleep(retry_delay)
                    continue

                # FINAL answer: no retrieval (or budget exhausted).
                return _finalize(current_node, response)

        except Exception as e:
            error_message = str(e)
            logger.error(f"LLM completion error for node {llm_node_id}: {error_message}", exc_info=True)
            # Fail the node in flight: mid-loop that's the continuation
            # placeholder. Failing llm_node here instead used to clobber the
            # completed interim step's status and strand the continuation at
            # 'processing' forever.
            current_node.llm_task_status = 'failed'
            current_node.llm_task_error = error_message
            db.session.commit()
            raise

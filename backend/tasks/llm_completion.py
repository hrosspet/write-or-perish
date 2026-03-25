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
from backend.utils.tokens import approximate_token_count, reduce_export_tokens
from backend.utils.quotes import resolve_quotes, has_quotes
from backend.utils.api_keys import determine_api_key_type, get_api_keys_for_usage
from backend.utils.cost import calculate_llm_cost_microdollars

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
        "name": "update_todo",
        "description": (
            "Signal that your text response contains proposed todo "
            "changes. Call this when the user mentions completing tasks, "
            "new tasks they need to do, or wants to reorganize "
            "priorities. Your text response must include the structured "
            "update (### Completed, ### New Tasks, ### Priority Order, "
            "### Note sections). The changes are NOT applied immediately "
            "— the user must confirm first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "apply_todo_changes",
        "description": (
            "Apply previously proposed todo changes to the user's actual "
            "todo list. Call this ONLY when the user explicitly confirms "
            "they want to apply the changes (e.g. 'ok apply those "
            "changes', 'yes update my todo', 'go ahead'). Do NOT call "
            "proactively. Do NOT call if changes were already applied "
            "(check the context notes for apply status)."
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
            "existing ones."
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


def _execute_tool_calls(tool_calls, llm_node, node_chain, user_id):
    """Execute tool calls and return metadata list."""
    tool_results = []

    for tc in tool_calls:
        name = tc["name"]
        inp = tc.get("input", {})
        result = {"name": name, "input": inp}

        try:
            if name == "update_todo":
                # Find existing pending draft before creating new one
                existing = _find_pending_todo_draft(node_chain, user_id)

                # Create lightweight draft flag on the LLM node.
                # The LLM's text response IS the update summary —
                # no separate node needed.
                draft = Draft(
                    user_id=user_id,
                    parent_id=llm_node.id,
                    label='todo_pending',
                )
                draft.set_content("")  # flag only, content is on LLM node
                db.session.add(draft)
                db.session.flush()

                # Delete previous pending draft now that new one exists
                if existing:
                    db.session.delete(existing)
                    db.session.flush()

                result["status"] = "success"
                result["draft_id"] = draft.id
                result["apply_status"] = "pending_approval"

            elif name == "apply_todo_changes":
                draft = _find_pending_todo_draft(node_chain, user_id)
                if not draft:
                    result["status"] = "error"
                    result["error"] = "No pending todo changes found"
                else:
                    # Kick off async background merge
                    from backend.routes.todo import (
                        _start_todo_merge,
                    )
                    task_id = _start_todo_merge(
                        draft, llm_node, user_id
                    )
                    result["status"] = "success"
                    result["apply_task_id"] = task_id

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
    Get the most recent user profile content if AI usage is permitted.

    Returns the profile content if ai_usage is 'chat' (AI can use for responses),
    otherwise returns None.
    """
    profile = UserProfile.query.filter_by(user_id=user_id).order_by(
        UserProfile.created_at.desc()
    ).first()

    if profile and profile.ai_usage == "chat":
        return profile.get_content()
    return None


def get_user_todo_content(user_id):
    """
    Get the most recent user todo content if AI usage is permitted.

    Returns the todo content if ai_usage is 'chat',
    otherwise returns None.
    """
    todo = UserTodo.query.filter_by(user_id=user_id).order_by(
        UserTodo.created_at.desc()
    ).first()

    if todo and todo.ai_usage == "chat":
        return todo.get_content()
    return None


def get_user_recent_content(user_id):
    """Get the latest recent context summary for a user.

    Filters by profile_id matching the current profile so old summaries
    (from before a profile update) are not returned.
    """
    profile = UserProfile.query.filter_by(user_id=user_id).filter(
        UserProfile.ai_usage.in_(["chat", "train"])
    ).order_by(UserProfile.created_at.desc()).first()

    profile_id = profile.id if profile else None

    q = UserRecentContext.query.filter_by(user_id=user_id)
    if profile_id is not None:
        q = q.filter_by(profile_id=profile_id)
    else:
        q = q.filter(UserRecentContext.profile_id.is_(None))

    rc = q.order_by(UserRecentContext.created_at.desc()).first()
    if rc and rc.ai_usage == "chat":
        return rc
    return None


def get_user_recent_raw_content(user_id, created_before=None):
    """Get raw user writing since the last recent context summary.

    Falls back to profile cutoff if no recent context exists,
    or returns None if neither exists.

    Args:
        created_before: Upper bound timestamp. Nodes created at/after this
            time are excluded to avoid duplicating current session context.
    """
    profile = UserProfile.query.filter_by(user_id=user_id).filter(
        UserProfile.ai_usage.in_(["chat", "train"])
    ).order_by(UserProfile.created_at.desc()).first()

    profile_id = profile.id if profile else None

    # Find latest recent context for current profile
    q = UserRecentContext.query.filter_by(user_id=user_id)
    if profile_id is not None:
        q = q.filter_by(profile_id=profile_id)
    else:
        q = q.filter(UserRecentContext.profile_id.is_(None))
    rc = q.order_by(UserRecentContext.created_at.desc()).first()

    # Determine cutoff timestamp
    if rc and rc.source_data_cutoff:
        created_after = rc.source_data_cutoff
    elif profile and profile.source_data_cutoff:
        created_after = profile.source_data_cutoff
    else:
        return None

    from backend.routes.export_data import (
        build_user_export_content as _build_export,
    )
    user = User.query.get(user_id)
    if not user:
        return None

    return _build_export(
        user,
        max_tokens=10000,
        filter_ai_usage=True,
        created_after=created_after,
        created_before=created_before,
        chronological_order=True,
    )


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
                user_profile_content = get_user_profile_content(user_id)
                if user_profile_content:
                    logger.info(f"Retrieved user profile for {user_id}: {len(user_profile_content)} chars")
                else:
                    logger.info(f"No profile with chat permission found for user {user_id}")

            if needs_todo:
                user_todo_content = get_user_todo_content(user_id)
                if user_todo_content:
                    logger.info(f"Retrieved user todo for {user_id}: {len(user_todo_content)} chars")
                else:
                    logger.info(f"No todo with chat permission found for user {user_id}")

            if needs_recent:
                rc = get_user_recent_content(user_id)
                if rc:
                    user_recent_content = rc.get_content()
                    logger.info(f"Retrieved recent context for {user_id}: {len(user_recent_content)} chars")

            if needs_recent_raw:
                raw_cutoff = recent_raw_node.created_at if recent_raw_node else None
                user_recent_raw_content = get_user_recent_raw_content(
                    user_id, created_before=raw_cutoff
                )
                if user_recent_raw_content:
                    logger.info(f"Retrieved recent raw data for {user_id}: {len(user_recent_raw_content)} chars, cutoff={raw_cutoff}")

            if needs_ai_prefs:
                user_ai_preferences_content = get_user_ai_preferences_content(user_id)
                if user_ai_preferences_content:
                    logger.info(f"Retrieved AI preferences for {user_id}: {len(user_ai_preferences_content)} chars")

            # Detect if this is a voice session (enables tools)
            is_voice = _is_voice_prompt(node_chain)
            voice_tools = VOICE_TOOLS if is_voice else None

            # Check for pending todo draft and inject context note
            pending_draft_note = None
            if is_voice:
                pending = _find_pending_todo_draft(node_chain, user_id)
                if pending:
                    pending_draft_note = (
                        "[Note: there are pending todo changes awaiting "
                        "confirmation. The user can say 'apply the "
                        "changes' to confirm.]"
                    )

            # Inject tool result context from previous LLM node
            prev_tool_note = None
            if is_voice and len(node_chain) >= 2:
                for prev_node in reversed(node_chain):
                    if prev_node.tool_calls_meta:
                        try:
                            prev_meta = json.loads(prev_node.tool_calls_meta)
                            summaries = []
                            for m in prev_meta:
                                name = m['name']
                                apply_s = m.get("apply_status")
                                if name == "update_todo" and apply_s:
                                    if apply_s == "completed":
                                        summaries.append(
                                            "Todo changes have been "
                                            "applied to user's todo."
                                        )
                                    elif apply_s == "started":
                                        summaries.append(
                                            "Todo changes are being "
                                            "applied (merge in progress)."
                                        )
                                    elif apply_s == "pending_approval":
                                        summaries.append(
                                            "You proposed todo changes "
                                            "that are waiting for user "
                                            "approval."
                                        )
                                    elif apply_s == "failed":
                                        summaries.append(
                                            "Todo changes failed: "
                                            + m.get("apply_error", "")
                                        )
                                elif name == "update_ai_preferences":
                                    summaries.append(
                                        "AI preferences were updated."
                                    )
                            if summaries:
                                prev_tool_note = (
                                    "[" + " ".join(summaries) + "]"
                                )
                        except (json.JSONDecodeError, KeyError):
                            pass
                        break

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
                if is_voice and (pending_draft_note or prev_tool_note):
                    notes = [n for n in [prev_tool_note, pending_draft_note] if n]
                    injected_text = "\n".join(notes)
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
                tool_meta = tool_results
                llm_node.tool_calls_meta = json.dumps(tool_results)

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

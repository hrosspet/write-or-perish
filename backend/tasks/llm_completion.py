"""
Celery task for asynchronous LLM completion.
"""
from celery import Task
from celery.utils.log import get_task_logger
from datetime import datetime

from backend.celery_app import celery, flask_app
from backend.models import Node, User, UserProfile, APICostLog
from backend.extensions import db
from backend.llm_providers import LLMProvider
from backend.utils.tokens import approximate_token_count, calculate_max_export_tokens
from backend.utils.quotes import resolve_quotes, has_quotes
from backend.utils.api_keys import determine_api_key_type, get_api_keys_for_usage
from backend.utils.cost import calculate_llm_cost_microdollars

logger = get_task_logger(__name__)

# Placeholder for injecting user's writing archive into messages
USER_EXPORT_PLACEHOLDER = "{user_export}"
# Placeholder for injecting user's AI-generated profile into messages
USER_PROFILE_PLACEHOLDER = "{user_profile}"


def build_user_export_content(user, max_tokens=None, filter_ai_usage=False, created_before=None):
    """Import the actual implementation from export_data routes."""
    from backend.routes.export_data import build_user_export_content as _build
    return _build(user, max_tokens, filter_ai_usage, created_before)


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
        return profile.content
    return None


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
            for node in node_chain:
                node_content = node.get_content()
                if node_content and USER_EXPORT_PLACEHOLDER in node_content:
                    export_node = node
                    break
            needs_export = export_node is not None
            user_export_content = None

            # Check if any node contains the {user_profile} placeholder
            needs_profile = any(
                USER_PROFILE_PLACEHOLDER in node.get_content()
                for node in node_chain if node.get_content()
            )
            user_profile_content = None

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

            if needs_export:
                # Calculate token budget for export (same logic as profile generation)
                from backend.utils.tokens import get_model_context_window
                context_window = get_model_context_window(model_id)
                conversation_tokens = sum(approximate_token_count(n.get_content() or "") for n in node_chain)
                max_export_tokens = calculate_max_export_tokens(model_id, reserved_tokens=conversation_tokens)
                logger.info(f"Export token budget: model={model_id}, context_window={context_window}, conversation_tokens={conversation_tokens}, max_export_tokens={max_export_tokens}")

                user = User.query.get(user_id)
                if user and max_export_tokens > 0:
                    # Use the timestamp of the node containing {user_export} as cutoff
                    # to only include archive data created before that node
                    created_before = export_node.created_at if export_node else None
                    user_export_content = build_user_export_content(
                        user,
                        max_tokens=max_export_tokens,
                        filter_ai_usage=True,
                        created_before=created_before
                    )
                    logger.info(f"Built user export for {user_id}: {len(user_export_content or '')} chars, ~{approximate_token_count(user_export_content or '')} tokens, cutoff={created_before}")

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
                    if user_export_content and USER_EXPORT_PLACEHOLDER in message_text:
                        message_text = message_text.replace(
                            USER_EXPORT_PLACEHOLDER,
                            user_export_content
                        )
                    # Replace {user_profile} placeholder if present
                    if user_profile_content and USER_PROFILE_PLACEHOLDER in message_text:
                        message_text = message_text.replace(
                            USER_PROFILE_PLACEHOLDER,
                            user_profile_content
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

            # Step 3: Call LLM API
            self.update_state(state='PROGRESS', meta={'progress': 40, 'status': 'Generating response'})
            llm_node.llm_task_progress = 40
            db.session.commit()

            # Determine which API key to use based on ai_usage settings
            key_type = determine_api_key_type(node_chain, logger=logger)
            api_keys = get_api_keys_for_usage(flask_app.config, key_type)

            model_config = flask_app.config["SUPPORTED_MODELS"][model_id]
            provider = model_config["provider"]
            api_model = model_config["api_model"]

            # Log total context being sent
            total_content = "".join(m["content"][0]["text"] for m in messages if m.get("content"))
            estimated_tokens = approximate_token_count(total_content)
            logger.info(f"Calling LLM API: model_id={model_id}, api_model={api_model}, provider={provider}, key_type={key_type}, estimated_tokens={estimated_tokens}, total_chars={len(total_content)}")

            if provider == "anthropic" and not api_keys["anthropic"]:
                raise ValueError("Anthropic API key is not configured.")
            elif provider == "openai" and not api_keys["openai"]:
                raise ValueError("OpenAI API key is not configured.")

            response = LLMProvider.get_completion(model_id, messages, api_keys)
            llm_text = response["content"]
            total_tokens = response["total_tokens"]
            input_tokens = response.get("input_tokens", 0)
            output_tokens = response.get("output_tokens", 0)

            logger.info(f"LLM response generated: {len(llm_text)} chars, {total_tokens} tokens")

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
            llm_node.llm_task_status = 'completed'
            llm_node.llm_task_progress = 100
            db.session.commit()

            logger.info(f"LLM completion successful, updated node {llm_node.id}")

            return {
                'parent_node_id': parent_node_id,
                'llm_node_id': llm_node.id,
                'status': 'completed',
                'total_tokens': total_tokens
            }

        except Exception as e:
            error_message = str(e)
            logger.error(f"LLM completion error for node {llm_node_id}: {error_message}", exc_info=True)
            llm_node.llm_task_status = 'failed'
            llm_node.llm_task_error = error_message
            db.session.commit()
            raise

"""
Celery task for asynchronous LLM completion.
"""
from celery import Task
from celery.utils.log import get_task_logger
from datetime import datetime

from backend.celery_app import celery, flask_app
from backend.models import Node, User
from backend.extensions import db
from backend.llm_providers import LLMProvider
from backend.utils.tokens import approximate_token_count, calculate_max_export_tokens

logger = get_task_logger(__name__)

# Placeholder for injecting user's writing archive into messages
USER_EXPORT_PLACEHOLDER = "{user_export}"


def build_user_export_content(user, max_tokens=None, filter_ai_usage=False):
    """Import the actual implementation from export_data routes."""
    from backend.routes.export_data import build_user_export_content as _build
    return _build(user, max_tokens, filter_ai_usage)


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
    logger.info(f"Starting LLM completion task for parent {parent_node_id}, updating node {llm_node_id}")

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
            needs_export = any(
                USER_EXPORT_PLACEHOLDER in node.content
                for node in node_chain if node.content
            )
            user_export_content = None

            if needs_export:
                # Calculate token budget for export (same logic as profile generation)
                conversation_tokens = sum(approximate_token_count(n.content or "") for n in node_chain)
                max_export_tokens = calculate_max_export_tokens(model_id, reserved_tokens=conversation_tokens)

                user = User.query.get(user_id)
                if user and max_export_tokens > 0:
                    user_export_content = build_user_export_content(
                        user,
                        max_tokens=max_export_tokens,
                        filter_ai_usage=True
                    )
                    logger.info(f"Built user export for {user_id}: {len(user_export_content or '')} chars")

            messages = []
            for node in node_chain:
                author = node.user.username if node.user else "Unknown"
                is_llm_node = node.node_type == "llm" or (node.llm_model is not None)

                if is_llm_node:
                    role = "assistant"
                    message_text = node.content
                else:
                    role = "user"
                    message_text = f"author {author}: {node.content}"
                    # Replace {user_export} placeholder if present
                    if user_export_content and USER_EXPORT_PLACEHOLDER in message_text:
                        message_text = message_text.replace(
                            USER_EXPORT_PLACEHOLDER,
                            user_export_content
                        )

                messages.append({
                    "role": role,
                    "content": [{"type": "text", "text": message_text}]
                })

            # Step 3: Call LLM API
            self.update_state(state='PROGRESS', meta={'progress': 40, 'status': 'Generating response'})
            llm_node.llm_task_progress = 40
            db.session.commit()

            api_keys = {
                "openai": flask_app.config.get("OPENAI_API_KEY"),
                "anthropic": flask_app.config.get("ANTHROPIC_API_KEY")
            }
            model_config = flask_app.config["SUPPORTED_MODELS"][model_id]
            provider = model_config["provider"]
            if provider == "anthropic" and not api_keys["anthropic"]:
                raise ValueError("Anthropic API key is not configured.")
            elif provider == "openai" and not api_keys["openai"]:
                raise ValueError("OpenAI API key is not configured.")
            
            response = LLMProvider.get_completion(model_id, messages, api_keys)
            llm_text = response["content"]
            total_tokens = response["total_tokens"]

            logger.info(f"LLM response generated: {len(llm_text)} chars, {total_tokens} tokens")

            # Step 4: Redistribute tokens
            self.update_state(state='PROGRESS', meta={'progress': 90, 'status': 'Redistributing tokens'})
            llm_node.llm_task_progress = 90
            db.session.commit()

            contributing_nodes = [n for n in node_chain if n.node_type != "llm"]
            if contributing_nodes and total_tokens:
                total_weight = sum(approximate_token_count(n.content) for n in contributing_nodes)
                for n in contributing_nodes:
                    weight = approximate_token_count(n.content)
                    share = int(round(total_tokens * (weight / total_weight))) if total_weight > 0 else 0
                    n.distributed_tokens += share
                    db.session.add(n)

            # Step 5: Update the placeholder LLM node with the response
            self.update_state(state='PROGRESS', meta={'progress': 95, 'status': 'Finalizing'})
            llm_node.content = llm_text
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

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

logger = get_task_logger(__name__)


def approximate_token_count(text: str) -> int:
    """
    Approximate token count using word count * 4/3.
    This is a rough estimate that works reasonably well for English text.
    """
    return max(1, int(len(text.split()) * 4 / 3))


class LLMCompletionTask(Task):
    """Custom task class with error handling."""

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        """Called when task fails."""
        node_id = args[0] if args else None
        if node_id:
            with flask_app.app_context():
                node = Node.query.get(node_id)
                if node:
                    node.llm_task_status = 'failed'
                    # Store error message if not already set
                    if not node.llm_task_error:
                        node.llm_task_error = str(exc)
                    db.session.commit()
                    logger.error(f"LLM completion failed for node {node_id}: {exc}")


@celery.task(base=LLMCompletionTask, bind=True)
def generate_llm_response(self, parent_node_id: int, model_id: str, user_id: int):
    """
    Asynchronously generate an LLM response.

    Args:
        parent_node_id: ID of the parent node to respond to
        model_id: Model identifier (e.g., "gpt-5", "claude-sonnet-4.5")
        user_id: ID of the user requesting the completion (for auth/logging)
    """
    logger.info(f"Starting LLM completion task for node {parent_node_id} with model {model_id}")

    with flask_app.app_context():
        # Get parent node from database
        parent_node = Node.query.get(parent_node_id)
        if not parent_node:
            raise ValueError(f"Node {parent_node_id} not found")

        # Update status to processing
        parent_node.llm_task_status = 'processing'
        parent_node.llm_task_progress = 10
        db.session.commit()

        try:
            # Validate model is supported
            if model_id not in flask_app.config["SUPPORTED_MODELS"]:
                raise ValueError(f"Unsupported model: {model_id}")

            # Step 1: Build the chain of nodes (20% progress)
            self.update_state(state='PROGRESS', meta={'progress': 20, 'status': 'Building context'})
            parent_node.llm_task_progress = 20
            db.session.commit()

            node_chain = []
            current = parent_node
            while current:
                node_chain.insert(0, current)
                current = current.parent

            # Step 2: Build messages array (30% progress)
            self.update_state(state='PROGRESS', meta={'progress': 30, 'status': 'Preparing messages'})
            parent_node.llm_task_progress = 30
            db.session.commit()

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

                messages.append({
                    "role": role,
                    "content": [
                        {
                            "type": "text",
                            "text": message_text
                        }
                    ]
                })

            # Step 3: Call LLM API (40% -> 80% progress)
            self.update_state(state='PROGRESS', meta={'progress': 40, 'status': 'Generating response'})
            parent_node.llm_task_progress = 40
            db.session.commit()

            api_keys = {
                "openai": flask_app.config.get("OPENAI_API_KEY"),
                "anthropic": flask_app.config.get("ANTHROPIC_API_KEY")
            }

            # Check if required API key is configured
            model_config = flask_app.config["SUPPORTED_MODELS"][model_id]
            provider = model_config["provider"]

            if provider == "anthropic" and not api_keys["anthropic"]:
                raise ValueError(
                    "Anthropic API key is not configured. "
                    "Please set the ANTHROPIC_API_KEY environment variable in your .flaskenv file."
                )
            elif provider == "openai" and not api_keys["openai"]:
                raise ValueError(
                    "OpenAI API key is not configured. "
                    "Please set the OPENAI_API_KEY environment variable in your .flaskenv file."
                )

            response = LLMProvider.get_completion(model_id, messages, api_keys)
            llm_text = response["content"]
            total_tokens = response["total_tokens"]

            logger.info(f"LLM response generated: {len(llm_text)} characters, {total_tokens} tokens")

            # Step 4: Create LLM user if needed (85% progress)
            self.update_state(state='PROGRESS', meta={'progress': 85, 'status': 'Saving response'})
            parent_node.llm_task_progress = 85
            db.session.commit()

            llm_user = User.query.filter_by(username=model_id).first()
            if not llm_user:
                llm_user = User(twitter_id=f"llm-{model_id}", username=model_id)
                db.session.add(llm_user)
                db.session.commit()

            # Step 5: Redistribute tokens (90% progress)
            self.update_state(state='PROGRESS', meta={'progress': 90, 'status': 'Redistributing tokens'})
            parent_node.llm_task_progress = 90
            db.session.commit()

            contributing_nodes = [n for n in node_chain if n.node_type != "llm"]
            if contributing_nodes and total_tokens:
                total_weight = sum(approximate_token_count(n.content) for n in contributing_nodes)
                for n in contributing_nodes:
                    weight = approximate_token_count(n.content)
                    share = int(round(total_tokens * (weight / total_weight))) if total_weight > 0 else 0
                    n.distributed_tokens += share
                    db.session.add(n)

            # Step 6: Create LLM response node (95% progress)
            self.update_state(state='PROGRESS', meta={'progress': 95, 'status': 'Finalizing'})
            parent_node.llm_task_progress = 95
            db.session.commit()

            llm_node = Node(
                user_id=llm_user.id,
                parent_id=parent_node.id,
                node_type="llm",
                llm_model=model_id,
                content=llm_text,
                token_count=0,
                distributed_tokens=0
            )
            db.session.add(llm_node)

            # Mark parent as completed
            parent_node.llm_task_status = 'completed'
            parent_node.llm_task_progress = 100
            db.session.commit()

            logger.info(f"LLM completion successful for node {parent_node_id}, created node {llm_node.id}")

            return {
                'parent_node_id': parent_node_id,
                'llm_node_id': llm_node.id,
                'status': 'completed',
                'total_tokens': total_tokens
            }

        except Exception as e:
            error_message = str(e)
            logger.error(f"LLM completion error for node {parent_node_id}: {error_message}", exc_info=True)
            parent_node.llm_task_status = 'failed'
            parent_node.llm_task_error = error_message
            db.session.commit()
            raise

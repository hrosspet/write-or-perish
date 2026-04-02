"""
Celery task for applying Voice todo updates to the user's todo list.

Runs entirely in the background without creating visible nodes.
Uses the orient_apply_todo prompt to merge the proposed changes
into the full todo via a single LLM call.

Merges are serialized per user via a Redis lock so concurrent
confirmations don't clobber each other's results.
"""
import json
import redis
from celery.utils.log import get_task_logger

from backend.celery_app import celery, flask_app
from backend.models import Node, UserTodo
from backend.extensions import db
from backend.llm_providers import LLMProvider
from backend.utils.api_keys import get_api_keys_for_usage
from backend.utils.cost import calculate_llm_cost_microdollars
from backend.models import APICostLog

logger = get_task_logger(__name__)

# Lock timeout: max time a single merge can hold the lock (seconds).
# Matches the LLM request timeout (600s) since large todo merges can
# take that long. Prevents deadlocks if a worker crashes mid-merge.
_MERGE_LOCK_TIMEOUT = 600
# How long to wait for the lock before giving up (seconds).
_MERGE_LOCK_ACQUIRE_TIMEOUT = 600


@celery.task(bind=True)
def apply_voice_todo(self, llm_node_id: int, model_id: str, user_id: int,
                     confirm_node_id: int = None):
    """
    Merge the Voice todo update into the user's full todo list.

    1. Read update summary from the LLM node's text content
    2. Get the current user todo
    3. Call LLM with orient_apply_todo prompt to produce merged todo
    4. Save as new UserTodo
    5. Update tool_calls_meta on the originating LLM node
    """
    with flask_app.app_context():
        llm_node = Node.query.get(llm_node_id)
        if not llm_node:
            logger.error(f"LLM node {llm_node_id} not found")
            return

        update_summary = llm_node.get_content()
        if not update_summary:
            logger.error(f"LLM node {llm_node_id} has no content")
            _update_apply_status(llm_node, "failed", error="No update summary",
                                confirm_node_id=confirm_node_id)
            return

        # Acquire per-user Redis lock to serialize concurrent merges.
        # This ensures the second merge reads the todo AFTER the first
        # one commits, preventing clobbered results.
        redis_url = flask_app.config.get(
            'CELERY_BROKER_URL', 'redis://localhost:6379/0')
        redis_client = redis.Redis.from_url(redis_url)
        lock_key = f"todo_merge_lock:{user_id}"
        lock = redis_client.lock(
            lock_key,
            timeout=_MERGE_LOCK_TIMEOUT,
            blocking_timeout=_MERGE_LOCK_ACQUIRE_TIMEOUT,
        )
        if not lock.acquire(blocking=True):
            logger.error(
                f"Could not acquire merge lock for user {user_id} "
                f"(node {llm_node_id}), another merge held it too long"
            )
            _update_apply_status(
                llm_node, "failed",
                error="Another todo merge is still running, please retry",
                confirm_node_id=confirm_node_id,
            )
            db.session.commit()
            return

        try:
            _run_merge(
                llm_node, update_summary, user_id, model_id,
                confirm_node_id,
            )
        finally:
            try:
                lock.release()
            except redis.exceptions.LockNotOwnedError:
                logger.warning(
                    f"Merge lock expired before release for user {user_id}")


def _run_merge(llm_node, update_summary, user_id, model_id,
               confirm_node_id):
    """Execute the merge while holding the per-user lock."""
    llm_node_id = llm_node.id

    # Get current todo (fresh read — any prior merge has committed)
    todo = UserTodo.query.filter_by(user_id=user_id).order_by(
        UserTodo.created_at.desc()
    ).first()
    current_todo = todo.get_content() if todo else ""

    # Get merge prompt
    from backend.utils.prompts import get_user_prompt
    merge_prompt = get_user_prompt(user_id, 'orient_apply_todo')

    # Build messages: system=merge_prompt, user=update_summary + current todo
    messages = [
        {
            "role": "system",
            "content": [{"type": "text", "text": merge_prompt}],
        },
        {
            "role": "assistant",
            "content": [{"type": "text", "text": update_summary}],
        },
        {
            "role": "user",
            "content": [{"type": "text", "text": (
                f"Here is the current full todo list:\n\n{current_todo}"
                "\n\nNow apply the changes described above."
            )}],
        },
    ]

    # Call LLM
    api_keys = get_api_keys_for_usage(flask_app.config, "chat")
    try:
        response = LLMProvider.get_completion(
            model_id, messages, api_keys
        )
    except Exception as e:
        logger.error(f"LLM call failed for todo merge: {e}", exc_info=True)
        _update_apply_status(llm_node, "failed", error=str(e),
                            confirm_node_id=confirm_node_id)
        return

    merged_todo = response["content"]
    if not merged_todo or not merged_todo.strip():
        logger.warning(f"LLM returned empty merged todo for node {llm_node_id}")
        _update_apply_status(llm_node, "failed", error="Empty merge result",
                            confirm_node_id=confirm_node_id)
        return

    # Log cost
    input_tokens = response.get("input_tokens", 0)
    output_tokens = response.get("output_tokens", 0)
    cost = calculate_llm_cost_microdollars(model_id, input_tokens, output_tokens)
    db.session.add(APICostLog(
        user_id=user_id,
        model_id=model_id,
        request_type="todo_merge",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_microdollars=cost,
    ))

    # Save new UserTodo
    new_todo = UserTodo(
        user_id=user_id,
        generated_by="voice_session",
        tokens_used=output_tokens,
    )
    new_todo.set_content(merged_todo)
    db.session.add(new_todo)

    # Update apply status
    truncated = response.get("truncated", False)
    if truncated:
        logger.warning(f"Todo merge response truncated for node {llm_node_id}")
    _update_apply_status(
        llm_node, "completed", todo_id=new_todo.id,
        truncated=truncated, confirm_node_id=confirm_node_id)

    db.session.commit()
    logger.info(f"Voice todo merge completed: todo_id={new_todo.id} for user {user_id}, truncated={truncated}")


def _update_apply_status(llm_node, status, error=None, todo_id=None,
                         truncated=False, confirm_node_id=None):
    """Update the apply_status in the LLM node's tool_calls_meta.

    Also updates the confirmation node (where apply_todo_changes lives)
    if confirm_node_id is provided.
    """
    meta = []
    if llm_node.tool_calls_meta:
        try:
            meta = json.loads(llm_node.tool_calls_meta)
        except (json.JSONDecodeError, TypeError):
            meta = []
    for entry in meta:
        if entry.get("name") == "propose_todo":
            entry["apply_status"] = status
            if error:
                entry["apply_error"] = error
            if todo_id:
                entry["todo_id"] = todo_id
            if truncated:
                entry["apply_truncated"] = True
            break
    llm_node.tool_calls_meta = json.dumps(meta)

    # Mirror status onto the confirmation node's apply_todo_changes entry
    if confirm_node_id:
        confirm_node = Node.query.get(confirm_node_id)
        if confirm_node and confirm_node.tool_calls_meta:
            try:
                cmeta = json.loads(confirm_node.tool_calls_meta)
                for entry in cmeta:
                    if entry.get("name") == "apply_todo_changes":
                        entry["apply_status"] = status
                        if error:
                            entry["apply_error"] = error
                        break
                confirm_node.tool_calls_meta = json.dumps(cmeta)
            except (json.JSONDecodeError, TypeError):
                pass

    db.session.flush()

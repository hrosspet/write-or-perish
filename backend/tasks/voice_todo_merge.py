"""
Celery task for applying Voice todo updates to the user's todo list.

Runs entirely in the background without creating visible nodes.
Uses the orient_apply_todo prompt to merge the proposed changes
into the full todo via a single LLM call.

Merges are serialized per user via a Redis lock so concurrent
confirmations don't clobber each other's results.
"""
import json
import re
import redis
from celery.utils.log import get_task_logger

from backend.celery_app import celery, flask_app
from backend.models import Node, UserTodo, User
from backend.extensions import db
from backend.llm_providers import LLMProvider
from backend.utils.api_keys import get_api_keys_for_usage
from backend.utils.cost import llm_cost_log_fields
from backend.models import APICostLog
from backend.utils.refusal_backoff import REFUSED_REF

logger = get_task_logger(__name__)

# Lock timeout: max time a single merge can hold the lock (seconds).
# Matches the LLM request timeout (600s) since large todo merges can
# take that long. Prevents deadlocks if a worker crashes mid-merge.
_MERGE_LOCK_TIMEOUT = 600
# How long to wait for the lock before giving up (seconds).
_MERGE_LOCK_ACQUIRE_TIMEOUT = 600

# For a list with no tasks yet (#410). Every new user starts with no list.
# Given an empty list, the model applied the prompt's rule for completed
# items that are "NOT on the todo list" to a new task and saved it as done.
NO_TASKS_RULE = (
    "Add the items under New Tasks as `- [ ]` and the items under "
    "Completed as `- [x]`; add nothing else."
)
# Sent in place of the todo list when the user has none, or a blank one.
EMPTY_TODO_MESSAGE = "The todo list is empty. " + NO_TASKS_RULE

# Shown on the card when the model's output hit the output cap (#432).
TRUNCATED_MESSAGE = (
    "The todo update was cut off, so nothing was changed. "
    "Please try again.")

# A list item (`- `, `* `, `+ `, `1. `), with the text after its checkbox,
# if it has one, in group 1.
_LIST_ITEM_RE = re.compile(
    r"^[ \t]*(?:[-*+]|\d+[.)])[ \t]+(?:\[[ xX]\](?=[ \t]|$))?(.*)$")


def has_tasks(todo_text):
    """Whether the list has an item with text: `- [ ] call mom`,
    `- [x] call mom` or `- call mom`. The Todo page's Create template
    (headings and empty `- [ ] ` lines) has none."""
    for line in (todo_text or "").splitlines():
        item = _LIST_ITEM_RE.match(line)
        if item and item.group(1).strip():
            return True
    return False


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

        from backend.utils.spend import user_is_capped
        if user_is_capped(user_id):
            logger.warning(
                "User %s is spend-capped; skipping voice todo merge", user_id)
            _update_apply_status(
                llm_node, "failed", error="Monthly spend limit reached",
                confirm_node_id=confirm_node_id)
            db.session.commit()
            return

        update_summary = llm_node.get_content()
        if not update_summary:
            logger.error(f"LLM node {llm_node_id} has no content")
            _update_apply_status(llm_node, "failed", error="No update summary",
                                confirm_node_id=confirm_node_id)
            db.session.commit()
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


def build_merge_messages(merge_prompt, update_summary, current_todo):
    """The merge call's messages: system=merge_prompt, assistant=the
    proposal, user=the current todo list. Also used by
    backend/scripts/compare_todo_merge_models.py to rebuild past merges."""
    if has_tasks(current_todo):
        todo_message = (
            f"Here is the current full todo list:\n\n{current_todo}"
            "\n\nNow apply the changes described above."
        )
    elif current_todo and current_todo.strip():
        # Headings but no tasks yet, e.g. the Todo page's Create template.
        todo_message = (
            f"Here is the current full todo list:\n\n{current_todo}"
            f"\n\n{NO_TASKS_RULE}"
            "\n\nNow apply the changes described above."
        )
    else:
        todo_message = (
            EMPTY_TODO_MESSAGE
            + "\n\nNow apply the changes described above."
        )
    return [
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
            "content": [{"type": "text", "text": todo_message}],
        },
    ]


def _run_merge(llm_node, update_summary, user_id, model_id,
               confirm_node_id):
    """Execute the merge while holding the per-user lock."""
    llm_node_id = llm_node.id

    # AI may not read the todo list or the proposal: no model call. Asked
    # here, under the lock, because the list can change after the merge
    # was started (an earlier merge saves one with the account default).
    from backend.routes.todo import todo_merge_refusal
    refusal = todo_merge_refusal(user_id, llm_node)
    if refusal is not None:
        logger.info(
            f"Todo merge for node {llm_node_id} refused: AI usage keeps "
            f"its inputs away from AI (user {user_id})")
        _update_apply_status(llm_node, "failed", error=refusal,
                             confirm_node_id=confirm_node_id)
        db.session.commit()
        return

    # Get current todo (fresh read — any prior merge has committed)
    todo = UserTodo.query.filter_by(user_id=user_id).order_by(
        UserTodo.created_at.desc()
    ).first()
    current_todo = todo.get_content() if todo else ""

    # Get merge prompt
    from backend.utils.prompts import get_user_prompt
    merge_prompt = get_user_prompt(user_id, 'orient_apply_todo')

    # Build messages: system=merge_prompt, user=update_summary + current todo
    messages = build_merge_messages(merge_prompt, update_summary, current_todo)

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
        db.session.commit()
        return

    merged_todo = response["content"]
    truncated = response.get("truncated", False)
    empty = not merged_todo or not merged_todo.strip()

    # Log cost, also for a result that is thrown away: the call was billed
    # (#368). Any cut-off result is marked like the other background jobs'
    # refusals: it hit the output cap and produced nothing usable.
    output_tokens = response.get("output_tokens", 0)
    db.session.add(APICostLog(
        user_id=user_id,
        model_id=model_id,
        request_type="todo_merge",
        request_ref=(REFUSED_REF if truncated else None),
        **llm_cost_log_fields(model_id, response),
    ))

    if empty:
        logger.warning(
            f"LLM returned empty merged todo for node {llm_node_id} "
            f"(truncated={truncated}, output_tokens={output_tokens})")
        _update_apply_status(llm_node, "failed", error="Empty merge result",
                            confirm_node_id=confirm_node_id)
        db.session.commit()
        return

    # A cut-off list (output cap, a repetition loop) is missing items:
    # saving it would replace the user's list with a partial one (#432).
    if truncated:
        logger.warning(
            f"Todo merge for node {llm_node_id} was cut off; nothing "
            f"saved (output_tokens={output_tokens})")
        _update_apply_status(llm_node, "failed", error=TRUNCATED_MESSAGE,
                             confirm_node_id=confirm_node_id)
        db.session.commit()
        return

    # Save new UserTodo
    merge_user = User.query.get(user_id)
    new_todo = UserTodo(
        user_id=user_id,
        generated_by="voice_session",
        tokens_used=output_tokens,
        # ai_usage follows the user's global default (#191).
        ai_usage=merge_user.default_ai_usage if merge_user else "chat",
    )
    new_todo.set_content(merged_todo)
    db.session.add(new_todo)
    # Assigns new_todo.id, so the proposal's tool_calls_meta records which
    # todo version this merge produced (#410).
    db.session.flush()

    # Update apply status
    _update_apply_status(
        llm_node, "completed", todo_id=new_todo.id,
        confirm_node_id=confirm_node_id)

    db.session.commit()
    logger.info(f"Voice todo merge completed: todo_id={new_todo.id} for user {user_id}")


def _update_apply_status(llm_node, status, error=None, todo_id=None,
                         confirm_node_id=None):
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

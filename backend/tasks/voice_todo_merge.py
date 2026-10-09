"""
Celery task for applying Voice todo updates to the user's todo list.

Runs entirely in the background without creating visible nodes.
Uses the orient_apply_todo prompt: the conversation's model replies with
edits to the full todo (the artifact tool's {old_text, new_text} format),
which code applies to the newest list (utils/todo_merge_edits.py, #234).

Merges are serialized per user via a Redis lock so concurrent
confirmations don't clobber each other's results. The save also takes the
user's todo lock, which every todo writer takes (utils/todo_lock.py,
#477), and builds on the list as it is then: a tick or an editor Save made
while the model worked stays.
"""
import json
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
from backend.utils.todo_lock import TodoBusy, lock_user_todo
from backend.utils.todo_merge_edits import (
    FAILURE_EMPTY, FAILURE_TRUNCATED, REPLY_FORMAT, MergeRun, has_tasks,
    rebase_merge, run_todo_merge)

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
# A failed merge leaves its proposal applicable (#434): the card shows
# "Apply again" next to the message.
TRUNCATED_MESSAGE = (
    "The todo update was cut off, so nothing was changed.")
EMPTY_RESULT_MESSAGE = "Empty merge result"
# Shown on the card when the model's edits were refused twice (#234): an
# anchor not found or not unique, a full rewrite of a list with tasks, an
# existing line changed, or an existing sub-item moved under a new line.
# Nothing was saved.
EDITS_FAILED_MESSAGE = (
    "The todo update couldn't be applied to your list, so nothing was "
    "changed.")
# Shown on the card when the list changed while the model worked (a tick,
# an editor Save) and the update's edits no longer fit it (#477). Nothing
# was saved; the card offers "Apply again", which runs on the newest list.
LIST_CHANGED_MESSAGE = (
    "Your todo list changed while this update was being applied, and the "
    "update no longer fits it, so nothing was changed.")
# Shown on the card when another write of the list held the user's todo
# lock past TODO_LOCK_WAIT_SECONDS (#477).
LOCK_BUSY_MESSAGE = (
    "Your todo list was busy saving another change, so nothing was "
    "changed.")


@celery.task(bind=True)
def apply_voice_todo(self, llm_node_id: int, model_id: str, user_id: int,
                     confirm_node_id: int = None):
    """
    Merge the Voice todo update into the user's full todo list.

    1. Read update summary from the LLM node's text content
    2. Get the current user todo
    3. Call LLM with orient_apply_todo prompt for edits, apply them to
       the todo and check the result (utils/todo_merge_edits.py)
    4. Save as new UserTodo, under the user's todo lock, on the newest
       list (the edits applied again if it changed meanwhile, #477)
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
            _merge_failed(
                llm_node, user_id, "Monthly spend limit reached",
                confirm_node_id)
            db.session.commit()
            return

        update_summary = llm_node.get_content()
        if not update_summary:
            logger.error(f"LLM node {llm_node_id} has no content")
            _merge_failed(llm_node, user_id, "No update summary",
                          confirm_node_id)
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
            _merge_failed(
                llm_node, user_id,
                "Another todo merge is still running, please retry",
                confirm_node_id,
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
    """The merge call's messages: system=merge_prompt followed by the
    reply format, assistant=the proposal, user=the current todo list.
    Also used by backend/scripts/compare_todo_merge_models.py to rebuild
    past merges.

    REPLY_FORMAT (#234) is the parser's contract, so it is added in code
    after whatever merge prompt the account has: the file default or one
    the user saved, whose text is sent unchanged (the user's edit wins).
    One text block: the Anthropic conversion reads a system message's
    first block only."""
    system_text = f"{(merge_prompt or '').rstrip()}\n\n{REPLY_FORMAT}"
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
            "content": [{"type": "text", "text": system_text}],
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
    from backend.routes.todo import todo_merge_refusal, todo_revision
    refusal = todo_merge_refusal(user_id, llm_node)
    if refusal is not None:
        logger.info(
            f"Todo merge for node {llm_node_id} refused: AI usage keeps "
            f"its inputs away from AI (user {user_id})")
        _merge_failed(llm_node, user_id, refusal, confirm_node_id)
        db.session.commit()
        return

    # Get current todo (fresh read — any prior merge has committed)
    todo = UserTodo.query.filter_by(user_id=user_id).order_by(
        UserTodo.created_at.desc()
    ).first()
    current_todo = todo.get_content() if todo else ""
    # Which version the merge read, to see at save time whether the list
    # changed while the model worked (#477).
    read_revision = todo_revision(todo) if todo else None

    # Get merge prompt
    from backend.utils.prompts import get_user_prompt
    merge_prompt = get_user_prompt(user_id, 'orient_apply_todo')

    # Build messages: system=merge_prompt, user=update_summary + current todo
    messages = build_merge_messages(merge_prompt, update_summary, current_todo)

    # Call the model for edits and apply them (one retry after a refused
    # reply). On the conversation's model: a user's provider is never
    # switched, not even when a merge fails.
    api_keys = get_api_keys_for_usage(flask_app.config, "chat")
    run = MergeRun()
    try:
        run_todo_merge(LLMProvider, model_id, messages, api_keys,
                       current_todo, run)
    except Exception as e:
        logger.error(f"LLM call failed for todo merge: {e}", exc_info=True)
        # Calls made before the failing one were billed.
        _log_merge_costs(user_id, model_id, run)
        _merge_failed(llm_node, user_id, str(e), confirm_node_id)
        db.session.commit()
        return

    # Log cost of every call, also of a result that is thrown away: each
    # was billed (#368).
    _log_merge_costs(user_id, model_id, run)
    # Counts only: the replies and refusal reasons hold list text.
    logger.info(f"Todo merge for node {llm_node_id}: {run.stats()}")
    if run.kept_lines_failures:
        logger.warning(
            f"Todo merge for node {llm_node_id}: the kept-lines check "
            f"refused {run.kept_lines_failures} of {len(run.responses)} "
            "replies")

    if run.failure is not None:
        output_tokens = sum(r.get("output_tokens", 0) or 0
                            for r in run.responses)
        if run.failure == FAILURE_EMPTY:
            logger.warning(
                f"LLM returned an empty todo merge for node {llm_node_id} "
                f"(output_tokens={output_tokens})")
            error = EMPTY_RESULT_MESSAGE
        elif run.failure == FAILURE_TRUNCATED:
            # A cut-off reply (output cap, a repetition loop) may be
            # missing edits: nothing is saved (#432).
            logger.warning(
                f"Todo merge for node {llm_node_id} was cut off; nothing "
                f"saved (output_tokens={output_tokens})")
            error = TRUNCATED_MESSAGE
        else:
            logger.warning(
                f"Todo merge for node {llm_node_id} failed after "
                f"{len(run.responses)} replies ({run.failure}); nothing "
                "saved")
            error = EDITS_FAILED_MESSAGE
        _merge_failed(llm_node, user_id, error, confirm_node_id)
        db.session.commit()
        return

    merged = _merged_on_newest_list(
        llm_node, user_id, confirm_node_id, run, current_todo, read_revision)
    if merged is None:
        return

    # Save new UserTodo (under the user's todo lock, which the commit below
    # releases)
    merge_user = User.query.get(user_id)
    new_todo = UserTodo(
        user_id=user_id,
        generated_by="voice_session",
        # The applied reply's output tokens, so the version still pairs
        # with one api_cost_log row (scripts/compare_todo_merge_models.py);
        # a refused first reply has its own row.
        tokens_used=run.responses[-1].get("output_tokens", 0) or 0,
        # ai_usage follows the user's global default (#191).
        ai_usage=merge_user.default_ai_usage if merge_user else "chat",
    )
    new_todo.set_content(merged)
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


def _merged_on_newest_list(llm_node, user_id, confirm_node_id, run,
                           current_todo, read_revision):
    """The list to save, built on the newest list, with the user's todo
    lock held until the caller commits; or None when the merge failed
    (recorded and committed here).

    The list may have changed while the model worked (#477): a tick, the
    row "+", quick-add, an editor Save or a revert. Then the merge's
    edits are applied to the newest list (rebase_merge), so the user's
    change stays. When they no longer fit, nothing is saved and the
    proposal can be applied again (the user's edit wins)."""
    from backend.routes.todo import newest_todo, todo_revision
    llm_node_id = llm_node.id
    # The calls were billed whatever happens next: their cost rows are
    # committed before the wait for the lock, which can roll back.
    db.session.commit()
    try:
        lock_user_todo(user_id)
    except TodoBusy:
        logger.warning(
            f"Todo merge for node {llm_node_id}: the user's todo lock "
            f"stayed taken; nothing saved (user {user_id})")
        _merge_failed(llm_node, user_id, LOCK_BUSY_MESSAGE, confirm_node_id)
        db.session.commit()
        return None

    newest = newest_todo(user_id)
    if (todo_revision(newest) if newest else None) == read_revision:
        return run.merged
    merged = rebase_merge(
        run, current_todo, newest.get_content() if newest else "")
    if merged is None:
        logger.warning(
            f"Todo merge for node {llm_node_id}: the list changed during "
            f"the merge and its edits no longer fit; nothing saved "
            f"(user {user_id})")
        _merge_failed(llm_node, user_id, LIST_CHANGED_MESSAGE,
                      confirm_node_id)
        db.session.commit()
        return None
    logger.info(
        f"Todo merge for node {llm_node_id}: the list changed during the "
        "merge; its edits were applied to the newest list")
    return merged


def _log_merge_costs(user_id, model_id, run):
    """One api_cost_log row per model call of the merge. A cut-off reply
    is marked like the other background jobs' refusals: it hit the output
    cap and produced nothing usable."""
    for response in run.responses:
        db.session.add(APICostLog(
            user_id=user_id,
            model_id=model_id,
            request_type="todo_merge",
            request_ref=(REFUSED_REF if response.get("truncated")
                         else None),
            **llm_cost_log_fields(model_id, response),
        ))


def _merge_failed(llm_node, user_id, error, confirm_node_id):
    """Record a failed merge. Its proposal becomes applicable again (#434):
    the pending draft the start removed comes back, unless the user has a
    newer todo proposal pending, and ``retryable`` tells the web and iPhone
    cards whether to show "Apply again" next to the error."""
    from backend.routes.todo import restore_todo_draft
    retryable = restore_todo_draft(llm_node, user_id)
    _update_apply_status(llm_node, "failed", error=error,
                         confirm_node_id=confirm_node_id,
                         retryable=retryable)


def _update_apply_status(llm_node, status, error=None, todo_id=None,
                         confirm_node_id=None, retryable=None):
    """Update the apply_status in the LLM node's tool_calls_meta.

    Also updates the confirmation node (where apply_todo_changes lives)
    if confirm_node_id is provided. A completed merge drops the error of
    an earlier failed one.
    """
    meta = []
    if llm_node.tool_calls_meta:
        try:
            meta = json.loads(llm_node.tool_calls_meta)
        except (json.JSONDecodeError, TypeError):
            meta = []
    for entry in meta:
        if entry.get("name") == "propose_todo":
            _set_apply_status(entry, status, error)
            if todo_id:
                entry["todo_id"] = todo_id
            if retryable is not None and status != "completed":
                entry["retryable"] = retryable
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
                        _set_apply_status(entry, status, error)
                        break
                confirm_node.tool_calls_meta = json.dumps(cmeta)
            except (json.JSONDecodeError, TypeError):
                pass

    db.session.flush()


def _set_apply_status(entry, status, error):
    entry["apply_status"] = status
    if error:
        entry["apply_error"] = error
    if status == "completed":
        entry.pop("apply_error", None)
        entry.pop("retryable", None)

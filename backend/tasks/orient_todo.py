"""
Celery task for applying Orient session updates to a user's todo list.

After the Orient LLM produces an update (completed tasks, new tasks, etc.),
this task chains a second LLM call that merges the update into the user's
full todo. The merge prompt and LLM response are added as nodes in the
conversation tree so the whole flow is visible in the Log.
"""
from celery import chain as celery_chain
from celery.utils.log import get_task_logger

from backend.celery_app import celery, flask_app
from backend.models import Node, UserTodo
from backend.extensions import db

logger = get_task_logger(__name__)


@celery.task(bind=True)
def run_orient_todo_chain(self, merge_prompt_node_id: int,
                          merge_llm_node_id: int,
                          merge_model: str, user_id: int):
    """
    Run LLM generation on pre-created merge nodes and save the result.

    The merge nodes are created synchronously in the HTTP handler so the
    frontend can learn the final node ID for correct thread continuation.
    """
    with flask_app.app_context():
        from backend.tasks.llm_completion import generate_llm_response

        logger.info(
            f"Running orient todo chain: prompt={merge_prompt_node_id}, "
            f"llm={merge_llm_node_id} (model={merge_model})"
        )

        celery_chain(
            generate_llm_response.si(
                merge_prompt_node_id, merge_llm_node_id,
                merge_model, user_id
            ),
            save_orient_todo.si(merge_llm_node_id, user_id)
        ).apply_async()


@celery.task
def save_orient_todo(llm_node_id: int, user_id: int):
    """Read the merged todo from the LLM node and save as a new UserTodo."""
    with flask_app.app_context():
        llm_node = Node.query.get(llm_node_id)
        if not llm_node:
            logger.error(f"Merge LLM node {llm_node_id} not found")
            return

        if llm_node.llm_task_status != 'completed':
            logger.error(
                f"Merge LLM node {llm_node_id} status is "
                f"{llm_node.llm_task_status}, expected completed"
            )
            return

        content = llm_node.get_content()
        if not content or not content.strip():
            logger.warning(f"Merge LLM node {llm_node_id} has no content")
            return

        todo = UserTodo(
            user_id=user_id,
            generated_by="orient_session",
            tokens_used=0,
        )
        todo.set_content(content)
        db.session.add(todo)
        db.session.commit()

        logger.info(f"Saved merged todo (id={todo.id}) for user {user_id}")

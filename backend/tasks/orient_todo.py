"""
Celery task for applying Orient session updates to a user's todo list.

After the Orient LLM produces an update (completed tasks, new tasks, etc.),
this task chains a second LLM call that merges the update into the user's
full todo. The merge prompt and LLM response are added as nodes in the
conversation tree so the whole flow is visible in the Log.
"""
import os
from celery import chain as celery_chain
from celery.utils.log import get_task_logger

from backend.celery_app import celery, flask_app
from backend.models import Node, User, UserTodo
from backend.extensions import db

logger = get_task_logger(__name__)

PROMPT_FILE = os.path.join(
    os.path.dirname(__file__), '..', 'prompts', 'orient_apply_todo.txt'
)


def _get_merge_prompt():
    with open(PROMPT_FILE, 'r') as f:
        return f.read()


@celery.task(bind=True)
def apply_orient_todo(self, llm_node_id: int, user_id: int):
    """
    Create merge nodes in the conversation tree and chain LLM generation.

    Appends a user node (merge prompt) and an LLM placeholder node to the
    orient conversation, then kicks off generate_llm_response followed by
    save_orient_todo.
    """
    with flask_app.app_context():
        orient_llm_node = Node.query.get(llm_node_id)
        if not orient_llm_node:
            logger.error(f"Orient LLM node {llm_node_id} not found")
            return

        # Use the same model the user selected for the orient session
        merge_model = orient_llm_node.llm_model

        # User node with merge prompt, parented to the orient LLM response
        merge_prompt_node = Node(
            user_id=user_id,
            parent_id=llm_node_id,
            node_type="user",
            privacy_level="private",
            ai_usage="chat",
        )
        merge_prompt_node.set_content(_get_merge_prompt())
        db.session.add(merge_prompt_node)
        db.session.flush()

        # LLM placeholder for the merged todo
        llm_user = User.query.filter_by(username=merge_model).first()
        if not llm_user:
            llm_user = User(
                twitter_id=f"llm-{merge_model}", username=merge_model
            )
            db.session.add(llm_user)
            db.session.flush()

        merge_llm_node = Node(
            user_id=llm_user.id,
            parent_id=merge_prompt_node.id,
            node_type="llm",
            llm_model=merge_model,
            llm_task_status="pending",
            privacy_level="private",
            ai_usage="chat",
        )
        merge_llm_node.set_content("[Merging todo...]")
        db.session.add(merge_llm_node)
        db.session.commit()

        logger.info(
            f"Created merge nodes: prompt={merge_prompt_node.id}, "
            f"llm={merge_llm_node.id} (model={merge_model})"
        )

        # Chain: generate merged todo, then save it as UserTodo
        from backend.tasks.llm_completion import generate_llm_response

        celery_chain(
            generate_llm_response.si(
                merge_prompt_node.id, merge_llm_node.id,
                merge_model, user_id
            ),
            save_orient_todo.si(merge_llm_node.id, user_id)
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

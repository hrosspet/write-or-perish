"""Shared factory for creating LLM placeholder nodes."""

from backend.models import Node, User
from backend.extensions import db


def create_llm_placeholder(parent_node_id, model_id, human_owner_id,
                           privacy_level="private", ai_usage="chat",
                           placeholder_text="[LLM response generation pending...]",
                           enqueue=True):
    """Create an LLM placeholder node, optionally enqueue generation task.

    Returns (llm_node, task_id) -- task_id is None if enqueue=False.
    """
    llm_user = User.query.filter_by(username=model_id).first()
    if not llm_user:
        llm_user = User(twitter_id=f"llm-{model_id}", username=model_id)
        db.session.add(llm_user)
        db.session.flush()

    from backend.utils.tokens import approximate_token_count

    llm_node = Node(
        user_id=llm_user.id,
        parent_id=parent_node_id,
        human_owner_id=human_owner_id,
        node_type="llm",
        llm_model=model_id,
        llm_task_status="pending",
        privacy_level=privacy_level,
        ai_usage=ai_usage,
        token_count=approximate_token_count(placeholder_text),
    )
    llm_node.set_content(placeholder_text)
    db.session.add(llm_node)
    db.session.commit()

    task_id = None
    if enqueue:
        from backend.tasks.llm_completion import generate_llm_response
        task = generate_llm_response.delay(
            parent_node_id, llm_node.id, model_id, human_owner_id
        )
        llm_node.llm_task_id = task.id
        db.session.commit()
        task_id = task.id

    return llm_node, task_id

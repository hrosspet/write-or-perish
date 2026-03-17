"""Utility for attaching context artifact versions to system nodes."""

from backend.extensions import db
from backend.models import (
    NodeContextArtifact, UserProfile, UserTodo,
)


def attach_context_artifacts(node_id, user_id, prompt_record=None):
    """Attach current context artifact versions to a system node.

    Creates NodeContextArtifact rows for:
      - prompt  (if *prompt_record* supplied)
      - profile (latest with ai_usage in ('chat', 'train'))
      - todo    (latest with ai_usage in ('chat', 'train'))

    Should be called right after the system node is flushed (so node_id
    is valid) and before the session is committed.
    """
    if prompt_record is not None:
        db.session.add(NodeContextArtifact(
            node_id=node_id,
            artifact_type="prompt",
            artifact_id=prompt_record.id,
        ))

    # Latest profile that the AI is allowed to see
    profile = (
        UserProfile.query
        .filter_by(user_id=user_id)
        .filter(UserProfile.ai_usage.in_(["chat", "train"]))
        .order_by(UserProfile.created_at.desc())
        .first()
    )
    if profile is not None:
        db.session.add(NodeContextArtifact(
            node_id=node_id,
            artifact_type="profile",
            artifact_id=profile.id,
        ))

    # Latest todo that the AI is allowed to see
    todo = (
        UserTodo.query
        .filter_by(user_id=user_id)
        .filter(UserTodo.ai_usage.in_(["chat", "train"]))
        .order_by(UserTodo.created_at.desc())
        .first()
    )
    if todo is not None:
        db.session.add(NodeContextArtifact(
            node_id=node_id,
            artifact_type="todo",
            artifact_id=todo.id,
        ))

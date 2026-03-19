"""Utility for attaching context artifact versions to system nodes."""

from backend.extensions import db
from backend.models import (
    NodeContextArtifact, UserProfile, UserRecentContext, UserTodo,
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

    # Latest recent context summary for the current profile
    profile_id = profile.id if profile is not None else None
    rc_query = UserRecentContext.query.filter_by(user_id=user_id)
    if profile_id is not None:
        rc_query = rc_query.filter_by(profile_id=profile_id)
    else:
        rc_query = rc_query.filter(UserRecentContext.profile_id.is_(None))
    recent_ctx = rc_query.order_by(
        UserRecentContext.created_at.desc()
    ).first()
    if recent_ctx is not None:
        db.session.add(NodeContextArtifact(
            node_id=node_id,
            artifact_type="recent_context",
            artifact_id=recent_ctx.id,
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

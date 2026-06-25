"""Utility for attaching context artifact versions to system nodes."""
import re

from backend.extensions import db
from backend.models import (
    NodeContextArtifact, UserProfile, UserRecentContext, UserTodo,
    UserArtifact,
)
from backend.utils.privacy import AI_ALLOWED

# Mapping from placeholder name to artifact_type in NodeContextArtifact.
# ai_preferences folded into the UserArtifact model (#158 Slice 5), so its
# placeholder pins the multi-row user_artifact type like memory/scratchpad.
PLACEHOLDER_TO_ARTIFACT = {
    'user_profile': 'profile',
    'user_todo': 'todo',
    'user_recent': 'recent_context',
    'user_ai_preferences': 'user_artifact',
    'user_memory': 'user_artifact',
    'user_scratchpad': 'user_artifact',
    'user_intentions': 'user_artifact',
    'user_artifacts_index': 'user_artifact',
}

_PLACEHOLDER_RE = re.compile(
    r'\{(' + '|'.join(PLACEHOLDER_TO_ARTIFACT.keys()) + r')\}'
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
        .filter(UserProfile.ai_usage.in_(AI_ALLOWED))
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
        .filter(UserTodo.ai_usage.in_(AI_ALLOWED))
        .order_by(UserTodo.created_at.desc())
        .first()
    )
    if todo is not None:
        db.session.add(NodeContextArtifact(
            node_id=node_id,
            artifact_type="todo",
            artifact_id=todo.id,
        ))

    # Latest version of every user artifact the AI is allowed to see — one
    # row per kind (#158). Since Slice 5 this includes 'ai_preferences'
    # (folded into the UserArtifact model), so it's pinned here too. There's
    # no dedicated ai_preferences pin anymore — that would double-pin once the
    # artifact exists. Pre-backfill (no artifact yet),
    # get_user_ai_preferences_content falls back to the legacy
    # UserAIPreferences row (#219).
    for artifact in UserArtifact.latest_per_kind(user_id).values():
        if artifact.ai_usage in AI_ALLOWED:
            db.session.add(NodeContextArtifact(
                node_id=node_id,
                artifact_type="user_artifact",
                artifact_id=artifact.id,
            ))


def sync_context_artifacts(node_id, user_id, content):
    """Sync context artifact rows to match placeholders in content.

    Adds artifacts for placeholders that are present but missing,
    removes artifacts for placeholders that were removed.
    Leaves the prompt artifact untouched.
    """
    # Detect which artifact types the content needs
    needed = set()
    for match in _PLACEHOLDER_RE.finditer(content):
        artifact_type = PLACEHOLDER_TO_ARTIFACT[match.group(1)]
        needed.add(artifact_type)

    # Get existing non-prompt artifacts. 'user_artifact' is a multi-row
    # type (one row per artifact kind), so collect rows in lists.
    existing = NodeContextArtifact.query.filter_by(
        node_id=node_id
    ).filter(
        NodeContextArtifact.artifact_type != 'prompt'
    ).all()
    existing_types = {}
    for row in existing:
        existing_types.setdefault(row.artifact_type, []).append(row)

    # Remove artifacts no longer referenced
    for artifact_type, rows in existing_types.items():
        if artifact_type not in needed:
            for row in rows:
                db.session.delete(row)

    # Add missing artifacts
    for artifact_type in needed:
        if artifact_type in existing_types:
            continue
        if artifact_type == 'user_artifact':
            for artifact in UserArtifact.latest_per_kind(user_id).values():
                if artifact.ai_usage in AI_ALLOWED:
                    db.session.add(NodeContextArtifact(
                        node_id=node_id,
                        artifact_type='user_artifact',
                        artifact_id=artifact.id,
                    ))
            continue
        artifact_id = _resolve_latest_artifact(
            artifact_type, user_id
        )
        if artifact_id is not None:
            db.session.add(NodeContextArtifact(
                node_id=node_id,
                artifact_type=artifact_type,
                artifact_id=artifact_id,
            ))


def _resolve_latest_artifact(artifact_type, user_id):
    """Find the latest artifact ID for a given type and user."""
    if artifact_type == 'profile':
        row = (
            UserProfile.query
            .filter_by(user_id=user_id)
            .filter(UserProfile.ai_usage.in_(AI_ALLOWED))
            .order_by(UserProfile.created_at.desc())
            .first()
        )
    elif artifact_type == 'todo':
        row = (
            UserTodo.query
            .filter_by(user_id=user_id)
            .filter(UserTodo.ai_usage.in_(AI_ALLOWED))
            .order_by(UserTodo.created_at.desc())
            .first()
        )
    elif artifact_type == 'recent_context':
        profile = (
            UserProfile.query
            .filter_by(user_id=user_id)
            .filter(UserProfile.ai_usage.in_(AI_ALLOWED))
            .order_by(UserProfile.created_at.desc())
            .first()
        )
        profile_id = profile.id if profile else None
        q = UserRecentContext.query.filter_by(user_id=user_id)
        if profile_id is not None:
            q = q.filter_by(profile_id=profile_id)
        else:
            q = q.filter(UserRecentContext.profile_id.is_(None))
        row = q.order_by(UserRecentContext.created_at.desc()).first()
    else:
        # ai_preferences is a user_artifact now (#158 Slice 5), handled by the
        # user_artifact branch in sync_context_artifacts, not here.
        return None
    return row.id if row else None

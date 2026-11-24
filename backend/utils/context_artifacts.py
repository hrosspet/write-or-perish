"""Utility for attaching context artifact versions to system nodes."""
import re

from backend.extensions import db
from backend.models import (
    Node, NodeContextArtifact, UserProfile, UserRecentContext, UserTodo,
    UserArtifact, UserPrompt,
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

# The artifact rows a NodeContextArtifact.artifact_type points at.
_ROW_MODELS = {
    'prompt': UserPrompt,
    'profile': UserProfile,
    'todo': UserTodo,
    'recent_context': UserRecentContext,
    'user_artifact': UserArtifact,
}


def pinned_row(artifact_type, artifact_id):
    """The artifact version a pin of *artifact_type* points at, or None.
    Each has a ``user_id``: whose version it is. A user artifact the
    user deleted (#603) is None, as if the pin were gone."""
    if artifact_type == 'user_artifact':
        return UserArtifact.get_live(artifact_id)
    model = _ROW_MODELS.get(artifact_type)
    return model.query.get(artifact_id) if model is not None else None


def attach_context_artifacts(node_id, user_id, prompt_record=None):
    """Attach context artifact versions referenced by a system node (#191).

    Pins the prompt row itself (if *prompt_record* supplied) plus one row
    per artifact type the prompt content actually references via
    placeholders — a pin means "this exact version is embedded in this
    node's context". Types the agent pulls on demand are NOT pinned here:
    the todo (#158 Slice 3) and non-inline artifacts, both read with
    read_artifact (#218), get pinned by the retrieval loop on the node
    whose turn fetched them.

    Should be called right after the system node is flushed (so node_id
    is valid) and before the session is committed.
    """
    node = Node.query.get(node_id)
    if prompt_record is not None:
        db.session.add(NodeContextArtifact(
            node_id=node_id,
            artifact_type="prompt",
            artifact_id=prompt_record.id,
        ))
        # Stamp the session kind on the node itself so it outlives the
        # reference: a per-thread prompt edit deletes the row (nodes.py
        # detach_prompt) and the thread must stay agentic afterwards.
        if node is not None:
            node.prompt_key = prompt_record.prompt_key
        content = prompt_record.get_content() or ""
    else:
        content = (node.get_content() or "") if node else ""

    sync_context_artifacts(node_id, user_id, content)


def _placeholder_types(text):
    """The pin types the placeholders in *text* need."""
    return {PLACEHOLDER_TO_ARTIFACT[m.group(1)]
            for m in _PLACEHOLDER_RE.finditer(text or "")}


def sync_context_artifacts(node_id, user_id, content, old_content=None):
    """Sync context artifact rows to match placeholders in content.

    Adds artifacts for placeholders that are present but missing,
    removes artifacts for placeholders that were removed.
    Leaves the prompt artifact untouched.

    *old_content*: the node's text before an edit. Given, only the pin
    types whose placeholders it had and *content* no longer has are
    removed; a pin that no placeholder in the text accounts for stays
    (#786 review: the versions a reply's turn read with a tool, pinned
    on the reply by the retrieval loop, whose text names no placeholder).
    None, for a new node: every pin no placeholder needs is removed.
    """
    needed = _placeholder_types(content)

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
    if old_content is None:
        gone = set(existing_types) - needed
    else:
        gone = _placeholder_types(old_content) - needed
    for artifact_type in gone:
        for row in existing_types.get(artifact_type, ()):
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
        # Never a large build's window or group (#545).
        from backend.utils.profile_versions import current_profile_query
        row = (
            current_profile_query(user_id)
            .filter(UserProfile.ai_usage.in_(AI_ALLOWED))
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
        from backend.utils.profile_versions import current_profile_query
        profile = (
            current_profile_query(user_id)
            .filter(UserProfile.ai_usage.in_(AI_ALLOWED))
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

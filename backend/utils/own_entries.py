"""Whether a user has written anything in Loore yet (#391, #392).

Until they have, the homepage, Voice and Text screens ask the welcome
question instead of "What's on your mind?", and the updates modal holds
back developer polls. Both read this one helper.

An entry counts when the user wrote it in Loore: typed, recorded, or an
uploaded recording. These do not count:

* imports (Twitter / Community Archive / X pre-fill, ChatGPT, Claude,
  Markdown / Obsidian): ``origin`` is set, or, for imports from before
  that column, ``source_key`` is;
* LLM nodes: authored by the model's placeholder account and typed
  ``llm``;
* the prompt node a Voice / Text / Read session starts with: Loore
  creates it, the user never wrote it (``prompt_key`` stamp, or a linked
  prompt version for roots made before the stamp, the same test search
  uses);
* soft-deleted nodes.

One EXISTS query on indexed columns: no node is loaded and nothing is
decrypted.
"""
from sqlalchemy import exists, select

from backend.extensions import db
from backend.models import Node, NodeContextArtifact


def has_own_entries(user_id):
    """True once *user_id* has at least one entry written in Loore."""
    is_prompt_node = exists().where(
        NodeContextArtifact.node_id == Node.id,
        NodeContextArtifact.artifact_type == "prompt",
    )
    own_entry = select(Node.id).where(
        Node.user_id == user_id,
        Node.node_type == "user",
        Node.origin.is_(None),
        Node.source_key.is_(None),
        Node.prompt_key.is_(None),
        Node.deleted_at.is_(None),
        ~is_prompt_node,
    )
    return bool(db.session.execute(select(own_entry.exists())).scalar())

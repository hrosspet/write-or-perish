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

One query: no node is loaded and nothing is decrypted. On PostgreSQL the
planner treats ``user_id = X`` and ``origin IS NULL`` as independent. It
expects many matches, so it can scan ``ix_node_origin`` or the whole
table and filter on ``user_id`` last, which for a user whose rows are all
imports means reading other users' rows. The query therefore selects the
user's rows first, in a materialized CTE that PostgreSQL fills through
``ix_node_user_id``, and applies the other conditions to those rows. Its
cost is bounded by that user's own row count (a large import with no
entry reads all of it). MATERIALIZED must stay: from PostgreSQL 12 a CTE
used once is otherwise folded into the outer query, which brings the old
plan back. A partial index on the predicate was tried and did not hold:
the planner dropped it once the visibility map went stale.
"""
from sqlalchemy import exists, select

from backend.extensions import db
from backend.models import Node, NodeContextArtifact


def own_entries_query(user_id):
    """The statement behind :func:`has_own_entries` (a tests hook: it is
    compiled for PostgreSQL to check the CTE keeps its MATERIALIZED prefix)."""
    mine = (
        select(Node.id, Node.node_type, Node.origin, Node.source_key,
               Node.prompt_key, Node.deleted_at)
        .where(Node.user_id == user_id)
        .cte("mine")
        .prefix_with("MATERIALIZED", dialect="postgresql")
    )
    is_prompt_node = exists().where(
        NodeContextArtifact.node_id == mine.c.id,
        NodeContextArtifact.artifact_type == "prompt",
    )
    own_entry = select(mine.c.id).where(
        mine.c.node_type == "user",
        mine.c.origin.is_(None),
        mine.c.source_key.is_(None),
        mine.c.prompt_key.is_(None),
        mine.c.deleted_at.is_(None),
        ~is_prompt_node,
    )
    return select(own_entry.exists())


def has_own_entries(user_id):
    """True once *user_id* has at least one entry written in Loore."""
    return bool(db.session.execute(own_entries_query(user_id)).scalar())

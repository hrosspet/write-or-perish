"""Apply a privacy / AI-usage change to a node's replies.

The edit dialog offers "this node and all my replies" when the edited
node has replies and a setting changed. Same ownership rule as editing
the replies one by one (can_user_edit_node): the user's own nodes, and
LLM nodes they are the human owner of. Other users' replies are left
alone but walked through, since the user's own replies may sit under
them — the same promise soft_delete_node makes.
"""
from backend.models import Node
from backend.utils.encryption import prefetch_deks
from backend.utils.privacy import can_user_edit_node


def apply_settings_to_descendants(root, user_id, *, privacy_level=None,
                                  ai_usage=None):
    """Set *privacy_level* and/or *ai_usage* (None = leave as is) on every
    alive descendant of *root* the user may edit. Returns the nodes that
    actually changed; the caller commits.

    Level-batched walk (one query per depth), no row locks: a reply that
    races in during the walk simply keeps the settings it was created
    with, which is what it would have had anyway.
    """
    if privacy_level is None and ai_usage is None:
        return []
    editable = []
    seen = {root.id}
    frontier = [root.id]
    while frontier:
        level = Node.query.filter(Node.parent_id.in_(frontier)).all()
        frontier = [n.id for n in level if n.id not in seen]
        seen.update(frontier)
        editable.extend(
            n for n in level
            if n.deleted_at is None and can_user_edit_node(n, user_id))
    if privacy_level is not None:
        # set_privacy_level moves content across the encryption boundary;
        # unwrap the DEKs concurrently first, or the decrypts below issue
        # one KMS call per node, in sequence. (Going private still wraps a
        # fresh DEK per node — encrypt_content has no batch path.)
        prefetch_deks(
            n.content for n in editable if n.privacy_level != privacy_level)
    changed = []
    for n in editable:
        touched = False
        if privacy_level is not None and n.privacy_level != privacy_level:
            n.set_privacy_level(privacy_level)
            # Same rule as the focal node: a private node can't stay pinned.
            if privacy_level == "private" and n.pinned_at is not None:
                n.pinned_at = None
                n.pinned_by = None
            touched = True
        if ai_usage is not None and n.ai_usage != ai_usage:
            n.ai_usage = ai_usage
            touched = True
        if touched:
            changed.append(n)
    return changed

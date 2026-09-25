"""Apply a privacy / AI-usage change to a node's replies.

The edit dialog offers "this node and all my replies" when the edited
node has replies and a setting changed. Same ownership rule as editing
the replies one by one (can_user_edit_node): the user's own nodes, and
LLM nodes they are the human owner of. Other users' replies are left
alone but walked through, since the user's own replies may sit under
them — the same promise soft_delete_node makes.
"""
from backend.models import Node
from backend.utils.ca_feed import is_feed_node
from backend.utils.encryption import prefetch_deks
from backend.utils.llm_nodes import llm_ids_built_on_a_read
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
    walked = []
    seen = {root.id}
    frontier = [root.id]
    while frontier:
        level = Node.query.filter(Node.parent_id.in_(frontier)).all()
        fresh = [n for n in level if n.id not in seen]
        frontier = [n.id for n in fresh]
        seen.update(frontier)
        walked.extend(fresh)
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
    read_built = set()
    if ai_usage == "train":
        # is_feed_node falls back to the node's text for the PoC read
        # shape, so the same batching applies to a cascade that raises
        # usage: unwrap first, then the checks below are cache hits.
        prefetch_deks(n.content for n in editable if n.ai_usage != ai_usage)
        # The LLM replies built on a read (#362), for the whole subtree.
        read_built = llm_ids_built_on_a_read(root, walked)
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
            # A read's nodes never take 'train' (ca_feed.FEED_AI_USAGE),
            # nor do the LLM replies built with one in their context
            # (#362): the cascade leaves them as they are, like the
            # editor refuses the same change on the node itself.
            if not (ai_usage == "train"
                    and (n.id in read_built or is_feed_node(n))):
                n.ai_usage = ai_usage
                touched = True
        if touched:
            changed.append(n)
    return changed

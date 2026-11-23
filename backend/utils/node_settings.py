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
from backend.utils.privacy import can_user_edit_node, train_refused


# How open a level is; an unknown one counts as private.
_OPENNESS = {"private": 0, "circles": 1, "public": 2}


def _prompt_entry_ids(nodes):
    """Ids of the prompt entries among *nodes*: those stamped with a
    prompt key, and those from before that column that only link a
    prompt version (one query for the lot, no row loads)."""
    from backend.models import NodeContextArtifact
    from backend.extensions import db
    ids = {n.id for n in nodes if n.prompt_key}
    unstamped = [n.id for n in nodes if not n.prompt_key]
    for start in range(0, len(unstamped), 900):
        ids.update(node_id for (node_id,) in db.session.query(
            NodeContextArtifact.node_id).filter(
                NodeContextArtifact.artifact_type == "prompt",
                NodeContextArtifact.node_id.in_(
                    unstamped[start:start + 900])))
    return ids


def apply_settings_to_descendants(root, user_id, *, privacy_level=None,
                                  ai_usage=None, kept_from_train=None):
    """Set *privacy_level* and/or *ai_usage* (None = leave as is) on every
    alive descendant of *root* the user may edit. Returns the nodes that
    actually changed; the caller commits.

    A change to 'train' leaves out the entries that may never be at Train
    (fetched through the X API, #689) and appends them to
    *kept_from_train* (a list), so the caller can say so.

    A change of privacy never opens a prompt entry below *root* further
    than it is: a prompt entry is shared only when its owner sets its
    level on the entry itself (Peter, 2026-11-23). A change to a level
    less open than the entry's own (private < circles < public) applies
    to it like to any reply.

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
    if ai_usage == "train":
        # is_feed_node falls back to the node's text for the PoC read
        # shape, so the same batching applies to a cascade that raises
        # usage: unwrap first, then the checks below are cache hits.
        prefetch_deks(n.content for n in editable if n.ai_usage != ai_usage)
    from backend.utils.llm_nodes import reply_in_progress
    # Prompt entries the new level would open further than they are.
    kept_prompts = set()
    if privacy_level not in (None, "private"):
        prompt_ids = _prompt_entry_ids(editable)
        kept_prompts = {
            n.id for n in editable
            if n.id in prompt_ids
            and _OPENNESS.get(privacy_level, 0)
            > _OPENNESS.get(n.privacy_level, 0)}
    changed = []
    for n in editable:
        touched = False
        # An AI reply still being written keeps its privacy: its owner
        # shares it once it is written (Peter, 2026-11-12). So does a
        # prompt entry: its owner shares it on the entry itself.
        if (privacy_level is not None and n.privacy_level != privacy_level
                and n.id not in kept_prompts
                and not (privacy_level != "private"
                         and reply_in_progress(n))):
            n.set_privacy_level(privacy_level)
            # Same rule as the focal node: a private node can't stay pinned.
            if privacy_level == "private" and n.pinned_at is not None:
                n.pinned_at = None
                n.pinned_by = None
            touched = True
        if ai_usage is not None and n.ai_usage != ai_usage:
            # A read's nodes never take 'train' (ca_feed.FEED_AI_USAGE),
            # nor do entries fetched through the X API (#689): the cascade
            # leaves them as they are, like the editor refuses the same
            # change on the node itself.
            if ai_usage == "train" and train_refused(n):
                if kept_from_train is not None:
                    kept_from_train.append(n)
            elif not (ai_usage == "train" and is_feed_node(n)):
                n.ai_usage = ai_usage
                touched = True
        if touched:
            changed.append(n)
    return changed

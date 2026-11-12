"""Apply a privacy / AI-usage change to a node's replies.

The edit dialog offers "this node and all my replies" when the edited
node has replies and a setting changed. Same ownership rule as editing
the replies one by one (can_user_edit_node): the user's own nodes, and
LLM nodes they are the human owner of. Other users' replies are left
alone but walked through, since the user's own replies may sit under
them — the same promise soft_delete_node makes.
"""
from backend.extensions import db
from backend.models import Node, NodeContextArtifact, UserPrompt
from backend.utils.ca_feed import is_feed_node
from backend.utils.encryption import prefetch_deks
from backend.utils.privacy import can_user_edit_node


def _prompt_linked_ids(nodes):
    """Ids of those of *nodes* without a prompt_key stamp that are prompt
    nodes all the same (Node.is_system_prompt: they link a prompt version
    with a key), in one query per 900 ids."""
    ids = sorted(n.id for n in nodes if not n.prompt_key)
    out = set()
    for start in range(0, len(ids), 900):
        out.update(nid for (nid,) in db.session.query(
            NodeContextArtifact.node_id)
            .join(UserPrompt, UserPrompt.id == NodeContextArtifact.artifact_id)
            .filter(NodeContextArtifact.node_id.in_(ids[start:start + 900]),
                    NodeContextArtifact.artifact_type == "prompt",
                    UserPrompt.prompt_key.isnot(None)))
    return out


def _circle_walk(root, levels, editable, privacy_level):
    """What the cascade needs about Circles, from the levels the walk
    read: ``(source_of, under_prompt, owners)``.

    * ``source_of``: where each reply came from (circles.thread_source),
      carried down the levels instead of asked per node: a reply's own
      pointer, else what its parent hands down. ``below`` is the entry a
      node's replies come from: its own pointer (an entry points at
      itself), else what it inherited.
    * ``under_prompt``: the prompt nodes and the sessions written below
      them, which never join an audience through a cascade (a prompt is
      shared only with the whole discussion leading up to an entry,
      circles.share_thread_path). Only for a cascade to "circles"; the
      prompt links are read in one query, not one per node.
    * ``owners``: the owner of each entry a reply came from (None for an
      entry that is gone: nobody's, so nobody widens)."""
    from backend.utils.circles import entry_owner_id, thread_source
    root_source = thread_source(root)
    below = {root.id: root.audience_root_id
             or (root_source.entry_id if root_source is not None else None)}
    source_of = {}
    under_prompt = set()
    prompt_ids = (_prompt_linked_ids([n for level in levels for n in level])
                  if privacy_level == "circles" else set())
    for level in levels:
        for n in level:
            inherited = below.get(n.parent_id)
            own = n.audience_root_id
            if own is not None and own != n.id:
                source_of[n.id] = own
            else:
                source_of[n.id] = inherited
            below[n.id] = own if own is not None else inherited
            if privacy_level == "circles" and (
                    n.parent_id in under_prompt or n.prompt_key
                    or n.id in prompt_ids):
                under_prompt.add(n.id)
    owners = {}
    for entry_id in {source_of.get(n.id) for n in editable} - {None}:
        entry = db.session.get(Node, entry_id)
        owners[entry_id] = entry_owner_id(entry) if entry is not None else None
    return source_of, under_prompt, owners


def apply_settings_to_descendants(root, user_id, *, privacy_level=None,
                                  ai_usage=None, audience_from=None):
    """Set *privacy_level* and/or *ai_usage* (None = leave as is) on every
    alive descendant of *root* the user may edit. Returns the nodes that
    actually changed; the caller commits.

    Level-batched walk (one query per depth), no row locks: a reply that
    races in during the walk simply keeps the settings it was created
    with, which is what it would have had anyway.

    *audience_from* (Circles): the edited node, when its privacy level or
    audience was saved. The user's replies that are "circles" afterwards
    are read by its audience; the ones that are not stop pointing at one.
    """
    if privacy_level is None and ai_usage is None:
        return []
    editable = []
    levels = []
    seen = {root.id}
    frontier = [root.id]
    while frontier:
        level = Node.query.filter(Node.parent_id.in_(frontier)).all()
        frontier = [n.id for n in level if n.id not in seen]
        seen.update(frontier)
        levels.append(level)
        editable.extend(
            n for n in level
            if n.deleted_at is None and can_user_edit_node(n, user_id))
    # Circles, only while a node may carry a thread pointer (the flag is
    # on, or a pointer is left from when it was): with none, the walk
    # above is all the cascade reads, as before the feature.
    from backend.utils.privacy import pointers_may_exist
    source_of, under_prompt, owners = (
        _circle_walk(root, levels, editable, privacy_level)
        if privacy_level is not None and pointers_may_exist()
        else ({}, set(), {}))

    def written_into_someone_elses_thread(n, uid):
        entry_id = source_of.get(n.id)
        return entry_id is not None and owners.get(entry_id) != uid
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
    # Circles: what the user wrote into someone else's thread (a reply,
    # an AI reply they asked for) is never made public and never moved to
    # another audience by a cascade either; it can be taken back.
    changed = []
    for n in editable:
        touched = False
        # What the node was before this cascade (level, thread pointer),
        # for the caller's quote check (a reply already read by the same
        # people is checked only against the readers it gains).
        n.settings_before = (n.privacy_level, n.audience_root_id)
        foreign = (privacy_level is not None
                   and written_into_someone_elses_thread(n, user_id))
        if foreign and privacy_level == "public":
            continue
        kept_out = privacy_level == "circles" and n.id in under_prompt
        # An AI reply still being written keeps its privacy: its owner
        # shares it once it is written (Peter, 2026-11-12).
        if (privacy_level is not None and not kept_out
                and n.privacy_level != privacy_level
                and not (privacy_level != "private"
                         and reply_in_progress(n))):
            n.set_privacy_level(privacy_level)
            # Same rule as the focal node: a private node can't stay pinned.
            if privacy_level == "private" and n.pinned_at is not None:
                n.pinned_at = None
                n.pinned_by = None
            touched = True
        if ai_usage is not None and n.ai_usage != ai_usage:
            # A read's nodes never take 'train' (ca_feed.FEED_AI_USAGE):
            # the cascade leaves them as they are, like the editor
            # refuses the same change on the node itself.
            if not (ai_usage == "train" and is_feed_node(n)):
                n.ai_usage = ai_usage
                touched = True
        if (audience_from is not None and privacy_level is not None
                and not foreign and not kept_out):
            entry_id = (audience_from.audience_root_id
                        if audience_from.privacy_level == "circles" else None)
            if n.privacy_level == "circles" and entry_id is not None:
                wanted = entry_id
            else:
                wanted = n.audience_root_id   # the record stays
                if n.audience_root_id == n.id:
                    # An entry of its own stops being shared.
                    from backend.utils.circles import clear_audience
                    clear_audience(n)
            if n.audience_root_id != wanted:
                if n.audience_root_id == n.id:
                    from backend.utils.circles import clear_audience
                    clear_audience(n)
                n.audience_root_id = wanted
                touched = True
        if touched:
            changed.append(n)
    if audience_from is not None:
        from backend.utils.privacy import forget_circle_access
        forget_circle_access()
    return changed

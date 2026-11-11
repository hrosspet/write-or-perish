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
from backend.utils.privacy import can_user_edit_node


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
    # Circles: where each reply came from (circles.thread_source),
    # carried down the walk instead of asked per node: a reply's own
    # pointer, else what its parent hands down. `below` is the
    # entry a node's replies come from: its own pointer (an entry points
    # at itself), else what it inherited.
    from backend.utils.circles import entry_owner_id, thread_source
    root_source = thread_source(root)
    below = {root.id: root.audience_root_id
             or (root_source.entry_id if root_source is not None else None)}
    source_of = {}
    editable = []
    under_prompt = set()
    seen = {root.id}
    frontier = [root.id]
    while frontier:
        level = Node.query.filter(Node.parent_id.in_(frontier)).all()
        frontier = [n.id for n in level if n.id not in seen]
        seen.update(frontier)
        for n in level:
            inherited = below.get(n.parent_id)
            own = n.audience_root_id
            if own is not None and own != n.id:
                source_of[n.id] = own
            else:
                source_of[n.id] = inherited
            below[n.id] = own if own is not None else inherited
            # Circles: a prompt node, and the session written below it,
            # never join an audience through a cascade. A prompt is
            # shared only with the whole discussion leading up to an
            # entry (circles.share_thread_path).
            if n.parent_id in under_prompt or n.is_system_prompt:
                under_prompt.add(n.id)
        editable.extend(
            n for n in level
            if n.deleted_at is None and can_user_edit_node(n, user_id))
    owners = {}
    for entry_id in {source_of.get(n.id) for n in editable} - {None}:
        entry = Node.query.get(entry_id) if entry_id else None
        # None: an entry that is gone is nobody's, so nobody widens.
        owners[entry_id] = entry_owner_id(entry) if entry is not None else None

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
    # Circles: what the user wrote into someone else's thread (a reply,
    # an AI reply they asked for) is never made public and never moved to
    # another audience by a cascade either; it can be taken back.
    changed = []
    for n in editable:
        touched = False
        foreign = (privacy_level is not None
                   and written_into_someone_elses_thread(n, user_id))
        if foreign and privacy_level == "public":
            continue
        kept_out = privacy_level == "circles" and n.id in under_prompt
        if (privacy_level is not None and not kept_out
                and n.privacy_level != privacy_level):
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

"""Which node is the top of a thread on the public surfaces.

A Text-mode thread starts with a prompt entry: its owner's prompt text,
with pinned versions of their profile and other documents. That entry is
private unless its owner sets its level themselves (session_helpers
.PROMPT_ENTRY_PRIVACY), so in a thread whose first message is public the
real root is not public and the message is not a root.

The public listings (Commons feed, a person's public page and its JSON,
feed.xml, the sitemap) therefore list "thread tops":

- a root (no parent), as before; or
- a node that has a permalink slug and hangs directly under a living
  root that is a prompt entry, is not public and belongs to the same
  person.

The slug is what makes such a node a listed thread, and only creation
gives it: a Text-mode thread created at Public gets it on its first
message (/textmode/start, a recorded entry that starts a thread; an
uploaded recording when its transcript is stored,
slugs.slug_transcribed_thread). The edit route gives none, so a message,
a Voice entry or a gleaning that its owner sets public later is read by
its link and is in no listing, as before this rule.

The prompt entry itself is never listed and never read here: the rule
looks at five of its columns, by primary key, inside the listing's own
query (a correlated EXISTS, no extra query and no row load). The
listings start from the public nodes (Node's partial index
ix_node_public_created_at), not from the roots.

A prompt entry is recognised by its stamped ``prompt_key``. Entries from
before that column (2026-09-02) only link a prompt version.

A prompt entry in the middle of a thread (Text mode continued under an
entry of one's own) is passed through by every thread view of someone
other than its owner: the entry itself and its text are never shown,
and the readable nodes under it that belong to the same person are
shown in its place, under the node above it (``passes_through`` for
nodes in hand, ``public_children_filter`` for the public pages' query).
Only a prompt entry is passed through, never another private node, and
only towards nodes of the entry's own owner.
"""
from sqlalchemy import and_, exists, func, literal, or_, select
from sqlalchemy.orm import aliased

from backend.models import Node
from backend.utils.privacy import PrivacyLevel


def thread_top_filter(node=Node):
    """SQL condition: *node* is the top of its thread as the public
    surfaces list it (see the module text). The caller adds what makes
    the node itself listable (public, alive, its owner sharing)."""
    above = aliased(Node, flat=True)
    return or_(
        node.parent_id.is_(None),
        and_(
            node.public_slug.isnot(None),
            exists().where(and_(
                above.id == node.parent_id,
                above.parent_id.is_(None),
                above.deleted_at.is_(None),
                above.prompt_key.isnot(None),
                above.privacy_level != PrivacyLevel.PUBLIC.value,
                func.coalesce(above.human_owner_id, above.user_id)
                == func.coalesce(node.human_owner_id, node.user_id),
            ))),
    )


def owner_id_of(node):
    return node.human_owner_id or node.user_id


def passes_through(entry, below):
    """True when a thread view that may not show *entry* shows *below*
    (a node directly under it) in its place: *entry* is a living prompt
    entry and *below* belongs to the same person. The caller has
    established that its viewer may not read *entry* (and decides by its
    own rule whether they may read *below*)."""
    return bool(
        entry is not None and below is not None
        and entry.prompt_key
        and entry.deleted_at is None
        and owner_id_of(entry) == owner_id_of(below))


def public_children_filter(parent_id, node=Node):
    """SQL condition: *node* is shown directly under the node
    *parent_id* on the public pages: its child, or the child of a
    living, non-public prompt entry under it that belongs to the same
    person as *node*. The caller adds what makes the node itself
    readable (public, alive, its owner sharing). One IN over the
    parent's id and the ids of its prompt entries, so the lookup stays
    on the parent_id index."""
    hop = aliased(Node, flat=True)
    hops = select(hop.id).where(
        hop.parent_id == parent_id,
        hop.prompt_key.isnot(None),
        hop.deleted_at.is_(None),
        hop.privacy_level != PrivacyLevel.PUBLIC.value)
    above = aliased(Node, flat=True)
    return and_(
        node.parent_id.in_(select(literal(parent_id)).union_all(hops)),
        or_(node.parent_id == parent_id,
            exists().where(and_(
                above.id == node.parent_id,
                func.coalesce(above.human_owner_id, above.user_id)
                == func.coalesce(node.human_owner_id, node.user_id)))),
    )


def public_root_of(node, visible):
    """The top of the public thread *node* (publicly visible) is shown
    in: up the parent chain while the parent is publicly visible
    (*visible*, the caller's test), passing through a prompt entry that
    is not (see the module text) when the node above it is."""
    root = node
    seen = set()
    while root.parent_id and root.id not in seen:
        seen.add(root.id)
        parent = Node.query.get(root.parent_id)
        if not visible(parent):
            if not (passes_through(parent, root) and parent.parent_id):
                break
            above = Node.query.get(parent.parent_id)
            if not visible(above):
                break
            seen.add(parent.id)
            parent = above
        root = parent
    return root

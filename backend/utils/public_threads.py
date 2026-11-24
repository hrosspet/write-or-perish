"""Which node is the top of a thread on the public surfaces, and what
its card shows.

A Text-mode thread starts with a prompt entry: its owner's prompt text,
with pinned versions of their profile and other documents. A new one is
Public only when its owner's default level (the Account page) is Public
and the message it starts with is created Public, else Private
(session_helpers.prompt_entry_privacy). Its owner may change it later,
and so may "This node and all my replies", so a thread's root and its
first message can have different levels. Voice and Glean prompt entries
are private.

The public listings (Commons feed, a person's public page and its JSON,
feed.xml, the sitemap) list "thread tops":

- a root (no parent) that is no prompt entry, as before;
- a public prompt root with a card: a living public node of its owner
  directly under it, whose text is stored (an uploaded recording counts
  once its transcript is). The card shows the first such node (oldest,
  then lowest id; ``card_entries``), not the prompt text, and links to
  the node's permalink when it has one, else to the root's
  (``PromptRoots.link_slug``). A public prompt root without a card is
  listed nowhere: it is read by its link, its owner's Log lists it
  (routes/log.py), and it is listed again once it has a card;
- a node that has a permalink slug and hangs directly under a living
  root that is a prompt entry, is not public and belongs to the same
  person.

The slug is what makes a node under a private prompt root a listed
thread, and only creation gives it: a Text-mode message created at
Public gets it (/textmode/start, a recorded entry that starts a thread;
an uploaded recording when its transcript is stored,
slugs.slug_transcribed_thread), under a private or a public prompt root.
The edit route gives none, so a message, a Voice entry or a gleaning
that its owner sets public later under a private prompt root is read by
its link and is in no listing, as before this rule.

The prompt entry is never read here. The public prompt roots of a
listing (one account's, or everyone's) and their cards are found first,
in a few small queries over the public nodes and the prompt links
(``public_prompt_roots``); the listing's own query then leaves out the
roots without a card by id. Correlated subqueries in the listing's query
would make PostgreSQL estimate it costly enough to compile it (JIT) on
every request. The listings start from the public nodes (Node's partial
index ix_node_public_created_at), not from the roots.

A prompt entry is recognised as Node.is_system_prompt does: a stamped
``prompt_key``, or, for entries from before that column (2026-09-02), a
linked prompt version. Only roots are recognised by the link; the rule
for a node under a private prompt root and the pass-through below read
the stamp only: no message under an entry from before the column has a
slug, and such an entry is not passed through, as no entry was before
the pass-through existed.

A private prompt entry in the middle of a thread (Text mode continued
under an entry of one's own) is passed through by every thread view of
someone other than its owner: the entry itself and its text are never
shown, and the readable nodes under it that belong to the same person
are shown in its place, under the node above it (``passes_through`` for
nodes in hand, ``public_children_filter`` for the public pages' query).
Only a prompt entry is passed through, never another private node, and
only towards nodes of the entry's own owner.
"""
from datetime import datetime

from sqlalchemy import and_, exists, func, literal, or_, select, union
from sqlalchemy.orm import aliased

from backend.models import Node, NodeContextArtifact
from backend.utils.privacy import PrivacyLevel

# An uploaded recording's text is stored when its transcription is
# completed; a node that was never transcribed has no status.
TEXT_STORED = (None, "completed")

# INTRODUCED CONSTANT: prompt entries created from this moment (UTC) on
# carry the prompt_key stamp. The column reached production on
# 2026-09-02 at 15:52 UTC, and every path that pins a prompt stamps it
# since (context_artifacts.attach_context_artifacts, dump_rows
# .pin_prompt). Only an older root can be a prompt entry by its prompt
# link alone, so the lookup by link reads only the public nodes created
# before this moment, a set that does not grow.
STAMPED_SINCE = datetime(2026, 9, 3)


def _owner(node):
    return func.coalesce(node.human_owner_id, node.user_id)


class PromptRoots:
    """The living public prompt roots of one account or of everyone
    (public_prompt_roots): *cards* {root id: (card id, card slug)} for
    those with a card, *bare* the ids of those without one."""

    def __init__(self, cards, bare):
        self.cards = cards
        self.bare = bare

    def link_slug(self, top_id, own_slug):
        """The slug a listing links the top *top_id* by: its card's when
        it is a public prompt root whose card has one, else its own."""
        card = self.cards.get(top_id)
        return (card[1] if card is not None and card[1] else own_slug)

    def slugged_card_roots(self):
        """Ids of the roots whose card has a slug of its own."""
        return sorted(r for r, (_, slug) in self.cards.items() if slug)

    def linked_filter(self, node=Node):
        """SQL condition: the top *node* has a slug to link it by (its
        own, or its card's: link_slug). The ids are added only when
        there are any, so the plain test stays as before."""
        roots = self.slugged_card_roots()
        own = node.public_slug.isnot(None)
        return or_(own, node.id.in_(roots)) if roots else own


def public_prompt_roots(owner_id=None):
    """PromptRoots for the living public prompt roots of *owner_id* (as
    owner or author), or of everyone. Two small queries: the stamped
    roots (Node's partial index ix_node_public_prompt_root) with the
    roots from before the stamp that link a prompt version (the public
    nodes created before STAMPED_SINCE), then, when there are any, the
    public nodes directly under them, from which the cards are picked
    here."""
    from backend.extensions import db

    root = [Node.privacy_level == PrivacyLevel.PUBLIC.value,
            Node.parent_id.is_(None), Node.deleted_at.is_(None)]
    if owner_id is not None:
        root.append(or_(Node.human_owner_id == owner_id,
                        Node.user_id == owner_id))
    columns = (Node.id, Node.human_owner_id, Node.user_id)
    stamped = select(*columns).where(*root, Node.prompt_key.isnot(None))
    linked = (select(*columns)
              .join(NodeContextArtifact,
                    NodeContextArtifact.node_id == Node.id)
              .where(*root, Node.prompt_key.is_(None),
                     Node.created_at < STAMPED_SINCE,
                     NodeContextArtifact.artifact_type == "prompt"))
    rows = db.session.execute(union(stamped, linked)).all()
    owner_of = {r.id: r.human_owner_id or r.user_id for r in rows}
    ids = sorted(owner_of)
    cards = {}
    for start in range(0, len(ids), 1000):
        under = db.session.query(
            Node.id, Node.parent_id, Node.public_slug, Node.created_at,
            Node.human_owner_id, Node.user_id, Node.transcription_status,
        ).filter(
            Node.parent_id.in_(ids[start:start + 1000]),
            Node.privacy_level == PrivacyLevel.PUBLIC.value,
            Node.deleted_at.is_(None),
        ).all()
        for n in sorted(under, key=lambda n: (n.created_at or datetime.min,
                                              n.id)):
            if (n.parent_id not in cards
                    and (n.human_owner_id or n.user_id)
                    == owner_of[n.parent_id]
                    and n.transcription_status in TEXT_STORED):
                cards[n.parent_id] = (n.id, n.public_slug)
    return PromptRoots(cards, set(ids) - set(cards))


def thread_top_filter(bare, node=Node):
    """SQL condition: *node* is the top of its thread as the public
    surfaces list it (see the module text); *bare* holds the ids of the
    public prompt roots without a card (PromptRoots.bare), which are
    left out. The caller adds what makes the node itself listable
    (public, alive, its owner sharing)."""
    above = aliased(Node, flat=True)
    root = node.parent_id.is_(None)
    if bare:
        root = and_(root, node.id.notin_(sorted(bare)))
    return or_(
        root,
        and_(
            node.public_slug.isnot(None),
            exists().where(and_(
                above.id == node.parent_id,
                above.parent_id.is_(None),
                above.deleted_at.is_(None),
                above.prompt_key.isnot(None),
                above.privacy_level != PrivacyLevel.PUBLIC.value,
                _owner(above) == _owner(node),
            ))),
    )


def card_entries(tops, roots):
    """{top id: node} for the listed *tops* that are public prompt roots
    with a card (*roots*, a PromptRoots): the node each one's card
    shows, in one query. A top missing from the answer shows itself."""
    top_of = {roots.cards[t.id][0]: t.id for t in tops
              if t.id in roots.cards}
    if not top_of:
        return {}
    return {top_of[n.id]: n
            for n in Node.query.filter(Node.id.in_(sorted(top_of))).all()}


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

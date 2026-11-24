"""Which node is the top of a thread on the public surfaces, and what
its card shows.

A Text-mode thread starts with a prompt entry: its owner's prompt text,
with pinned versions of their profile and other documents. A new one is
stored at its owner's default level (the Account page: Public, else
Private; session_helpers.prompt_entry_privacy), never at the level of
the message under it, so a thread's root and its first message can have
different levels. Voice and Glean prompt entries are private.

The public listings (Commons feed, a person's public page and its JSON,
feed.xml, the sitemap) list "thread tops":

- a root (no parent) that is no prompt entry, as before;
- a public prompt root with a public node of its owner directly under
  it. Its card shows that node (the first one: oldest, then lowest id;
  ``card_entries``), not the prompt text, and links to the node's
  permalink when it has one, else to the root's (``link_slug``). A
  public prompt root with no such node is listed nowhere: it is read by
  its link, and listed again once a public node of its owner is under
  it;
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

The prompt entry is never read here: the rules look at its columns and
its prompt link inside the listing's own query (correlated EXISTS and
subqueries, no row load), and one more query per listing page finds the
cards. The listings start from the public nodes (Node's partial index
ix_node_public_created_at), not from the roots.

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
from sqlalchemy import and_, case, exists, func, literal, not_, or_, select
from sqlalchemy.orm import aliased

from backend.models import Node, NodeContextArtifact
from backend.utils.privacy import PrivacyLevel


def _owner(node):
    return func.coalesce(node.human_owner_id, node.user_id)


def is_prompt_entry(node=Node):
    """SQL condition: *node* is a prompt entry (Node.is_system_prompt):
    stamped with a prompt key, or linked to a prompt version."""
    link = aliased(NodeContextArtifact, flat=True)
    return or_(
        node.prompt_key.isnot(None),
        exists().where(and_(link.node_id == node.id,
                            link.artifact_type == "prompt")),
    )


def _card_conditions(entry, top):
    """SQL condition: *entry* may be the card of the prompt root *top*:
    a living public node of the root's owner directly under it."""
    return and_(
        entry.parent_id == top.id,
        entry.privacy_level == PrivacyLevel.PUBLIC.value,
        entry.deleted_at.is_(None),
        _owner(entry) == _owner(top),
    )


def _card_order(entry):
    return (entry.created_at.asc(), entry.id.asc())


def _is_prompt_root(node):
    return and_(node.parent_id.is_(None), is_prompt_entry(node))


def thread_top_filter(node=Node):
    """SQL condition: *node* is the top of its thread as the public
    surfaces list it (see the module text). The caller adds what makes
    the node itself listable (public, alive, its owner sharing)."""
    above = aliased(Node, flat=True)
    card = aliased(Node, flat=True)
    return or_(
        and_(
            node.parent_id.is_(None),
            or_(not_(is_prompt_entry(node)),
                exists().where(_card_conditions(card, node))),
        ),
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


def unlisted_public_prompt_root(node=Node):
    """SQL condition: *node* is a living public prompt root that no
    public listing shows, having no public node of its owner directly
    under it (thread_top_filter). Its owner's Log lists it instead, by
    their own private writing under it (routes/log.py), so their entries
    in such a thread stay reachable."""
    card = aliased(Node, flat=True)
    return and_(
        _is_prompt_root(node),
        node.deleted_at.is_(None),
        node.privacy_level == PrivacyLevel.PUBLIC.value,
        not_(exists().where(_card_conditions(card, node))),
    )


def link_slug(node=Node):
    """SQL expression: the slug a listing links the top *node* by: for a
    prompt root, its card's slug, else the root's own; for any other top,
    its own. NULL when there is none (the top is then left out of
    feed.xml and the sitemap, as an unslugged root always was)."""
    card = aliased(Node, flat=True)
    card_slug = (select(card.public_slug)
                 .where(_card_conditions(card, node))
                 .order_by(*_card_order(card))
                 .limit(1)
                 .scalar_subquery())
    return case(
        (_is_prompt_root(node), func.coalesce(card_slug, node.public_slug)),
        else_=node.public_slug,
    )


def card_entries(tops):
    """{top id: node} for the listed *tops* that are prompt roots: the
    node each one's card shows (see the module text), in one query. A
    top missing from the answer shows itself."""
    from backend.extensions import db

    ids = [t.id for t in tops if t.parent_id is None]
    if not ids:
        return {}
    top = aliased(Node, flat=True)
    rank = func.row_number().over(
        partition_by=Node.parent_id, order_by=_card_order(Node))
    ranked = (db.session.query(Node.id.label("id"), rank.label("rank"))
              .join(top, top.id == Node.parent_id)
              .filter(top.id.in_(ids), is_prompt_entry(top),
                      _card_conditions(Node, top))
              .subquery())
    rows = (Node.query.join(ranked, ranked.c.id == Node.id)
            .filter(ranked.c.rank == 1).all())
    return {n.parent_id: n for n in rows}


def shown_slug(top, card):
    """The slug *top*'s card links to: *card*'s (card_entries), else the
    top's own; as link_slug says in SQL."""
    return (card.public_slug if card is not None and card.public_slug
            else top.public_slug)


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

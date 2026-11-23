"""Which node is the top of a thread on the public surfaces.

A Text-mode thread starts with a prompt entry: its owner's prompt text,
with pinned versions of their profile and other documents. That entry is
private unless its owner sets its level themselves (session_helpers
.PROMPT_ENTRY_PRIVACY), so in a thread whose first message is public the
real root is not public and the message is not a root.

The public listings (Commons feed, a person's public page and its JSON,
feed.xml, the sitemap) therefore list "thread tops":

- a root (no parent), as before; or
- a node directly under a root that is a prompt entry, is not public and
  belongs to the same person.

The prompt entry itself is never listed and never read here: the rule
looks at four of its columns, by primary key, inside the listing's own
query (a correlated EXISTS, no extra query and no row load).

A prompt entry is recognised by its stamped ``prompt_key``. Entries from
before that column (2026-09-02) only link a prompt version; they were
all created at their message's level, and
scripts/set_prompt_entries_private.py stamps the ones it sets private.

Only the root case: a private prompt entry in the middle of a thread
(Text mode continued under an entry of one's own) ends the public
thread above it, as any private node does.
"""
from sqlalchemy import and_, exists, func, or_
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
        exists().where(and_(
            above.id == node.parent_id,
            above.parent_id.is_(None),
            above.prompt_key.isnot(None),
            above.privacy_level != PrivacyLevel.PUBLIC.value,
            func.coalesce(above.human_owner_id, above.user_id)
            == func.coalesce(node.human_owner_id, node.user_id),
        )),
    )


def is_thread_top(node):
    """thread_top_filter for one node in hand (one primary-key read of
    the parent, usually already in the session)."""
    if node.parent_id is None:
        return True
    above = Node.query.get(node.parent_id)
    return bool(
        above is not None
        and above.parent_id is None
        and above.prompt_key
        and above.privacy_level != PrivacyLevel.PUBLIC.value
        and (above.human_owner_id or above.user_id)
        == (node.human_owner_id or node.user_id))

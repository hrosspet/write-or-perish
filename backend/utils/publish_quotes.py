"""A public node never quotes a node that is not public (Peter, 2026-11-12).

"If it's about private quotes in public nodes, that shouldn't be allowed.
If a user wants to publish a private node with a quote of another private
node, a dialog stating that this is not possible should be displayed.
Informing the user that they first need to make the quoted node public,
too."

Every way a node becomes public, or a public node gets new text, asks
here first: an edit that makes a node (an entry or an AI reply) public,
with the replies it carries along; an edit of a public node's text; a
new public entry (a reply under a public node, a public Text-mode entry,
a recorded entry, a published share). The answer names the quoted nodes
that are not public, so the page can link them.

What is checked is a {quote:ID} marker of a node the user can open,
followed into the text of quoted nodes the user wrote, to the depth the
quote resolver follows (quotes.DEFAULT_MAX_DEPTH). Someone else's quoted
text is not followed: its own quotes show each reader what that reader
can open, as the resolver's rule for text the reader did not write
(quotes.quotes_inside). A quoted node the user cannot open is treated
like one that does not exist or was deleted: no reader is shown its text
through the quote (each reader's own access decides), so it is neither
counted nor named. A saved reference ({quote_ext:ID}) is not a node and
is not checked.
Making a quoted node private later is not refused: its readers then see
the quote as one they cannot open, as before.
"""
from flask import jsonify

from backend.extensions import db
from backend.models import Node
from backend.utils.quotes import (
    DEFAULT_MAX_DEPTH, find_quote_ids, written_by,
)

QUOTES_NOT_PUBLIC_CODE = "quotes_not_public"
QUOTES_NOT_PUBLIC_MESSAGE = (
    "This can't be made public yet: it quotes an entry that isn't public. "
    "Make the quoted entry public first, then try again.")
# Characters of a quoted entry's text the answer names it by.
QUOTED_PREVIEW_CHARS = 120


def unpublished_quotes(texts, user_id, becoming_public=(), skip_ids=(),
                       max_depth=DEFAULT_MAX_DEPTH):
    """The nodes *user_id* can open that the {quote:ID} markers in
    *texts* show and that are not public, in the order found, each once.
    *becoming_public* are ids made public by the same change: they count
    as public. Quotes are followed into the text of quoted nodes *user_id*
    wrote, to *max_depth*, the resolver's depth. A quoted node the user
    cannot open
    is passed over like a missing one. *skip_ids*: quotes at the first
    level that are not checked (an edit of a public node keeps the
    quotes it already had).
    """
    from backend.utils.privacy import can_user_access_node, owner_hidden
    becoming_public = set(becoming_public)
    skip_ids = set(skip_ids)
    found = []
    seen = set()

    def walk(text, depth, first):
        if depth <= 0 or not text:
            return
        for qid in find_quote_ids(text):
            if qid in seen or (first and qid in skip_ids):
                continue
            seen.add(qid)
            node = db.session.get(Node, qid)
            if (node is None or node.deleted_at is not None
                    or owner_hidden(node)
                    or not can_user_access_node(node, user_id)):
                continue
            if (node.privacy_level != "public"
                    and node.id not in becoming_public):
                found.append(node)
            if written_by(node, user_id):
                walk(node.get_content(), depth - 1, False)

    for text in texts:
        walk(text, max_depth, True)
    return found


def quotes_not_public_response(nodes, user_id):
    """The 409 answer naming *nodes* (nodes *user_id* can open): each with
    its id, a short preview of its text and its link, so the dialog can
    say which entry to make public first."""
    from backend.utils.slugs import permalink_for
    quoted = []
    for node in nodes:
        text = " ".join((node.get_content() or "").split())
        if len(text) > QUOTED_PREVIEW_CHARS:
            text = text[:QUOTED_PREVIEW_CHARS].rstrip() + "…"
        quoted.append({"id": node.id, "can_open": True, "preview": text,
                       "permalink": permalink_for(node),
                       "privacy_level": node.privacy_level})
    return jsonify({
        "error": QUOTES_NOT_PUBLIC_MESSAGE,
        "code": QUOTES_NOT_PUBLIC_CODE,
        "quoted": quoted,
    }), 409


def publish_refusal(texts, user_id, becoming_public=(), skip_ids=()):
    """The 409 answer when *texts*, about to be public, quote a node that
    is not public (unpublished_quotes), else None."""
    if isinstance(texts, str):
        texts = [texts]
    nodes = unpublished_quotes(texts, user_id, becoming_public=becoming_public,
                               skip_ids=skip_ids)
    if nodes:
        return quotes_not_public_response(nodes, user_id)
    return None

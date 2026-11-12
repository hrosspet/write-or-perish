"""Text in a reply's thread history that the requester did not write
(Peter, 2026-11-12).

"direct writing to artifacts should be in the case when other user's text
is in the context, intercepted and via a dialog raised to the user"
"let's keep the confirmation dialog only for nodes directly in history,
including quotes of public / circles nodes in private entries"

A reply's tool calls that write the requester's data directly wait for
their approval when the history the reply is built from holds text they
did not write. The history is the chain of nodes above the reply, with
the quotes resolved inside them, read the way the message builder reads
it (llm_completion.generate_llm_response):

  * a node of another Loore user, or an AI reply someone else asked for;
  * a {quote:ID} of another person's node inside a node of the
    requester's own, followed into quoted text to the resolver's depth;
  * a {quote_ext:ID} of a saved reference (a clipped page, a bookmark, a
    tweet) in an entry of the requester's own;
  * a Glean reply's picks, which the history gives back as the tweets
    themselves when a conversation continues under it.

Not counted: what a placeholder fills in (the recent writing, the archive,
the profile, memory; "The UX degradation would be too big compared to the
risk"), and what a tool of the same reply reads. Text with no authorship
mark (typed or pasted text, transcripts, imported files) counts as the
requester's own.

The state is known before the reply's first model call and does not
change during the reply.
"""
from backend.extensions import db
from backend.models import ExternalItem, Node, READ_PICK_SOURCE
from backend.utils.privacy import (
    AI_ALLOWED, can_user_access_node, owner_hidden, shown_as_deleted,
)
from backend.utils.quotes import (
    DEFAULT_MAX_DEPTH, find_ext_quote_ids, find_quote_ids, written_by,
)

OTHER_USER = "other_user"
SAVED_REFERENCE = "saved_reference"
GLEAN = "glean"


def _is_llm(node):
    return node.node_type == "llm" or bool(node.llm_model)


def _quoted_by_others(text, user_id, depth, path, ext_ids):
    """True when a {quote:ID} in *text* (the requester's own) resolves to
    a node another person wrote, at any depth the resolver follows: the
    quoted node is readable by the requester, alive and AI-readable, as
    resolve_quotes requires before it shows a quote's text. *path* holds
    the ids quoted on the way here: only a quote back into it is passed
    over (the resolver's cycle rule, its _seen_ids), so a node quoted
    again elsewhere is followed again, as the message builder does. The
    saved references the requester's quoted entries carry are added to
    *ext_ids*: the message's reference pass resolves them too."""
    if depth <= 0 or not text:
        return False
    for qid in find_quote_ids(text):
        if qid in path:
            continue
        quoted = db.session.get(Node, qid)
        if (quoted is None or quoted.deleted_at is not None
                or owner_hidden(quoted)
                or quoted.ai_usage not in AI_ALLOWED
                or not can_user_access_node(quoted, user_id)):
            continue
        if not written_by(quoted, user_id):
            return True
        quoted_text = quoted.get_content()
        ext_ids.extend(find_ext_quote_ids(quoted_text or ""))
        if _quoted_by_others(quoted_text, user_id, depth - 1,
                             path | {qid}, ext_ids):
            return True
    return False


def _reference_kinds(item_ids, user_id):
    """The kinds of the requester's saved references among *item_ids*
    (resolve_ext_quotes shows only the requester's own)."""
    if not item_ids:
        return set()
    sources = db.session.query(ExternalItem.source).filter(
        ExternalItem.id.in_(set(item_ids)),
        ExternalItem.user_id == user_id).all()
    return {GLEAN if source == READ_PICK_SOURCE else SAVED_REFERENCE
            for (source,) in sources}


def history_by_others(node_chain, user_id, read_reply_ids=()):
    """The kinds of text in *node_chain* (a reply's history, root first)
    that *user_id* did not write: a set of OTHER_USER, SAVED_REFERENCE and
    GLEAN, empty when the history is all theirs. *read_reply_ids* are the
    Glean replies in the chain whose picks the history gives back as
    tweets."""
    kinds = set()
    read_reply_ids = set(read_reply_ids or ())
    for node in node_chain:
        if shown_as_deleted(node, user_id):
            continue  # sent as a notice, never its text
        if not written_by(node, user_id):
            kinds.add(OTHER_USER)
            continue
        if _is_llm(node):
            # An assistant turn keeps its markers as written; a Glean
            # reply's picks come back as the tweets.
            if node.id in read_reply_ids:
                kinds.add(GLEAN)
            continue
        content = node.get_content() or ""
        ext_ids = find_ext_quote_ids(content)
        if _quoted_by_others(content, user_id, DEFAULT_MAX_DEPTH,
                             frozenset(), ext_ids):
            kinds.add(OTHER_USER)
        kinds |= _reference_kinds(ext_ids, user_id)
    return kinds

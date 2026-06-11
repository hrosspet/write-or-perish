"""Per-node content size cap and serial-chain splitting.

A single node larger than the profile chunk budget stalls chunked
profile generation: the chronological budget window admits it alone,
the quote resolver cannot fit it, and the export returns None — so the
resume cursor never advances past it (observed in prod: a ~900KB
financial-statements paste pinned a user's profile chain at one cutoff
for five weeks). The cap keeps every node comfortably below the
90k-token chunk budget (100k chars ≈ 25k tokens ≈ 1/3 of a chunk).

Splitting is lossless — segments concatenate back to the original
text — and boundaries fall on newlines unless a single line exceeds
the cap by itself. Split parts are chained in series (each part the
parent of the next) with strictly increasing created_at, so context
assembly reads them as one continuous text and chunked exports can
window between them.
"""

from datetime import timedelta

NODE_CHAR_CAP = 100_000


def split_text_at_cap(text, cap=NODE_CHAR_CAP):
    """Split *text* into segments of <= cap chars at newline boundaries.

    Lossless: ``"".join(result) == text``. The boundary newline stays at
    the end of the earlier segment. A single line longer than the cap is
    hard-cut at the cap.
    """
    if text is None or len(text) <= cap:
        return [text]
    segments = []
    rest = text
    while len(rest) > cap:
        cut = rest.rfind("\n", 0, cap)
        cut_at = cut + 1 if cut != -1 else cap
        segments.append(rest[:cut_at])
        rest = rest[cut_at:]
    if rest:
        segments.append(rest)
    return segments


def split_node_into_chain(node, segments=None):
    """Trim *node* to the first segment and chain the remainder as new
    serial child nodes (each part the parent of the next).

    The original node keeps its id — and therefore its quotes,
    source_key, audio linkage, and artifacts. Pre-existing children of
    *node* are re-parented onto the last part, so replies continue after
    the full content. Each part's created_at is +1ms from the previous,
    keeping chronological windowing and the profile resume cursor
    strictly ordered.

    Returns the list of newly created part nodes (empty when no split
    is needed). The caller commits.
    """
    from backend.extensions import db
    from backend.models import Node
    from backend.utils.tokens import approximate_token_count

    if segments is None:
        segments = split_text_at_cap(node.get_content())
    if len(segments) <= 1:
        return []

    original_children = Node.query.filter(
        Node.parent_id == node.id).all()

    node.set_content(segments[0])
    node.token_count = approximate_token_count(segments[0])

    parts = []
    prev = node
    for i, seg in enumerate(segments[1:], start=1):
        part = Node(
            user_id=node.user_id,
            human_owner_id=node.human_owner_id,
            parent_id=prev.id,
            node_type=node.node_type,
            llm_model=node.llm_model,
            privacy_level=node.privacy_level,
            ai_usage=node.ai_usage,
            token_count=approximate_token_count(seg),
        )
        part.set_content(seg)
        part.created_at = node.created_at + timedelta(milliseconds=i)
        db.session.add(part)
        db.session.flush()
        parts.append(part)
        prev = part

    for child in original_children:
        child.parent_id = prev.id

    return parts

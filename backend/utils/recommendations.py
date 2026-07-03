"""Retrieval layer for context-aware resurfacing of external items.

The PoC recommendations rail was superseded by quote-as-response (#208):
surfacing now happens as quotes inside the model's reply, judged by the
live LLM over labeled search results. These utils remain as the retrieval
stage for the overnight batch pre-selection follow-up — compose a query
from what Loore knows about the user (thread tail, intentions, profile),
embed it, and rank their imported external items by semantic relevance.

Privacy: only content the AI is already allowed to see enters the query
(thread nodes filtered by ai_usage, profile/intentions resolved through
their own ai_usage-checking accessors). External items belong to the
requesting user and are served only to them.

V1 ranking is PURE semantic relevance. The vision doc's 70/30
semantic/spaced-repetition blend is deliberately NOT implemented — no
hidden recency fudge; revisit when spaced repetition exists.
"""
from backend.extensions import db
from backend.models import (
    ExternalItem, ExternalItemEmbedding, UserArtifact, UserProfile,
)
from backend.utils.embeddings import embed_texts, top_k_similar
from backend.utils.privacy import AI_ALLOWED

# Character budgets for the composed query (embedding input is capped at
# ~8k tokens anyway; the split below decides the balance of the three
# signals, thread-first). Explicitly a heuristic — tune freely.
THREAD_CHAR_BUDGET = 6000
INTENTIONS_CHAR_BUDGET = 2000
PROFILE_CHAR_BUDGET = 2000

SNIPPET_CHARS = 400


def _thread_tail_text(node, char_budget=THREAD_CHAR_BUDGET):
    """Walk from *node* up the parent chain collecting AI-visible content,
    newest first, until the budget is spent. The current sharing is the
    strongest signal, so it gets the head of the budget."""
    parts = []
    used = 0
    current = node
    visited = set()
    while current and current.id not in visited and used < char_budget:
        visited.add(current.id)
        if (current.ai_usage in AI_ALLOWED
                and current.deleted_at is None):
            text = (current.get_content() or "").strip()
            if text:
                take = text[:char_budget - used]
                parts.append(take)
                used += len(take)
        current = current.parent
    return "\n\n".join(parts)


def compose_recommendation_query(user_id, node):
    """The query text: current sharing first, then intentions, then a slice
    of the profile — the three things Peter named as the matching context."""
    sections = []
    thread = _thread_tail_text(node)
    if thread:
        sections.append(f"Current writing:\n{thread}")

    intentions = UserArtifact.latest_for(user_id, "intentions")
    if intentions is not None and intentions.ai_usage in AI_ALLOWED:
        text = (intentions.get_content() or "").strip()
        if text:
            sections.append(
                f"Intentions:\n{text[:INTENTIONS_CHAR_BUDGET]}")

    profile = UserProfile.query.filter_by(user_id=user_id).order_by(
        UserProfile.created_at.desc()).first()
    if profile is not None and profile.ai_usage in AI_ALLOWED:
        text = (profile.get_content() or "").strip()
        if text:
            sections.append(f"Profile:\n{text[:PROFILE_CHAR_BUDGET]}")

    return "\n\n".join(sections)


def recommend_external_items(user_id, node, api_key, k=3, min_score=0.2):
    """Top-*k* of the user's external items for the current context.

    Returns [] when the user has no embedded items or nothing scores above
    *min_score* — the caller renders nothing, never an empty shell.
    """
    rows = db.session.query(
        ExternalItemEmbedding.item_id, ExternalItemEmbedding.vector
    ).filter(ExternalItemEmbedding.user_id == user_id).all()
    if not rows:
        return []

    query_text = compose_recommendation_query(user_id, node)
    if not query_text.strip():
        return []

    query_vector = embed_texts(
        [query_text], api_key, user_id=user_id,
        request_type="embedding_query",
    )[0]

    ranked = top_k_similar(query_vector, rows, k=k, min_score=min_score)
    if not ranked:
        return []

    items_by_id = {
        item.id: item for item in ExternalItem.query.filter(
            ExternalItem.id.in_([iid for iid, _ in ranked]),
            ExternalItem.user_id == user_id,
        ).all()
    }
    results = []
    for item_id, score in ranked:
        item = items_by_id.get(item_id)
        if item is None:
            continue
        text = (item.get_content() or "").strip()
        if not text:
            continue
        results.append({
            "id": item.id,
            "source": item.source,
            "author_handle": item.author_handle,
            "content": text[:SNIPPET_CHARS],
            "url": item.url,
            "posted_at": item.posted_at.isoformat() + "Z"
            if item.posted_at else None,
            "score": round(score, 4),
        })
    return results

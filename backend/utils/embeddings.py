"""Embedding generation + similarity for semantic search (#155).

Vectors are packed float32 blobs (stdlib struct/array — no numpy, no
pgvector). Cosine similarity is brute-force in Python: at alpha scale
(thousands of nodes x 1536 dims) a full scan is tens of milliseconds.

Embedding calls use the OpenAI chat key (retrieval is 'chat' usage) and
log cost to APICostLog like every other provider call.
"""
import hashlib
import logging
import math
from array import array

from backend.extensions import db
from backend.models import APICostLog
from backend.utils.cost import EMBEDDING_PRICE_PER_MTOK

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "text-embedding-3-small"
# text-embedding-3-small hard-caps each input at 8191 tokens. We cap by
# chars (no tokenizer dependency). Prose runs ~4 chars/token in English but
# can be ~2.2 in Czech and lower still for code/CJK, so 30000 chars routinely
# blew past 8191 tokens and 400'd the whole batch. 16000 keeps typical
# token-dense prose under the limit; embed_texts halves-and-retries for any
# outlier that still trips it.
MAX_EMBED_CHARS = 16000
# Don't keep halving forever — 2000 chars is well under 8191 tokens for any
# realistic content, so if that still 400s we surface the error.
MIN_EMBED_CHARS = 2000


def content_hash(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def pack_vector(values):
    return array("f", values).tobytes()


def unpack_vector(blob):
    vec = array("f")
    vec.frombytes(blob)
    return vec


def cosine_similarity(a, b):
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / math.sqrt(norm_a * norm_b)


def embed_texts(texts, api_key, user_id=None, request_type="embedding"):
    """Embed a batch of texts via OpenAI. Returns list of float lists.

    Logs cost per call (attributed to *user_id* when given). Raises on
    API errors — callers decide whether to retry or skip.
    """
    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    # Cap by chars, then halve-and-retry if a token-dense input still exceeds
    # the 8191-token limit. Halving re-truncates the whole batch, but only
    # inputs longer than the new cap are actually shortened, and we only embed
    # a prefix anyway (the head of a node is representative for retrieval).
    cap = MAX_EMBED_CHARS
    while True:
        inputs = [(t or "")[:cap] for t in texts]
        try:
            response = client.embeddings.create(
                model=EMBEDDING_MODEL, input=inputs)
            break
        except Exception as e:  # noqa: BLE001 — only the token-limit 400 retries
            if "maximum input length" in str(e) and cap > MIN_EMBED_CHARS:
                logger.warning(
                    "embed_texts: input over 8191 tokens at %d chars; "
                    "retrying at %d chars", cap, cap // 2)
                cap //= 2
                continue
            raise

    tokens = response.usage.total_tokens if response.usage else 0
    if user_id is not None:
        cost = int(tokens * EMBEDDING_PRICE_PER_MTOK)  # microdollars/Mtok
        db.session.add(APICostLog(
            user_id=user_id,
            model_id=EMBEDDING_MODEL,
            request_type=request_type,
            input_tokens=tokens,
            output_tokens=0,
            cost_microdollars=cost,
        ))

    # API returns embeddings in input order
    return [item.embedding for item in response.data]


def top_k_similar(query_vector, rows, k=10, min_score=0.0):
    """Score (node_id, vector_blob) rows against *query_vector*.

    Returns [(node_id, score)] sorted desc, filtered by *min_score*.
    """
    scored = []
    for node_id, blob in rows:
        score = cosine_similarity(query_vector, unpack_vector(blob))
        if score >= min_score:
            scored.append((node_id, score))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:k]


def retrieve_relevant_snippets(user_id, query_text, exclude_node_ids,
                               api_key, k=4, min_score=0.35,
                               snippet_chars=400, query_vector=None):
    """Top-k archive snippets relevant to *query_text* for agentic context
    injection (#155). Returns [(node_id, created_at, snippet, score)].

    Excludes nodes already present in the conversation. Any failure is the
    caller's to swallow — retrieval must never break a completion.
    Pass *query_vector* to reuse an embedding computed by the caller
    (e.g. when the same query also ranks external references).
    """
    from backend.models import Node, NodeEmbedding

    if query_vector is None:
        query_vector = embed_texts(
            [query_text], api_key, user_id=user_id,
            request_type="embedding_query",
        )[0]

    rows = db.session.query(
        NodeEmbedding.node_id, NodeEmbedding.vector
    ).filter(
        NodeEmbedding.user_id == user_id,
        ~NodeEmbedding.node_id.in_(exclude_node_ids or [-1]),
    ).all()

    ranked = top_k_similar(query_vector, rows, k=k, min_score=min_score)
    if not ranked:
        return []

    nodes_by_id = {
        n.id: n for n in Node.query.filter(
            Node.id.in_([nid for nid, _ in ranked]),
            Node.deleted_at.is_(None),
        ).all()
    }
    results = []
    for node_id, score in ranked:
        node = nodes_by_id.get(node_id)
        if node is None:
            continue
        text = (node.get_content() or "").strip()
        if not text:
            continue
        snippet = text[:snippet_chars] + (
            "…" if len(text) > snippet_chars else "")
        results.append((node_id, node.created_at, snippet, score))
    return results


def retrieve_relevant_references(user_id, query_vector, k=4, min_score=0.35,
                                 snippet_chars=400):
    """Top-k of the user's external references (imported tweets/bookmarks)
    for an already-embedded query. Returns a list of dicts with the item's
    identity, a preview snippet, and its surfacing history — the metadata
    travels WITH the result so the model can weigh repetition itself.
    """
    from backend.models import ExternalItem, ExternalItemEmbedding

    rows = db.session.query(
        ExternalItemEmbedding.item_id, ExternalItemEmbedding.vector
    ).filter(ExternalItemEmbedding.user_id == user_id).all()
    if not rows:
        return []

    ranked = top_k_similar(query_vector, rows, k=k, min_score=min_score)
    if not ranked:
        return []

    items_by_id = {
        i.id: i for i in ExternalItem.query.filter(
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
        snippet = text[:snippet_chars] + (
            "…" if len(text) > snippet_chars else "")
        results.append({
            "item_id": item.id,
            "source": item.source,
            "author_handle": item.author_handle,
            "posted_at": item.posted_at,
            "snippet": snippet,
            "score": score,
            "surfaced_count": item.surfaced_count or 0,
            "last_surfaced_at": item.last_surfaced_at,
        })
    return results

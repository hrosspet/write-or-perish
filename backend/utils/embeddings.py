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

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_PRICE_PER_MTOK = 0.02
# text-embedding-3-small accepts 8191 tokens; stay safely under it.
MAX_EMBED_CHARS = 30000


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
    inputs = [(t or "")[:MAX_EMBED_CHARS] for t in texts]
    response = client.embeddings.create(model=EMBEDDING_MODEL, input=inputs)

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
                               snippet_chars=400):
    """Top-k archive snippets relevant to *query_text* for agentic context
    injection (#155). Returns [(node_id, created_at, snippet, score)].

    Excludes nodes already present in the conversation. Any failure is the
    caller's to swallow — retrieval must never break a completion.
    """
    from backend.models import Node, NodeEmbedding

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

"""Background embedding generation for semantic search (#155).

A periodic sweep keeps NodeEmbedding rows in sync with node content. The
sweep design (rather than per-write dispatch) covers every content path —
text nodes, voice transcripts, LLM responses, imports — without touching
each of them. New/edited nodes become searchable within one sweep period.

Only nodes with ai_usage in AI_ALLOWED are embedded: embedding submits
content to OpenAI, so 'none' nodes are never sent (their keyword search
still works). Deleted nodes' embeddings are removed.
"""
from celery.utils.log import get_task_logger
from sqlalchemy import or_

from backend.celery_app import celery, flask_app
from backend.extensions import db
from backend.models import (
    ExternalItem, ExternalItemEmbedding, Node, NodeEmbedding,
)
from backend.utils.api_keys import get_openai_chat_key
from backend.utils.embeddings import (
    EMBEDDING_MODEL, content_hash, embed_texts, pack_vector,
)
from backend.utils.privacy import AI_ALLOWED

logger = get_task_logger(__name__)

SWEEP_BATCH_SIZE = 100
EMBED_API_BATCH = 32


def _candidate_nodes(limit):
    """Nodes needing (re-)embedding: AI-readable, alive, with content,
    and either missing an embedding row or carrying a stale hash."""
    rows = (
        db.session.query(Node, NodeEmbedding)
        .outerjoin(NodeEmbedding, NodeEmbedding.node_id == Node.id)
        .filter(
            Node.deleted_at.is_(None),
            Node.content.isnot(None),
            Node.ai_usage.in_(AI_ALLOWED),
        )
        .order_by(Node.id.desc())
        .all()
    )
    out = []
    for node, emb in rows:
        text = node.get_content()
        if not text or not text.strip():
            continue
        digest = content_hash(text)
        if emb is not None and emb.content_hash == digest \
                and emb.model == EMBEDDING_MODEL:
            continue
        out.append((node, emb, text, digest))
        if len(out) >= limit:
            break
    return out


@celery.task(name='backend.tasks.embeddings.sweep_embeddings')
def sweep_embeddings(limit=SWEEP_BATCH_SIZE):
    with flask_app.app_context():
        api_key = get_openai_chat_key(flask_app.config)
        if not api_key:
            logger.warning("Embedding sweep skipped: no OpenAI key")
            return {"status": "skipped", "reason": "no_api_key"}

        # Remove embeddings of deleted / opted-out nodes
        stale = (
            db.session.query(NodeEmbedding)
            .join(Node, Node.id == NodeEmbedding.node_id)
            .filter(or_(
                Node.deleted_at.isnot(None),
                ~Node.ai_usage.in_(AI_ALLOWED),
            ))
            .all()
        )
        for row in stale:
            db.session.delete(row)
        if stale:
            db.session.commit()

        candidates = _candidate_nodes(limit)
        embedded = 0
        for start in range(0, len(candidates), EMBED_API_BATCH):
            batch = candidates[start:start + EMBED_API_BATCH]
            texts = [text for _, _, text, _ in batch]
            # Cost attribution: each node's owner pays for their content;
            # one log row per batch under the first node's owner keeps it
            # simple (alpha: typically a single-user sweep anyway).
            vectors = embed_texts(
                texts, api_key, user_id=batch[0][0].user_id)
            for (node, emb, _text, digest), vector in zip(batch, vectors):
                if emb is None:
                    emb = NodeEmbedding(
                        node_id=node.id,
                        user_id=node.human_owner_id or node.user_id,
                    )
                    db.session.add(emb)
                emb.user_id = node.human_owner_id or node.user_id
                emb.model = EMBEDDING_MODEL
                emb.content_hash = digest
                emb.vector = pack_vector(vector)
                embedded += 1
            db.session.commit()

        # External references (#155 component 2): embed imported items
        # the same way. They're user-curated content (bookmarks, CA
        # tweets) — embedding makes them semantically searchable.
        ext_rows = (
            db.session.query(ExternalItem, ExternalItemEmbedding)
            .outerjoin(ExternalItemEmbedding,
                       ExternalItemEmbedding.item_id == ExternalItem.id)
            .filter(ExternalItemEmbedding.id.is_(None))
            .order_by(ExternalItem.id.desc())
            .limit(limit)
            .all()
        )
        ext_candidates = []
        for item, _ in ext_rows:
            text = item.get_content()
            if text and text.strip():
                ext_candidates.append((item, text))
        ext_embedded = 0
        for start in range(0, len(ext_candidates), EMBED_API_BATCH):
            batch = ext_candidates[start:start + EMBED_API_BATCH]
            vectors = embed_texts(
                [text for _, text in batch], api_key,
                user_id=batch[0][0].user_id)
            for (item, text), vector in zip(batch, vectors):
                db.session.add(ExternalItemEmbedding(
                    item_id=item.id,
                    user_id=item.user_id,
                    model=EMBEDDING_MODEL,
                    content_hash=content_hash(text),
                    vector=pack_vector(vector),
                ))
                ext_embedded += 1
            db.session.commit()

        logger.info(
            "Embedding sweep: %d nodes embedded, %d external items "
            "embedded, %d stale removed", embedded, ext_embedded,
            len(stale))
        return {"status": "ok", "embedded": embedded,
                "external_embedded": ext_embedded,
                "removed": len(stale)}

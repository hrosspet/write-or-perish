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
from sqlalchemy import func, or_

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


def _embedding_owner_id(node):
    """The *human* who owns a node's embedding — and pays for it.

    For AI replies ``node.user_id`` is the synthetic ``llm-<model>`` author;
    ``human_owner_id`` points at the real owner. Both the NodeEmbedding row
    and the embedding cost must use this same resolution, or cost lands on a
    placeholder account that should never spend (the original #155 bug)."""
    return node.human_owner_id or node.user_id


def _candidate_nodes(limit):
    """Nodes needing (re-)embedding: AI-readable, alive, with content,
    and either missing an embedding row, embedded with another model, or
    edited since the last embed (Node.updated_at vs the row's snapshot).

    All of that is filtered IN SQL with a LIMIT. Only the resulting batch
    is loaded and decrypted — never the full corpus. Content is KMS
    envelope-encrypted with a per-node DEK, so the previous decrypt-
    everything-each-sweep design made ~1.6M KMS calls/day (the dominant
    GCP cost) and OOM-thrashed the prod VM as the corpus grew.
    """
    rows = (
        db.session.query(Node, NodeEmbedding)
        .outerjoin(NodeEmbedding, NodeEmbedding.node_id == Node.id)
        .filter(
            Node.deleted_at.is_(None),
            Node.content.isnot(None),
            Node.content != "",
            Node.ai_usage.in_(AI_ALLOWED),
        )
        .filter(or_(
            NodeEmbedding.id.is_(None),
            NodeEmbedding.model != EMBEDDING_MODEL,
            func.coalesce(NodeEmbedding.node_updated_at,
                          NodeEmbedding.created_at)
            < func.coalesce(Node.updated_at, Node.created_at),
        ))
        .order_by(Node.id.desc())
        .limit(limit)
        .all()
    )
    out = []
    touched = False
    for node, emb in rows:
        # Hash the STORED bytes (the encrypted blob for encrypted nodes),
        # not the plaintext: the unchanged-check then needs no KMS call,
        # and no plaintext fingerprint lands in the DB. Trade-off:
        # re-encrypting identical plaintext (fresh DEK) changes the blob
        # and triggers one spurious re-embed — rare and cheap.
        digest = content_hash(node.content)
        if emb is not None and emb.content_hash == digest \
                and emb.model == EMBEDDING_MODEL:
            # Content unchanged (a non-content column bumped updated_at,
            # or a legacy row without the snapshot): record the snapshot
            # so the node drops out of the candidate query.
            emb.node_updated_at = node.updated_at
            touched = True
            continue
        text = node.get_content()
        if not text or not text.strip():
            continue
        out.append((node, emb, text, digest))
    if touched:
        db.session.commit()
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
            # Cost attribution: the *human* owner pays, not the synthetic
            # llm-<model> author of AI replies. One log row per batch under the
            # first node's human owner keeps it simple (alpha scale).
            vectors = embed_texts(
                texts, api_key, user_id=_embedding_owner_id(batch[0][0]))
            for (node, emb, _text, digest), vector in zip(batch, vectors):
                if emb is None:
                    emb = NodeEmbedding(
                        node_id=node.id,
                        user_id=_embedding_owner_id(node),
                    )
                    db.session.add(emb)
                emb.user_id = _embedding_owner_id(node)
                emb.model = EMBEDDING_MODEL
                emb.content_hash = digest
                emb.vector = pack_vector(vector)
                # Snapshot the node version this vector reflects. Uses the
                # value read at fetch time, so an edit racing the sweep
                # keeps the node stale and it's re-embedded next sweep.
                emb.node_updated_at = node.updated_at
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
                    # Stored-bytes hash, same rationale as nodes above.
                    content_hash=content_hash(item.content),
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

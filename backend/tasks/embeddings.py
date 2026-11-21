"""Background embedding generation for semantic search (#155).

A periodic sweep keeps NodeEmbedding rows in sync with node content. The
sweep design (rather than per-write dispatch) covers every content path —
text nodes, voice transcripts, LLM responses, imports — without touching
each of them. New/edited nodes become searchable within one sweep period.

Only nodes with ai_usage in AI_ALLOWED are embedded: embedding submits
content to OpenAI, so 'none' nodes are never sent (their keyword search
still works). Nothing new is embedded for an owner whose account setting
is 'none' (#346); their existing vectors are kept. Deleted nodes'
embeddings are removed.

One sweep runs at a time (a Redis lock, utils/run_locks), and a new row
that another sweep wrote in the meantime is left as it is (INSERT ... ON
CONFLICT DO NOTHING), so two sweeps never fail on the unique node or item.
"""
from celery.utils.log import get_task_logger
from flask import current_app
from sqlalchemy import LargeBinary, cast, func, or_
from sqlalchemy.orm import defer

from backend.celery_app import celery, flask_app
from backend.extensions import db
from backend.models import (
    ExternalItem, ExternalItemEmbedding, Node, NodeEmbedding, User,
)
from backend.utils import pgvector
from backend.utils import run_locks, soft_limit
from backend.utils.api_keys import get_openai_chat_key
from backend.utils.embeddings import (
    EMBEDDING_MODEL, MAX_EMBED_CHARS, content_hash, embed_texts, pack_vector,
)
from backend.utils.encryption import key_service_unavailable
from backend.utils.error_privacy import describe_exception
from backend.utils.privacy import AI_ALLOWED

from backend.utils.hidden_rows import account_on_hold_user_ids, hidden_ids

logger = get_task_logger(__name__)

SWEEP_BATCH_SIZE = 100
EMBED_API_BATCH = 32
SWEEP_LOCK_KEY = "loore:embeddings:sweep"
# HEURISTIC: how long the sweep lock lives when its sweep dies without
# releasing it. A sweep (at most SWEEP_BATCH_SIZE nodes and as many
# references) takes seconds; three sweep periods is far above that and
# delays embedding by at most 15 minutes after a killed sweep. A sweep
# that runs longer than this overlaps the next one, which the
# ON CONFLICT insert then tolerates.
SWEEP_LOCK_TTL = 15 * 60
# HEURISTIC: the largest stored value (bytes of the column) the sweep
# loads. An entry holds at most 100,000 characters (NODE_CHAR_CAP), which
# is at most about 540,000 bytes stored encrypted; the largest entry from
# before the cap was a ~900 KB paste. Ten million is many times both, so
# no entry written in the apps is left out, while one row of this size
# needs only tens of MB to load and decrypt. A row above it is never
# embedded (it stays out of semantic search) and takes no place among the
# sweep's candidates.
EMBED_MAX_STORED_BYTES = 10_000_000


def _embedding_owner_id(node):
    """The *human* who owns a node's embedding — and pays for it.

    For AI replies ``node.user_id`` is the synthetic ``llm-<model>`` author;
    ``human_owner_id`` points at the real owner. Both the NodeEmbedding row
    and the embedding cost must use this same resolution, or cost lands on a
    placeholder account that should never spend (the original #155 bug)."""
    return node.human_owner_id or node.user_id


def _paid_features_filter():
    """Payments on (#438): no background model call (embedding included)
    for an account without the paid set, or one the admin blocked (#697).
    Nothing while payments are off, so the query is the one it was."""
    from backend.utils.approval import background_work_clause
    return background_work_clause()


def _readable_text(row, kind):
    """The row's text, or None when the stored value cannot be read (it
    does not decrypt). One row of one account must not stop the sweep for
    every account: it is logged by id only and left for the next sweep,
    which writes nothing for it now. A key-service outage
    (encryption.key_service_unavailable) ends the sweep as before: every
    row would fail the same way, each after its own retries."""
    try:
        return row.get_content()
    except Exception as e:  # noqa: BLE001 — a read failure skips the row
        soft_limit.reraise(e)
        if key_service_unavailable(e):
            raise
        logger.warning("Embedding sweep: %s %s could not be read (%s); "
                       "skipped", kind, row.id, describe_exception(e))
        return None


def _candidate_nodes(limit):
    """Nodes needing (re-)embedding: AI-readable, alive, with content,
    and either missing an embedding row, embedded with another model, or
    edited since the last embed (Node.updated_at vs the row's snapshot).

    All of that is filtered IN SQL with a LIMIT. Only the resulting batch
    is loaded and decrypted — never the full corpus. Content is KMS
    envelope-encrypted with a per-node DEK, so the previous decrypt-
    everything-each-sweep design made ~1.6M KMS calls/day (the dominant
    GCP cost) and OOM-thrashed the prod VM as the corpus grew.

    The batch's stored values are loaded one row at a time and only the
    first MAX_EMBED_CHARS of each text (all the embedding call sends) is
    kept, so the memory a sweep needs is bounded by its largest row, not
    by the sum of its rows. A row stored above EMBED_MAX_STORED_BYTES is
    not a candidate: it is never loaded, and takes no place in the batch.
    """
    rows = _candidate_query(limit).all()
    out = []
    touched = False
    for node, emb in rows:
        # Hash the STORED bytes (the encrypted blob for encrypted nodes),
        # not the plaintext: the unchanged-check then needs no KMS call,
        # and no plaintext fingerprint lands in the DB. Trade-off:
        # re-encrypting identical plaintext (fresh DEK) changes the blob
        # and triggers one spurious re-embed — rare and cheap.
        digest = content_hash(node.content)   # loads this row's value
        if emb is not None and emb.content_hash == digest \
                and emb.model == EMBEDDING_MODEL:
            # Content unchanged (a non-content column bumped updated_at,
            # or a legacy row without the snapshot): record the snapshot
            # so the node drops out of the candidate query.
            emb.node_updated_at = node.updated_at
            touched = True
            db.session.expire(node, ["content"])
            continue
        text = _readable_text(node, "node")
        # The stored value is not needed again: let it go before the
        # next row is loaded.
        db.session.expire(node, ["content"])
        if not text or not text.strip():
            continue
        out.append((node, emb, text[:MAX_EMBED_CHARS], digest))
    if touched:
        db.session.commit()
    return out


def _stored_size(column, dialect):
    """The stored size of a text column in bytes, read without fetching
    the value where the database allows it. PostgreSQL's octet_length
    reads the size kept with the value (length() would fetch and
    decompress every value the scan passes, across the whole table);
    sqlite gets length() of the value as a blob, which counts bytes on
    every sqlite version (octet_length exists only from 3.43)."""
    if dialect == "sqlite":
        return func.length(cast(column, LargeBinary))
    return func.octet_length(column)


def _candidate_query(limit, dialect=None):
    """The candidate query of _candidate_nodes, for the session's
    database (or the named *dialect*)."""
    if dialect is None:
        dialect = db.session.get_bind().dialect.name
    return (
        db.session.query(Node, NodeEmbedding)
        .options(defer(Node.content))
        .outerjoin(NodeEmbedding, NodeEmbedding.node_id == Node.id)
        # The owner's account setting (see _embedding_owner_id): an
        # AI reply's author is the llm-<model> account, not the owner.
        .join(User, User.id == func.coalesce(Node.human_owner_id,
                                             Node.user_id))
        .filter(
            Node.deleted_at.is_(None),
            Node.content.isnot(None),
            Node.content != "",
            _stored_size(Node.content, dialect) <= EMBED_MAX_STORED_BYTES,
            Node.ai_usage.in_(AI_ALLOWED),
            User.default_ai_usage.in_(AI_ALLOWED),
            # An account being deleted (#269): nothing new is embedded.
            # The nodes a "Delete all my content" hid are soft-deleted,
            # so they are left out above (#590).
            ~User.id.in_(account_on_hold_user_ids()),
            *_paid_features_filter(),
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
    )


def _candidate_external_items(limit):
    """Saved references without an embedding, whose owner's account
    setting allows AI (#346)."""
    return (
        db.session.query(ExternalItem, ExternalItemEmbedding)
        .outerjoin(ExternalItemEmbedding,
                   ExternalItemEmbedding.item_id == ExternalItem.id)
        .join(User, User.id == ExternalItem.user_id)
        .filter(ExternalItemEmbedding.id.is_(None),
                User.default_ai_usage.in_(AI_ALLOWED),
                ~User.id.in_(account_on_hold_user_ids()),   # #269
                *_paid_features_filter(),
                # A Read pick nobody saved is not a reference (#352).
                ExternalItem.saved())
        .order_by(ExternalItem.id.desc())
        .limit(limit)
        .all()
    )


def _sync_pgvector_copies():
    """pgvector copies (#331): what the sweep embedded is ranked in the
    database from the next search on. Nothing to do until `flask
    pgvector-enable --apply` has run. A failure (e.g. the copies were
    dropped, the off switch) is logged once and this process stops using
    pgvector until it restarts, as after a failed ranking; the rows it
    embedded stay with the Python path."""
    if not pgvector.enabled():
        return
    try:
        pgvector.sync_copies(limit=pgvector.SWEEP_SYNC_LIMIT)
    except Exception:
        db.session.rollback()
        logger.exception(
            "pgvector copy sync failed; this process ranks in Python")
        pgvector.turn_off()


def _insert_absent(model, unique_col, values):
    """Insert one embedding row unless a row for the same node or item
    exists already (another sweep wrote it after this one read the
    candidates). Returns whether this call inserted it. Postgres and
    SQLite: INSERT ... ON CONFLICT DO NOTHING on the unique column; any
    other database: the insert in a savepoint, an IntegrityError rolled
    back to it."""
    table = model.__table__
    dialect = db.session.get_bind().dialect.name
    if dialect in ("postgresql", "sqlite"):
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        stmt = insert(table).values(**values).on_conflict_do_nothing(
            index_elements=[unique_col])
        return db.session.execute(stmt).rowcount > 0
    from sqlalchemy.exc import IntegrityError
    try:
        with db.session.begin_nested():
            db.session.execute(table.insert().values(**values))
        return True
    except IntegrityError:
        return False


@celery.task(name='backend.tasks.embeddings.sweep_embeddings')
def sweep_embeddings(limit=SWEEP_BATCH_SIZE):
    with flask_app.app_context():
        # One sweep at a time: sweeps queued while the server was frozen
        # ran side by side afterwards, picked the same newest nodes and
        # inserted the same rows (2026-11-07).
        with run_locks.single_run(SWEEP_LOCK_KEY, SWEEP_LOCK_TTL) as ok:
            if not ok:
                logger.info("Embedding sweep skipped: another sweep is "
                            "running")
                return {"status": "skipped", "reason": "running"}
            return sweep(limit)


def sweep(limit=SWEEP_BATCH_SIZE):
    """The sweep, inside an app context (the task holds the sweep lock)."""
    api_key = get_openai_chat_key(current_app.config)
    if not api_key:
        logger.warning("Embedding sweep skipped: no OpenAI key")
        return {"status": "skipped", "reason": "no_api_key"}

    # Remove embeddings of deleted / opted-out nodes. A node hidden by
    # a waiting "Delete all my content" keeps its embedding (#268): a
    # restore brings it back as it was, without decrypting and
    # embedding it again; the purge deletes both.
    stale = (
        db.session.query(NodeEmbedding)
        .join(Node, Node.id == NodeEmbedding.node_id)
        .filter(or_(
            Node.deleted_at.isnot(None),
            ~Node.ai_usage.in_(AI_ALLOWED),
        ), ~NodeEmbedding.node_id.in_(hidden_ids("node")))
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
                # A new row: kept as it is when another sweep wrote the
                # node's row after this one read its candidates.
                if _insert_absent(NodeEmbedding, "node_id", dict(
                        node_id=node.id,
                        user_id=_embedding_owner_id(node),
                        model=EMBEDDING_MODEL,
                        content_hash=digest,
                        vector=pack_vector(vector),
                        node_updated_at=node.updated_at)):
                    embedded += 1
                continue
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
    # tweets) — embedding makes them semantically searchable. The node
    # half above is committed: its rows reach the pgvector copies even
    # when this half fails.
    try:
        ext_embedded = _embed_external_items(limit, api_key)
    except Exception:
        db.session.rollback()
        _sync_pgvector_copies()
        raise

    _sync_pgvector_copies()

    logger.info(
        "Embedding sweep: %d nodes embedded, %d external items "
        "embedded, %d stale removed", embedded, ext_embedded,
        len(stale))
    return {"status": "ok", "embedded": embedded,
            "external_embedded": ext_embedded,
            "removed": len(stale)}


def _embed_external_items(limit, api_key):
    """The sweep's saved-reference half. Returns the number embedded."""
    ext_rows = _candidate_external_items(limit)
    ext_candidates = []
    for item, _ in ext_rows:
        text = _readable_text(item, "reference")
        if text and text.strip():
            # A web clip's title is often the best one-line summary
            # of the page; fold it in so the vector reflects it.
            if item.title:
                text = f"{item.title}\n\n{text}"
            ext_candidates.append((item, text))
    ext_embedded = 0
    for start in range(0, len(ext_candidates), EMBED_API_BATCH):
        batch = ext_candidates[start:start + EMBED_API_BATCH]
        vectors = embed_texts(
            [text for _, text in batch], api_key,
            user_id=batch[0][0].user_id)
        for (item, text), vector in zip(batch, vectors):
            if _insert_absent(ExternalItemEmbedding, "item_id", dict(
                    item_id=item.id,
                    user_id=item.user_id,
                    model=EMBEDDING_MODEL,
                    # Stored-bytes hash, same rationale as nodes above.
                    content_hash=content_hash(item.content),
                    vector=pack_vector(vector))):
                ext_embedded += 1
        db.session.commit()
    return ext_embedded

"""Optional pgvector ranking for semantic search (#331).

Off until it is turned on in a database: CREATE EXTENSION vector (as a
superuser), then `flask pgvector-enable --apply`. Until then, and always on
sqlite, every search ranks in Python exactly as before
(embeddings.top_k_similar), and nothing here touches the database beyond
one read-only check per process.

Where the vectors live. Each embedding row's vector is COPIED, as a
pgvector value, into a table of the same name in a separate schema:
vec.node_embedding and vec.external_item_embedding, keyed by the source
row's id (foreign key, ON DELETE CASCADE, so a deleted or purged embedding
loses its copy in the same statement). The models know nothing about these
tables, and Alembic's autogenerate compares only the default schema
(include_schemas is off), so the deploy's `flask db migrate` neither needs
the extension nor ever proposes to drop the copies. The bytea column in
the source table stays the source of truth; the sweep writes it as before.

Keeping the copies right. A copy carries the content_hash and model of the
row it was made from. Ranking only uses a copy whose two values still
match its source row (a re-embed changes at least one of them), so a stale
copy is never ranked. Rows with no matching copy, i.e. embedded or
re-embedded since the last sync, are scored in Python and merged with the
database's top k, so results never depend on how far the sync has got.
sync_copies() fills the gaps: the enable command for the whole corpus, the
embedding sweep for the rows it has just written.

Rows whose vector is not a finite, non-zero EMBEDDING_DIM-dimensional
vector are never copied. They stay with the Python path, which skips the
wrong-dimension ones and scores zero vectors 0.0, as before.
"""
import logging
import math

import numpy as np
import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.types import UserDefinedType

from backend.extensions import db
from backend.utils.embeddings import EMBEDDING_DIM

logger = logging.getLogger(__name__)

SCHEMA = "vec"
# The source tables that get a copy table of the same name in SCHEMA.
TABLES = ("node_embedding", "external_item_embedding")

# Rows read per sync chunk: 500 x 6 KB = 3 MB of blobs plus ~10 MB of text
# literals in the process at a time, one commit per chunk.
SYNC_CHUNK = 500
# Rows the embedding sweep reads per table per run (it runs every 5 min and
# writes at most 100 nodes + 100 references). A larger gap is the enable
# command's job, not the sweep's.
SWEEP_SYNC_LIMIT = 1000
# How long the enable command's DDL waits for a lock on a source table (the
# foreign key needs one) before giving up instead of queueing writers.
DDL_LOCK_TIMEOUT = "5s"


class Vector(UserDefinedType):
    """pgvector's `vector` type, just enough to compile casts and
    operators (the pgvector Python package is not a dependency)."""
    cache_ok = True

    def get_col_spec(self, **kw):
        return "vector"


# ── Detection (once per process) ─────────────────────────────────────────

_state = {"enabled": None}

_DETECT_SQL = sa.text(
    "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')"
    " AS extension, "
    + ", ".join(
        f"to_regclass('{SCHEMA}.{t}') IS NOT NULL AS {t}" for t in TABLES)
)


def _detect():
    """True when the extension and both copy tables exist; None when the
    check itself failed (not cached, so the next call retries)."""
    engine = db.engine
    if engine.dialect.name != "postgresql":
        return False
    try:
        # Own connection: a failure here must not abort the caller's
        # transaction.
        with engine.connect() as conn:
            row = conn.execute(_DETECT_SQL).mappings().one()
    except SQLAlchemyError:
        logger.warning("pgvector check failed; ranking in Python",
                       exc_info=True)
        return None
    return bool(row["extension"]) and all(row[t] for t in TABLES)


def enabled():
    """Whether this process ranks in the database. Checked once and cached:
    a process started before `flask pgvector-enable --apply` keeps ranking
    in Python until it restarts."""
    if _state["enabled"] is None:
        found = _detect()
        if found is not None:
            _state["enabled"] = found
            if found:
                logger.info("pgvector ranking on (schema %s)", SCHEMA)
    return bool(_state["enabled"])


def turn_off():
    """Rank in Python for the rest of this process (after a SQL failure)."""
    _state["enabled"] = False


def reset():
    """Forget the cached check (tests; the CLI after creating the tables)."""
    _state["enabled"] = None


# ── Vectors as pgvector text literals ────────────────────────────────────

def _usable(arr):
    if arr.size != EMBEDDING_DIM or not np.isfinite(arr).all():
        return False
    return bool(np.any(arr))


def vector_literal(values):
    """'[v1,v2,...]' for a vector, or None when the database cannot rank
    it (wrong dimension, zero or non-finite). The values go through
    float32, the precision pgvector stores; 9 significant digits give back
    the same float32 exactly when pgvector parses them."""
    arr = np.asarray(values, dtype=np.float32).ravel()
    if not _usable(arr):
        return None
    return "[" + ",".join(["%.9g" % x for x in arr.tolist()]) + "]"


def blob_literal(blob):
    """vector_literal for a packed float32 blob (embeddings.pack_vector)."""
    if len(blob) != EMBEDDING_DIM * 4:
        return None
    return vector_literal(np.frombuffer(bytes(blob), dtype=np.float32))


# ── Ranking queries ──────────────────────────────────────────────────────

def _copy_of(source):
    """The copy table of *source* (NodeEmbedding or ExternalItemEmbedding),
    aliased so the source table keeps its own name in the SQL."""
    return sa.table(
        source.__tablename__,
        sa.column("id"), sa.column("content_hash"), sa.column("model"),
        sa.column("embedding", Vector()),
        schema=SCHEMA,
    ).alias("pgv")


def _matches(copy, source):
    """A copy that still matches its source row (see the module note)."""
    return sa.and_(
        copy.c.id == source.id,
        copy.c.content_hash == source.content_hash,
        copy.c.model == source.model,
    )


def ranked_in_db(query, source, query_literal, k):
    """*query* (rows of (id, vector blob) on *source*'s table, with the
    caller's filters and joins) turned into the database's top *k* by
    cosine similarity to *query_literal* (vector_literal) over the rows
    with a matching copy: [(id, score)], best first. `1 - (a <=> b)` is
    the cosine similarity top_k_similar computes. Exact search: no ANN
    index; the scan is bounded by the caller's user_id filter (#331)."""
    copy = _copy_of(source)
    id_col = query.column_descriptions[0]["expr"]
    target = sa.cast(
        sa.bindparam("query_vector", query_literal, type_=sa.Text),
        Vector())
    score = (1 - copy.c.embedding.op("<=>", return_type=sa.Float)(target)
             ).label("score")
    return (query.join(copy, _matches(copy, source))
            .with_entities(id_col, score)
            .order_by(score.desc(), id_col)
            .limit(k))


def without_copy(query, source):
    """*query* narrowed to the rows that have no matching copy: the ones
    the database cannot rank yet, scored in Python."""
    copy = _copy_of(source)
    return (query.outerjoin(copy, _matches(copy, source))
            .filter(copy.c.id.is_(None)))


def finite_scores(rows, min_score):
    return [(row_id, float(score)) for row_id, score in rows
            if score is not None and math.isfinite(score)
            and score >= min_score]


# ── Creating and filling the copies ──────────────────────────────────────

def ddl_statements():
    """Idempotent DDL for the copy tables (the schema itself is created
    separately: CREATE SCHEMA needs a database privilege the app's role
    may not have)."""
    statements = [f"SET LOCAL lock_timeout = '{DDL_LOCK_TIMEOUT}'"]
    for t in TABLES:
        statements.append(
            f"CREATE TABLE IF NOT EXISTS {SCHEMA}.{t} ("
            f" id integer PRIMARY KEY REFERENCES {t} (id) ON DELETE CASCADE,"
            " content_hash varchar(64) NOT NULL,"
            " model varchar(64) NOT NULL,"
            f" embedding vector({EMBEDDING_DIM}) NOT NULL)")
    statements.append(
        f"COMMENT ON SCHEMA {SCHEMA} IS 'pgvector copies of the embedding"
        " vectors for semantic search (#331). Derived data: flask"
        " pgvector-enable --apply rebuilds it; DROP SCHEMA vec CASCADE"
        " turns it off.'")
    return statements


# Rows of table t without a matching copy, newest first, below a keyset
# cursor. Identifiers come from TABLES and SCHEMA, never from input.
_MISSING_SQL = {
    t: sa.text(
        f"SELECT e.id, e.content_hash, e.model, e.vector FROM {t} e"  # nosec B608
        f" LEFT JOIN {SCHEMA}.{t} c ON c.id = e.id"
        " AND c.content_hash = e.content_hash AND c.model = e.model"
        " WHERE c.id IS NULL AND e.id < :before"
        " ORDER BY e.id DESC LIMIT :n")
    for t in TABLES
}

# Copy one row, only while its source still has the values it was read
# with (a row re-embedded or deleted in between is skipped and picked up,
# or dropped, on the next sync).
_UPSERT_SQL = {
    t: sa.text(
        f"INSERT INTO {SCHEMA}.{t} (id, content_hash, model, embedding)"  # nosec B608
        " SELECT e.id, e.content_hash, e.model, CAST(:embedding AS vector)"
        f" FROM {t} e WHERE e.id = :id"
        " AND e.content_hash = :content_hash AND e.model = :model"
        " ON CONFLICT (id) DO UPDATE SET"
        " content_hash = EXCLUDED.content_hash, model = EXCLUDED.model,"
        " embedding = EXCLUDED.embedding")
    for t in TABLES
}

_MAX_ID = 2 ** 31 - 1  # above any integer id


def sync_copies(limit=None, on_chunk=None):
    """Copy the vectors of rows that have no matching copy. Reads at most
    *limit* rows per table (all when None), SYNC_CHUNK at a time, one
    commit per chunk. *on_chunk(table, read, copied)* reports progress.
    Returns {table: (read, copied)}; read - copied rows were not usable
    vectors and stay with the Python path."""
    result = {}
    for t in TABLES:
        read = copied = 0
        before = _MAX_ID
        while limit is None or read < limit:
            n = SYNC_CHUNK if limit is None else min(SYNC_CHUNK, limit - read)
            rows = db.session.execute(
                _MISSING_SQL[t], {"before": before, "n": n}).all()
            if not rows:
                break
            params = []
            for row_id, digest, model, blob in rows:
                literal = blob_literal(blob)
                if literal is not None:
                    params.append({"id": row_id, "content_hash": digest,
                                   "model": model, "embedding": literal})
            if params:
                db.session.execute(_UPSERT_SQL[t], params)
            db.session.commit()
            read += len(rows)
            copied += len(params)
            before = rows[-1][0]
            if on_chunk is not None:
                on_chunk(t, read, copied)
        result[t] = (read, copied)
    return result

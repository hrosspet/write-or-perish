"""Very long texts: a Twitter import leaves out a "tweet" above the entry
cap, and the embedding sweep holds one large row at a time.

- create_twitter_nodes stores no text above NODE_CHAR_CAP (100,000
  characters, the cap every other writer splits at; a real post on X
  holds at most 25,000): such a row is left out and counted in the
  result as ``too_long``, also when it would restore a deleted entry.
- The sweep's candidate step loads stored values one row at a time and
  keeps only the part of each text the embedding call sends, so its
  memory follows the largest row, not the sum of the batch; a row stored
  above EMBED_MAX_STORED_BYTES is not a candidate and takes no place in
  the batch. The bound reads the stored size (octet_length on
  PostgreSQL), never the value itself.

Encryption on with a local key-service stand-in; no outside calls.
"""
import sys
import tracemalloc
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from backend.tests.test_no_replies_for_none import (  # noqa: F401 (fixtures)
    app, task_mod, _user,
)
from backend.extensions import db as _db
from backend.models import Node
from backend.utils import encryption
from backend.utils.embeddings import MAX_EMBED_CHARS
from backend.utils.node_split import NODE_CHAR_CAP

_GLUE = ("backend.celery_app", "backend.tasks.embeddings")
_saved = {k: sys.modules.get(k) for k in _GLUE}
sys.modules["backend.celery_app"] = MagicMock()
sys.modules.pop("backend.tasks.embeddings", None)
import backend.tasks.embeddings as emb_mod  # noqa: E402
for _k, _v in _saved.items():
    if _v is None:
        sys.modules.pop(_k, None)
    else:
        sys.modules[_k] = _v

TS = "Wed Aug 26 08:17:00 +0000 2026"


@pytest.fixture
def kms(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_DISABLED", "false")
    monkeypatch.setenv("GCP_KMS_KEY_NAME",
                       "projects/t/locations/l/keyRings/r/cryptoKeys/k")
    monkeypatch.setattr(encryption, "_wrap_dek", lambda dek: b"wrapped:" + dek)
    monkeypatch.setattr(encryption, "_unwrap_dek",
                        lambda w: w[len(b"wrapped:"):])


def _row(i, text):
    return {"id_str": str(i), "full_text": text, "created_at": TS,
            "is_reply": False, "token_count": len(text) // 4}


def _import(user, rows, on_deleted=None):
    from backend.routes.import_data import (
        PROVENANCE_ARCHIVE_UPLOAD, create_twitter_nodes)
    return create_twitter_nodes(
        user_id=user.id, rows=iter(rows), total=len(rows),
        import_type="separate_nodes", include_replies=False,
        privacy_level="private", ai_usage="chat", on_deleted=on_deleted,
        provenance=PROVENANCE_ARCHIVE_UPLOAD)


# ── Twitter import ───────────────────────────────────────────────────────

def test_a_tweet_above_the_entry_cap_is_left_out_and_counted(app, kms):  # noqa: F811
    user = _user("alice")
    _db.session.commit()
    result = _import(user, [
        _row(1, "an ordinary tweet"),
        _row(2, "x" * (NODE_CHAR_CAP + 1)),
        _row(3, "y" * NODE_CHAR_CAP),
    ])
    assert result["created"] == 2
    assert result["too_long"] == 1
    keys = {n.source_key for n in Node.query.filter_by(user_id=user.id)}
    assert keys == {"twitter:1", "twitter:3"}


def test_such_a_tweet_does_not_restore_a_deleted_entry(app, kms):  # noqa: F811
    user = _user("alice")
    old = Node(user_id=user.id, human_owner_id=user.id, node_type="user",
               privacy_level="private", ai_usage="chat",
               source_key="twitter:2", deleted_at=datetime.utcnow())
    old.set_content("the tweet as it was")
    _db.session.add(old)
    _db.session.commit()
    result = _import(user, [_row(2, "x" * (NODE_CHAR_CAP + 1))],
                     on_deleted="restore")
    assert result["restored"] == 0 and result["too_long"] == 1
    _db.session.expire(old)
    assert old.deleted_at is not None
    assert old.get_content() == "the tweet as it was"


# ── Embedding sweep ──────────────────────────────────────────────────────

ROW_CHARS = 400_000
ROWS = 40


def _entry(user, text):
    n = Node(user_id=user.id, human_owner_id=user.id, node_type="user",
             privacy_level="private", ai_usage="chat")
    n.set_content(text)
    _db.session.add(n)
    _db.session.flush()
    return n


def test_the_candidate_step_holds_one_large_row_at_a_time(app, kms):  # noqa: F811
    one = _user("one")
    two = _user("two")
    for i in range(ROWS):
        _entry(one, chr(65 + i) * ROW_CHARS)
    ordinary_id = _entry(two, "an ordinary entry").id
    _db.session.commit()
    _db.session.expire_all()
    _db.session.close()

    tracemalloc.start()
    out = emb_mod._candidate_nodes(emb_mod.SWEEP_BATCH_SIZE)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert len(out) == ROWS + 1
    assert all(len(text) <= MAX_EMBED_CHARS for _, _, text, _ in out)
    assert [t for n, _, t, _ in out if n.id == ordinary_id] == [
        "an ordinary entry"]
    # Bounded by one row (its stored value and the copies decrypting it
    # makes), not by the ROWS rows of the batch together.
    assert peak < ROWS * ROW_CHARS // 2, peak


def test_the_unchanged_check_still_reads_the_stored_value(app, kms):  # noqa: F811
    from backend.models import NodeEmbedding
    from backend.utils.embeddings import EMBEDDING_MODEL, content_hash
    one = _user("one")
    n = _entry(one, "unchanged text")
    edited_at = datetime.utcnow()
    n.updated_at = edited_at       # a non-content column changed
    _db.session.add(NodeEmbedding(
        node_id=n.id, user_id=one.id, model=EMBEDDING_MODEL,
        content_hash=content_hash(n.content), vector=b"\x00" * 8,
        created_at=datetime(2026, 1, 1), node_updated_at=None))
    _db.session.commit()
    assert emb_mod._candidate_nodes(100) == []
    row = NodeEmbedding.query.filter_by(node_id=n.id).one()
    assert row.node_updated_at == edited_at


def test_a_row_stored_above_the_bound_takes_no_place_in_the_batch(
        app, kms, monkeypatch):  # noqa: F811
    monkeypatch.setattr(emb_mod, "EMBED_MAX_STORED_BYTES", 1_000)
    one = _user("one")
    ordinary = _entry(one, "an ordinary entry")
    _entry(one, "z" * 2_000)          # newer, stored above the bound
    _db.session.commit()
    out = emb_mod._candidate_nodes(1)
    assert [n.id for n, _, _, _ in out] == [ordinary.id]


def test_the_bound_counts_stored_bytes(app, monkeypatch):  # noqa: F811
    """Plain text (encryption off): 600 two-byte characters are 1,200
    bytes, above a 1,000-byte bound; 900 one-byte characters are not."""
    monkeypatch.setattr(emb_mod, "EMBED_MAX_STORED_BYTES", 1_000)
    one = _user("one")
    fits = _entry(one, "a" * 900)
    _entry(one, "é" * 600)
    _db.session.commit()
    assert [n.id for n, _, _, _ in emb_mod._candidate_nodes(10)] == [fits.id]


def _compiled(query, dialect):
    return str(query.statement.compile(dialect=dialect))


def test_the_postgresql_query_reads_the_stored_size_not_the_value(app):  # noqa: F811
    """On PostgreSQL the bound is octet_length, which reads the size kept
    with the value; length() would fetch and decompress every value the
    scan passes."""
    import re
    from sqlalchemy.dialects import postgresql
    sql = _compiled(emb_mod._candidate_query(100, dialect="postgresql"),
                    postgresql.dialect())
    assert "octet_length(node.content)" in sql
    assert not re.search(r"(?<![\w])length\(node\.content\)", sql)


def test_the_sqlite_query_counts_bytes_on_any_version(app):  # noqa: F811
    from sqlalchemy.dialects import sqlite
    sql = _compiled(emb_mod._candidate_query(100, dialect="sqlite"),
                    sqlite.dialect())
    assert "length(CAST(node.content AS BLOB))" in sql
    assert "octet_length" not in sql

"""Shared passes go on past one row that cannot be read.

The embedding sweep (entries and saved references of every account) and
the Commons feed read many accounts' rows in one loop. A row whose
stored value does not decrypt (a key-service error, or a value stored
before text that begins with ENC:v2: was encrypted like any other text)
is left out of that pass, logged by id only, and everything else goes on.
Encryption on, key service replaced by a local stand-in, embedding calls
stubbed; no outside calls.
"""
import sys
from unittest.mock import MagicMock

import pytest
from backend.tests.test_no_replies_for_none import (  # noqa: F401 (fixtures)
    app, task_mod, _user, _client,
)
from backend.extensions import db as _db
from backend.models import (
    ExternalItem, ExternalItemEmbedding, Node, NodeEmbedding,
)
from backend.utils import encryption

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

UNREADABLE = "ENC:v2:AAAA:BBBB"


@pytest.fixture
def kms(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_DISABLED", "false")
    monkeypatch.setenv("GCP_KMS_KEY_NAME",
                       "projects/t/locations/l/keyRings/r/cryptoKeys/k")
    monkeypatch.setattr(encryption, "_wrap_dek", lambda dek: b"wrapped:" + dek)
    calls = []

    def unwrap(w):
        calls.append(w)
        if not w.startswith(b"wrapped:"):
            raise ValueError("key service: unknown wrapped key")
        return w[len(b"wrapped:"):]
    monkeypatch.setattr(encryption, "_unwrap_dek", unwrap)
    assert encryption.is_encryption_enabled()
    return calls


@pytest.fixture
def embed(monkeypatch, app):  # noqa: F811
    app.config["OPENAI_API_KEY_CHAT"] = "fake-key"
    sent = []

    def fake_embed(texts, key, user_id=None):
        sent.append(list(texts))
        return [[0.1, 0.2] for _ in texts]
    monkeypatch.setattr(emb_mod, "embed_texts", fake_embed)
    synced = []
    monkeypatch.setattr(emb_mod, "_sync_pgvector_copies",
                        lambda: synced.append(1))
    return sent, synced


def _stored_unreadable_entry(user, level="private"):
    """An entry whose stored value does not decrypt, written to the
    column directly (as such rows were stored before)."""
    n = Node(user_id=user.id, human_owner_id=user.id, node_type="user",
             privacy_level=level, ai_usage="chat")
    n.content = UNREADABLE
    _db.session.add(n)
    _db.session.flush()
    return n


def _entry(user, text, level="private"):
    n = Node(user_id=user.id, human_owner_id=user.id, node_type="user",
             privacy_level=level, ai_usage="chat")
    n.set_content(text)
    _db.session.add(n)
    _db.session.flush()
    return n


def _embedded(node_id):
    return NodeEmbedding.query.filter_by(node_id=node_id).count()


# ── Embedding sweep: entries ─────────────────────────────────────────────

def test_an_entry_sent_with_such_text_is_embedded_like_any_other(
        app, kms, embed):  # noqa: F811
    sent, _ = embed
    one = _user("one")
    two = _user("two")
    _db.session.commit()
    made = _client(app, one).post("/api/nodes/", json={
        "content": UNREADABLE, "privacy_level": "private", "ai_usage": "chat"})
    assert made.status_code in (200, 201)
    ok = _client(app, two).post("/api/nodes/", json={
        "content": "a new entry", "privacy_level": "private",
        "ai_usage": "chat"})
    assert ok.status_code in (200, 201)

    emb_mod.sweep()
    assert _embedded(made.get_json()["id"]) == 1
    assert _embedded(ok.get_json()["id"]) == 1
    assert sorted(t for batch in sent for t in batch) == sorted(
        [UNREADABLE, "a new entry"])


def test_one_unreadable_entry_does_not_stop_the_sweep_for_others(
        app, kms, embed):  # noqa: F811
    one = _user("one")
    two = _user("two")
    older = _entry(two, "written before")
    bad = _stored_unreadable_entry(one)
    newer = _entry(two, "written after")
    _db.session.commit()

    result = emb_mod.sweep()
    assert result["status"] == "ok" and result["embedded"] == 2
    assert _embedded(older.id) == 1 and _embedded(newer.id) == 1
    assert _embedded(bad.id) == 0

    # Later sweeps go on as well; the row is tried again and skipped.
    later = _entry(two, "written later still")
    _db.session.commit()
    emb_mod.sweep()
    assert _embedded(later.id) == 1
    assert _embedded(bad.id) == 0


def test_the_candidate_list_leaves_out_an_unreadable_entry(app, kms):  # noqa: F811
    one = _user("one")
    two = _user("two")
    _stored_unreadable_entry(one)
    good = _entry(two, "readable")
    _db.session.commit()
    rows = emb_mod._candidate_nodes(100)
    assert [(node.id, text) for node, _, text, _ in rows] == [
        (good.id, "readable")]


# ── Embedding sweep: saved references ────────────────────────────────────

def _reference(user, text=None, url="https://example.com/a", raw=None):
    item = ExternalItem(user_id=user.id, source="web", external_id=url,
                        url=url)
    if raw is not None:
        item.content = raw
    else:
        item.set_content(text)
    _db.session.add(item)
    _db.session.flush()
    return item


def _ref_embedded(item_id):
    return ExternalItemEmbedding.query.filter_by(item_id=item_id).count()


def test_one_unreadable_reference_does_not_stop_reference_embedding(
        app, kms, embed):  # noqa: F811
    _, synced = embed
    one = _user("one")
    two = _user("two")
    older = _reference(two, "saved before", url="https://example.com/0")
    bad = _reference(one, raw=UNREADABLE, url="https://example.com/x")
    newer = _reference(two, "saved after", url="https://example.com/1")
    _db.session.commit()

    result = emb_mod.sweep()
    assert result["external_embedded"] == 2
    assert _ref_embedded(older.id) == 1 and _ref_embedded(newer.id) == 1
    assert _ref_embedded(bad.id) == 0
    assert synced == [1]


def test_the_copies_are_synced_when_the_reference_half_fails(
        app, kms, embed, monkeypatch):  # noqa: F811
    _, synced = embed
    one = _user("one")
    node = _entry(one, "an entry")
    _reference(one, "a reference")
    _db.session.commit()

    def refuse(limit, api_key):
        raise RuntimeError("provider down")
    monkeypatch.setattr(emb_mod, "_embed_external_items", refuse)
    with pytest.raises(RuntimeError):
        emb_mod.sweep()
    assert _embedded(node.id) == 1
    assert synced == [1]


# ── Commons feed ─────────────────────────────────────────────────────────

def test_one_unreadable_public_entry_leaves_the_feed_working(
        app, kms):  # noqa: F811
    from backend.routes.commons import commons_bp
    app.register_blueprint(commons_bp, url_prefix="/api/commons")
    app.config["SHARE_V1"] = True
    reader = _user("reader")
    writer = _user("writer")
    reader.public_sharing_enabled = True
    writer.public_sharing_enabled = True
    shown = _entry(writer, "a published entry", level="public")
    bad = _stored_unreadable_entry(writer, level="public")
    _db.session.commit()

    res = _client(app, reader).get("/api/commons/feed")
    assert res.status_code == 200
    ids = [item["id"] for item in res.get_json()["items"]]
    assert ids == [shown.id]
    assert bad.id not in ids

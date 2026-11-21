"""A key-service outage ends a shared pass; one unreadable row does not.

The passes that read many accounts' rows (the embedding sweep, the
Commons feed, the nightly digest sweep, the profile batch poller) skip a
row whose stored value does not decrypt and go on. When the key service
itself is down (a failure the key-service call retries, about 30 s each)
every row would fail the same way after its own retries, so the pass
ends at the first such failure, as it did before. The key service is a
local stand-in here; "down" raises the provider's 503 at once, so the
tests count the attempts instead of waiting.
"""
import sys
from unittest.mock import MagicMock

import pytest
from google.api_core import exceptions as gexc

from backend.extensions import db as _db
from backend.models import Node, NodeEmbedding
from backend.utils import encryption


class _KeyService:
    def __init__(self):
        self.down = False
        self.unwraps = 0

    def wrap(self, dek):
        return b"wrapped:" + dek

    def unwrap(self, wrapped):
        self.unwraps += 1
        if self.down:
            raise gexc.ServiceUnavailable("the service is unavailable")
        if not wrapped.startswith(b"wrapped:"):
            raise ValueError("key service: unknown wrapped key")
        return wrapped[len(b"wrapped:"):]


def _key_service(monkeypatch):
    service = _KeyService()
    monkeypatch.setenv("ENCRYPTION_DISABLED", "false")
    monkeypatch.setenv("GCP_KMS_KEY_NAME",
                       "projects/t/locations/l/keyRings/r/cryptoKeys/k")
    monkeypatch.setattr(encryption, "_wrap_dek", service.wrap)
    monkeypatch.setattr(encryption, "_unwrap_dek", service.unwrap)
    return service


def test_the_helper_tells_an_outage_from_an_unreadable_value():
    assert encryption.key_service_unavailable(
        gexc.ServiceUnavailable("down"))
    assert encryption.key_service_unavailable(
        RuntimeError("502 Bad Gateway"))
    assert not encryption.key_service_unavailable(
        gexc.InvalidArgument("the ciphertext is invalid"))
    assert not encryption.key_service_unavailable(
        ValueError("not enough values to unpack"))
    try:
        try:
            raise gexc.ServiceUnavailable("down")
        except gexc.ServiceUnavailable as inner:
            raise RuntimeError("reading failed") from inner
    except RuntimeError as outer:
        assert encryption.key_service_unavailable(outer)


# ── Embedding sweep and Commons feed ─────────────────────────────────────

from backend.tests.test_no_replies_for_none import (  # noqa: E402,F401
    app, task_mod, _user, _client,
)

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


def _entries(level="private", n=5):
    rows = []
    for i in range(n):
        user = _user(f"writer{i}")
        if level == "public":
            user.public_sharing_enabled = True
        node = Node(user_id=user.id, human_owner_id=user.id,
                    node_type="user", privacy_level=level, ai_usage="chat")
        node.set_content(f"entry {i}")
        _db.session.add(node)
        rows.append(node)
    _db.session.commit()
    return rows


@pytest.fixture
def embed(monkeypatch, app):  # noqa: F811
    app.config["OPENAI_API_KEY_CHAT"] = "fake-key"
    monkeypatch.setattr(
        emb_mod, "embed_texts",
        lambda texts, key, user_id=None: [[0.1, 0.2] for _ in texts])
    monkeypatch.setattr(emb_mod, "_sync_pgvector_copies", lambda: None)


def test_sweep_skips_one_unreadable_entry_among_readable_ones(
        app, embed, monkeypatch):  # noqa: F811
    _key_service(monkeypatch)
    good = _entries()
    owner = _user("owner")
    bad = Node(user_id=owner.id, human_owner_id=owner.id, node_type="user",
               privacy_level="private", ai_usage="chat")
    bad.content = "ENC:v2:AAAA:BBBB"
    _db.session.add(bad)
    _db.session.commit()

    assert emb_mod.sweep()["embedded"] == len(good)
    assert NodeEmbedding.query.filter_by(node_id=bad.id).count() == 0


def test_sweep_ends_at_the_first_key_service_outage(
        app, embed, monkeypatch):  # noqa: F811
    service = _key_service(monkeypatch)
    _entries()
    service.down = True

    with pytest.raises(gexc.ServiceUnavailable):
        emb_mod.sweep()
    assert service.unwraps == 1
    assert NodeEmbedding.query.count() == 0


def test_commons_feed_fails_at_the_first_key_service_outage(
        app, monkeypatch):  # noqa: F811
    from backend.routes.commons import commons_bp
    app.register_blueprint(commons_bp, url_prefix="/api/commons")
    app.config["SHARE_V1"] = True
    service = _key_service(monkeypatch)
    reader = _user("reader")
    reader.public_sharing_enabled = True
    entries = _entries(level="public")
    for node in entries:              # public rows stored before #257
        node.content = encryption.encrypt_content(node.get_content())
    _db.session.commit()
    service.down = True

    with pytest.raises(gexc.ServiceUnavailable):
        _client(app, reader).get("/api/commons/feed")
    assert service.unwraps == 1

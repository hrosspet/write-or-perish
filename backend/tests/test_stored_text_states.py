"""Every way stored text exists today reads back as it did.

Stored text has these forms:

- a v2 envelope written by ``encrypt_content`` (one data key per value),
- a v2 envelope written with a shared data key (``encrypt_with_dek``:
  imports, dumps, a streaming reply),
- a legacy v1 value (direct key-service encryption),
- plain text: public entries since #257, and rows written while
  encryption was off (local, or before encryption existed),
- a public entry still stored as an envelope from before #257,
- empty or missing text.

These tests pin how each form reads back, how a privacy change moves it,
and what the persist check does with it, for the entry and for every
other model that keeps text through ``encrypt_content``. They run with
encryption on and the key service replaced by a reversible local stand-in
that counts its calls; a few run with encryption off.
"""
import base64
import os
import sys
from unittest.mock import MagicMock

os.environ["ENCRYPTION_DISABLED"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("TWITTER_API_KEY", "fake")
os.environ.setdefault("TWITTER_API_SECRET", "fake")

sys.modules.setdefault("celery", MagicMock())
sys.modules.setdefault("celery.utils", MagicMock())
sys.modules.setdefault("celery.utils.log", MagicMock())
sys.modules.setdefault("celery.result", MagicMock())
sys.modules.setdefault("ffmpeg", MagicMock())

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

for _mod in ["backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

from backend.extensions import db  # noqa: E402
from backend import models  # noqa: E402
from backend.models import User, Node, NodeVersion  # noqa: E402
from backend.utils import encryption  # noqa: E402

V2 = encryption.ENCRYPTED_PREFIX_V2
V1 = encryption.ENCRYPTED_PREFIX_V1


class _KeyService:
    """A reversible stand-in for the key service that counts its calls
    and refuses a wrapped key it did not make."""

    def __init__(self):
        self.wraps = 0
        self.unwraps = 0
        self.v1_decrypts = 0

    def wrap(self, dek):
        self.wraps += 1
        return b"wrapped:" + dek

    def unwrap(self, wrapped):
        self.unwraps += 1
        if not wrapped.startswith(b"wrapped:"):
            raise ValueError("key service: unknown wrapped key")
        return wrapped[len(b"wrapped:"):]

    def kms_decrypt(self, request):
        self.v1_decrypts += 1
        blob = request["ciphertext"]
        if not blob.startswith(b"v1:"):
            raise ValueError("key service: unknown ciphertext")
        out = MagicMock()
        out.plaintext = blob[len(b"v1:"):]
        return out


@pytest.fixture
def kms(monkeypatch):
    service = _KeyService()
    monkeypatch.setenv("ENCRYPTION_DISABLED", "false")
    monkeypatch.setenv(
        "GCP_KMS_KEY_NAME", "projects/t/locations/l/keyRings/r/cryptoKeys/k")
    monkeypatch.setattr(encryption, "_wrap_dek", service.wrap)
    monkeypatch.setattr(encryption, "_unwrap_dek", service.unwrap)
    monkeypatch.setattr(encryption, "_kms_decrypt", service.kms_decrypt)
    assert encryption.is_encryption_enabled()
    return service


@pytest.fixture
def app():
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    db.init_app(app)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


def _v1(plaintext):
    """A legacy v1 value as the key service wrote it."""
    return V1 + base64.b64encode(b"v1:" + plaintext.encode()).decode()


def _user(name="alice"):
    u = User(username=name, approved=True, plan="alpha")
    db.session.add(u)
    db.session.flush()
    return u


def _raw(node):
    return db.session.execute(
        db.text("SELECT content FROM node WHERE id = :id"),
        {"id": node.id}).scalar()


def _store_raw(user, stored, level):
    """An entry whose column holds *stored* exactly, as a row written
    earlier would."""
    n = Node(user_id=user.id, human_owner_id=user.id, node_type="user",
             privacy_level=level)
    db.session.add(n)
    db.session.flush()
    db.session.execute(
        db.text("UPDATE node SET content = :c WHERE id = :id"),
        {"c": stored, "id": n.id})
    db.session.commit()
    db.session.expire(n)
    return n


# ── Entries: each stored form reads back ─────────────────────────────────

def test_private_entry_written_by_set_content_reads_back(app, kms):
    u = _user()
    n = Node(user_id=u.id, human_owner_id=u.id, node_type="user",
             privacy_level="private")
    n.set_content("a private entry")
    db.session.add(n)
    db.session.commit()
    stored = _raw(n)
    assert stored.startswith(V2) and "a private entry" not in stored
    db.session.expire(n)
    assert n.get_content() == "a private entry"
    assert _raw(n) == stored


def test_private_entry_written_with_a_shared_key_reads_back(app, kms):
    u = _user()
    dek = encryption.new_dek()
    rows = []
    for text in ("first imported", "second imported"):
        n = Node(user_id=u.id, human_owner_id=u.id, node_type="user",
                 privacy_level="private")
        n.set_content(text, dek=dek)
        db.session.add(n)
        rows.append(n)
    db.session.commit()
    assert kms.wraps == 1
    for n, text in zip(rows, ("first imported", "second imported")):
        assert _raw(n).startswith(V2)
        db.session.expire(n)
        assert n.get_content() == text


def test_legacy_v1_entry_reads_back(app, kms):
    u = _user()
    n = _store_raw(u, _v1("an early entry"), "private")
    assert n.get_content() == "an early entry"
    assert kms.v1_decrypts == 1


def test_public_entry_is_plain_and_reads_without_the_key_service(app, kms):
    u = _user()
    n = Node(user_id=u.id, human_owner_id=u.id, node_type="user",
             privacy_level="public")
    n.set_content("a published entry")
    db.session.add(n)
    db.session.commit()
    assert _raw(n) == "a published entry"
    db.session.expire(n)
    assert n.get_content() == "a published entry"
    assert kms.wraps == 0 and kms.unwraps == 0


def test_public_entry_stored_as_an_envelope_before_257_reads_back(app, kms):
    """Public rows written before #257 were encrypted and were never
    rewritten; they still read as their text."""
    u = _user()
    stored = encryption.encrypt_content("published long ago")
    n = _store_raw(u, stored, "public")
    assert n.get_content() == "published long ago"
    assert _raw(n) == stored


@pytest.mark.parametrize("level", ["private", "circles", "public"])
def test_plain_entry_from_before_encryption_reads_as_is(app, kms, level):
    u = _user()
    n = _store_raw(u, "written while encryption was off", level)
    assert n.get_content() == "written while encryption was off"
    assert kms.unwraps == 0


@pytest.mark.parametrize("stored", [None, ""])
def test_empty_entry_reads_as_empty(app, kms, stored):
    u = _user()
    n = _store_raw(u, stored, "private")
    assert n.get_content() == ""


def test_streaming_text_reads_back(app, kms):
    u = _user()
    n = Node(user_id=u.id, human_owner_id=u.id, node_type="llm",
             privacy_level="private")
    dek = encryption.new_dek()
    n.streaming_content = encryption.encrypt_with_dek("partial reply", dek)
    db.session.add(n)
    db.session.commit()
    db.session.expire(n)
    assert n.get_streaming_content() == "partial reply"


def test_entry_version_reads_back(app, kms):
    u = _user()
    n = Node(user_id=u.id, human_owner_id=u.id, node_type="user",
             privacy_level="private")
    n.set_content("now")
    db.session.add(n)
    db.session.flush()
    v = NodeVersion(node_id=n.id)
    v.set_content("before")
    legacy = NodeVersion(node_id=n.id, content=_v1("older"))
    plain = NodeVersion(node_id=n.id, content="plain older")
    db.session.add_all([v, legacy, plain])
    db.session.commit()
    for row in (v, legacy, plain):
        db.session.expire(row)
    assert v.content.startswith(V2)
    assert v.get_content() == "before"
    assert legacy.get_content() == "older"
    assert plain.get_content() == "plain older"


# ── Entries: privacy changes from each stored form ───────────────────────

def test_private_to_public_stores_plain_text(app, kms):
    u = _user()
    n = Node(user_id=u.id, human_owner_id=u.id, node_type="user",
             privacy_level="private")
    n.set_content("going public")
    db.session.add(n)
    db.session.commit()
    n.set_privacy_level("public")
    db.session.commit()
    assert _raw(n) == "going public"
    assert n.get_content() == "going public"


def test_public_to_private_encrypts(app, kms):
    u = _user()
    n = Node(user_id=u.id, human_owner_id=u.id, node_type="user",
             privacy_level="public")
    n.set_content("going private")
    db.session.add(n)
    db.session.commit()
    n.set_privacy_level("private")
    db.session.commit()
    assert _raw(n).startswith(V2)
    db.session.expire(n)
    assert n.get_content() == "going private"


def test_public_envelope_from_before_257_made_private_is_not_wrapped_again(
        app, kms):
    u = _user()
    stored = encryption.encrypt_content("published long ago")
    n = _store_raw(u, stored, "public")
    n.set_privacy_level("private")
    db.session.commit()
    assert _raw(n) == stored
    assert n.get_content() == "published long ago"


def test_public_envelope_from_before_257_republished_becomes_plain(app, kms):
    u = _user()
    n = _store_raw(u, encryption.encrypt_content("published long ago"),
                   "public")
    n.set_privacy_level("public")
    db.session.commit()
    assert _raw(n) == "published long ago"


def test_legacy_v1_made_public_becomes_plain(app, kms):
    u = _user()
    n = _store_raw(u, _v1("an early entry"), "private")
    n.set_privacy_level("public")
    db.session.commit()
    assert _raw(n) == "an early entry"


def test_private_envelope_between_private_levels_is_unchanged(app, kms):
    u = _user()
    n = Node(user_id=u.id, human_owner_id=u.id, node_type="user",
             privacy_level="private")
    n.set_content("stays sealed")
    db.session.add(n)
    db.session.commit()
    stored = _raw(n)
    n.set_privacy_level("circles")
    n.set_privacy_level("private")
    db.session.commit()
    assert _raw(n) == stored
    assert kms.wraps == 1


def test_plain_private_entry_from_before_encryption_gets_encrypted(app, kms):
    """The backfill's path (backfill_encrypt_imported): the same level
    again encrypts a plain non-public row."""
    u = _user()
    n = _store_raw(u, "imported before #262", "private")
    n.set_privacy_level("private")
    db.session.commit()
    assert _raw(n).startswith(V2)
    db.session.expire(n)
    assert n.get_content() == "imported before #262"


def test_empty_entry_privacy_change_writes_nothing(app, kms):
    u = _user()
    n = _store_raw(u, None, "private")
    n.set_privacy_level("public")
    n.set_privacy_level("private")
    db.session.commit()
    assert _raw(n) is None
    assert kms.wraps == 0


# ── Entries: the persist check ───────────────────────────────────────────

def test_persist_check_leaves_stored_envelopes_alone(app, kms):
    u = _user()
    n = Node(user_id=u.id, human_owner_id=u.id, node_type="user",
             privacy_level="private")
    n.set_content("sealed")
    db.session.add(n)
    db.session.commit()
    stored = _raw(n)
    legacy = _store_raw(u, _v1("early"), "private")
    old_public = _store_raw(
        u, encryption.encrypt_content("published long ago"), "public")
    old_public_stored = _raw(old_public)
    wraps = kms.wraps
    # An update of another column persists the rows again.
    n.title = "renamed"
    legacy.title = "renamed"
    old_public.title = "renamed"
    db.session.commit()
    assert _raw(n) == stored
    assert _raw(legacy) == _v1("early")
    assert _raw(old_public) == old_public_stored
    assert kms.wraps == wraps


def test_persist_check_encrypts_plain_text_on_a_private_entry(app, kms):
    u = _user()
    n = Node(user_id=u.id, human_owner_id=u.id, node_type="user",
             privacy_level="private", content="raw plain text")
    db.session.add(n)
    db.session.commit()
    assert _raw(n).startswith(V2)
    db.session.expire(n)
    assert n.get_content() == "raw plain text"


def test_persist_check_leaves_public_plain_text_alone(app, kms):
    u = _user()
    n = Node(user_id=u.id, human_owner_id=u.id, node_type="user",
             privacy_level="public", content="open text")
    db.session.add(n)
    db.session.commit()
    assert _raw(n) == "open text"


# ── Encryption off ───────────────────────────────────────────────────────

def test_with_encryption_off_text_is_stored_and_read_as_is(app):
    assert not encryption.is_encryption_enabled()
    u = _user()
    for level in ("private", "public"):
        n = Node(user_id=u.id, human_owner_id=u.id, node_type="user",
                 privacy_level=level)
        n.set_content("local text")
        db.session.add(n)
        db.session.commit()
        assert _raw(n) == "local text"
        db.session.expire(n)
        assert n.get_content() == "local text"


def test_with_encryption_off_a_stored_envelope_reads_as_stored(app):
    assert not encryption.is_encryption_enabled()
    u = _user()
    n = _store_raw(u, V2 + "d3JhcHBlZA==:cGF5bG9hZA==", "private")
    assert n.get_content() == V2 + "d3JhcHBlZA==:cGF5bG9hZA=="


# ── Every other model that keeps text through encrypt_content ────────────

def _other_models():
    """(model, attribute, writer, reader) for every model whose text goes
    through encrypt_content. Readers differ on empty text; those cases
    are covered per model below."""
    def setter(name):
        return lambda row, text: getattr(row, name)(text)

    def getter(name):
        return lambda row: getattr(row, name)()

    return [
        (models.NodeVersion, "content", setter("set_content"), getter("get_content")),
        (models.Draft, "content", setter("set_content"), getter("get_content")),
        (models.UserProfile, "content", setter("set_content"), getter("get_content")),
        (models.UserRecentContext, "content", setter("set_content"), getter("get_content")),
        (models.UserTodo, "content", setter("set_content"), getter("get_content")),
        (models.UserArtifact, "content", setter("set_content"), getter("get_content")),
        (models.UserFeedback, "content", setter("set_content"), getter("get_content")),
        (models.ShareDraft, "content", setter("set_content"), getter("get_content")),
        (models.UserPrompt, "content", setter("set_content"), getter("get_content")),
        (models.NodeTranscriptChunk, "text", setter("set_text"), getter("get_text")),
        (models.ExternalItem, "content", setter("set_content"), getter("get_content")),
        (models.PollResponse, "content", setter("set_content"), getter("get_content")),
        (models.FeedPick, "why", setter("set_why"), getter("get_why")),
        (models.Thread, "name", setter("set_name"), getter("get_name")),
        (models.ExternalAccount, "access_token",
         lambda row, text: row.set_tokens(text), getter("get_access_token")),
        (models.ExternalAccount, "refresh_token",
         lambda row, text: row.set_tokens("access", text), getter("get_refresh_token")),
    ]


_OTHER = _other_models()
_IDS = [f"{m.__name__}.{attr}" for m, attr, _, _ in _OTHER]


@pytest.mark.parametrize("model,attr,write,read", _OTHER, ids=_IDS)
def test_other_model_written_text_reads_back(app, kms, model, attr, write, read):
    row = model()
    write(row, "kept text")
    stored = getattr(row, attr)
    assert stored.startswith(V2) and "kept text" not in stored
    assert read(row) == "kept text"


@pytest.mark.parametrize("model,attr,write,read", _OTHER, ids=_IDS)
def test_other_model_stored_forms_read_back(app, kms, model, attr, write, read):
    shared = encryption.encrypt_with_dek("shared key text", encryption.new_dek())
    for stored, expected in (
            (encryption.encrypt_content("single key text"), "single key text"),
            (shared, "shared key text"),
            (_v1("legacy text"), "legacy text"),
            ("plain stored text", "plain stored text")):
        row = model()
        setattr(row, attr, stored)
        assert read(row) == expected


@pytest.mark.parametrize("model,attr,write,read", _OTHER, ids=_IDS)
def test_other_model_with_encryption_off_keeps_text_as_is(app, model, attr, write, read):
    assert not encryption.is_encryption_enabled()
    row = model()
    write(row, "local text")
    assert getattr(row, attr) == "local text"
    assert read(row) == "local text"

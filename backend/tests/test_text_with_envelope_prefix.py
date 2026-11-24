"""Text that begins like a stored value (ENC:v1:/ENC:v2:) reads back as
it was written.

What a person writes is text, even when it begins with the prefix the
app puts in front of a stored encrypted value, and even when it is an
exact copy of such a stored value. It is encrypted like any other text,
so every later read returns it as it was sent, on private and public
entries, through privacy changes, and in every other model that keeps
text. Runs with encryption on and the key service replaced by a local
stand-in; no outside calls.
"""
import pytest
from backend.tests.test_no_replies_for_none import (  # noqa: F401 (fixtures)
    app, task_mod, _user, _client,
)
from backend.tests.test_stored_text_states import _OTHER, _IDS
from backend.extensions import db as _db
from backend.models import Node
from backend.utils import encryption

PRIVATE_TEXT = "alice's private journal entry"
MADE_UP = "ENC:v2:AAAA:BBBB"


@pytest.fixture
def kms(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_DISABLED", "false")
    monkeypatch.setenv("GCP_KMS_KEY_NAME",
                       "projects/t/locations/l/keyRings/r/cryptoKeys/k")
    monkeypatch.setattr(encryption, "_wrap_dek", lambda dek: b"wrapped:" + dek)

    def unwrap(w):
        if not w.startswith(b"wrapped:"):
            raise ValueError("key service: unknown wrapped key")
        return w[len(b"wrapped:"):]
    monkeypatch.setattr(encryption, "_unwrap_dek", unwrap)
    assert encryption.is_encryption_enabled()


def _raw(node_id):
    return _db.session.execute(_db.text(
        "SELECT content FROM node WHERE id = :i"), {"i": node_id}).scalar()


def _stored_value_of_another_account():
    alice = _user("alice")
    entry = Node(user_id=alice.id, human_owner_id=alice.id, node_type="user",
                 privacy_level="private", ai_usage="none")
    entry.set_content(PRIVATE_TEXT)
    _db.session.add(entry)
    bob = _user("bob")
    _db.session.commit()
    stored = _raw(entry.id)
    assert stored.startswith("ENC:v2:") and PRIVATE_TEXT not in stored
    return entry.id, bob, stored


@pytest.mark.parametrize("level", ["private", "public"])
def test_a_copy_of_a_stored_value_sent_as_an_entry_reads_back_as_sent(
        app, kms, level):  # noqa: F811
    other_id, bob, stored_value = _stored_value_of_another_account()
    bobs = _client(app, bob)
    assert bobs.get(f"/api/nodes/{other_id}").status_code == 404

    made = bobs.post("/api/nodes/", json={
        "content": stored_value, "privacy_level": level, "ai_usage": "none"})
    assert made.status_code in (200, 201)
    nid = made.get_json()["id"]
    assert made.get_json()["content"] == stored_value

    stored = _raw(nid)
    assert stored.startswith("ENC:v2:") and stored != stored_value
    read = bobs.get(f"/api/nodes/{nid}")
    assert read.status_code == 200
    assert read.get_json()["content"] == stored_value
    assert PRIVATE_TEXT not in read.get_data(as_text=True)


@pytest.mark.parametrize("level", ["private", "public"])
def test_text_shaped_like_a_stored_value_is_kept_as_written(app, kms, level):  # noqa: F811
    bob = _user("bob")
    _db.session.commit()
    bobs = _client(app, bob)
    made = bobs.post("/api/nodes/", json={
        "content": MADE_UP, "privacy_level": level, "ai_usage": "none"})
    assert made.status_code in (200, 201)
    nid = made.get_json()["id"]
    assert made.get_json()["content"] == MADE_UP
    assert bobs.get(f"/api/nodes/{nid}").get_json()["content"] == MADE_UP


def test_an_edit_to_such_text_reads_back_as_written(app, kms):  # noqa: F811
    bob = _user("bob")
    _db.session.commit()
    bobs = _client(app, bob)
    nid = bobs.post("/api/nodes/", json={
        "content": "first words", "privacy_level": "private",
        "ai_usage": "none"}).get_json()["id"]
    put = bobs.put(f"/api/nodes/{nid}", json={"content": MADE_UP})
    assert put.status_code == 200
    assert bobs.get(f"/api/nodes/{nid}").get_json()["content"] == MADE_UP


def test_such_text_keeps_reading_as_written_through_privacy_changes(app, kms):  # noqa: F811
    bob = _user("bob")
    _db.session.commit()
    n = Node(user_id=bob.id, human_owner_id=bob.id, node_type="user",
             privacy_level="private")
    n.set_content(MADE_UP)
    _db.session.add(n)
    _db.session.commit()
    for level in ("public", "circles", "public", "private"):
        n.set_privacy_level(level)
        _db.session.commit()
        stored = _raw(n.id)
        assert stored.startswith("ENC:v2:") and stored != MADE_UP
        _db.session.expire(n)
        assert n.get_content() == MADE_UP


def test_public_text_without_the_prefix_stays_plain(app, kms):  # noqa: F811
    bob = _user("bob")
    _db.session.commit()
    n = Node(user_id=bob.id, human_owner_id=bob.id, node_type="user",
             privacy_level="public")
    n.set_content("ENC is how I sign my notes")
    _db.session.add(n)
    _db.session.commit()
    assert _raw(n.id) == "ENC is how I sign my notes"


def test_a_shared_key_writer_keeps_such_text_as_written(app, kms):  # noqa: F811
    bob = _user("bob")
    _db.session.commit()
    dek = encryption.new_dek()
    for level in ("private", "public"):
        n = Node(user_id=bob.id, human_owner_id=bob.id, node_type="user",
                 privacy_level=level)
        n.set_content(MADE_UP, dek=dek)
        _db.session.add(n)
        _db.session.commit()
        assert _raw(n.id) != MADE_UP
        _db.session.expire(n)
        assert n.get_content() == MADE_UP


def test_encrypt_content_encrypts_text_that_begins_with_the_prefix(kms):
    stored = encryption.encrypt_content(PRIVATE_TEXT)
    for text in (stored, MADE_UP, "ENC:v1:abc"):
        again = encryption.encrypt_content(text)
        assert again != text and again.startswith("ENC:v2:")
        assert encryption.decrypt_content(again) == text


@pytest.mark.parametrize("model,attr,write,read", _OTHER, ids=_IDS)
def test_other_models_keep_such_text_as_written(app, kms, model, attr, write, read):  # noqa: F811
    stored_value = encryption.encrypt_content(PRIVATE_TEXT)
    for text in (stored_value, MADE_UP):
        row = model()
        write(row, text)
        assert getattr(row, attr) != text
        assert read(row) == text

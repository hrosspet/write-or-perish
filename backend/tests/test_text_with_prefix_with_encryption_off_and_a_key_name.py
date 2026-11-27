"""Text that itself begins with ENC:v1: or ENC:v2:, written on an
instance that runs with ENCRYPTION_DISABLED=true and a key name.

Stored values with the prefix are decrypted on read there
(encryption.reads_decrypt), so such text is still encrypted with the
key (encrypt_content, encrypt_with_dek): it reads back as written, for
its writer and for every other reader of the thread, with the switch
and after the switch is removed, and no read asks the key service about
a key it does not know. Other text is stored unencrypted. With the
switch and no key name nothing is encrypted or decrypted: such text is
stored and read as it is.

Through the API where a person writes; the key service is a local
stand-in that answers an unknown wrapped key as the real one does."""
import base64
import types

import pytest
from google.api_core import exceptions as gexc
from sqlalchemy import text as sql

from backend.extensions import db as _db
from backend.models import Node, UserArtifact
from backend.utils import encryption
from backend.tests.test_no_replies_for_none import (  # noqa: F401 (fixtures)
    app, task_mod, _user, _client,
)

SHAPED = ("ENC:v2:" + base64.b64encode(b"not a wrapped key").decode()
          + ":" + base64.b64encode(b"x" * 40).decode())
TEXTS = [SHAPED, "ENC:v2:hello", "ENC:v1:a note about formats",
         "ENC:v2:AAAA:BBBB"]


class _KeyService:
    def __init__(self):
        self.wraps = self.unwraps = self.refused = 0

    def encrypt(self, request):
        self.wraps += 1
        return types.SimpleNamespace(ciphertext=b"w:" + request["plaintext"])

    def decrypt(self, request):
        self.unwraps += 1
        blob = request["ciphertext"]
        if not blob.startswith(b"w:") or len(blob) != 34:
            self.refused += 1
            raise gexc.InvalidArgument("the ciphertext is invalid")
        return types.SimpleNamespace(plaintext=blob[2:])

    def forget(self):
        """A process that has read nothing yet."""
        encryption._dek_cache.clear()
        del encryption._dek_cache_order[:]
        self.wraps = self.unwraps = self.refused = 0


@pytest.fixture
def keys(monkeypatch, app):  # noqa: F811
    """Encryption off, the key name set."""
    monkeypatch.setenv("ENCRYPTION_DISABLED", "true")
    monkeypatch.setenv("GCP_KMS_KEY_NAME",
                       "projects/t/locations/l/keyRings/r/cryptoKeys/k")
    service = _KeyService()
    monkeypatch.setattr(encryption, "_kms_encrypt", service.encrypt)
    monkeypatch.setattr(encryption, "_kms_decrypt", service.decrypt)
    service.forget()
    yield service
    service.forget()


def _people(*names):
    people = [_user(name) for name in names]
    for person in people:
        person.public_sharing_enabled = True
    _db.session.commit()
    return people


def _post(client, content, parent=None, level="private"):
    body = {"content": content, "privacy_level": level, "ai_usage": "none"}
    if parent is not None:
        body["parent_id"] = parent
    return client.post("/api/nodes/", json=body)


def _stored(node_id):
    return _db.session.execute(
        sql("SELECT content FROM node WHERE id = :i"),
        {"i": node_id}).scalar()


def _fresh(keys):
    _db.session.expire_all()
    keys.forget()


@pytest.mark.parametrize("typed", TEXTS)
def test_a_public_reply_under_another_accounts_post_reads_back_for_all(
        app, keys, typed):  # noqa: F811
    a, b, c = _people("a", "b", "c")
    root = _post(_client(app, a), "A's public post", level="public")
    assert root.status_code == 201
    root_id = root.get_json()["id"]
    assert _stored(root_id) == "A's public post"       # other text: plain

    reply = _post(_client(app, b), typed, parent=root_id, level="public")
    assert reply.status_code == 201, reply.get_data(as_text=True)[:300]
    reply_id = reply.get_json()["id"]
    stored = _stored(reply_id)
    assert stored.startswith("ENC:v2:") and stored != typed

    _fresh(keys)
    for reader in (a, b, c):
        thread = _client(app, reader).get(f"/api/nodes/{root_id}")
        assert thread.status_code == 200
        assert typed in thread.get_data(as_text=True)
        own = _client(app, reader).get(f"/api/nodes/{reply_id}")
        assert own.status_code == 200
        assert own.get_json()["content"] == typed
    assert keys.refused == 0
    assert keys.unwraps == 1                # the reply's key, asked once


def test_an_own_thread_with_such_an_entry_reads_and_takes_an_edit(
        app, keys):  # noqa: F811
    (a,) = _people("a")
    client = _client(app, a)
    root_id = _post(client, "root").get_json()["id"]
    entry = _post(client, SHAPED, parent=root_id)
    assert entry.status_code == 201
    entry_id = entry.get_json()["id"]

    _fresh(keys)
    assert client.get(f"/api/nodes/{root_id}").status_code == 200
    assert client.get(f"/api/nodes/{entry_id}").get_json()["content"] \
        == SHAPED

    # Edited to other such text: encrypted again, reads back.
    edited = client.put(f"/api/nodes/{entry_id}",
                        json={"content": "ENC:v2:hello"})
    assert edited.status_code == 200, edited.get_data(as_text=True)[:300]
    assert _stored(entry_id).startswith("ENC:v2:")
    assert _stored(entry_id) != "ENC:v2:hello"
    _fresh(keys)
    assert client.get(f"/api/nodes/{entry_id}").get_json()["content"] \
        == "ENC:v2:hello"

    # Edited to ordinary text: stored unencrypted, as everything else.
    edited = client.put(f"/api/nodes/{entry_id}",
                        json={"content": "an ordinary note"})
    assert edited.status_code == 200
    assert _stored(entry_id) == "an ordinary note"
    assert client.get(f"/api/nodes/{root_id}").status_code == 200
    assert keys.refused == 0


@pytest.mark.parametrize("level", ["private", "public"])
def test_the_row_reads_back_after_the_switch_is_removed(
        app, keys, monkeypatch, level):  # noqa: F811
    (a,) = _people("a")
    client = _client(app, a)
    ids = [_post(client, typed, level=level).get_json()["id"]
           for typed in TEXTS]

    monkeypatch.setenv("ENCRYPTION_DISABLED", "false")
    _fresh(keys)
    for node_id, typed in zip(ids, TEXTS):
        got = client.get(f"/api/nodes/{node_id}")
        assert got.status_code == 200
        assert got.get_json()["content"] == typed
    assert keys.refused == 0


def test_model_output_that_begins_with_the_prefix_reads_back(
        app, keys):  # noqa: F811
    (a,) = _people("a")
    root = Node(user_id=a.id, human_owner_id=a.id, node_type="user",
                privacy_level="private")
    root.set_content("a question")
    reply = Node(user_id=a.id, human_owner_id=a.id, node_type="llm",
                 llm_model="m", privacy_level="private")
    _db.session.add_all([root, reply])
    _db.session.flush()
    reply.parent_id = root.id
    # As the reply task stores the text, and as the partial text of a
    # reply in progress is stored (utils/llm_stream: no data key while
    # encryption is off).
    reply.set_content(SHAPED)
    assert encryption.new_dek() is None
    reply.streaming_content = encryption.encrypt_with_dek(SHAPED, None)
    _db.session.commit()
    assert _stored(reply.id).startswith("ENC:v2:")
    assert _stored(reply.id) != SHAPED
    assert reply.streaming_content != SHAPED

    _fresh(keys)
    reply = _db.session.get(Node, reply.id)
    assert reply.get_content() == SHAPED
    assert encryption.decrypt_content(reply.streaming_content) == SHAPED
    assert _client(app, a).get(f"/api/nodes/{root.id}").status_code == 200
    assert keys.refused == 0
    # Ordinary partial text has no key and is stored as it is.
    assert encryption.encrypt_with_dek("so far", None) == "so far"


def test_artifact_text_that_begins_with_the_prefix_reads_back(
        app, keys):  # noqa: F811
    (a,) = _people("a")
    row = UserArtifact(user_id=a.id, kind="formats", title="ENC:v2:hello",
                       description="about formats", generated_by="user")
    row.set_content(SHAPED)
    _db.session.add(row)
    _db.session.commit()
    stored = _db.session.execute(sql(
        "SELECT content, title, title_enc FROM user_artifact WHERE id = :i"),
        {"i": row.id}).one()
    assert stored.content.startswith("ENC:v2:") and stored.content != SHAPED
    assert (stored.title, stored.title_enc) == ("ENC:v2:hello", None)

    _fresh(keys)
    row = _db.session.get(UserArtifact, row.id)
    assert (row.get_content(), row.title) == (SHAPED, "ENC:v2:hello")
    assert keys.refused == 0


def test_other_text_is_stored_unencrypted_with_no_call(app, keys):  # noqa: F811
    (a,) = _people("a")
    client = _client(app, a)
    for typed in ("a note", "about ENC:v2: values", " ENC:v2:hello"):
        node_id = _post(client, typed).get_json()["id"]
        assert _stored(node_id) == typed
    assert (keys.wraps, keys.unwraps) == (0, 0)


@pytest.mark.parametrize("typed", TEXTS)
def test_without_a_key_name_such_text_is_stored_and_read_as_it_is(
        app, keys, monkeypatch, typed):  # noqa: F811
    monkeypatch.setenv("GCP_KMS_KEY_NAME", "")
    (a,) = _people("a")
    client = _client(app, a)
    posted = _post(client, typed)
    assert posted.status_code == 201
    node_id = posted.get_json()["id"]
    assert _stored(node_id) == typed
    assert client.get(f"/api/nodes/{node_id}").get_json()["content"] == typed
    assert encryption.encrypt_with_dek(typed, None) == typed
    assert (keys.wraps, keys.unwraps) == (0, 0)

"""A reply's partial text reads back as written, also when it begins
like a stored value (ENC:v1:/ENC:v2:).

PartialTextWriter keeps the text of a reply while the model writes it.
In a private thread it is always encrypted; in a public one it is stored
as it is, except text that begins with the prefix, which reads decrypt
by: that text is encrypted, so it reads back as the model wrote it.
Encryption on with a local key-service stand-in; no outside calls.
"""
import time

import pytest
from backend.tests.test_no_replies_for_none import (  # noqa: F401 (fixtures)
    app, task_mod, _user,
)
from backend.extensions import db as _db
from backend.models import Node
from backend.utils import encryption
from backend.utils.llm_stream import PartialTextWriter

PRIVATE_TEXT = "alice's private journal entry"


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


def _node(user, level, text, node_type="user"):
    n = Node(user_id=user.id, human_owner_id=user.id, node_type=node_type,
             privacy_level=level, ai_usage="none")
    n.set_content(text)
    _db.session.add(n)
    _db.session.commit()
    return n


def _write(reply, text):
    writer = PartialTextWriter(reply, clock=time.monotonic)
    writer.feed(text)
    writer.flush()
    _db.session.expire_all()
    return _db.session.get(Node, reply.id)


@pytest.mark.parametrize("level", ["private", "public"])
def test_partial_text_copying_a_stored_value_reads_back_as_written(
        app, kms, level):  # noqa: F811
    alice, bob = _user("alice"), _user("bob")
    stored_value = _node(alice, "private", PRIVATE_TEXT).content
    assert stored_value.startswith("ENC:v2:")
    reply = _node(bob, level, "placeholder", node_type="llm")

    reply = _write(reply, stored_value)
    assert reply.streaming_content != stored_value
    assert reply.get_streaming_content() == stored_value


def test_public_partial_text_shaped_like_a_stored_value_reads_as_written(
        app, kms):  # noqa: F811
    bob = _user("bob")
    reply = _node(bob, "public", "placeholder", node_type="llm")
    reply = _write(reply, "ENC:v2:AAAA")
    assert reply.get_streaming_content() == "ENC:v2:AAAA"


def test_public_partial_text_is_otherwise_stored_as_it_is(app, kms):  # noqa: F811
    bob = _user("bob")
    reply = _node(bob, "public", "placeholder", node_type="llm")
    reply = _write(reply, "A public answer, as it is being written")
    assert reply.streaming_content == "A public answer, as it is being written"
    assert reply.get_streaming_content() == reply.streaming_content

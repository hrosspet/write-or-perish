"""Imported nodes must be encrypted at rest (#256); public nodes are plaintext (#257).

Every importer used to build ``Node(content=plaintext)``, writing the raw
column and bypassing ``set_content()`` — the only place KMS envelope
encryption happens. These tests run with encryption ENABLED (KMS
wrap/unwrap mocked) and assert the stored column carries the ``ENC:v2``
envelope while ``get_content()`` still round-trips, for both the shared
ChatGPT/Claude/Markdown helper and the Twitter task path.
"""
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
from backend.models import User, Node  # noqa: E402
from backend.utils import encryption  # noqa: E402

ENC_PREFIX = encryption.ENCRYPTED_PREFIX_V2


@pytest.fixture
def app(monkeypatch):
    # Encryption ON for these tests; KMS replaced by a reversible fake.
    monkeypatch.setenv("ENCRYPTION_DISABLED", "false")
    monkeypatch.setenv("GCP_KMS_KEY_NAME", "projects/t/locations/l/keyRings/r/cryptoKeys/k")
    monkeypatch.setattr(encryption, "_wrap_dek", lambda dek: b"wrapped:" + dek)
    monkeypatch.setattr(encryption, "_unwrap_dek", lambda w: w[len(b"wrapped:"):])
    assert encryption.is_encryption_enabled()

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


def _user():
    u = User(username="alice", approved=True, plan="alpha")
    db.session.add(u)
    db.session.flush()
    return u


def _assert_encrypted(node, plaintext):
    raw = db.session.execute(
        db.text("SELECT content FROM node WHERE id = :id"), {"id": node.id}
    ).scalar()
    assert raw.startswith(ENC_PREFIX), raw[:40]
    assert plaintext not in raw
    assert node.get_content() == plaintext


def test_shared_import_helper_encrypts(app):
    from backend.routes.import_data import _add_imported_message_nodes
    u = _user()
    tip, created = _add_imported_message_nodes(
        user_id=u.id, human_owner_id=u.id, parent_id=None, node_type="user",
        llm_model=None, node_content="a private chat message",
        privacy_level="private", ai_usage="none", source_key="chatgpt:1",
        msg_created_at=None, origin="chatgpt",
    )
    db.session.commit()
    assert created == 1
    _assert_encrypted(tip, "a private chat message")


def test_shared_import_helper_encrypts_every_split_segment(app, monkeypatch):
    from backend.routes import import_data
    from backend.utils import node_split
    monkeypatch.setattr(node_split, "split_text_at_cap", lambda t: [t[:5], t[5:]])
    u = _user()
    tip, created = import_data._add_imported_message_nodes(
        user_id=u.id, human_owner_id=u.id, parent_id=None, node_type="user",
        llm_model=None, node_content="hello world", privacy_level="private",
        ai_usage="none", source_key="claude:1", msg_created_at=None, origin="claude",
    )
    db.session.commit()
    assert created == 2
    parts = Node.query.filter_by(human_owner_id=u.id).order_by(Node.id).all()
    _assert_encrypted(parts[0], "hello")
    _assert_encrypted(parts[1], " world")
    assert parts[1].id == tip.id and parts[1].parent_id == parts[0].id


def test_twitter_nodes_encrypt(app):
    from backend.routes.import_data import create_twitter_nodes
    u = _user()
    rows = [{"id_str": "1", "full_text": "a tweet", "created_at": "Mon Aug 24 10:00:00 +0000 2026",
             "is_reply": False, "token_count": 2}]
    create_twitter_nodes(
        user_id=u.id, rows=iter(rows), total=1, import_type="separate_nodes",
        include_replies=False, privacy_level="private", ai_usage="none", on_deleted=None,
    )
    node = Node.query.filter_by(source_key="twitter:1").one()
    _assert_encrypted(node, "a tweet")


def test_restore_re_encrypts(app):
    from backend.routes.import_data import _restore_node
    from datetime import datetime
    u = _user()
    n = Node(user_id=u.id, human_owner_id=u.id, node_type="user",
             source_key="twitter:9", deleted_at=datetime.utcnow())
    n.set_content("old")
    db.session.add(n)
    db.session.commit()
    _restore_node(n.id, "restored text", "private", "none")
    db.session.commit()
    assert n.deleted_at is None
    _assert_encrypted(n, "restored text")


# ── #257: public nodes are stored in plaintext; transitions move content ──

def _raw(node):
    return db.session.execute(
        db.text("SELECT content FROM node WHERE id = :id"), {"id": node.id}).scalar()


def test_public_import_is_stored_plaintext(app):
    from backend.routes.import_data import create_twitter_nodes
    u = _user()
    rows = [{"id_str": "1", "full_text": "a public tweet", "created_at": "Mon Aug 24 10:00:00 +0000 2026",
             "is_reply": False, "token_count": 3}]
    create_twitter_nodes(
        user_id=u.id, rows=iter(rows), total=1, import_type="separate_nodes",
        include_replies=False, privacy_level="public", ai_usage="chat", on_deleted=None,
    )
    node = Node.query.filter_by(source_key="twitter:1").one()
    assert _raw(node) == "a public tweet"
    assert node.get_content() == "a public tweet"


def test_set_privacy_level_moves_content_across_the_boundary(app):
    u = _user()
    n = Node(user_id=u.id, human_owner_id=u.id, node_type="user", privacy_level="private")
    n.set_content("secret")
    db.session.add(n)
    db.session.commit()
    assert _raw(n).startswith(ENC_PREFIX)

    n.set_privacy_level("public")
    db.session.commit()
    assert _raw(n) == "secret"

    n.set_privacy_level("private")
    db.session.commit()
    _assert_encrypted(n, "secret")

    n.set_privacy_level("circles")  # anything non-public stays encrypted
    db.session.commit()
    _assert_encrypted(n, "secret")


def test_persist_guard_heals_plaintext_on_private_node(app, caplog):
    u = _user()
    n = Node(user_id=u.id, human_owner_id=u.id, node_type="user",
             privacy_level="private", content="leaked plaintext")  # the #256 bug shape
    db.session.add(n)
    with caplog.at_level("WARNING"):
        db.session.commit()
    _assert_encrypted(n, "leaked plaintext")
    assert "plaintext content" in caplog.text

    # raw assignment on update is healed too
    n.privacy_level = "private"
    n.content = "edited raw"
    db.session.commit()
    _assert_encrypted(n, "edited raw")

    # public nodes are left alone
    p = Node(user_id=u.id, human_owner_id=u.id, node_type="user",
             privacy_level="public", content="open")
    db.session.add(p)
    db.session.commit()
    assert _raw(p) == "open"


def test_reimport_settings_change_reencrypts_skipped_nodes(app):
    from backend.routes.import_data import _apply_settings_to_skipped
    u = _user()
    n = Node(user_id=u.id, human_owner_id=u.id, node_type="user",
             privacy_level="public", ai_usage="chat", source_key="twitter:5")
    n.set_content("was public")
    db.session.add(n)
    db.session.commit()
    assert _raw(n) == "was public"

    assert _apply_settings_to_skipped([n.id, None], "private", "none") == 1
    db.session.commit()
    assert n.ai_usage == "none"
    _assert_encrypted(n, "was public")
    # no-op when nothing differs
    assert _apply_settings_to_skipped([n.id], "private", "none") == 0

"""backend/scripts/backfill_encrypt_imported.py (#265): re-encrypts the
imported, non-public rows that predate #262 and still sit in plaintext.
Encryption ON with the KMS wrap mocked; plaintext rows are seeded through
raw SQL because the persist-time guard would otherwise heal them."""
import importlib.util
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


def _load_script():
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "scripts", "backfill_encrypt_imported.py")
    spec = importlib.util.spec_from_file_location("_bf_encrypt", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_DISABLED", "false")
    monkeypatch.setenv("GCP_KMS_KEY_NAME",
                       "projects/t/locations/l/keyRings/r/cryptoKeys/k")
    monkeypatch.setattr(encryption, "_wrap_dek", lambda dek: b"wrapped:" + dek)
    monkeypatch.setattr(encryption, "_unwrap_dek",
                        lambda w: w[len(b"wrapped:"):])
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


def _seed(user, text, *, source_key, privacy="private", plaintext=True):
    node = Node(user_id=user.id, human_owner_id=user.id, node_type="user",
                privacy_level=privacy, ai_usage="chat", source_key=source_key)
    node.set_content(text)
    db.session.add(node)
    db.session.flush()
    if plaintext:
        # Bypass the model (and the persist-time guard) the way the old
        # importers effectively did.
        db.session.execute(db.text("UPDATE node SET content = :c WHERE id = :id"),
                           {"c": text, "id": node.id})
    db.session.commit()
    return node.id


def _raw(node_id):
    return db.session.execute(
        db.text("SELECT content FROM node WHERE id = :id"),
        {"id": node_id}).scalar()


def test_backfill_encrypts_only_plaintext_non_public_imports(app):
    mod = _load_script()
    u = User(username="alice", approved=True, plan="alpha")
    db.session.add(u)
    db.session.flush()
    plain_private = _seed(u, "private import", source_key="chatgpt:1")
    plain_public = _seed(u, "public import", source_key="chatgpt:2",
                         privacy="public")
    already = _seed(u, "already encrypted", source_key="claude:3",
                    plaintext=False)
    native = _seed(u, "native plaintext", source_key=None)

    assert _raw(plain_private) == "private import"
    assert mod.count_pending() == (1, 1)

    assert mod.encrypt_pending(chunk=1) == 1
    db.session.expire_all()

    assert _raw(plain_private).startswith(ENC_PREFIX)
    assert db.session.get(Node, plain_private).get_content() == "private import"
    # Public stays plaintext (#257); already-encrypted untouched; native
    # rows (no source_key) are outside this backfill's scope.
    assert _raw(plain_public) == "public import"
    assert _raw(already).startswith(ENC_PREFIX)
    assert _raw(native) == "native plaintext"
    assert mod.count_pending() == (0, 0)


def test_backfill_limit_and_resume(app):
    mod = _load_script()
    u = User(username="bob", approved=True, plan="alpha")
    db.session.add(u)
    db.session.flush()
    ids = [_seed(u, f"row {i}", source_key=f"twitter:{i}") for i in range(5)]

    assert mod.encrypt_pending(limit=2, chunk=10) == 2
    db.session.expire_all()
    assert mod.count_pending()[0] == 3
    # Lowest ids first, so a staged run is deterministic and resumable.
    assert _raw(ids[0]).startswith(ENC_PREFIX)
    assert _raw(ids[1]).startswith(ENC_PREFIX)
    assert _raw(ids[2]) == "row 2"

    assert mod.encrypt_pending(chunk=2) == 3
    db.session.expire_all()
    assert mod.count_pending() == (0, 0)
    assert all(_raw(i).startswith(ENC_PREFIX) for i in ids)


def test_empty_content_rows_are_not_pending(app):
    """A row whose content is '' has nothing to encrypt: set_privacy_level
    and encrypt_content both no-op on a falsy value, so it can never leave
    the pending set. Counting it as pending made encrypt_pending re-select
    the same rows every chunk — reported on prod as "encrypted 500 ... 39
    still pending" for 39 rows, and an unbounded run never terminates."""
    mod = _load_script()
    u = User(username="carol", approved=True, plan="alpha")
    db.session.add(u)
    db.session.flush()
    empty = _seed(u, "", source_key="chatgpt:e")
    real = _seed(u, "real text", source_key="chatgpt:r")

    assert _raw(empty) == ""
    assert mod.count_pending() == (1, 1)

    assert mod.encrypt_pending(chunk=10) == 1
    db.session.expire_all()
    assert _raw(real).startswith(ENC_PREFIX)
    assert _raw(empty) == ""
    assert mod.count_pending() == (0, 0)


def test_unencryptable_chunk_stops_instead_of_looping(app):
    """Belt and braces: if some future row lands in the pending set that
    the encryptor cannot change, the loop stops after one chunk with a
    message instead of spinning until --limit (or forever, without one)."""
    mod = _load_script()
    u = User(username="dave", approved=True, plan="alpha")
    db.session.add(u)
    db.session.flush()
    stuck = _seed(u, "stuck", source_key="chatgpt:s")

    # Make it unencryptable the way an empty row used to be.
    monkey = mod.Node.set_privacy_level
    mod.Node.set_privacy_level = lambda self, level: None
    try:
        lines = []
        assert mod.encrypt_pending(limit=500, chunk=10, log=lines.append) == 0
    finally:
        mod.Node.set_privacy_level = monkey

    assert any("no progress" in line for line in lines), lines
    assert _raw(stuck) == "stuck"

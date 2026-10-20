"""Initial profile generation must size the corpus with SQL, not by
rendering it (backend/tasks/exports._do_initial_generation).

Regression for the staging OOM after a 61k-node Twitter import: the
unbudgeted `build_user_export_content(max_tokens=None)` measurement
loaded and decrypted every node at once. Now `_estimate_source_tokens`
decides whether there is anything to do, and the planned chunk loop
renders budgeted windows only.
"""
import os
import sys
from datetime import datetime
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

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

for _mod in ["backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

from backend.extensions import db  # noqa: E402
from backend.models import User, Node  # noqa: E402

ex = None


@pytest.fixture
def app():
    global ex
    import backend.tasks.exports as _ex
    ex = _ex
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


def _user(name="alice"):
    u = User(username=name, approved=True, plan="alpha")
    db.session.add(u)
    db.session.flush()
    return u


def _node(user, tokens, ai_usage="train", deleted=False, owner=None):
    n = Node(user_id=user.id, human_owner_id=(owner or user).id,
             node_type="user", token_count=tokens, ai_usage=ai_usage,
             deleted_at=datetime.utcnow() if deleted else None)
    n.set_content("x")
    db.session.add(n)
    db.session.flush()
    return n


def test_estimate_counts_only_ai_readable_alive_nodes_in_scope(app):
    u, other = _user(), _user("bob")
    _node(u, 100)
    _node(u, 50, ai_usage="chat")
    _node(u, 999, ai_usage="none")      # not AI-readable
    _node(u, 999, deleted=True)         # soft-deleted
    _node(other, 999)                   # someone else's
    _node(other, 7, owner=u)            # addressed to u (human_owner_id)
    assert ex._estimate_source_tokens(u) == 157
    assert ex._estimate_source_tokens(other) == 1006  # own 999 + the node addressed to alice


def test_large_corpus_never_renders_unbudgeted_export(app, monkeypatch):
    """The from-scratch build goes straight to the planned chunk loop
    (backend/utils/chunk_plan.py): no unbudgeted render of the whole
    corpus decides anything, and a corpus that plans into one chunk is
    simply the loop's k == 1 case (tests/test_chunk_loop_planning.py)."""
    u = _user()
    monkeypatch.setattr(ex, "_estimate_source_tokens", lambda user: 1_500_000)
    monkeypatch.setattr(ex, "_load_prompt", lambda *a, **k: "T {user_export}")
    export = MagicMock()
    monkeypatch.setattr(ex, "build_user_export_content", export)
    iterative = MagicMock(return_value="iterative")
    monkeypatch.setattr(ex, "_iterative_generation", iterative)

    assert ex._do_initial_generation(MagicMock(), u, "m", 1000, {}) == "iterative"

    export.assert_not_called()
    assert iterative.call_args.args[3] == "T {user_export}"   # the generation template


def test_empty_corpus_raises(app, monkeypatch):
    u = _user()
    monkeypatch.setattr(ex, "_load_prompt", lambda *a, **k: "T")
    with pytest.raises(ValueError, match="No writing"):
        ex._do_initial_generation(MagicMock(), u, "m", 1000, {})

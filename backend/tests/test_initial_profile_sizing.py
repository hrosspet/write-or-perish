"""Initial profile generation must size the corpus with SQL, not by
rendering it (backend/tasks/exports._do_initial_generation).

Regression for the staging OOM after a 61k-node Twitter import: the
unbudgeted `build_user_export_content(max_tokens=None)` measurement
loaded and decrypted every node at once. Now `_estimate_source_tokens`
decides; the full export is only built when it plausibly fits.
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


def _run_initial(monkeypatch, user, estimate, budget_tokens=10_000):
    """Drive _do_initial_generation with the export builder and both
    generation branches mocked; returns (export_mock, single, iterative)."""
    monkeypatch.setattr(ex, "_estimate_source_tokens", lambda u: estimate)
    monkeypatch.setattr(ex, "_load_prompt", lambda *a, **k: "T {user_export}")
    export = MagicMock(return_value={"content": "c", "token_count": estimate,
                                     "latest_node_created_at": None})
    monkeypatch.setattr(ex, "build_user_export_content", export)
    single = MagicMock(return_value="single")
    iterative = MagicMock(return_value="iterative")
    monkeypatch.setattr(ex, "_single_pass_generation", single)
    monkeypatch.setattr(ex, "_iterative_generation", iterative)
    task = MagicMock()
    # context_window // 2 - prompt - out - 500 == budget_tokens
    context_window = 2 * (budget_tokens + 3 + 1000 + 500)
    result = ex._do_initial_generation(task, user, "m", context_window, 1000, {})
    return result, export, single, iterative


def test_large_corpus_never_renders_unbudgeted_export(app, monkeypatch):
    u = _user()
    result, export, single, iterative = _run_initial(monkeypatch, u, estimate=1_500_000)
    assert result == "iterative"
    export.assert_not_called()
    single.assert_not_called()


def test_small_corpus_keeps_single_pass_path(app, monkeypatch):
    u = _user()
    result, export, single, iterative = _run_initial(monkeypatch, u, estimate=2_000)
    assert result == "single"
    export.assert_called_once()
    assert export.call_args.kwargs["max_tokens"] is None
    iterative.assert_not_called()


def test_underestimate_falls_back_to_iterative(app, monkeypatch):
    """SQL sum says it fits, the rendered export says it doesn't."""
    u = _user()
    monkeypatch.setattr(ex, "_estimate_source_tokens", lambda user: 5_000)
    monkeypatch.setattr(ex, "_load_prompt", lambda *a, **k: "T {user_export}")
    monkeypatch.setattr(ex, "build_user_export_content", MagicMock(return_value={
        "content": "c", "token_count": 50_000, "latest_node_created_at": None}))
    iterative = MagicMock(return_value="iterative")
    monkeypatch.setattr(ex, "_iterative_generation", iterative)
    monkeypatch.setattr(ex, "_single_pass_generation", MagicMock(return_value="single"))
    assert ex._do_initial_generation(MagicMock(), u, "m", 2 * 11_503, 1000, {}) == "iterative"


def test_empty_corpus_raises(app, monkeypatch):
    u = _user()
    monkeypatch.setattr(ex, "_load_prompt", lambda *a, **k: "T")
    with pytest.raises(ValueError, match="No writing"):
        ex._do_initial_generation(MagicMock(), u, "m", 100_000, 1000, {})

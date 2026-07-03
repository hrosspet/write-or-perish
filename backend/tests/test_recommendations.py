"""Tests for the external-item retrieval layer (quote-as-response).

Covers: query composition (thread tail privacy filter, intentions/profile
ai_usage gates, char budgets) and ranking (mocked embeddings, top-k,
score floor). The PoC recommendations endpoint was superseded by
quote-as-response (#208); these utils remain as the retrieval layer for
the overnight batch pre-selection. No OpenAI traffic — embed_texts is
mocked.
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

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

from backend.extensions import db as _db  # noqa: E402
from backend.models import (  # noqa: E402
    User, Node, UserArtifact, UserProfile,
    ExternalItem, ExternalItemEmbedding,
)
from backend.utils.embeddings import pack_vector  # noqa: E402
from backend.utils import recommendations as reco  # noqa: E402


@pytest.fixture
def app():
    from flask_login import LoginManager

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["OPENAI_API_KEY_CHAT"] = "fake-key"
    app.config["DOWNLOAD_V1"] = True

    _db.init_app(app)
    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from backend.routes.external import external_bp
    app.register_blueprint(external_bp, url_prefix="/api/external")

    with app.app_context():
        _db.create_all()
        user = User(username="tester")
        _db.session.add(user)
        _db.session.commit()
        yield app
        _db.session.rollback()
        _db.drop_all()


@pytest.fixture
def client(app):
    client = app.test_client()
    user = User.query.first()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return client


def _mk_node(uid, content, ai_usage="chat", parent=None, node_type="user"):
    node = Node(user_id=uid, node_type=node_type, ai_usage=ai_usage,
                parent_id=parent.id if parent else None,
                human_owner_id=uid)
    node.set_content(content)
    _db.session.add(node)
    _db.session.commit()
    return node


def _mk_item(uid, content, vector, author="someone"):
    item = ExternalItem(user_id=uid, source="twitter_bookmark",
                        external_id=f"x-{content[:8]}-{len(content)}",
                        author_handle=author, url="https://x.com/x/status/1")
    item.set_content(content)
    _db.session.add(item)
    _db.session.flush()
    emb = ExternalItemEmbedding(
        item_id=item.id, user_id=uid, model="test",
        content_hash="h", vector=pack_vector(vector))
    _db.session.add(emb)
    _db.session.commit()
    return item


# ── Query composition ────────────────────────────────────────────────────

def test_compose_query_uses_thread_intentions_profile(app):
    with app.app_context():
        uid = User.query.first().id
        root = _mk_node(uid, "thinking about meditation retreats")
        art = UserArtifact(user_id=uid, kind="intentions",
                           title="Intentions", generated_by="test")
        art.set_content("deepen contemplative practice")
        profile = UserProfile(user_id=uid, generated_by="test",
                              tokens_used=0)
        profile.set_content("a person who values stillness")
        _db.session.add_all([art, profile])
        _db.session.commit()

        q = reco.compose_recommendation_query(uid, root)
        assert "meditation retreats" in q
        assert "deepen contemplative practice" in q
        assert "a person who values stillness" in q


def test_compose_query_respects_ai_usage(app):
    with app.app_context():
        uid = User.query.first().id
        root = _mk_node(uid, "visible thoughts")
        hidden = _mk_node(uid, "private musings", ai_usage="none",
                          parent=root)
        art = UserArtifact(user_id=uid, kind="intentions",
                           title="Intentions", generated_by="test",
                           ai_usage="none")
        art.set_content("secret intentions")
        _db.session.add(art)
        _db.session.commit()

        q = reco.compose_recommendation_query(uid, hidden)
        # The none-usage focal node is excluded; its parent is included.
        assert "private musings" not in q
        assert "visible thoughts" in q
        assert "secret intentions" not in q


def test_thread_tail_walks_parents_newest_first(app):
    with app.app_context():
        uid = User.query.first().id
        root = _mk_node(uid, "oldest root message")
        child = _mk_node(uid, "newest reply", parent=root)
        text = reco._thread_tail_text(child)
        assert text.index("newest reply") < text.index("oldest root message")


# ── Ranking ──────────────────────────────────────────────────────────────

def test_recommend_ranks_by_similarity(app, monkeypatch):
    with app.app_context():
        uid = User.query.first().id
        node = _mk_node(uid, "I want to learn about zen")
        _mk_item(uid, "a thread about zen practice", [1.0, 0.0, 0.0],
                 author="zenith")
        _mk_item(uid, "a recipe for goulash", [0.0, 1.0, 0.0],
                 author="chef")

        monkeypatch.setattr(reco, "embed_texts",
                            lambda texts, key, **kw: [[0.9, 0.1, 0.0]])
        items = reco.recommend_external_items(
            uid, node, "fake-key", k=3, min_score=0.0)
        assert [i["author_handle"] for i in items] == ["zenith", "chef"]
        assert items[0]["content"].startswith("a thread about zen")
        # With the default floor, the orthogonal item drops out.
        items = reco.recommend_external_items(uid, node, "fake-key", k=3)
        assert [i["author_handle"] for i in items] == ["zenith"]


def test_recommend_applies_k_and_score_floor(app, monkeypatch):
    with app.app_context():
        uid = User.query.first().id
        node = _mk_node(uid, "query context")
        _mk_item(uid, "close match", [1.0, 0.0, 0.0])
        _mk_item(uid, "orthogonal", [0.0, 1.0, 0.0])

        monkeypatch.setattr(reco, "embed_texts",
                            lambda texts, key, **kw: [[1.0, 0.0, 0.0]])
        items = reco.recommend_external_items(
            uid, node, "fake-key", k=1, min_score=0.5)
        assert len(items) == 1
        assert items[0]["content"] == "close match"


def test_recommend_empty_without_items(app, monkeypatch):
    with app.app_context():
        uid = User.query.first().id
        node = _mk_node(uid, "anything")
        called = []
        monkeypatch.setattr(
            reco, "embed_texts",
            lambda *a, **kw: called.append(1) or [[1.0]])
        assert reco.recommend_external_items(uid, node, "fake-key") == []
        # No items -> no query embed, no cost.
        assert called == []

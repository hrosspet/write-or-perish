"""A change anywhere in a public thread drops the cached /node/<id> page
of every node of that thread: each of those URLs serves the whole
thread's page from a cache entry of its own.

Harness from test_forum.py, plus the public pages and an in-memory
stand-in for Redis.
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

_mock_llm_task_module = MagicMock()
_mock_task_result = MagicMock()
_mock_task_result.id = "fake-task-id"
_mock_llm_task_module.generate_llm_response.delay.return_value = (
    _mock_task_result
)
sys.modules["backend.tasks.llm_completion"] = _mock_llm_task_module

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login          # noqa: E402
from backend.extensions import db as _db         # noqa: E402
from backend.models import User, Node            # noqa: E402
import backend.models as _real_backend_models    # noqa: E402


def _make_app():
    from flask_login import LoginManager

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["SHARE_V1"] = True
    app.config["DEFAULT_LLM_MODEL"] = "gpt-5"
    app.config["SUPPORTED_MODELS"] = {
        "gpt-5": {"provider": "openai", "api_model": "gpt-5"},
    }
    app.config["FRONTEND_BUILD_DIR"] = None

    _db.init_app(app)
    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from backend.routes.nodes import nodes_bp
    from backend.routes.public_pages import public_pages_bp
    app.register_blueprint(nodes_bp, url_prefix="/api/nodes")
    app.register_blueprint(public_pages_bp)

    @app.before_request
    def _forget_cached_user():
        # One app context stays open, so flask-login's cached user in g
        # would carry over from one test client to the next.
        from flask import g
        g.pop("_login_user", None)
    return app


@pytest.fixture
def app():
    _affected = lambda k: (  # noqa: E731
        k == "flask_login"
        or k.startswith("backend.routes")
        or k == "backend.models"
    )
    saved = {k: sys.modules[k] for k in list(sys.modules) if _affected(k)}

    sys.modules["flask_login"] = _real_flask_login
    sys.modules["backend.models"] = _real_backend_models
    for _k in [k for k in list(sys.modules) if k.startswith("backend.routes")]:
        del sys.modules[_k]

    app = _make_app()
    with app.app_context():
        _db.create_all()
        _db.session.add_all([
            User(username="author", default_ai_usage="chat",
                 public_sharing_enabled=True),
            User(username="visitor", default_ai_usage="chat",
                 public_sharing_enabled=True),
        ])
        _db.session.commit()
        yield app
        _db.session.remove()
        _db.drop_all()

    for k in [k for k in list(sys.modules) if _affected(k)]:
        del sys.modules[k]
    sys.modules.update(saved)


class _FakeRedis:
    def __init__(self):
        self.store = {}
        self.deletes = []

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value

    def delete(self, *keys):
        self.deletes.append(len(keys))
        for key in keys:
            self.store.pop(key, None)


@pytest.fixture
def fake(monkeypatch):
    from backend.utils import public_cache
    redis = _FakeRedis()
    monkeypatch.setattr(public_cache, "_redis", lambda: redis)
    return redis


def _client_for(app, username):
    client = app.test_client()
    user = User.query.filter_by(username=username).first()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return client


def _mk_node(username, content, parent=None, privacy="public"):
    user = User.query.filter_by(username=username).first()
    node = Node(user_id=user.id, human_owner_id=user.id,
                parent_id=parent.id if parent else None,
                node_type="user", privacy_level=privacy, ai_usage="chat")
    node.set_content(content)
    _db.session.add(node)
    _db.session.commit()
    return node


TEXT = "text the author takes back"


def _thread(app):
    root = _mk_node("author", "# Piece\n\n" + TEXT)
    reply = _mk_node("visitor", "a reply by someone else", parent=root)
    deeper = _mk_node("author", "a reply to the reply", parent=reply)
    anon = app.test_client()
    for node in (root, reply, deeper):
        assert TEXT in anon.get(f"/node/{node.id}").get_data(as_text=True)
    return root, reply, deeper, anon


def _gone_everywhere(anon, *nodes, text=TEXT):
    for node in nodes:
        assert text not in anon.get(f"/node/{node.id}").get_data(as_text=True)


def test_root_set_private_drops_every_reply_page(app, fake):
    root, reply, deeper, anon = _thread(app)
    r = _client_for(app, "author").put(f"/api/nodes/{root.id}", json={
        "content": "# Piece\n\n" + TEXT, "privacy_level": "private"})
    assert r.status_code == 200
    _gone_everywhere(anon, root, reply, deeper)


def test_root_deleted_drops_every_reply_page(app, fake):
    root, reply, deeper, anon = _thread(app)
    assert _client_for(app, "author").delete(
        f"/api/nodes/{root.id}").status_code == 200
    _gone_everywhere(anon, root, reply, deeper)


def test_root_edit_reaches_every_reply_page(app, fake):
    root, reply, deeper, anon = _thread(app)
    r = _client_for(app, "author").put(f"/api/nodes/{root.id}", json={
        "content": "# Piece\n\nrewritten"})
    assert r.status_code == 200
    _gone_everywhere(anon, root, reply, deeper)
    assert "rewritten" in anon.get(f"/node/{deeper.id}").get_data(
        as_text=True)


def test_sharing_switched_off_drops_every_page_of_its_threads(app, fake):
    from backend.utils import public_cache
    root, reply, deeper, anon = _thread(app)
    author = User.query.filter_by(username="author").first()
    author.public_sharing_enabled = False
    _db.session.commit()
    public_cache.invalidate_for_user(author)
    _gone_everywhere(anon, root, reply, deeper)


def test_reply_taken_back_drops_its_siblings_pages(app, fake):
    root = _mk_node("author", "# Piece\n\nbody")
    mine = _mk_node("visitor", "REPLY-TAKEN-BACK", parent=root)
    sibling = _mk_node("author", "another reply", parent=root)
    anon = app.test_client()
    assert "REPLY-TAKEN-BACK" in anon.get(
        f"/node/{sibling.id}").get_data(as_text=True)
    r = _client_for(app, "visitor").put(f"/api/nodes/{mine.id}", json={
        "content": "REPLY-TAKEN-BACK", "privacy_level": "private"})
    assert r.status_code == 200
    _gone_everywhere(anon, root, sibling, text="REPLY-TAKEN-BACK")


def test_settings_change_of_many_nodes_drops_their_threads_pages(app, fake):
    from backend.utils import public_cache
    root, reply, deeper, anon = _thread(app)
    root.privacy_level = "private"
    _db.session.commit()
    public_cache.invalidate_for_nodes([root.id])
    _gone_everywhere(anon, root, reply, deeper)


def test_large_drop_goes_to_redis_in_batches(app, fake):
    from backend.utils import public_cache
    public_cache.invalidate(*[f"/node/{i}" for i in range(2500)])
    assert fake.deletes == [1000, 1000, 500]

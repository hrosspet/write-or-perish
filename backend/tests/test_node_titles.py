"""Tests for GET /nodes/titles — the batch lookup behind in-text node links.

MarkdownBody renders a bare `https://loore.org/node/123` as the target's
title. These pin down what that title is (thread name over first line,
markdown markers stripped) and that invisible nodes come back as null
without distinguishing "private" from "missing".
"""
import os
import sys
from unittest.mock import MagicMock

# ── Environment ──────────────────────────────────────────────────────────
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

import flask_login as _real_flask_login  # noqa: E402
from backend.extensions import db as _db  # noqa: E402
from backend.models import User, Node, Thread  # noqa: E402
import backend.models as _real_backend_models  # noqa: E402


def _make_app():
    from flask_login import LoginManager

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True

    _db.init_app(app)
    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from backend.routes.nodes import nodes_bp
    app.register_blueprint(nodes_bp, url_prefix="/nodes")
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
        yield app
        _db.session.remove()
        _db.drop_all()

    for k in [k for k in list(sys.modules) if _affected(k)]:
        if k not in saved:
            del sys.modules[k]
    for k, mod in saved.items():
        sys.modules[k] = mod


@pytest.fixture
def alice(app):
    u = User(username="alice", twitter_id="alice-twitter-id")
    _db.session.add(u)
    _db.session.commit()
    return u


@pytest.fixture
def bob(app):
    u = User(username="bob", twitter_id="bob-twitter-id")
    _db.session.add(u)
    _db.session.commit()
    return u


@pytest.fixture
def llm_user(app):
    u = User(username="claude-opus-4.6", twitter_id="claude-opus-4-6")
    _db.session.add(u)
    _db.session.commit()
    return u


def _login(client, user):
    with client.session_transaction() as session:
        session["_user_id"] = str(user.id)
        session["_fresh"] = True


def _make_node(user, parent=None, content="hi", node_type="user",
               human_owner_id=None, privacy_level="public"):
    n = Node(
        user_id=user.id,
        human_owner_id=human_owner_id if human_owner_id is not None else user.id,
        parent_id=parent.id if parent else None,
        node_type=node_type,
        privacy_level=privacy_level,
        ai_usage="chat",
        token_count=1,
    )
    n.set_content(content)
    _db.session.add(n)
    _db.session.commit()
    return n




def test_titles_first_line_stripped_of_markdown(app, alice):
    _login_client = app.test_client()
    _login(_login_client, alice)
    n1 = _make_node(alice, content="# **Growth** plan\n\n- [ ] publish")
    n2 = _make_node(alice, content="plain first line\nsecond line")
    r = _login_client.get(f"/nodes/titles?ids={n1.id},{n2.id}")
    assert r.status_code == 200
    titles = r.get_json()["titles"]
    assert titles[str(n1.id)] == {"id": n1.id, "title": "Growth plan"}
    assert titles[str(n2.id)] == {"id": n2.id, "title": "plain first line"}


def test_titles_prefer_thread_name_on_root(app, alice):
    client = app.test_client()
    _login(client, alice)
    root = _make_node(alice, content="first line of the root")
    child = _make_node(alice, parent=root, content="first line of the child")
    t = Thread(root_node_id=root.id)
    t.set_name("My named thread")
    _db.session.add(t)
    _db.session.commit()
    r = client.get(f"/nodes/titles?ids={root.id},{child.id}")
    titles = r.get_json()["titles"]
    assert titles[str(root.id)]["title"] == "My named thread"
    # The name belongs to the thread root; a child keeps its own first line.
    assert titles[str(child.id)]["title"] == "first line of the child"


def test_titles_hide_private_and_missing_alike_but_mark_own_deleted(app, alice, bob):
    client = app.test_client()
    _login(client, alice)
    private = _make_node(bob, content="bob's secret", privacy_level="private")
    public = _make_node(bob, content="bob's public note", privacy_level="public")
    gone = _make_node(alice, content="was here")
    bobs_gone = _make_node(bob, content="bob's, gone", privacy_level="private")
    from datetime import datetime
    gone.deleted_at = datetime.utcnow()
    bobs_gone.deleted_at = datetime.utcnow()
    _db.session.commit()
    missing = 999999
    r = client.get(
        f"/nodes/titles?ids={private.id},{public.id},{gone.id},{bobs_gone.id},{missing}"
    )
    titles = r.get_json()["titles"]
    assert titles[str(private.id)] is None
    assert titles[str(missing)] is None
    # A deleted node the viewer could see is a tombstone, not "inaccessible";
    # one they never could see stays indistinguishable from missing.
    assert titles[str(gone.id)] == {"id": gone.id, "deleted": True, "title": None}
    assert titles[str(bobs_gone.id)] is None
    assert titles[str(public.id)]["title"] == "bob's public note"


def test_titles_ignore_junk_and_cap_ids(app, alice):
    client = app.test_client()
    _login(client, alice)
    n = _make_node(alice, content="only real one")
    r = client.get(f"/nodes/titles?ids=abc,,{n.id},{n.id}, -1")
    assert r.get_json()["titles"] == {str(n.id): {"id": n.id, "title": "only real one"}}
    assert client.get("/nodes/titles").get_json() == {"titles": {}}
    from backend.routes.nodes import NODE_TITLES_MAX_IDS
    many = ",".join(str(i) for i in range(1, NODE_TITLES_MAX_IDS + 10))
    r = client.get(f"/nodes/titles?ids={many}")
    assert len(r.get_json()["titles"]) == NODE_TITLES_MAX_IDS


def test_titles_require_login(app, alice):
    n = _make_node(alice, content="x")
    r = app.test_client().get(f"/nodes/titles?ids={n.id}")
    assert r.status_code in (401, 302)

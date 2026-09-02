"""A per-thread prompt edit must not switch off the agentic session.

Editing a system node's text in a thread detaches its prompt reference
(nodes.py `detach_prompt`) and copies the text into the node. Agentic
detection — tools, proposal parsing, mode notes — used to read only the
linked UserPrompt's key, so a detached thread was silently called with no
tools and the model wrote its tool calls as prose. Node.prompt_key now
carries the session kind: stamped on attach, stamped from the row being
removed on detach, and read via Node.get_prompt_key() with a fallback to
the linked prompt for roots created before the column existed.

Patterned after test_tts_invalidation.py: sqlite in-memory, minimal Flask
app, ENCRYPTION_DISABLED.
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

# ── Force-import real modules ────────────────────────────────────────────
for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login  # noqa: E402
from backend.extensions import db as _db  # noqa: E402
from backend.models import (  # noqa: E402
    User, Node, UserPrompt, NodeContextArtifact,
)
import backend.models as _real_backend_models  # noqa: E402

AGENTIC_KEYS = ("voice", "textmode")


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
    # Same sys.modules dance as test_tts_invalidation.py: other test files
    # may leave flask_login swapped for a MagicMock, which turns every edit
    # into a 403 through backend.utils.privacy's current_user.
    _affected = lambda k: (  # noqa: E731
        k == "flask_login"
        or k.startswith("backend.routes")
        or k == "backend.models"
        or k == "backend.utils.privacy"
    )
    saved = {k: sys.modules[k] for k in list(sys.modules) if _affected(k)}

    sys.modules["flask_login"] = _real_flask_login
    sys.modules["backend.models"] = _real_backend_models
    for _k in [
        k for k in list(sys.modules)
        if k.startswith("backend.routes") or k == "backend.utils.privacy"
    ]:
        del sys.modules[_k]

    app = _make_app()
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()

    current = [k for k in list(sys.modules) if _affected(k)]
    for k in current:
        if k not in saved:
            del sys.modules[k]
    for k, mod in saved.items():
        sys.modules[k] = mod
    # The fresh imports above also re-pointed the PACKAGE attributes
    # (backend.utils.privacy, backend.routes.nodes, ...) at the new module
    # objects, and `patch('backend.utils.privacy.current_user')` in sibling
    # tests resolves through that attribute — so restore those too, or the
    # patch lands on a module the code under test never calls.
    for k in set(current) | set(saved):
        pkg_name, _, attr = k.rpartition(".")
        pkg = sys.modules.get(pkg_name)
        if pkg is None or not attr:
            continue
        if k in saved:
            setattr(pkg, attr, saved[k])
        elif hasattr(pkg, attr):
            delattr(pkg, attr)


@pytest.fixture
def alice(app):
    u = User(username="alice", twitter_id="alice-twitter-id")
    _db.session.add(u)
    _db.session.commit()
    return u


def _login(client, user):
    with client.session_transaction() as session:
        session["_user_id"] = str(user.id)
        session["_fresh"] = True


def _make_prompt(user, prompt_key="textmode"):
    prompt = UserPrompt(user_id=user.id, prompt_key=prompt_key,
                        title="Agentic", generated_by="default")
    prompt.set_content("You are Loore. {user_profile}")
    _db.session.add(prompt)
    _db.session.flush()
    return prompt


def _make_node(user, parent_id=None, content="", stamp=None):
    node = Node(user_id=user.id, human_owner_id=user.id, node_type="user",
                privacy_level="private", ai_usage="chat", token_count=1,
                parent_id=parent_id, prompt_key=stamp)
    node.set_content(content)
    _db.session.add(node)
    _db.session.flush()
    return node


def _link_prompt(node, prompt):
    """Pin *prompt* on *node* the way roots were pinned before the stamp
    existed: reference row only, no prompt_key on the node."""
    _db.session.add(NodeContextArtifact(
        node_id=node.id, artifact_type="prompt", artifact_id=prompt.id))
    _db.session.commit()


def _is_agentic(chain):
    # The generation task's _is_agentic_prompt delegates here; importing
    # the task module itself would pick up sibling tests' MagicMock stubs.
    from backend.utils.session_helpers import chain_has_agentic_prompt
    return chain_has_agentic_prompt(chain)


def _ancestors_have_prompt(node, user):
    from backend.utils.session_helpers import ancestors_have_prompt
    return ancestors_have_prompt(node, user.id, AGENTIC_KEYS)


# ── Attach stamps the session kind ───────────────────────────────────────


def test_attach_stamps_prompt_key(app, alice):
    from backend.utils.context_artifacts import attach_context_artifacts
    prompt = _make_prompt(alice, "voice")
    root = _make_node(alice)
    attach_context_artifacts(root.id, alice.id, prompt_record=prompt)
    _db.session.commit()

    refreshed = Node.query.get(root.id)
    assert refreshed.is_system_prompt
    assert refreshed.prompt_key == "voice"
    assert refreshed.get_prompt_key() == "voice"


# ── Detach keeps it ──────────────────────────────────────────────────────


def test_detach_keeps_thread_agentic(app, alice):
    """The exact failure: a root pinned before the stamp existed, edited
    in-thread. The reference goes, the text is the user's own, and the
    thread is still an agentic session for both readers."""
    prompt = _make_prompt(alice, "textmode")
    root = _make_node(alice)
    _link_prompt(root, prompt)
    child = _make_node(alice, parent_id=root.id, content="hi")
    _db.session.commit()
    assert root.prompt_key is None                     # pre-stamp root

    client = app.test_client()
    _login(client, alice)
    resp = client.put(f"/nodes/{root.id}", json={
        "content": "You are Loore, but terser. {user_profile}",
        "privacy_level": "private", "ai_usage": "chat",
        "detach_prompt": True,
    })
    assert resp.status_code == 200

    refreshed = Node.query.get(root.id)
    assert not refreshed.is_system_prompt               # reference gone
    assert refreshed.get_content().startswith("You are Loore, but terser")
    assert refreshed.prompt_key == "textmode"           # stamped on the way out
    assert refreshed.get_prompt_key() == "textmode"

    child = Node.query.get(child.id)
    assert _is_agentic([refreshed, child])
    assert _ancestors_have_prompt(child, alice)


def test_detach_keeps_existing_stamp(app, alice):
    """A root stamped on attach keeps its stamp through detach."""
    prompt = _make_prompt(alice, "voice")
    root = _make_node(alice, stamp="voice")
    _link_prompt(root, prompt)

    client = app.test_client()
    _login(client, alice)
    resp = client.put(f"/nodes/{root.id}", json={
        "content": "my own instructions", "privacy_level": "private",
        "ai_usage": "chat", "detach_prompt": True,
    })
    assert resp.status_code == 200
    refreshed = Node.query.get(root.id)
    assert not refreshed.is_system_prompt
    assert refreshed.prompt_key == "voice"
    assert _is_agentic([refreshed])


# ── Readers: fallback and negatives ──────────────────────────────────────


def test_linked_root_without_stamp_falls_back_to_prompt(app, alice):
    prompt = _make_prompt(alice, "textmode")
    root = _make_node(alice)
    _link_prompt(root, prompt)
    child = _make_node(alice, parent_id=root.id, content="hi")
    _db.session.commit()

    assert root.prompt_key is None
    assert root.get_prompt_key() == "textmode"
    assert _is_agentic([root, child])
    assert _ancestors_have_prompt(child, alice)


def test_non_agentic_prompt_key_is_not_agentic(app, alice):
    prompt = _make_prompt(alice, "reflect")
    root = _make_node(alice, stamp="reflect")
    _link_prompt(root, prompt)
    child = _make_node(alice, parent_id=root.id, content="hi")
    _db.session.commit()

    assert root.get_prompt_key() == "reflect"
    assert not _is_agentic([root, child])
    assert not _ancestors_have_prompt(child, alice)


def test_ordinary_nodes_are_not_agentic(app, alice):
    """Plain nodes, and a root detached BEFORE the stamp existed (no row,
    no stamp — unrecoverable), both read as non-agentic."""
    root = _make_node(alice, content="You are Loore. {user_profile}")
    child = _make_node(alice, parent_id=root.id, content="hi")
    _db.session.commit()

    assert root.get_prompt_key() is None
    assert not _is_agentic([root, child])
    assert not _ancestors_have_prompt(child, alice)

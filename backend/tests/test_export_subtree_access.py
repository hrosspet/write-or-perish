"""The export renders a node only for a user who may see it, at any depth
and under any placeholder.

``format_node_tree`` decides, for each node below the entry it starts
from, what the user's export shows: its text, the deleted-entry
placeholder (only for a user who could see the node before it was
deleted; otherwise nothing, with everything below it), or the
not-accessible placeholder. The same rule holds under a live entry,
under a deleted one, and under a placeholder for another user's private
reply, whichever path builds the export: the user's own data download
(``/export/threads``, no budget, so no pre-selected rows), the same
export filtered to AI-readable rows (the recent-context build of a user
without a profile), and the incremental path.

In-memory SQLite, ENCRYPTION_DISABLED; the export runs for real and
calls no outside service.
"""
import os
import sys
from datetime import datetime, timedelta
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

import flask_login as _real_flask_login  # noqa: E402
from backend.extensions import db as _db  # noqa: E402
from backend.models import User, Node  # noqa: E402
import backend.models as _real_backend_models  # noqa: E402

T0 = datetime(2026, 5, 1, 9, 0, 0)
TOMBSTONE = "[Node deleted by author]"
INACCESSIBLE = "[Content not accessible"


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

    from backend.routes.export_data import export_bp
    app.register_blueprint(export_bp, url_prefix="/api")
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

    current = [k for k in list(sys.modules) if _affected(k)]
    for k in current:
        if k not in saved:
            del sys.modules[k]
    for k, mod in saved.items():
        sys.modules[k] = mod
    # The fresh imports also re-pointed the package attributes
    # (backend.routes, backend.routes.export_data, ...) at the new module
    # objects; restore them too, or a later file's
    # `import backend.routes.export_data as m` gets a module its code
    # never calls (see test_detached_prompt_stays_agentic.py).
    for k in set(current) | set(saved):
        pkg_name, _, attr = k.rpartition(".")
        pkg = sys.modules.get(pkg_name)
        if pkg is None or not attr:
            continue
        if k in saved:
            setattr(pkg, attr, saved[k])
        elif hasattr(pkg, attr):
            delattr(pkg, attr)


def _user(name):
    u = User(username=name, twitter_id=f"{name}-twitter-id")
    _db.session.add(u)
    _db.session.commit()
    return u


@pytest.fixture
def alice(app):
    return _user("alice")


@pytest.fixture
def bob(app):
    return _user("bob")


_clock = {"n": 0}


def _node(user, content, parent=None, privacy="private", ai_usage="chat",
          deleted=False, node_type="user"):
    _clock["n"] += 1
    node = Node(
        user_id=user.id,
        human_owner_id=user.id,
        parent_id=parent.id if parent else None,
        node_type=node_type,
        llm_model="claude-x" if node_type == "llm" else None,
        privacy_level=privacy,
        ai_usage=ai_usage,
        token_count=5,
        created_at=T0 + timedelta(minutes=_clock["n"]),
    )
    node.set_content(content)
    if deleted:
        node.deleted_at = T0 + timedelta(days=1)
    _db.session.add(node)
    _db.session.commit()
    return node


def _download(app, user):
    """The user's own data download, through the route."""
    client = app.test_client()
    with client.session_transaction() as session:
        session["_user_id"] = str(user.id)
        session["_fresh"] = True
    resp = client.get("/api/export/threads")
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def _ai_export(user):
    """The same export filtered to AI-readable rows, as the recent-context
    build of a user without a profile cutoff makes it (no budget, no
    created_after: the authored-threads path)."""
    from backend.routes.export_data import build_user_export_content
    result = build_user_export_content(
        user, filter_ai_usage=True, chronological_order=True,
        return_metadata=True, collapse_artifacts=True)
    return result["content"] if result else ""


def _incremental_export(user):
    """The engaged-threads export with no budget (the uncapped
    {user_export} and recent context after a profile cutoff)."""
    from backend.routes.export_data import build_user_export_content
    return build_user_export_content(
        user, filter_ai_usage=True, include_strategy="engaged_threads") or ""


# ── under the user's deleted entry ─────────────────────────────────────

def test_private_reply_under_a_deleted_entry_is_not_downloaded(
        app, alice, bob):
    root = _node(alice, "ALICEROOT", deleted=True)
    _node(bob, "BOBPRIVATE", parent=root)

    content = _download(app, alice)
    assert TOMBSTONE in content
    assert "BOBPRIVATE" not in content
    assert INACCESSIBLE in content


def test_public_reply_under_a_deleted_entry_is_downloaded(app, alice, bob):
    root = _node(alice, "ALICEROOT", deleted=True)
    _node(bob, "BOBPUBLIC", parent=root, privacy="public")

    content = _download(app, alice)
    assert "BOBPUBLIC" in content
    assert "ALICEROOT" not in content


def test_private_reply_below_a_chain_of_deleted_entries(app, alice, bob):
    root = _node(alice, "ALICEROOT", deleted=True)
    mid = _node(alice, "ALICEMID", parent=root, deleted=True)
    _node(bob, "BOBPRIVATE", parent=mid)
    # A live entry of alice's in the thread, so the thread is exported
    # however the private reply counts.
    _node(alice, "ALICELIVE", parent=root)

    content = _download(app, alice)
    assert "ALICELIVE" in content
    assert content.count(TOMBSTONE) == 2
    assert "BOBPRIVATE" not in content


def test_ai_reply_to_a_private_reply_under_a_deleted_entry(app, alice, bob):
    root = _node(alice, "ALICEROOT", deleted=True)
    reply = _node(bob, "BOBPRIVATE", parent=root)
    _node(bob, "BOBSAI", parent=reply, node_type="llm")

    content = _download(app, alice)
    assert "BOBPRIVATE" not in content
    assert "BOBSAI" not in content


def test_others_deleted_private_reply_leaves_no_trace(app, alice, bob):
    """A deleted node the user could not see before is left out with
    everything below it, as under a live entry: no "User (bob)" header,
    no timestamp."""
    root = _node(alice, "ALICEROOT", deleted=True)
    gone = _node(bob, "BOBGONE", parent=root, deleted=True)
    _node(bob, "BOBBELOW", parent=gone, privacy="public")
    _node(alice, "ALICELIVE", parent=root)

    content = _download(app, alice)
    assert "ALICELIVE" in content
    assert "bob" not in content
    assert "BOBBELOW" not in content
    assert content.count(TOMBSTONE) == 1


def test_others_deleted_public_reply_keeps_its_placeholder(app, alice, bob):
    root = _node(alice, "ALICEROOT", deleted=True)
    gone = _node(bob, "BOBGONE", parent=root, privacy="public", deleted=True)
    _node(bob, "BOBBELOW", parent=gone, privacy="public")

    content = _download(app, alice)
    assert content.count(TOMBSTONE) == 2
    assert "BOBGONE" not in content
    assert "BOBBELOW" in content


# ── under the placeholder for another user's private reply ─────────────

def test_reply_below_a_private_reply_is_not_downloaded(app, alice, bob):
    root = _node(alice, "ALICEROOT", privacy="public")
    reply = _node(bob, "BOBPRIVATE", parent=root)
    _node(bob, "BOBSAI", parent=reply, node_type="llm")

    content = _download(app, alice)
    assert "ALICEROOT" in content
    assert "BOBPRIVATE" not in content
    assert "BOBSAI" not in content
    assert content.count(INACCESSIBLE) == 2


def test_public_reply_below_a_private_reply_is_downloaded(app, alice, bob):
    root = _node(alice, "ALICEROOT", privacy="public")
    reply = _node(bob, "BOBPRIVATE", parent=root)
    _node(alice, "ALICEPUBLIC", parent=reply, privacy="public")

    content = _download(app, alice)
    assert "BOBPRIVATE" not in content
    assert "ALICEPUBLIC" in content


# ── the other ways the same renderer runs ──────────────────────────────

def test_ai_filtered_export_leaves_out_private_replies(app, alice, bob):
    root = _node(alice, "ALICEROOT", deleted=True)
    _node(bob, "BOBPRIVATE", parent=root)
    live = _node(alice, "ALICELIVE", privacy="public")
    reply = _node(bob, "BOBPRIVATE2", parent=live)
    _node(bob, "BOBSAI", parent=reply, node_type="llm")

    content = _ai_export(alice)
    assert "ALICELIVE" in content
    for text in ("BOBPRIVATE", "BOBPRIVATE2", "BOBSAI"):
        assert text not in content


def test_incremental_export_leaves_out_others_deleted_private_reply(
        app, alice, bob):
    root = _node(alice, "ALICEROOT", deleted=True)
    _node(bob, "BOBGONE", parent=root, deleted=True)
    _node(bob, "BOBPRIVATE", parent=root)
    _node(alice, "ALICELIVE", parent=root)

    content = _incremental_export(alice)
    assert "ALICELIVE" in content
    assert "bob" not in content
    assert "BOBPRIVATE" not in content


def test_budgeted_raw_window_leaves_out_others_deleted_private_reply(
        app, alice, bob):
    """The {user_recent_raw} window: the rows come from the budget
    pre-selection, and a deleted row passes the selection under a
    tombstone; the user still sees only what they could see before."""
    from backend.routes.export_data import build_user_export_content
    root = _node(alice, "ALICEROOT", deleted=True)
    _node(bob, "BOBGONE", parent=root, deleted=True)
    _node(alice, "ALICELIVE", parent=root)

    result = build_user_export_content(
        alice, max_tokens=10000, filter_ai_usage=True,
        return_metadata=True, collapse_artifacts=True)
    assert "ALICELIVE" in result["content"]
    assert "bob" not in result["content"]


def test_reply_by_an_account_in_its_grace_period(app, alice, bob):
    """A reply whose author deleted the account counts as deleted (#269):
    under the user's deleted entry a private one leaves no trace, a
    public one is a placeholder with no name."""
    root = _node(alice, "ALICEROOT", deleted=True)
    _node(bob, "BOBPRIVATE", parent=root)
    _node(bob, "BOBPUBLIC", parent=root, privacy="public")
    _node(alice, "ALICELIVE", parent=root)
    bob.deleted_at = T0 + timedelta(days=2)
    _db.session.commit()

    content = _download(app, alice)
    assert "ALICELIVE" in content
    assert "BOBPRIVATE" not in content
    assert "BOBPUBLIC" not in content
    assert "bob" not in content
    assert content.count(TOMBSTONE) == 2   # the entry and the public reply


def test_live_entry_renders_as_before(app, alice, bob):
    """Under a live entry nothing changes: a private reply is a
    placeholder, a deleted one the user could not see is left out, a
    public one is shown, and branches keep their markers."""
    root = _node(alice, "ALICEROOT")
    _node(bob, "BOBPRIVATE", parent=root)
    _node(bob, "BOBGONE", parent=root, deleted=True)
    _node(bob, "BOBPUBLIC", parent=root, privacy="public")

    content = _download(app, alice)
    assert "ALICEROOT" in content
    assert "BOBPUBLIC" in content
    assert "BOBPRIVATE" not in content
    assert content.count(INACCESSIBLE) == 1
    assert TOMBSTONE not in content
    assert content.count("**BRANCH**") == 2

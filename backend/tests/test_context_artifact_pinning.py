"""Tests for per-session pinning of context artifacts (#191).

The agentic/voice system prompt embeds {user_profile}, {user_recent},
{user_recent_raw}, {user_todo}, and {user_ai_preferences}. Before #191 only
the 10k-raw window was pinned; profile, recent-context, todo, and AI-prefs
were re-fetched "latest now" and drifted mid-thread.

The fix resolves each artifact from the version recorded on the node that
carries its placeholder — the NodeContextArtifact row written by
``attach_context_artifacts`` (agentic system nodes) / ``sync_context_artifacts``
(ad-hoc placeholders), the same source of truth the data export reads. These
tests pin that behavior:

  - With a recorded version, the fetcher returns *that* version, not the
    latest.
  - With no binding (legacy nodes / pinned_node=None), it falls back to the
    latest.
  - ai_usage is re-checked on the resolved row, so a mid-session opt-out is
    honored even though the version is pinned.
  - recent-context resolves the recorded summary; the legacy fallback uses
    the latest summary for the current profile.

A lightweight AST check guards the call-site wiring (each fetcher must be
invoked with a ``pinned_node`` kwarg) without importing the heavy Celery
task body.
"""

import ast
import os
import sys
from datetime import datetime
from unittest.mock import MagicMock

# ── Environment (mirror the lightweight setup used by other model tests) ─
os.environ["ENCRYPTION_DISABLED"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("TWITTER_API_KEY", "fake")
os.environ.setdefault("TWITTER_API_SECRET", "fake")

# The fetchers under test live in backend.tasks.llm_completion. Importing
# that module normally pulls in backend.celery_app, which calls
# create_app() — fragile in a shared test process where sibling modules
# mock backend.routes.* (a mocked blueprint view has no __name__ and
# create_app blows up). The fetchers themselves only touch backend.models /
# backend.extensions, so we stub the celery glue (no create_app), import,
# bind the function objects, then RESTORE sys.modules so sibling tests that
# rely on a real celery_app / task import are undisturbed regardless of
# collection order.
sys.modules.setdefault("celery", MagicMock())
sys.modules.setdefault("celery.utils", MagicMock())
sys.modules.setdefault("celery.utils.log", MagicMock())
sys.modules.setdefault("celery.result", MagicMock())

# Force real models/extensions if a sibling left MagicMock stubs.
for _mod in ["backend.models", "backend.extensions"]:
    if isinstance(sys.modules.get(_mod), MagicMock):
        del sys.modules[_mod]

_GLUE = ("backend.celery_app", "backend.llm_providers",
         "backend.tasks.llm_completion")
_saved_glue = {k: sys.modules.get(k) for k in _GLUE}
sys.modules["backend.celery_app"] = MagicMock()
sys.modules["backend.llm_providers"] = MagicMock()
# Drop any cached/mocked task module so it re-imports against our stubs.
sys.modules.pop("backend.tasks.llm_completion", None)

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

from backend.extensions import db  # noqa: E402
from backend.models import (  # noqa: E402
    User, UserProfile, UserRecentContext, UserTodo, UserAIPreferences,
    Node, NodeContextArtifact,
)
from backend.tasks.llm_completion import (  # noqa: E402
    get_user_profile_content,
    get_user_recent_content,
    get_user_todo_content,
    get_user_ai_preferences_content,
)

# Restore prior sys.modules state. The imported function objects keep
# working: they reference the real backend.models / db via the (still
# alive) llm_completion module __dict__.
for _k, _v in _saved_glue.items():
    if _v is None:
        sys.modules.pop(_k, None)
    else:
        sys.modules[_k] = _v

# Two artifact versions: the OLD one is what the node was pinned to at
# session start; the NEW one is written mid-session (later created_at).
T_OLD = datetime(2026, 6, 1, 12, 0, 0)
T_NEW = datetime(2026, 6, 1, 14, 0, 0)


@pytest.fixture
def app():
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
    u = User(username="pinner", approved=True, plan="alpha")
    db.session.add(u)
    db.session.flush()
    return u


def _node(user_id):
    """A node carrying every artifact placeholder (stands in for the
    agentic system node)."""
    n = Node(user_id=user_id, node_type="user")
    n.set_content(
        "{user_profile} {user_recent} {user_todo} {user_ai_preferences}"
    )
    db.session.add(n)
    db.session.flush()
    return n


def _bind(node, artifact_type, artifact_id):
    """Record an artifact version on a node (what attach/sync write)."""
    db.session.add(NodeContextArtifact(
        node=node, artifact_type=artifact_type, artifact_id=artifact_id,
    ))
    db.session.flush()


def _profile(user_id, created_at, content, ai_usage="chat"):
    p = UserProfile(
        user_id=user_id, generated_by="gpt-5",
        created_at=created_at, ai_usage=ai_usage,
    )
    p.set_content(content)
    db.session.add(p)
    db.session.flush()
    return p


def _todo(user_id, created_at, content, ai_usage="chat"):
    t = UserTodo(
        user_id=user_id, generated_by="voice_session",
        created_at=created_at, ai_usage=ai_usage,
    )
    t.set_content(content)
    db.session.add(t)
    db.session.flush()
    return t


def _prefs(user_id, created_at, content, ai_usage="chat"):
    p = UserAIPreferences(
        user_id=user_id, generated_by="voice_session",
        created_at=created_at, ai_usage=ai_usage,
    )
    p.set_content(content)
    db.session.add(p)
    db.session.flush()
    return p


def _recent(user_id, created_at, content, profile_id, ai_usage="chat"):
    rc = UserRecentContext(
        user_id=user_id, generated_by="gpt-5",
        created_at=created_at, profile_id=profile_id, ai_usage=ai_usage,
    )
    rc.set_content(content)
    db.session.add(rc)
    db.session.flush()
    return rc


# ── profile ──────────────────────────────────────────────────────────────

class TestProfilePinning:
    def test_returns_recorded_version_not_latest(self, app):
        u = _user()
        old = _profile(u.id, T_OLD, "OLD profile")
        _profile(u.id, T_NEW, "NEW profile")
        node = _node(u.id)
        _bind(node, "profile", old.id)
        db.session.commit()

        resolved = get_user_profile_content(u.id, pinned_node=node)
        assert resolved is not None
        assert resolved.get_content() == "OLD profile"

    def test_no_binding_falls_back_to_latest(self, app):
        u = _user()
        _profile(u.id, T_OLD, "OLD profile")
        _profile(u.id, T_NEW, "NEW profile")
        node = _node(u.id)  # no binding recorded
        db.session.commit()

        assert get_user_profile_content(
            u.id, pinned_node=node).get_content() == "NEW profile"
        # pinned_node=None behaves the same (legacy callers).
        assert get_user_profile_content(u.id).get_content() == "NEW profile"

    def test_mid_session_opt_out_honored(self, app):
        """The version is pinned, but if the user flips that exact version's
        ai_usage to 'off' mid-session the prompt must withhold it."""
        u = _user()
        p = _profile(u.id, T_OLD, "OLD profile")
        node = _node(u.id)
        _bind(node, "profile", p.id)
        db.session.commit()

        assert get_user_profile_content(u.id, pinned_node=node) is not None
        p.ai_usage = "none"
        db.session.commit()
        assert get_user_profile_content(u.id, pinned_node=node) is None


# ── todo ───────────────────────────────────────────────────────────────────

class TestTodoPinning:
    def test_returns_recorded_version_not_latest(self, app):
        u = _user()
        old = _todo(u.id, T_OLD, "OLD todo")
        _todo(u.id, T_NEW, "NEW todo")
        node = _node(u.id)
        _bind(node, "todo", old.id)
        db.session.commit()

        assert get_user_todo_content(u.id, pinned_node=node) == "OLD todo"

    def test_no_binding_falls_back_to_latest(self, app):
        u = _user()
        _todo(u.id, T_OLD, "OLD todo")
        _todo(u.id, T_NEW, "NEW todo")
        node = _node(u.id)
        db.session.commit()

        assert get_user_todo_content(u.id, pinned_node=node) == "NEW todo"
        assert get_user_todo_content(u.id) == "NEW todo"


# ── ai preferences ─────────────────────────────────────────────────────────

class TestAIPreferencesPinning:
    def test_returns_recorded_version_not_latest(self, app):
        u = _user()
        old = _prefs(u.id, T_OLD, "be terse")
        _prefs(u.id, T_NEW, "be verbose")
        node = _node(u.id)
        _bind(node, "ai_preferences", old.id)
        db.session.commit()

        assert get_user_ai_preferences_content(
            u.id, pinned_node=node) == "be terse"

    def test_no_binding_falls_back_to_latest(self, app):
        u = _user()
        _prefs(u.id, T_OLD, "be terse")
        _prefs(u.id, T_NEW, "be verbose")
        node = _node(u.id)
        db.session.commit()

        assert get_user_ai_preferences_content(
            u.id, pinned_node=node) == "be verbose"
        assert get_user_ai_preferences_content(u.id) == "be verbose"


# ── recent context ─────────────────────────────────────────────────────────

class TestRecentContextPinning:
    def test_returns_recorded_summary_not_latest(self, app):
        """A session pinned to the old summary must not see the one written
        against the newer profile after the session started."""
        u = _user()
        p_old = _profile(u.id, T_OLD, "OLD profile")
        p_new = _profile(u.id, T_NEW, "NEW profile")
        rc_old = _recent(u.id, T_OLD, "summary for OLD profile",
                         profile_id=p_old.id)
        _recent(u.id, T_NEW, "summary for NEW profile", profile_id=p_new.id)
        node = _node(u.id)
        _bind(node, "recent_context", rc_old.id)
        db.session.commit()

        rc = get_user_recent_content(u.id, pinned_node=node)
        assert rc is not None
        assert rc.get_content() == "summary for OLD profile"

    def test_no_binding_falls_back_to_latest_for_current_profile(self, app):
        u = _user()
        p_old = _profile(u.id, T_OLD, "OLD profile")
        p_new = _profile(u.id, T_NEW, "NEW profile")
        _recent(u.id, T_OLD, "summary for OLD profile", profile_id=p_old.id)
        _recent(u.id, T_NEW, "summary for NEW profile", profile_id=p_new.id)
        node = _node(u.id)
        db.session.commit()

        rc = get_user_recent_content(u.id, pinned_node=node)
        assert rc.get_content() == "summary for NEW profile"

    def test_mid_session_opt_out_honored(self, app):
        u = _user()
        p = _profile(u.id, T_OLD, "OLD profile")
        rc = _recent(u.id, T_OLD, "summary", profile_id=p.id)
        node = _node(u.id)
        _bind(node, "recent_context", rc.id)
        db.session.commit()

        assert get_user_recent_content(u.id, pinned_node=node) is not None
        rc.ai_usage = "none"
        db.session.commit()
        assert get_user_recent_content(u.id, pinned_node=node) is None


# ── call-site wiring (AST guard) ───────────────────────────────────────────

class TestCallSiteWiring:
    """Guard that generate_llm_response resolves every artifact from the
    node's recorded version by passing a ``pinned_node`` kwarg. Parsed via
    ``ast`` to avoid importing the heavy Celery task body."""

    @pytest.fixture(scope="class")
    def llm_completion_ast(self):
        path = os.path.join(
            os.path.dirname(__file__), "..", "tasks", "llm_completion.py",
        )
        with open(path) as f:
            return ast.parse(f.read())

    @staticmethod
    def _calls_named(tree, name):
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            actual = (
                func.attr if isinstance(func, ast.Attribute)
                else func.id if isinstance(func, ast.Name)
                else None
            )
            if actual == name:
                yield node

    @pytest.mark.parametrize("fn", [
        "get_user_profile_content",
        "get_user_todo_content",
        "get_user_recent_content",
        "get_user_ai_preferences_content",
    ])
    def test_fetcher_called_with_pinned_node(self, llm_completion_ast, fn):
        calls = list(self._calls_named(llm_completion_ast, fn))
        assert calls, f"Expected a call to {fn} in llm_completion.py"
        assert any(
            any(kw.arg == "pinned_node" for kw in call.keywords)
            for call in calls
        ), f"{fn} must be called with a pinned_node= (per #191)"

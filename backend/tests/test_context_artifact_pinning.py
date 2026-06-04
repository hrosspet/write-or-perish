"""Tests for per-session pinning of context artifacts (#191).

The agentic/voice system prompt embeds {user_profile}, {user_recent},
{user_recent_raw}, {user_todo}, and {user_ai_preferences}. Before #191 only
the 10k-raw window was pinned to the conversation's system-node timestamp;
the rest were re-fetched "latest now" and drifted mid-thread. These tests
pin the corrected behavior:

  - Each fetcher, given an ``as_of`` anchor, returns the artifact
    version/state as of that timestamp (the latest row with
    ``created_at <= as_of``).
  - recent-context resolves its ``profile_id`` as-of the anchor too, so a
    summary written against a *newer* profile is not leaked into a session
    pinned to the older one.
  - ``as_of=None`` preserves the legacy "latest" behavior.
  - ai_usage gating is unchanged.

A lightweight AST check guards the call-site wiring in llm_completion.py
(each fetcher must be invoked with an ``as_of`` kwarg) without importing
the heavy Celery task.
"""

import ast
import os
import sys
from datetime import datetime, timedelta
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

# Timeline: two artifact versions straddling the session anchor.
T_OLD = datetime(2026, 6, 1, 12, 0, 0)
ANCHOR = datetime(2026, 6, 1, 13, 0, 0)   # session/system-node created_at
T_NEW = datetime(2026, 6, 1, 14, 0, 0)    # written mid-session, after anchor


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
    def test_as_of_returns_version_at_anchor(self, app):
        u = _user()
        _profile(u.id, T_OLD, "OLD profile")
        _profile(u.id, T_NEW, "NEW profile")
        db.session.commit()

        pinned = get_user_profile_content(u.id, as_of=ANCHOR)
        assert pinned is not None
        assert pinned.get_content() == "OLD profile"

    def test_as_of_none_returns_latest(self, app):
        u = _user()
        _profile(u.id, T_OLD, "OLD profile")
        _profile(u.id, T_NEW, "NEW profile")
        db.session.commit()

        assert get_user_profile_content(u.id).get_content() == "NEW profile"

    def test_ai_usage_gating_still_applies(self, app):
        """The latest-as-of row is selected first, then gated — an opted-out
        latest profile yields None (it does not fall back to an older
        allowed one). This matches the pre-#191 behavior."""
        u = _user()
        _profile(u.id, T_OLD, "OLD profile")
        _profile(u.id, ANCHOR - timedelta(minutes=1), "PRIVATE", ai_usage="off")
        db.session.commit()

        assert get_user_profile_content(u.id, as_of=ANCHOR) is None


# ── todo ───────────────────────────────────────────────────────────────────

class TestTodoPinning:
    def test_as_of_returns_version_at_anchor(self, app):
        u = _user()
        _todo(u.id, T_OLD, "OLD todo")
        _todo(u.id, T_NEW, "NEW todo")
        db.session.commit()

        assert get_user_todo_content(u.id, as_of=ANCHOR) == "OLD todo"

    def test_as_of_none_returns_latest(self, app):
        u = _user()
        _todo(u.id, T_OLD, "OLD todo")
        _todo(u.id, T_NEW, "NEW todo")
        db.session.commit()

        assert get_user_todo_content(u.id) == "NEW todo"


# ── ai preferences ─────────────────────────────────────────────────────────

class TestAIPreferencesPinning:
    def test_as_of_returns_version_at_anchor(self, app):
        u = _user()
        _prefs(u.id, T_OLD, "be terse")
        _prefs(u.id, T_NEW, "be verbose")
        db.session.commit()

        assert get_user_ai_preferences_content(u.id, as_of=ANCHOR) == "be terse"

    def test_as_of_none_returns_latest(self, app):
        u = _user()
        _prefs(u.id, T_OLD, "be terse")
        _prefs(u.id, T_NEW, "be verbose")
        db.session.commit()

        assert get_user_ai_preferences_content(u.id) == "be verbose"


# ── recent context (profile_id resolved as-of the anchor) ───────────────────

class TestRecentContextPinning:
    def test_resolves_profile_and_summary_at_anchor(self, app):
        """A session pinned before the profile update must see the summary
        tied to the *old* profile, not the one written against the new
        profile after the anchor."""
        u = _user()
        p_old = _profile(u.id, T_OLD, "OLD profile")
        p_new = _profile(u.id, T_NEW, "NEW profile")
        _recent(u.id, T_OLD + timedelta(minutes=10),
                "summary for OLD profile", profile_id=p_old.id)
        _recent(u.id, T_NEW + timedelta(minutes=10),
                "summary for NEW profile", profile_id=p_new.id)
        db.session.commit()

        rc = get_user_recent_content(u.id, as_of=ANCHOR)
        assert rc is not None
        assert rc.get_content() == "summary for OLD profile"

    def test_as_of_none_returns_latest_for_current_profile(self, app):
        u = _user()
        p_old = _profile(u.id, T_OLD, "OLD profile")
        p_new = _profile(u.id, T_NEW, "NEW profile")
        _recent(u.id, T_OLD + timedelta(minutes=10),
                "summary for OLD profile", profile_id=p_old.id)
        _recent(u.id, T_NEW + timedelta(minutes=10),
                "summary for NEW profile", profile_id=p_new.id)
        db.session.commit()

        rc = get_user_recent_content(u.id)
        assert rc.get_content() == "summary for NEW profile"

    def test_summary_after_anchor_excluded_even_for_pinned_profile(self, app):
        """If the only summary for the as-of profile was written after the
        anchor, the pinned fetch returns nothing (no time-travel)."""
        u = _user()
        p_old = _profile(u.id, T_OLD, "OLD profile")
        _recent(u.id, T_NEW, "late summary", profile_id=p_old.id)
        db.session.commit()

        assert get_user_recent_content(u.id, as_of=ANCHOR) is None
        # Without the anchor it's the latest and is returned.
        assert get_user_recent_content(u.id).get_content() == "late summary"


# ── call-site wiring (AST guard) ───────────────────────────────────────────

class TestCallSiteWiring:
    """Guard that generate_llm_response pins every artifact fetch to the
    per-session anchor by passing an ``as_of`` kwarg. Parsed via ``ast`` to
    avoid importing the heavy Celery task body."""

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
    def test_fetcher_called_with_as_of(self, llm_completion_ast, fn):
        calls = list(self._calls_named(llm_completion_ast, fn))
        assert calls, f"Expected a call to {fn} in llm_completion.py"
        assert any(
            any(kw.arg == "as_of" for kw in call.keywords)
            for call in calls
        ), f"{fn} must be called with an as_of= anchor (per #191)"

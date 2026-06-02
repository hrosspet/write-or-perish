"""Regression test for fix 2b: a from-scratch full profile regen clears
``profile_needs_full_regen`` after the FIRST committed chunk.

Why it matters: full regen rebuilds the profile in chronological ~90k-token
chunks, each committed independently. If the run later times out, the flag
must already be off so the next heartbeat resumes *incrementally* from the
last saved chunk instead of restarting the whole rebuild from zero (the
behavior that made user 44 burn cost forever without finishing).

Patterned after the other task tests: in-memory SQLite, ENCRYPTION_DISABLED,
celery mocked so the module imports. Only ``@celery.task`` entry points
become mocks — the plain helper ``_chunked_profile_loop`` under test stays
real, with its LLM / export calls monkeypatched.
"""
import os
import sys
from datetime import datetime
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

from backend.extensions import db as _db          # noqa: E402
from backend.models import User, UserProfile      # noqa: E402


@pytest.fixture
def app():
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    # _save_profile -> calculate_llm_cost_microdollars reads this; empty
    # dict makes the unknown model cost 0 without a KeyError.
    app.config["SUPPORTED_MODELS"] = {}
    _db.init_app(app)
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()


def test_full_regen_clears_flag_after_first_chunk(app, monkeypatch):
    # Importing here (inside the app context) keeps the celery-mock + create_app
    # side effects scoped, matching the other task tests.
    import backend.tasks.exports as exports

    user = User(username="deep_user", plan="alpha", twitter_id=None,
                approved=True, profile_needs_full_regen=True)
    _db.session.add(user)
    _db.session.commit()

    # One chunk of source data, then the export is exhausted. The user has
    # no nodes, so the loop's has_more check also stops it after chunk 1.
    chunk = {
        "content": "the user's oldest writing",
        "token_count": 90000,
        "latest_node_created_at": datetime(2025, 2, 11, 10, 26, 14),
    }
    monkeypatch.setattr(exports, "build_user_export_content",
                        MagicMock(side_effect=[chunk, None]))
    monkeypatch.setattr(exports, "_call_llm_with_retries",
                        MagicMock(return_value={
                            "content": "PROFILE v1",
                            "input_tokens": 1000,
                            "output_tokens": 500,
                            "total_tokens": 1500,
                        }))

    fake_task = MagicMock()  # supplies .update_state(...)

    profile_id, chunk_num, _ = exports._chunked_profile_loop(
        fake_task, user, "gpt-5.5", update_template="{new_data}",
        api_keys={},
        first_chunk_prompt_fn=lambda c: "GEN PROMPT",
        initial_profile_content=None,
        generation_type="iterative",
    )

    # The whole point of fix 2b: flag is off after the first committed chunk.
    assert user.profile_needs_full_regen is False
    # And that first chunk really was persisted (so a resume has an anchor).
    assert chunk_num == 1
    saved = UserProfile.query.get(profile_id)
    assert saved is not None
    assert saved.generation_type == "iterative"


def test_incremental_update_null_cutoff_falls_back_to_full_regen(app, monkeypatch):
    """A previous profile with no source_data_cutoff (user-written/legacy)
    must NOT crash _do_incremental_update on `Node.created_at > None` — it
    falls back to full generation instead."""
    import backend.tasks.exports as exports

    user = User(username="nullcut", plan="alpha", twitter_id=None,
                approved=True)
    _db.session.add(user)
    _db.session.flush()
    prev = UserProfile(
        user_id=user.id, generated_by="user", tokens_used=0,
        generation_type="initial", source_tokens_used=0,
        source_data_cutoff=None,
    )
    prev.set_content("USER-WRITTEN PROFILE")
    _db.session.add(prev)
    _db.session.commit()

    sentinel = {"status": "full-regen"}
    full = MagicMock(return_value=sentinel)
    monkeypatch.setattr(exports, "_do_initial_generation", full)

    result = exports._do_incremental_update(
        MagicMock(), user, "gpt-5.5", prev.id,
        context_window=200000, max_output_tokens=10000, api_keys={})

    full.assert_called_once()      # fell back to full regen, no crash
    assert result is sentinel

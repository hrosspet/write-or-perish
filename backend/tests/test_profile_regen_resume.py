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
    # Warm celery_app first (lazily, at run time) so it resolves
    # backend.tasks.exports then backend.tasks.profile_batch in the safe order.
    # Importing exports directly as the first module trips the
    # exports <-> profile_batch import cycle; doing this at module top instead
    # breaks full-suite collection. So warm it here, in the fixture.
    import backend.celery_app  # noqa: F401
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


def test_incremental_update_null_cutoff_uses_existing_base_with_note(app, monkeypatch):
    """A user-written (null-cutoff) profile must NOT crash and must NOT be
    discarded by full regen — it's used as the incremental base, annotated so
    the LLM knows it's the user's own words."""
    import backend.tasks.exports as exports
    from backend.models import Node

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
    node = Node(user_id=user.id, node_type="user", ai_usage="chat")
    node.set_content("some recent writing")
    _db.session.add(node)
    _db.session.commit()

    captured = {}

    def fake_loop(*a, **kw):
        captured["base"] = kw.get("initial_profile_content")
        return (prev.id, 1, 0)
    monkeypatch.setattr(exports, "_chunked_profile_loop", fake_loop)
    full = MagicMock()
    monkeypatch.setattr(exports, "_do_initial_generation", full)

    exports._do_incremental_update(
        MagicMock(), user, "gpt-5.5", prev.id,
        context_window=200000, max_output_tokens=10000, api_keys={})

    full.assert_not_called()                            # NOT full regen
    assert "USER-WRITTEN PROFILE" in captured["base"]   # existing kept as base
    assert "written by the user" in captured["base"]    # annotated as user-written


def test_integration_annotates_user_written_chain_root(app):
    """The integration chain root can be the user's hand-written profile — it
    must be flagged as user-written; generated versions must not be."""
    import backend.tasks.exports as exports
    from datetime import datetime

    user = User(username="iu", plan="alpha", twitter_id=None, approved=True)
    _db.session.add(user)
    _db.session.flush()
    p1 = UserProfile(user_id=user.id, generated_by="user", tokens_used=0,
                     generation_type="initial", source_tokens_used=0,
                     source_data_cutoff=None)
    p1.set_content("USER BASE PROFILE")
    _db.session.add(p1)
    _db.session.flush()
    p2 = UserProfile(user_id=user.id, generated_by="gpt-5.5", tokens_used=0,
                     generation_type="update", source_tokens_used=1000,
                     source_data_cutoff=datetime(2026, 6, 1),
                     parent_profile_id=p1.id)
    p2.set_content("GENERATED UPDATE PROFILE")
    _db.session.add(p2)
    _db.session.commit()

    messages, chain = exports.build_integration_messages(user.id, p2.id)
    assert messages is not None and len(chain) == 2
    texts = [m["content"][0]["text"] for m in messages]
    user_msg = next(t for t in texts if "USER BASE PROFILE" in t)
    gen_msg = next(t for t in texts if "GENERATED UPDATE PROFILE" in t)
    assert "written by the user" in user_msg        # root flagged
    assert "written by the user" not in gen_msg      # generated not flagged


def _seed_null_cutoff_user(username, node_tokens):
    """A user whose only profile is hand-written (null cutoff), plus one old
    node carrying ``node_tokens``. Old timestamps so the inactivity (30m) and
    interval (1h) gates both pass, isolating the token-threshold decision."""
    from backend.models import Node
    user = User(username=username, plan="alpha", twitter_id=None, approved=True)
    _db.session.add(user)
    _db.session.flush()
    prof = UserProfile(
        user_id=user.id, generated_by="user", tokens_used=0,
        generation_type="initial", source_tokens_used=0,
        source_data_cutoff=None, created_at=datetime(2025, 1, 1),
    )
    prof.set_content("USER-WRITTEN")
    _db.session.add(prof)
    node = Node(user_id=user.id, node_type="user", ai_usage="chat",
                token_count=node_tokens, created_at=datetime(2025, 1, 2),
                updated_at=datetime(2025, 1, 2))
    node.set_content("writing")
    _db.session.add(node)
    _db.session.commit()
    return user


def test_null_cutoff_low_data_does_not_trigger(app, monkeypatch):
    """A hand-written (null-cutoff) profile with <80k tokens of data must NOT
    force-trigger an update — the heartbeat now measures the real data instead
    of sentinelling a null cutoff straight to the threshold."""
    import backend.tasks.exports as exports
    import backend.tasks.profile_batch as pb
    monkeypatch.setattr(pb, "use_batch_for_user", lambda *a, **k: False)

    user = _seed_null_cutoff_user("nulllow", node_tokens=2884)

    called = {}
    monkeypatch.setattr(exports, "maybe_trigger_profile_update",
                        lambda *a, **k: called.setdefault("yes", (a, k)))

    result = exports.maybe_trigger_incremental_profile_update(user)
    assert result is None
    assert "yes" not in called          # did NOT trigger despite the null cutoff


def test_null_cutoff_high_data_still_triggers(app, monkeypatch):
    """A hand-written (null-cutoff) profile WITH >=80k tokens still triggers, so
    the base eventually gets folded into a data-grounded profile."""
    import backend.tasks.exports as exports
    import backend.tasks.profile_batch as pb
    monkeypatch.setattr(pb, "use_batch_for_user", lambda *a, **k: False)

    user = _seed_null_cutoff_user("nullhigh", node_tokens=90000)

    called = {}
    monkeypatch.setattr(exports, "maybe_trigger_profile_update",
                        lambda *a, **k: called.setdefault("yes", (a, k)))

    exports.maybe_trigger_incremental_profile_update(user)
    assert "yes" in called              # crossed threshold -> triggered
    assert called["yes"][0][0] == user.id

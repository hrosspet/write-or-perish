"""#368: background jobs must not save empty model output that was cut off
at the output limit (the chat path fails the node since #366).

The worst case is the synchronous profile chunk loop: an empty cut-off
chunk used to be saved as a profile version and became the "previous
profile" the next chunk built on, so the profile lost everything before
it. Each job now saves nothing, keeps the previous version, writes the
cost row and fails.

Same harness as test_profile_regen_resume: in-memory SQLite,
ENCRYPTION_DISABLED, celery mocked so the modules import; only the model
call and the export builder are faked. The batch collectors are covered
in test_profile_batch / test_updates / test_intentions_task.
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
from backend.models import (                      # noqa: E402
    User, UserProfile, UserRecentContext, APICostLog, Node)
from backend.llm_providers import (               # noqa: E402
    is_empty_truncated, EmptyTruncatedOutputError)


EMPTY_CUT_OFF = {"content": "", "truncated": True, "input_tokens": 1000,
                 "output_tokens": 32000, "total_tokens": 33000}


@pytest.fixture
def app():
    # Warm celery_app first so it resolves the exports <-> profile_batch
    # import cycle in the safe order (see test_profile_regen_resume).
    import backend.celery_app  # noqa: F401
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    # Unknown model -> cost 0 without a KeyError.
    app.config["SUPPORTED_MODELS"] = {}
    _db.init_app(app)
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()


def _user(name, **kw):
    user = User(username=name, plan="alpha", twitter_id=None, approved=True,
                default_ai_usage="chat", **kw)
    _db.session.add(user)
    _db.session.commit()
    return user


# ── the shared predicate ─────────────────────────────────────────────────

@pytest.mark.parametrize("response, expected", [
    ({"content": "", "truncated": True}, True),
    ({"content": " \n", "truncated": True}, True),
    ({"content": None, "truncated": True}, True),
    ({"content": "", "truncated": True, "tool_calls": [{"id": "t"}]}, False),
    ({"content": "partial", "truncated": True}, False),
    ({"content": "", "truncated": False}, False),
    ({"content": ""}, False),   # a result without the flag is not refused
])
def test_is_empty_truncated(response, expected):
    assert is_empty_truncated(response) is expected


# ── synchronous profile chunk loop ───────────────────────────────────────

def _chunk(content, latest):
    return {"content": content, "token_count": 90000, "unit_count": 90000,
            "latest_node_created_at": latest}


def test_chunk_loop_stops_at_empty_cut_off_chunk(app, monkeypatch):
    """Chunk 1 is saved; chunk 2 comes back empty and cut off. The loop
    stops there: no second version, chunk 1 stays the tip the next run
    resumes from, both calls are logged as cost, and the job fails."""
    import backend.tasks.exports as exports
    import backend.llm_providers as lp

    user = _user("chunky", profile_needs_full_regen=True)
    monkeypatch.setattr(exports, "build_user_export_content", MagicMock(
        side_effect=[_chunk("oldest writing", datetime(2025, 1, 1)),
                     _chunk("newer writing", datetime(2025, 6, 1))]))
    monkeypatch.setattr(exports, "count_remaining_units",
                        MagicMock(side_effect=[300_000, 200_000, 0]))
    monkeypatch.setattr(exports, "should_continue_chain",
                        lambda user, profile: True)
    monkeypatch.setattr(lp.LLMProvider, "count_tokens",
                        staticmethod(lambda m, msgs, k: None))
    llm = MagicMock(side_effect=[
        {"content": "PROFILE v1", "input_tokens": 1000,
         "output_tokens": 500, "total_tokens": 1500},
        dict(EMPTY_CUT_OFF),
    ])
    monkeypatch.setattr(exports, "_call_llm_with_retries", llm)

    with pytest.raises(EmptyTruncatedOutputError):
        exports._chunked_profile_loop(
            MagicMock(), user, "gpt-5.5", update_template="{existing_profile}",
            api_keys={}, first_chunk_prompt_fn=lambda c: "GEN PROMPT",
            initial_profile_content=None, generation_type="iterative")

    assert llm.call_count == 2
    profiles = UserProfile.query.filter_by(user_id=user.id).all()
    assert len(profiles) == 1
    assert "PROFILE v1" in profiles[0].get_content()
    assert exports.profile_update_base(user.id).id == profiles[0].id
    # Chunk 1 committed, so the next run resumes incrementally from it.
    assert user.profile_needs_full_regen is False
    assert APICostLog.query.filter_by(
        user_id=user.id, request_type="profile").count() == 2


def test_update_task_fails_and_keeps_previous_profile(app, monkeypatch):
    """Through update_user_profile's body: an incremental update whose
    only chunk is cut off empty leaves the previous profile as the latest
    version and raises (the task is marked failed)."""
    import backend.tasks.exports as exports
    import backend.llm_providers as lp

    user = _user("updater")
    prev = UserProfile(user_id=user.id, generated_by="gpt-5.5",
                       tokens_used=0, generation_type="update",
                       source_tokens_used=1000,
                       source_data_cutoff=datetime(2025, 1, 1),
                       ai_usage="chat")
    prev.set_content("GOOD PROFILE")
    node = Node(user_id=user.id, node_type="user", ai_usage="chat")
    node.set_content("new writing")
    _db.session.add_all([prev, node])
    _db.session.commit()

    monkeypatch.setattr(exports, "build_user_export_content", MagicMock(
        return_value=_chunk("new writing", datetime(2025, 6, 1))))
    monkeypatch.setattr(exports, "count_remaining_units",
                        MagicMock(side_effect=[90_000, 0]))
    monkeypatch.setattr(lp.LLMProvider, "count_tokens",
                        staticmethod(lambda m, msgs, k: None))
    monkeypatch.setattr(exports, "_call_llm_with_retries",
                        MagicMock(return_value=dict(EMPTY_CUT_OFF)))
    integration = MagicMock()
    monkeypatch.setattr(exports, "_do_integration", integration)

    with pytest.raises(EmptyTruncatedOutputError):
        exports._do_incremental_update(
            MagicMock(), user, "gpt-5.5", prev.id, 200000, 32000, {})

    assert UserProfile.query.filter_by(user_id=user.id).all() == [prev]
    integration.assert_not_called()


def test_integration_empty_cut_off_saves_nothing(app, monkeypatch):
    import backend.tasks.exports as exports
    import backend.llm_providers as lp

    user = _user("integrator")
    tip = UserProfile(user_id=user.id, generated_by="gpt-5.5", tokens_used=0,
                      generation_type="iterative", source_tokens_used=5000,
                      source_data_cutoff=datetime(2025, 6, 1))
    tip.set_content("CHAIN TIP")
    _db.session.add(tip)
    _db.session.commit()
    monkeypatch.setattr(exports, "build_integration_messages",
                        lambda uid, pid: ([{"role": "user", "content": []}],
                                          [tip, tip]))
    monkeypatch.setattr(lp.LLMProvider, "get_completion",
                        staticmethod(lambda *a, **k: dict(EMPTY_CUT_OFF)))

    with pytest.raises(EmptyTruncatedOutputError):
        exports._do_integration(MagicMock(), user, "gpt-5.5", tip.id, {})

    assert UserProfile.query.filter_by(user_id=user.id).all() == [tip]
    assert APICostLog.query.filter_by(user_id=user.id).count() == 1


# ── recent context ───────────────────────────────────────────────────────

def test_recent_context_empty_cut_off_keeps_previous(app, monkeypatch):
    import backend.tasks.recent_context as rc
    import backend.routes.export_data as ed
    import backend.utils.llm_nodes as llm_nodes
    import backend.llm_providers as lp

    user = _user("recent")
    old = UserRecentContext(user_id=user.id, generated_by="gpt-5.5",
                            tokens_used=0, ai_usage="chat",
                            source_data_cutoff=datetime(2025, 1, 1),
                            created_at=datetime(2025, 1, 1))
    old.set_content("PREVIOUS CONTEXT")
    _db.session.add(old)
    _db.session.commit()

    monkeypatch.setattr(ed, "build_user_export_content", lambda user, **kw: {
        "content": "recent writing", "token_count": 12000,
        "latest_node_created_at": datetime(2025, 6, 1)})
    monkeypatch.setattr(rc, "_load_prompt",
                        lambda name, user_id=None: "{user_profile}{recent_data}")
    monkeypatch.setattr(llm_nodes, "default_model_for", lambda u: "gpt-5.5")
    monkeypatch.setattr(lp.LLMProvider, "get_completion",
                        staticmethod(lambda *a, **k: dict(EMPTY_CUT_OFF)))

    with pytest.raises(EmptyTruncatedOutputError):
        rc._generate_recent_context_impl(user.id)

    assert UserRecentContext.query.filter_by(user_id=user.id).all() == [old]
    assert APICostLog.query.filter_by(
        user_id=user.id, request_type="recent_context").count() == 1

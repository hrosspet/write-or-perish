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


# ── partial cut-off profile output + the refusal backoff ─────────────────

def test_chunk_loop_refuses_partial_cut_off_chunk(app, monkeypatch):
    """A chunk stopped at the output limit mid-document is refused like an
    empty one: it would become the next chunk's base."""
    import backend.tasks.exports as exports
    import backend.llm_providers as lp
    from backend.utils.refusal_backoff import REFUSED_REF

    user = _user("partial")
    monkeypatch.setattr(exports, "build_user_export_content", MagicMock(
        return_value=_chunk("writing", datetime(2025, 1, 1))))
    monkeypatch.setattr(exports, "count_remaining_units",
                        MagicMock(side_effect=[90_000, 0]))
    monkeypatch.setattr(lp.LLMProvider, "count_tokens",
                        staticmethod(lambda m, msgs, k: None))
    monkeypatch.setattr(exports, "_call_llm_with_retries", MagicMock(
        return_value={**EMPTY_CUT_OFF, "content": "# Profile\nHalf a"}))

    with pytest.raises(EmptyTruncatedOutputError, match="mid-output"):
        exports._chunked_profile_loop(
            MagicMock(), user, "gpt-5.5", update_template="{existing_profile}",
            api_keys={}, first_chunk_prompt_fn=lambda c: "GEN PROMPT",
            initial_profile_content=None, generation_type="iterative")

    _db.session.rollback()   # only what was committed counts
    assert UserProfile.query.filter_by(user_id=user.id).count() == 0
    assert APICostLog.query.filter_by(
        user_id=user.id, request_ref=REFUSED_REF).count() == 1


def test_two_strikes_constants():
    from datetime import timedelta
    from backend.utils import refusal_backoff as rb
    assert rb.RETRY_WAIT == timedelta(hours=1)
    assert rb.STOP_AFTER == 2


def _refusal(user, ago, request_type="profile"):
    from backend.utils.refusal_backoff import REFUSED_REF
    _db.session.add(APICostLog(
        user_id=user.id, model_id="gpt-5.5", request_type=request_type,
        request_ref=REFUSED_REF, input_tokens=1, output_tokens=1,
        cost_microdollars=0, created_at=datetime.utcnow() - ago))
    _db.session.commit()


def _profile(user, generation_type="update", parent=None, ago=None,
             cutoff=datetime(2025, 1, 1), **kw):
    from datetime import timedelta
    p = UserProfile(user_id=user.id, generated_by="gpt-5.5", tokens_used=0,
                    generation_type=generation_type, source_tokens_used=200_000,
                    source_data_cutoff=cutoff, ai_usage="chat",
                    parent_profile_id=parent.id if parent else None,
                    created_at=datetime.utcnow() - (ago or timedelta(days=1)),
                    **kw)
    p.set_content(f"PROFILE {generation_type}")
    _db.session.add(p)
    _db.session.commit()
    return p


def test_backoff_counts_only_refusals_after_the_last_saved_output(app):
    from datetime import timedelta
    from backend.utils import refusal_backoff as rb
    user = _user("counter")
    _refusal(user, timedelta(minutes=30))
    # A plain (not refused) cost row of the same type is not a failure.
    _db.session.add(APICostLog(
        user_id=user.id, model_id="gpt-5.5", request_type="profile",
        input_tokens=1, output_tokens=1, cost_microdollars=0))
    _db.session.commit()
    n, until, stopped = rb.profile_backoff_state(user.id)
    assert (n, stopped) == (1, False)
    assert rb.profile_in_backoff(user.id) is True
    assert rb.profile_in_backoff(
        user.id, now=until + timedelta(seconds=1)) is False
    # Refusals before the newest saved version don't count.
    assert rb.backoff_state(
        user.id, rb.PROFILE_REQUEST_TYPES,
        datetime.utcnow() - timedelta(minutes=1)) == (0, None, False)
    # Recent-context refusals are a separate streak.
    assert rb.recent_context_in_backoff(user.id) is False


def test_second_refusal_stops_the_job_for_good(app):
    """Two refusals in a row: stopped. No time passing releases it — the
    weekly retry is gone (voice review, 2026-10-01)."""
    from datetime import timedelta
    from backend.utils import refusal_backoff as rb
    user = _user("stopped")
    _refusal(user, timedelta(hours=3))
    _refusal(user, timedelta(hours=1))
    n, until, stopped = rb.profile_backoff_state(user.id)
    assert (n, until, stopped) == (2, None, True)
    assert rb.profile_in_backoff(
        user.id, now=datetime.utcnow() + timedelta(days=365)) is True


def test_a_stop_with_fewer_than_two_billed_failures_lifts_after_24_hours(
        app, monkeypatch):
    """#380 review: failures without a billed output (a provider outage,
    a batch unreadable until abandoned) stop the job for
    UNBILLED_STOP_EXPIRY, not until a new version. STOP_AFTER billed
    failures keep the stop until a new version. The Sentry report says
    which kind of stop it is."""
    from datetime import timedelta
    from backend.utils import refusal_backoff as rb
    assert rb.UNBILLED_STOP_EXPIRY == timedelta(hours=24)
    user = _user("outage")
    types = rb.RECENT_CONTEXT_REQUEST_TYPES
    now = datetime.utcnow()
    since = now - timedelta(days=2)
    older, newer = now - timedelta(hours=3), now - timedelta(hours=2)
    lifts = newer + timedelta(hours=24)

    # Two unbilled failures: stopped until 24 h after the newer one.
    assert rb.backoff_state(user.id, types, since,
                            unbilled_failures=[older, newer]) == (
        2, lifts, True)
    assert rb.in_backoff(user.id, types, since, "recent context",
                         now=lifts - timedelta(seconds=1),
                         unbilled_failures=[older, newer]) is True
    assert rb.in_backoff(user.id, types, since, "recent context",
                         now=lifts + timedelta(seconds=1),
                         unbilled_failures=[older, newer]) is False

    # One billed (a refusal) and one unbilled: fewer than two billed.
    _refusal(user, timedelta(hours=3), "recent_context")
    assert rb.backoff_state(user.id, types, since,
                            unbilled_failures=[newer]) == (2, lifts, True)

    # Two billed: stopped until a new version, however long it waits.
    assert rb.backoff_state(user.id, types, since,
                            billed_failures=[newer]) == (2, None, True)
    assert rb.in_backoff(user.id, types, since, "recent context",
                         now=now + timedelta(days=365),
                         billed_failures=[newer]) is True

    sentry = MagicMock()
    monkeypatch.setitem(sys.modules, "sentry_sdk", sentry)
    rb.report_stop(user.id, "recent context", 2, "gpt-5.5",
                   "recent_context", cause="batch item failed or cut off",
                   until=lifts)
    assert sentry.capture_message.call_args.args[0] == (
        "Background job stopped for 24 h after 2 runs in a row "
        "(batch item failed or cut off): recent context")
    scope = sentry.new_scope.return_value.__enter__.return_value
    tags = {c.args[0]: c.args[1] for c in scope.set_tag.call_args_list}
    assert tags["stop_lifts"] == "after_expiry"


def test_sync_trigger_backs_off_after_a_refusal(app, monkeypatch):
    """#368: after a refused chunk the unfinished chain re-triggers every
    hour. The backoff holds the sync trigger for the wait, then lets it
    dispatch."""
    from datetime import timedelta
    import backend.tasks.exports as exports

    user = _user("hourly")
    _profile(user)
    monkeypatch.setattr(exports, "should_continue_chain",
                        lambda user, profile: True)
    dispatch = MagicMock(return_value="task-1")
    monkeypatch.setattr(exports, "maybe_trigger_profile_update", dispatch)

    _refusal(user, timedelta(minutes=20))
    assert exports.maybe_trigger_incremental_profile_update(user) is None
    dispatch.assert_not_called()

    APICostLog.query.filter_by(user_id=user.id).update(
        {"created_at": datetime.utcnow() - timedelta(hours=2)})
    _db.session.commit()
    assert exports.maybe_trigger_incremental_profile_update(user) == "task-1"


def test_sync_trigger_never_runs_a_stopped_job_but_an_import_does(
        app, monkeypatch):
    """Stopped after two refusals: the hourly check skips the user however
    long ago they were. The import hand-off (ignore_backoff) runs it."""
    from datetime import timedelta
    import backend.tasks.exports as exports

    user = _user("stopped_sync")
    _profile(user, ago=timedelta(days=40))
    monkeypatch.setattr(exports, "should_continue_chain",
                        lambda user, profile: True)
    dispatch = MagicMock(return_value="task-1")
    monkeypatch.setattr(exports, "maybe_trigger_profile_update", dispatch)
    _refusal(user, timedelta(days=30))
    _refusal(user, timedelta(days=29))

    assert exports.maybe_trigger_incremental_profile_update(user) is None
    dispatch.assert_not_called()
    assert exports.maybe_trigger_incremental_profile_update(
        user, ignore_backoff=True) == "task-1"


def test_second_refusal_stops_reports_to_sentry_and_shows_in_admin(
        app, monkeypatch):
    """The 2nd refusal in a row: the admin column's seed_error says the job
    stopped, a tagged Sentry error is sent, and the next saved version
    clears both the note and the stop."""
    import backend.tasks.exports as exports
    from backend.utils import refusal_backoff as rb

    sentry = MagicMock()
    monkeypatch.setitem(sys.modules, "sentry_sdk", sentry)
    user = _user("giveup")
    with pytest.raises(EmptyTruncatedOutputError):
        exports.refuse_truncated_profile(
            user, "gpt-5.5", dict(EMPTY_CUT_OFF), "chunk 1")
    assert user.profile_seed_error is None
    sentry.capture_message.assert_not_called()

    with pytest.raises(EmptyTruncatedOutputError):
        exports.refuse_truncated_profile(
            user, "gpt-5.5", dict(EMPTY_CUT_OFF), "integration")
    _db.session.rollback()
    user = User.query.get(user.id)
    assert user.profile_seed_error.startswith(
        "Stopped: output cut off 2 times in a row (integration)")
    sentry.capture_message.assert_called_once()
    assert sentry.capture_message.call_args.kwargs["level"] == "error"
    scope = sentry.new_scope.return_value.__enter__.return_value
    tags = {c.args[0]: c.args[1] for c in scope.set_tag.call_args_list}
    assert tags["user_id"] == str(user.id)
    assert tags["job_type"] == "profile"
    assert rb.profile_in_backoff(user.id) is True

    exports._save_profile(
        user, "gpt-5.5", "GOOD", {"total_tokens": 1, "input_tokens": 1,
                                  "output_tokens": 1},
        source_tokens_used=1, source_data_cutoff=datetime(2025, 1, 1),
        generation_type="update")
    _db.session.rollback()
    assert User.query.get(user.id).profile_seed_error is None
    assert rb.profile_in_backoff(user.id) is False


# ── integration retry ────────────────────────────────────────────────────

def test_integration_due_only_for_a_finished_chain_without_integration(app):
    from datetime import timedelta
    import backend.tasks.exports as exports

    user = _user("integrate_me")
    root = _profile(user, "iterative", ago=timedelta(days=2))
    tip = _profile(user, "iterative", parent=root, ago=timedelta(days=1),
                   cutoff=datetime(2025, 6, 1))
    assert exports.integration_due(user, tip) is True

    # A from-scratch build pending replaces the chain: no integration.
    user.profile_needs_full_regen = True
    assert exports.integration_due(user, tip) is False
    user.profile_needs_full_regen = False

    # Integrated: done.
    integ = _profile(user, "integration", parent=tip, ago=timedelta(hours=1))
    assert exports.integration_due(user, tip) is False
    _db.session.delete(integ)
    _db.session.commit()

    # A single-chunk build ("initial") or a lone chunk has nothing to merge.
    lone = _user("lone")
    only = _profile(lone, "update")
    assert exports.integration_due(lone, only) is False
    single = _profile(_user("single"), "initial")
    assert exports.integration_due(User.query.get(single.user_id),
                                   single) is False


def test_integration_due_waits_for_an_unfinished_chain(app, monkeypatch):
    from datetime import timedelta
    import backend.tasks.exports as exports
    user = _user("unfinished")
    root = _profile(user, "iterative", ago=timedelta(days=2))
    tip = _profile(user, "iterative", parent=root)
    monkeypatch.setattr(exports, "should_continue_chain",
                        lambda user, profile: True)
    assert exports.integration_due(user, tip) is False


def test_sync_trigger_runs_the_integration_alone_after_a_refusal(
        app, monkeypatch):
    """The integration was refused once: the hourly check waits an hour,
    then dispatches an integration-only run; after a second refusal it
    never dispatches again (no loop)."""
    from datetime import timedelta
    import backend.tasks.exports as exports

    user = _user("int_retry")
    root = _profile(user, "iterative", ago=timedelta(days=2))
    _profile(user, "iterative", parent=root, ago=timedelta(hours=3),
             cutoff=datetime(2025, 6, 1))
    dispatch = MagicMock(return_value="task-int")
    monkeypatch.setattr(exports, "maybe_trigger_profile_update", dispatch)

    _refusal(user, timedelta(minutes=10))
    assert exports.maybe_trigger_incremental_profile_update(user) is None

    APICostLog.query.filter_by(user_id=user.id).update(
        {"created_at": datetime.utcnow() - timedelta(hours=2)})
    _db.session.commit()
    assert exports.maybe_trigger_incremental_profile_update(user) == "task-int"
    dispatch.assert_called_once_with(user.id, integrate_only=True)

    _refusal(user, timedelta(minutes=5))
    dispatch.reset_mock()
    assert exports.maybe_trigger_incremental_profile_update(user) is None
    dispatch.assert_not_called()


def test_integration_only_dispatch_targets_the_chain_tip(app, monkeypatch):
    """maybe_trigger_profile_update(integrate_only=True) hands the task the
    chain tip and the integrate_only flag."""
    from datetime import timedelta
    import backend.tasks.exports as exports
    import backend.utils.llm_nodes as llm_nodes

    user = _user("int_dispatch")
    root = _profile(user, "iterative", ago=timedelta(days=2))
    tip = _profile(user, "iterative", parent=root)
    task = MagicMock()
    task.delay.return_value.id = "t-1"
    monkeypatch.setattr(exports, "update_user_profile", task)
    monkeypatch.setattr(llm_nodes, "default_model_for", lambda u: "gpt-5.5")

    assert exports.maybe_trigger_profile_update(
        user.id, integrate_only=True) == "t-1"
    task.delay.assert_called_once_with(
        user.id, "gpt-5.5", tip.id, integrate_only=True)


# ── recent context: a new profile version restarts a stopped job ──────────

def test_recent_context_streak_restarts_after_a_new_profile(app):
    from datetime import timedelta
    from backend.utils import refusal_backoff as rb
    user = _user("rc_restart")
    _refusal(user, timedelta(hours=3), "recent_context")
    _refusal(user, timedelta(hours=2), "recent_context")
    assert rb.recent_context_backoff_state(user.id)[2] is True
    _profile(user, ago=timedelta(minutes=5))
    assert rb.recent_context_backoff_state(user.id) == (0, None, False)
    assert rb.recent_context_in_backoff(user.id) is False


def test_recent_context_backs_off_after_a_refusal(app, monkeypatch):
    """#368 MUST-FIX 1: the refusal is committed with the refusal marker,
    and the 10-minute check does not dispatch again until the wait is
    over."""
    from datetime import timedelta
    import backend.tasks.recent_context as rc
    import backend.routes.export_data as ed
    import backend.utils.llm_nodes as llm_nodes
    import backend.llm_providers as lp

    user = _user("rc_backoff")
    monkeypatch.setattr(rc, "_count_total_eligible_tokens", lambda uid: 20_000)
    monkeypatch.setattr(rc, "_count_new_tokens", lambda uid, since: 20_000)
    assert rc._should_generate_recent_context(user)[0] is True

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
    _db.session.rollback()   # the refusal row must have been committed

    assert rc._should_generate_recent_context(user)[0] is False
    APICostLog.query.filter_by(user_id=user.id).update(
        {"created_at": datetime.utcnow() - timedelta(hours=1, minutes=1)})
    _db.session.commit()
    assert rc._should_generate_recent_context(user)[0] is True


# ── voice todo merge: an empty result is billed, so it is logged ─────────

def test_todo_merge_logs_the_cost_of_an_empty_result(app, monkeypatch):
    import json
    import backend.tasks.voice_todo_merge as vtm
    import backend.utils.prompts as prompts
    import backend.llm_providers as lp
    from backend.models import UserTodo
    from backend.utils.refusal_backoff import REFUSED_REF

    user = _user("todo")
    node = Node(user_id=user.id, node_type="llm", ai_usage="chat",
                tool_calls_meta=json.dumps([{"name": "propose_todo"}]))
    node.set_content("add: buy milk")
    _db.session.add(node)
    _db.session.commit()
    monkeypatch.setattr(prompts, "get_user_prompt", lambda uid, key: "MERGE")
    monkeypatch.setattr(lp.LLMProvider, "get_completion",
                        staticmethod(lambda *a, **k: dict(EMPTY_CUT_OFF)))

    vtm._run_merge(node, "add: buy milk", user.id, "gpt-5.5", None)
    _db.session.rollback()   # only what was committed counts

    assert UserTodo.query.filter_by(user_id=user.id).count() == 0
    log = APICostLog.query.filter_by(
        user_id=user.id, request_type="todo_merge").one()
    assert log.request_ref == REFUSED_REF
    assert log.output_tokens == 32000
    meta = json.loads(Node.query.get(node.id).tool_calls_meta)
    assert meta[0]["apply_status"] == "failed"


# ── backfill_intentions.py: the intentions task's guard ──────────────────

def test_backfill_script_saves_nothing_for_an_empty_cut_off_result(
        app, monkeypatch):
    import importlib
    from backend.models import UserArtifact
    from backend.utils.refusal_backoff import REFUSED_REF
    # backend.app builds the whole app at import; the guard needs none of it.
    monkeypatch.setitem(sys.modules, "backend.app", MagicMock())
    monkeypatch.delitem(sys.modules, "backend.scripts.backfill_intentions",
                        raising=False)
    script = importlib.import_module("backend.scripts.backfill_intentions")
    monkeypatch.delitem(sys.modules, "backend.scripts.backfill_intentions")

    user = _user("backfill")
    with pytest.raises(EmptyTruncatedOutputError):
        script._refuse_empty_truncated(
            user, "gpt-5.5", dict(EMPTY_CUT_OFF), batch=True)
    _db.session.rollback()
    assert UserArtifact.query.filter_by(user_id=user.id).count() == 0
    log = APICostLog.query.filter_by(
        user_id=user.id, request_type="intentions_backfill").one()
    assert log.request_ref == REFUSED_REF

    # Text, even cut off, passes the guard (saved as before).
    script._refuse_empty_truncated(
        user, "gpt-5.5", {**EMPTY_CUT_OFF, "content": "- one"}, batch=True)

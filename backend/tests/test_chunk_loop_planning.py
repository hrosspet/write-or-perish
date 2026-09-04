"""The chunk loop plans every window over the remainder (design note
2026-09-03, docs/design/chunk-planner.md): equal chunks by rounding the
COUNT, the final window over-asks, coverage is tracked in stored units,
the per-user tokens-per-unit ratio is learned per tokenizer family, and
the real-token cap only ever raises the chunk count. Plus the input cap
helper and the non-strict fit that feed it, and the sync heartbeat's
continue rule.

Same harness as test_profile_regen_resume.py: in-memory SQLite,
ENCRYPTION_DISABLED, celery mocked so the module imports; the plain
helper ``_chunked_profile_loop`` under test stays real, with its LLM,
export and remainder-count calls scripted.
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

from backend.extensions import db as _db                 # noqa: E402
from backend.models import User, UserProfile, Node       # noqa: E402
from backend.utils.chunk_plan import (                   # noqa: E402
    FINAL_CHUNK_OVERASK_UNITS, CAP_MARGIN)

MODEL = {"provider": "anthropic", "api_model": "claude-x",
         "context_window": 1_000_000, "tokenizer_family": "claude_new",
         "input_price_per_mtok": 5.0, "output_price_per_mtok": 25.0}


@pytest.fixture
def app():
    import backend.celery_app  # noqa: F401  (import order, see regen test)
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["SUPPORTED_MODELS"] = {"m": dict(MODEL)}
    _db.init_app(app)
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()


def _user(name="planner", **kw):
    u = User(username=name, plan="alpha", twitter_id=None, approved=True, **kw)
    _db.session.add(u)
    _db.session.commit()
    return u


def _window(units, day):
    return {"content": f"W{units}", "token_count": units, "unit_count": units,
            "latest_node_created_at": datetime(2026, 1, day)}


def _node(user, units, created_at):
    n = Node(user_id=user.id, human_owner_id=user.id, node_type="user",
             ai_usage="chat", token_count=units, created_at=created_at,
             updated_at=created_at)
    n.set_content("x")
    _db.session.add(n)
    _db.session.commit()
    return n


def _run_loop(exports, monkeypatch, user, remainders, windows,
              input_tokens=1000, on_llm_call=None, **kw):
    """Drive _chunked_profile_loop with the export windows scripted and,
    unless ``remainders`` is None, the remainder sums too (None leaves
    count_remaining_units and the continue rule on the real test DB).
    Returns (result, budgets asked of the builder, prompts sent)."""
    budgets, prompts, windows = [], [], list(windows)

    def export(u, max_tokens=None, **k):
        budgets.append(max_tokens)
        return windows.pop(0) if windows else None

    def llm(task, model_id, prompt, user_id, keys, **k):
        prompts.append(prompt)
        if on_llm_call:
            on_llm_call()
        return {"content": f"PROFILE {len(prompts)}", "input_tokens": input_tokens,
                "output_tokens": 5, "total_tokens": input_tokens + 5}

    monkeypatch.setattr(exports, "build_user_export_content", export)
    if remainders is not None:
        monkeypatch.setattr(exports, "count_remaining_units",
                            MagicMock(side_effect=list(remainders)))
        monkeypatch.setattr(exports, "should_continue_chain",
                            lambda u, p: True)
    monkeypatch.setattr(exports, "_call_llm_with_retries", llm)
    import backend.llm_providers as lp
    monkeypatch.setattr(lp.LLMProvider, "count_tokens",
                        staticmethod(lambda m, msgs, k: None))
    result = exports._chunked_profile_loop(
        MagicMock(), user, "m",
        update_template="U {source_tokens_past}|{source_tokens_new}|{ratio_percent}|{new_data}",
        api_keys={}, **kw)
    return result, budgets, prompts


def test_loop_plans_equal_windows_and_over_asks_the_last(app, monkeypatch):
    """350k units → 4 chunks of 87.5k; after each chunk the remainder is
    re-planned; the last window over-asks so nothing is left behind."""
    import backend.tasks.exports as exports
    u = _user()
    (profile_id, n, covered), budgets, _ = _run_loop(
        exports, monkeypatch, u,
        remainders=[350_000, 262_500, 175_000, 87_500, 0],
        windows=[_window(87_500, d) for d in (1, 2, 3, 4)],
        initial_profile_content="BASE", generation_type="update")

    assert n == 4 and covered == 350_000
    assert budgets == [87_500, 87_500, 87_500, 87_500 + FINAL_CHUNK_OVERASK_UNITS]
    chain = UserProfile.query.filter_by(user_id=u.id).order_by(UserProfile.id).all()
    assert [p.source_tokens_used for p in chain] == [87_500, 175_000, 262_500, 350_000]
    assert chain[-1].id == profile_id
    assert chain[-1].source_data_cutoff == datetime(2026, 1, 4)


def test_loop_tracks_units_and_learns_the_ratio_per_family(app, monkeypatch):
    """Coverage and the update prompt's share are in stored units on both
    sides; the billed input tokens only calibrate tokens-per-unit, tagged
    with the model's tokenizer family."""
    import backend.tasks.exports as exports
    u = _user()
    (_, n, covered), _, prompts = _run_loop(
        exports, monkeypatch, u,
        remainders=[180_000, 90_000, 0],
        windows=[_window(90_000, 1), _window(90_000, 2)],
        input_tokens=150_000,
        initial_profile_content="BASE", initial_source_tokens=0,
        generation_type="update")

    assert (n, covered) == (2, 180_000)
    assert prompts[0].startswith("U 0|90000|100.0|W90000")
    assert prompts[1].startswith("U 90000|90000|50.0|W90000")
    assert u.profile_token_ratio == pytest.approx(150_000 / 90_000, abs=1e-3)
    assert u.profile_token_ratio_family == "claude_new"
    assert UserProfile.query.filter_by(user_id=u.id).count() == 2


def test_single_chunk_from_scratch_is_saved_as_initial(app, monkeypatch):
    """A from-scratch corpus that plans into one chunk is the old
    single-pass case: one call with the generation prompt, saved as
    "initial". More than one chunk → an iterative chain."""
    import backend.tasks.exports as exports
    u = _user()
    (pid, n, _), budgets, prompts = _run_loop(
        exports, monkeypatch, u,
        remainders=[60_000, 0], windows=[_window(60_000, 1)],
        first_chunk_prompt_fn=lambda c: "GEN " + c["content"])
    assert n == 1 and prompts == ["GEN W60000"]
    assert budgets == [60_000 + FINAL_CHUNK_OVERASK_UNITS]
    assert UserProfile.query.get(pid).generation_type == "initial"

    v = _user("two")
    (pid, n, _), _, prompts = _run_loop(
        exports, monkeypatch, v,
        remainders=[200_000, 100_000, 0],
        windows=[_window(100_000, 1), _window(100_000, 2)],
        first_chunk_prompt_fn=lambda c: "GEN " + c["content"])
    assert n == 2 and prompts[0] == "GEN W100000"
    types = [p.generation_type for p in
             UserProfile.query.filter_by(user_id=v.id).order_by(UserProfile.id)]
    assert types == ["iterative", "iterative"]


def test_cap_raises_the_chunk_count_from_the_measured_ratio(app, monkeypatch):
    """A measured 4 tokens per unit on a 200k window caps a window near
    45k units: 350k units plan into 8 chunks of 43,750, not 4 of 87,500."""
    import backend.tasks.exports as exports
    app.config["SUPPORTED_MODELS"]["m"]["context_window"] = 200_000
    u = _user(profile_token_ratio=4.0, profile_token_ratio_family="claude_new")
    _, budgets, _ = _run_loop(
        exports, monkeypatch, u,
        remainders=[350_000, 0], windows=[_window(43_750, 1)],
        initial_profile_content="BASE", generation_type="update")
    assert budgets == [43_750]


def test_model_input_cap():
    from backend.llm_providers import model_input_cap
    sol = {"context_window": 1_050_000, "max_input_tokens": 922_000,
           "long_context_threshold": 272_000}
    assert model_input_cap(sol) == 272_000            # the pricing tier binds first
    assert model_input_cap({"context_window": 1_050_000,
                            "max_input_tokens": 922_000}) == 922_000
    assert model_input_cap({"context_window": 1_000_000}) == 990_000
    assert model_input_cap({"context_window": 200_000,
                            "max_output_tokens": 4096}) == 200_000 - 4096
    assert model_input_cap({"context_window": 200_000}, 50_000) == 190_000  # capped at the default
    assert model_input_cap({"context_window": 200_000}, 2_000) == 198_000
    assert model_input_cap({}) == 190_000
    # The margin the planner takes off it.
    assert CAP_MARGIN == 0.05


def test_fit_by_count_non_strict_returns_the_last_build(app, monkeypatch):
    import backend.llm_providers as lp
    monkeypatch.setattr(lp.LLMProvider, "count_tokens",
                        staticmethod(lambda m, msgs, k: 500_000))  # never fits
    builds = []

    def build(budget):
        builds.append(budget)
        return ([{"role": "user", "content": [{"type": "text", "text": "x"}]}], budget)

    with pytest.raises(lp.PromptTooLongError):
        lp.fit_by_count("m", {}, 100_000, 90_000, build, max_rounds=2)
    built, _budget, real = lp.fit_by_count(
        "m", {}, 100_000, 90_000, build, max_rounds=2, strict=False)
    assert real == 500_000 and built[1] == builds[-1]


def test_sync_heartbeat_continues_an_unfinished_chain(app, monkeypatch):
    """maybe_trigger_incremental_profile_update: data beyond the cutoff
    that is older than the latest version dispatches an update inside
    MIN_INTERVAL and below the 80k gate; data newer than it does not."""
    import backend.tasks.exports as exports
    import backend.tasks.profile_batch as pb
    monkeypatch.setattr(pb, "use_batch_for_user", lambda *a, **k: False)
    calls = []
    monkeypatch.setattr(exports, "maybe_trigger_profile_update",
                        lambda uid, **k: calls.append((uid, k)) or "task")
    now = datetime.utcnow()
    u = _user()
    prof = UserProfile(user_id=u.id, generated_by="m", tokens_used=0,
                       generation_type="update", source_tokens_used=100,
                       source_data_cutoff=datetime(2026, 5, 1),
                       source_rendered_at=now - timedelta(minutes=45),
                       created_at=now - timedelta(minutes=40))
    prof.set_content("P")
    node = Node(user_id=u.id, node_type="user", ai_usage="chat", token_count=300,
                created_at=now - timedelta(minutes=50),
                updated_at=now - timedelta(minutes=50))
    node.set_content("older than the version")
    _db.session.add_all([prof, node])
    _db.session.commit()

    assert exports.maybe_trigger_incremental_profile_update(u) == "task"
    assert calls == [(u.id, {"force_full_regen": False})]

    node.created_at = now - timedelta(minutes=35)   # newer than the version
    _db.session.commit()
    assert exports.maybe_trigger_incremental_profile_update(u) is None
    assert len(calls) == 1


def _window_for(nodes):
    units = sum(n.token_count for n in nodes)
    return {"content": f"W{units}", "token_count": units, "unit_count": units,
            "latest_node_created_at": max(n.created_at for n in nodes)}


def test_run_ends_when_only_data_written_after_the_render_remains(app, monkeypatch):
    """A node written while a chunk is generating (after its window was
    rendered) is organic growth: the run ends at the planned chunks, and
    the seed gate does not chase the node either — it waits for the 80k
    gate like any other new writing."""
    import backend.tasks.exports as exports
    u = _user()
    a = _node(u, 60_000, datetime(2026, 1, 1))
    b = _node(u, 60_000, datetime(2026, 1, 2))
    written = []

    def write_during_generation():
        written.append(_node(u, 500, datetime.utcnow()))

    (pid, n, covered), budgets, _ = _run_loop(
        exports, monkeypatch, u, remainders=None, windows=[_window_for([a, b])],
        on_llm_call=write_during_generation,
        initial_profile_content="BASE", generation_type="update")

    assert (n, covered) == (1, 120_000)
    tip = UserProfile.query.get(pid)
    assert tip.source_rendered_at is not None
    assert tip.source_rendered_at < written[0].created_at
    assert exports.count_remaining_units(u.id, tip.source_data_cutoff) == 500  # unread...
    assert exports.should_continue_chain(u, tip) is False                     # ...but growth


def test_run_continues_over_data_that_existed_at_the_render(app, monkeypatch):
    """The rest of a multi-chunk remainder existed before the first window
    was rendered, so the run goes on to the planned second chunk."""
    import backend.tasks.exports as exports
    u = _user()
    a = _node(u, 90_000, datetime(2026, 1, 1))
    b = _node(u, 90_000, datetime(2026, 1, 2))
    (pid, n, covered), budgets, _ = _run_loop(
        exports, monkeypatch, u, remainders=None,
        windows=[_window_for([a]), _window_for([b])],
        initial_profile_content="BASE", generation_type="update")
    assert (n, covered) == (2, 180_000)
    assert budgets[0] == 90_000
    tip = UserProfile.query.get(pid)
    assert exports.count_remaining_units(u.id, tip.source_data_cutoff) == 0


def test_continue_rule_boundary_is_the_render_time(app):
    """Unread data counts as an unfinished chain only if it existed when the
    version's window was rendered; a legacy version without a render time
    falls back to its save time."""
    import backend.tasks.exports as exports
    now = datetime.utcnow()
    u = _user()
    _node(u, 500, now - timedelta(minutes=10))     # beyond the cutoff

    def version(rendered_at, created_at):
        p = UserProfile(user_id=u.id, generated_by="m", tokens_used=0,
                        generation_type="update", source_data_cutoff=now - timedelta(days=1),
                        source_rendered_at=rendered_at, created_at=created_at)
        p.set_content("P")
        _db.session.add(p)
        _db.session.flush()
        return p

    # written between the render and the save → growth
    assert exports.should_continue_chain(
        u, version(now - timedelta(minutes=20), now - timedelta(minutes=5))) is False
    # written before the render → unfinished chain
    assert exports.should_continue_chain(
        u, version(now - timedelta(minutes=5), now - timedelta(minutes=1))) is True
    # legacy row (no render time): an organic account waits for its next
    # gate-triggered update; a pinned one continues from the save time
    assert exports.should_continue_chain(
        u, version(None, now - timedelta(minutes=5))) is False
    u.profile_force_batch = True
    assert exports.should_continue_chain(
        u, version(None, now - timedelta(minutes=5))) is True



def test_sync_heartbeat_rebuilds_a_provisional_profile_from_scratch(app, monkeypatch):
    """Same rule on the sync heartbeat: a 10k-unit first build plus 85k
    organic units dispatches a from-scratch build, not an update."""
    import backend.tasks.exports as exports
    import backend.tasks.profile_batch as pb
    monkeypatch.setattr(pb, "use_batch_for_user", lambda *a, **k: False)
    calls = []
    monkeypatch.setattr(exports, "maybe_trigger_profile_update",
                        lambda uid, **k: calls.append(k) or "task")
    now = datetime.utcnow()
    for units, force in ((10_000, True), (90_000, False)):
        u = _user(f"prov{units}")
        prof = UserProfile(user_id=u.id, generated_by="m", tokens_used=0,
                           generation_type="initial", source_tokens_used=units,
                           source_data_cutoff=now - timedelta(days=30),
                           source_rendered_at=now - timedelta(days=30),
                           created_at=now - timedelta(days=30))
        prof.set_content("P")
        _db.session.add(prof)
        _node(u, 85_000, now - timedelta(days=2))
        assert exports.maybe_trigger_incremental_profile_update(u) == "task"
        assert calls[-1] == {"force_full_regen": force}

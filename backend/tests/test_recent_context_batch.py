"""#380: recent-context summaries go through the provider Batch API.

Nobody waits on a recent context, so the 10-minute check submits one batch
request per due user instead of calling the model, and a beat collector
saves the results at batch price. Until a batch is collected the prompts
keep reading the previous summary; an empty result is never saved; a
failed item stays stale and counts in the #368 backoff (one more try after
an hour, then stopped), so it is not resubmitted every 10 minutes.

Same harness as test_empty_truncated_bg_output: in-memory SQLite,
ENCRYPTION_DISABLED, celery mocked so the modules import. The provider is
never called: batch_submit / batch_check_and_collect / count_tokens /
get_completion and the export builder are faked.
"""
import os
import sys
from datetime import datetime, timedelta
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
    APICostLog, Node, RecentContextBatchJob, User, UserProfile,
    UserRecentContext)


ANTHROPIC_MODEL = "claude-opus-5"
OPENAI_MODEL = "gpt-6-astra"
OLD_CUTOFF = datetime(2025, 1, 1)
NEW_CUTOFF = datetime(2025, 6, 1)


@pytest.fixture
def app():
    # Warm celery_app first so it resolves the exports <-> profile_batch
    # import cycle in the safe order (see test_profile_regen_resume).
    import backend.celery_app  # noqa: F401
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["ANTHROPIC_API_KEY"] = "fake-key"
    app.config["OPENAI_API_KEY"] = "fake-key"
    app.config["DEFAULT_LLM_MODEL"] = ANTHROPIC_MODEL
    app.config["SUPPORTED_MODELS"] = {
        ANTHROPIC_MODEL: {
            "provider": "anthropic",
            "api_model": "claude-opus-5-20260101",
            "input_price_per_mtok": 5.0,
            "output_price_per_mtok": 25.0,
            "context_window": 200_000,
        },
        OPENAI_MODEL: {
            "provider": "openai",
            "api_model": "gpt-6-astra",
            "input_price_per_mtok": 10.0,
            "output_price_per_mtok": 50.0,
            "context_window": 1_050_000,
        },
    }
    _db.init_app(app)
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()


@pytest.fixture
def rc():
    import backend.tasks.recent_context as rc
    return rc


def _user(name, **kw):
    user = User(username=name, plan="alpha", twitter_id=None, approved=True,
                default_ai_usage="chat", **kw)
    _db.session.add(user)
    _db.session.commit()
    return user


def _previous_summary(user, text="PREVIOUS CONTEXT"):
    row = UserRecentContext(user_id=user.id, generated_by=ANTHROPIC_MODEL,
                            tokens_used=0, ai_usage="chat",
                            source_data_cutoff=OLD_CUTOFF,
                            created_at=datetime(2025, 1, 2))
    row.set_content(text)
    _db.session.add(row)
    _db.session.commit()
    return row


@pytest.fixture
def world(app, rc, monkeypatch):
    """Fakes everything below the scheduler: the token counts that gate a
    user (per user id; default: due), the export, the prompt file, the
    API keys, the token counter, and the provider. Any synchronous model
    call fails the test. The provider class is patched where the module
    under test holds it (rc.LLMProvider): in a full run a sibling test may
    have re-imported backend.llm_providers, and the module's direct path
    reads the real app's keys — local dev has a real, billing key."""
    import backend.routes.export_data as ed

    state = {"new_tokens": {}, "exports": [], "submitted": [],
             "count": lambda msgs: 5_000, "batch_n": 0}
    monkeypatch.setattr(rc, "get_api_keys_for_usage",
                        lambda config, kind: {"anthropic": "fake-key",
                                              "openai": "fake-key"})

    def tokens(uid, since=None):
        return state["new_tokens"].get(uid, 20_000)
    monkeypatch.setattr(rc, "_count_total_eligible_tokens", tokens)
    monkeypatch.setattr(rc, "_count_new_tokens", tokens)

    def export(user, **kw):
        state["exports"].append(kw)
        budget = kw.get("max_tokens")
        return {"content": "recent writing" if budget is None
                else f"recent writing within {budget}",
                "token_count": 12_000 if budget is None else budget,
                "latest_node_created_at": NEW_CUTOFF}
    monkeypatch.setattr(ed, "build_user_export_content", export)
    monkeypatch.setattr(rc, "_load_prompt",
                        lambda name, user_id=None:
                        "PROFILE:{user_profile} DATA:{recent_data}")
    monkeypatch.setattr(rc.LLMProvider, "count_tokens", staticmethod(
        lambda model_id, msgs, keys: state["count"](msgs)))

    def no_sync_call(*a, **k):
        raise AssertionError("the scheduled path must not call the model")
    monkeypatch.setattr(rc.LLMProvider, "get_completion",
                        staticmethod(no_sync_call))

    def fake_submit(requests_by_provider, api_keys, phase=None):
        state["submitted"].append(requests_by_provider)
        state["batch_n"] += 1
        ids = {}
        for provider, reqs in requests_by_provider.items():
            if provider == "anthropic":
                ids["anthropic"] = f"batch{state['batch_n']}-anthropic"
            else:
                for req in reqs:
                    ids[f"openai:{req['api_model']}"] = (
                        f"batch{state['batch_n']}-{req['api_model']}")
        return ids
    monkeypatch.setattr(rc, "batch_submit", fake_submit)
    return state


def _collect_with(rc, monkeypatch, results=None, pending=False, exc=None):
    def fake_collect(batch_ids, api_keys):
        if exc is not None:
            raise exc
        if pending:
            return {}, dict(batch_ids), {}
        return dict(results or {}), {}, {}
    monkeypatch.setattr(rc, "batch_check_and_collect", fake_collect)
    return rc._collect_recent_context_batches()


def _summary(user, text="NEW SUMMARY", **kw):
    return {f"recent_context_{user.id}": {
        "content": text, "input_tokens": 1000, "output_tokens": 100,
        "batch": True, **kw}}


def _check(rc):
    return rc._check_pending_recent_context_updates()


def _job_for(user):
    return [j for j in RecentContextBatchJob.query.all()
            if any(i["user_id"] == user.id for i in j.items)]


def _age_jobs(delta):
    for job in RecentContextBatchJob.query.all():
        if job.collected_at is not None:
            job.collected_at = job.collected_at - delta
    _db.session.commit()


def _shown_summary(user):
    """The summary a voice / chat session starting now would embed: the
    version context_artifacts pins for {user_recent} (the same lookup as
    llm_completion.get_user_recent_content without a pinned node)."""
    from backend.utils.context_artifacts import _resolve_latest_artifact
    row_id = _resolve_latest_artifact("recent_context", user.id)
    return (_db.session.get(UserRecentContext, row_id).get_content()
            if row_id is not None else None)


# ── submit ───────────────────────────────────────────────────────────────

def test_check_submits_one_batch_request_per_due_user_on_their_model(
        app, rc, world):
    """The 10-minute check puts each due user in a batch on the model the
    direct call would use, grouped by provider; it calls no model, bills
    nothing, and never submits a user twice while a batch is pending."""
    from backend.utils.llm_nodes import default_model_for

    claude_user = _user("claude-user")
    openai_user = _user("openai-user", preferred_model=OPENAI_MODEL)
    quiet_user = _user("quiet")
    world["new_tokens"][quiet_user.id] = 500       # under the threshold
    in_flight = _user("in-flight")
    _db.session.add(RecentContextBatchJob(
        provider_key="anthropic", batch_id="older", items=[{
            "custom_id": f"recent_context_{in_flight.id}",
            "user_id": in_flight.id, "model_id": ANTHROPIC_MODEL}]))
    # The model's placeholder account authors llm nodes: never a target,
    # so no cost is ever attributed to it.
    placeholder = _user(f"llm-{ANTHROPIC_MODEL}")
    node = Node(user_id=placeholder.id, node_type="llm", ai_usage="chat",
                created_at=datetime.utcnow() - timedelta(hours=1))
    node.set_content("a reply")
    _db.session.add(node)
    _db.session.commit()

    assert _check(rc) == {"status": "ok", "submitted": 2}

    assert len(world["submitted"]) == 1
    by_provider = world["submitted"][0]
    [claude_req] = by_provider["anthropic"]
    [openai_req] = by_provider["openai"]
    assert claude_req["custom_id"] == f"recent_context_{claude_user.id}"
    assert claude_req["model_id"] == default_model_for(claude_user) \
        == ANTHROPIC_MODEL
    assert claude_req["api_model"] == "claude-opus-5-20260101"
    assert openai_req["model_id"] == default_model_for(openai_user) \
        == OPENAI_MODEL
    assert openai_req["api_model"] == "gpt-6-astra"
    # The output cap the direct call gets by default.
    assert claude_req["max_tokens"] == 32_000
    prompt = claude_req["messages"][0]["content"][0]["text"]
    assert prompt == "PROFILE: DATA:recent writing"

    jobs = {j.provider_key: j for j in RecentContextBatchJob.query.filter(
        RecentContextBatchJob.batch_id != "older").all()}
    assert set(jobs) == {"anthropic", "openai:gpt-6-astra"}
    [meta] = jobs["anthropic"].items
    assert meta["user_id"] == claude_user.id
    assert meta["source_data_cutoff"] == NEW_CUTOFF.isoformat()
    assert meta["source_tokens"] == 12_000
    assert jobs["openai:gpt-6-astra"].items[0]["user_id"] == openai_user.id
    assert all(j.status == "pending" for j in jobs.values())

    # Nothing billed at submit time; nothing saved yet.
    assert APICostLog.query.count() == 0
    assert UserRecentContext.query.count() == 0

    # The next check submits nothing: everyone due is in a pending batch.
    assert _check(rc) == {"status": "ok", "submitted": 0}
    assert len(world["submitted"]) == 1


def test_paused_updates_submit_nothing(app, rc, world):
    _user("due")
    app.config["PROFILE_UPDATES_PAUSED"] = True
    assert _check(rc)["submitted"] == 0
    assert world["submitted"] == []


def test_an_oversize_prompt_is_shrunk_before_it_is_submitted(
        app, rc, world):
    """A batch request gets no prompt-too-long retry, so the window is
    counted and shrunk before submitting (the direct path shrinks it
    after a rejection)."""
    user = _user("long-writer")
    counts = iter([250_000, 100_000])
    world["count"] = lambda msgs: next(counts)

    assert _check(rc)["submitted"] == 1
    budget = world["exports"][-1]["max_tokens"]
    assert budget is not None and budget < 12_000
    [req] = world["submitted"][0]["anthropic"]
    assert req["messages"][0]["content"][0]["text"].endswith(
        f"within {budget}")
    assert _job_for(user)[0].items[0]["source_tokens"] == budget


# ── collect ──────────────────────────────────────────────────────────────

def test_previous_summary_stays_in_use_until_the_batch_is_collected(
        app, rc, world, monkeypatch):
    user = _user("voice-user")
    _previous_summary(user)
    assert _check(rc)["submitted"] == 1

    # Submitted, not ended: the prompts still read the previous summary.
    assert _shown_summary(user) == "PREVIOUS CONTEXT"
    assert _collect_with(rc, monkeypatch, pending=True) == {
        "collected": 0, "abandoned": 0}
    assert _shown_summary(user) == "PREVIOUS CONTEXT"
    assert _job_for(user)[0].status == "pending"

    # Collected: the new summary replaces it.
    assert _collect_with(rc, monkeypatch, _summary(user)) == {
        "collected": 1, "abandoned": 0}
    shown = _shown_summary(user)
    assert shown.endswith("NEW SUMMARY")
    assert shown != "PREVIOUS CONTEXT"


def test_collector_saves_at_batch_price_on_the_human_owner(
        app, rc, world, monkeypatch):
    user = _user("owner")
    profile = UserProfile(user_id=user.id, generated_by=ANTHROPIC_MODEL,
                          tokens_used=0, ai_usage="chat",
                          generation_type="initial",
                          source_data_cutoff=OLD_CUTOFF,
                          created_at=datetime(2025, 1, 1))
    profile.set_content("THE PROFILE")
    _db.session.add(profile)
    _db.session.commit()
    assert _check(rc)["submitted"] == 1
    prompt = world["submitted"][0]["anthropic"][0][
        "messages"][0]["content"][0]["text"]
    assert prompt == "PROFILE:THE PROFILE DATA:recent writing"

    _collect_with(rc, monkeypatch, _summary(user, "BATCHED SUMMARY"))

    log = APICostLog.query.one()
    assert log.user_id == user.id                 # the human, not llm-*
    assert log.request_type == "recent_context"
    assert log.model_id == ANTHROPIC_MODEL
    # 1000 in x $5 + 100 out x $25 = 7500 microdollars, halved for batch.
    assert log.cost_microdollars == 3750
    assert log.request_ref is None

    row = UserRecentContext.query.one()
    assert row.generated_by == ANTHROPIC_MODEL
    assert row.profile_id == profile.id
    assert row.source_data_cutoff == NEW_CUTOFF
    assert row.source_tokens_covered == 12_000
    assert row.tokens_used == 1100
    assert row.ai_usage == "chat"
    assert row.get_content().endswith("BATCHED SUMMARY")

    [job] = _job_for(user)
    assert job.status == "collected"
    assert job.items[0]["outcome"] == "saved"


def test_a_failed_item_stays_stale_and_is_retried_once_then_stops(
        app, rc, world, monkeypatch):
    """No result for the item (errored / expired at the provider): nothing
    saved, nothing billed, the previous summary stays. The user is
    retried by one later check (after the hour's wait), not every 10
    minutes; a second failure in a row stops the job and reports it."""
    from backend.utils import refusal_backoff
    stops = []
    monkeypatch.setattr(refusal_backoff, "report_stop",
                        lambda *a, **k: stops.append((a, k)))
    user = _user("unlucky")
    _previous_summary(user)

    assert _check(rc)["submitted"] == 1
    assert _collect_with(rc, monkeypatch, results={}) == {
        "collected": 1, "abandoned": 0}
    [job] = _job_for(user)
    assert job.status == "collected"
    assert job.items[0]["outcome"] == "failed"
    assert _shown_summary(user) == "PREVIOUS CONTEXT"
    assert APICostLog.query.count() == 0
    assert stops == []

    # The next checks inside the hour leave it alone...
    assert _check(rc)["submitted"] == 0
    assert _check(rc)["submitted"] == 0
    # ...the first one after it retries, once.
    _age_jobs(timedelta(hours=1, minutes=1))
    assert _check(rc)["submitted"] == 1
    assert _check(rc)["submitted"] == 0           # pending: not again
    assert len(world["submitted"]) == 2

    # A second failure in a row stops the job for this user.
    _collect_with(rc, monkeypatch, results={})
    assert len(stops) == 1
    assert stops[0][0][0] == user.id
    _age_jobs(timedelta(hours=3))
    assert _check(rc)["submitted"] == 0
    assert _shown_summary(user) == "PREVIOUS CONTEXT"

    # A new profile version restarts it (as after a refused output).
    profile = UserProfile(user_id=user.id, generated_by=ANTHROPIC_MODEL,
                          tokens_used=0, ai_usage="chat",
                          generation_type="update",
                          source_data_cutoff=OLD_CUTOFF)
    profile.set_content("NEWER PROFILE")
    _db.session.add(profile)
    _db.session.commit()
    assert _check(rc)["submitted"] == 1


def test_an_empty_cut_off_result_is_billed_refused_and_backs_off(
        app, rc, world, monkeypatch):
    from backend.utils.refusal_backoff import REFUSED_REF
    user = _user("thinker")
    _previous_summary(user)
    assert _check(rc)["submitted"] == 1

    _collect_with(rc, monkeypatch, _summary(
        user, "", truncated=True, output_tokens=32_000))

    assert _shown_summary(user) == "PREVIOUS CONTEXT"
    assert UserRecentContext.query.count() == 1
    log = APICostLog.query.one()
    assert log.request_ref == REFUSED_REF
    assert log.user_id == user.id
    # Billed at batch price: (1000 x $5 + 32000 x $25) / 2.
    assert log.cost_microdollars == 402_500
    assert _job_for(user)[0].items[0]["outcome"] == "refused"
    # One strike (the cost row; the item is not counted a second time).
    assert _check(rc)["submitted"] == 0
    APICostLog.query.update({"created_at": datetime.utcnow()
                             - timedelta(hours=1, minutes=1)})
    _db.session.commit()
    assert _check(rc)["submitted"] == 1


def test_an_empty_result_that_was_not_cut_off_is_billed_and_not_saved(
        app, rc, world, monkeypatch):
    user = _user("silent")
    _previous_summary(user)
    _check(rc)
    _collect_with(rc, monkeypatch, _summary(user, "  \n"))

    assert _shown_summary(user) == "PREVIOUS CONTEXT"
    log = APICostLog.query.one()
    assert log.request_ref is None
    assert log.cost_microdollars == 3750
    assert _job_for(user)[0].items[0]["outcome"] == "failed"
    assert _check(rc)["submitted"] == 0           # waits like a failure


def _raise_not_found():
    return RuntimeError("404: no such batch")


@pytest.mark.parametrize("mode", ["pending", "unreadable"])
def test_a_batch_it_cannot_close_is_abandoned_after_the_window(
        app, rc, world, monkeypatch, mode):
    """Abandoned after the providers' 24h window plus slack — the 25h the
    digest collector uses, the same constant."""
    from backend.utils import llm_batch
    from backend.tasks import external_digest
    assert rc.BATCH_JOB_MAX_AGE == timedelta(hours=25)
    assert rc.BATCH_JOB_MAX_AGE is llm_batch.BATCH_JOB_MAX_AGE \
        is external_digest.BATCH_JOB_MAX_AGE

    user = _user("lost-batch")
    _previous_summary(user)
    _check(rc)
    kwargs = ({"pending": True} if mode == "pending"
              else {"exc": _raise_not_found()})

    # A young batch stays pending, readable or not.
    assert _collect_with(rc, monkeypatch, **kwargs) == {
        "collected": 0, "abandoned": 0}
    assert _job_for(user)[0].status == "pending"

    [job] = _job_for(user)
    job.submitted_at = datetime.utcnow() - timedelta(hours=25, minutes=1)
    _db.session.commit()
    assert _collect_with(rc, monkeypatch, **kwargs) == {
        "collected": 0, "abandoned": 1}
    [job] = _job_for(user)
    assert job.status == "abandoned"
    assert job.items[0]["outcome"] == "failed"
    assert user.id not in rc._users_in_pending_batches()
    assert _shown_summary(user) == "PREVIOUS CONTEXT"
    # Counted as a failure: retried after the wait, not at once.
    assert _check(rc)["submitted"] == 0
    _age_jobs(timedelta(hours=1, minutes=1))
    assert _check(rc)["submitted"] == 1


def test_an_overlapping_collector_run_does_not_save_twice(
        app, rc, world, monkeypatch):
    user = _user("raced")
    _check(rc)
    [job] = _job_for(user)

    def other_run_claims_first(batch_ids, api_keys):
        RecentContextBatchJob.query.filter_by(id=job.id).update(
            {"status": "collected", "collected_at": datetime.utcnow()})
        _db.session.commit()
        return _summary(user), {}, {}
    monkeypatch.setattr(rc, "batch_check_and_collect",
                        other_run_claims_first)

    assert rc._collect_recent_context_batches() == {
        "collected": 0, "abandoned": 0}
    assert UserRecentContext.query.count() == 0
    assert APICostLog.query.count() == 0


def test_a_result_for_an_account_opted_out_meanwhile_is_billed_not_saved(
        app, rc, world, monkeypatch):
    user = _user("opted-out-later")
    _check(rc)
    user.default_ai_usage = "none"
    _db.session.commit()

    _collect_with(rc, monkeypatch, _summary(user))
    assert UserRecentContext.query.count() == 0
    assert APICostLog.query.one().cost_microdollars == 3750
    assert _job_for(user)[0].items[0]["outcome"] == "skipped"


def test_a_result_older_than_the_saved_summary_is_not_saved(
        app, rc, world, monkeypatch):
    """The direct path's 'nothing new' guard, re-checked at save time: if
    a summary covering this window was saved while the batch ran, the
    batch result does not replace it."""
    user = _user("covered")
    _check(rc)
    newer = UserRecentContext(user_id=user.id, generated_by=ANTHROPIC_MODEL,
                              tokens_used=0, ai_usage="chat",
                              source_data_cutoff=NEW_CUTOFF)
    newer.set_content("SAVED MEANWHILE")
    _db.session.add(newer)
    _db.session.commit()

    _collect_with(rc, monkeypatch, _summary(user))
    assert _shown_summary(user) == "SAVED MEANWHILE"
    assert APICostLog.query.count() == 1
    assert _job_for(user)[0].items[0]["outcome"] == "skipped"


# ── the direct path ──────────────────────────────────────────────────────

def test_the_direct_path_still_calls_the_model_at_full_price(
        app, rc, world, monkeypatch):
    """Nothing in the app waits on a recent context, so no caller uses the
    direct path; it stays a working synchronous call (one model call,
    full price) for a manual run."""
    calls = []

    def completion(model_id, messages, api_keys, **kw):
        calls.append((model_id, api_keys["anthropic"]))
        return {"content": "DIRECT SUMMARY", "input_tokens": 1000,
                "output_tokens": 100, "total_tokens": 1100}
    monkeypatch.setattr(rc.LLMProvider, "get_completion",
                        staticmethod(completion))
    user = _user("direct")

    rc._generate_recent_context_impl(user.id)

    assert calls == [(ANTHROPIC_MODEL, "fake-key")]
    assert world["submitted"] == []
    assert APICostLog.query.one().cost_microdollars == 7500
    assert UserRecentContext.query.one().get_content().endswith(
        "DIRECT SUMMARY")

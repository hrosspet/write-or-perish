"""State-machine tests for the profile-batch pipeline (issue #173, Part A).

The network boundary (batch_submit / batch_check_and_collect) and the export
builder are mocked; _save_profile and the DB run for real so we assert the
chain actually advances. The @celery.task wrappers aren't called directly
(they're mocks under the celery stub) — we exercise the _impl functions.
"""
import os
import re
import sys
from datetime import datetime, timedelta
from unittest.mock import MagicMock

# Anthropic requires batch custom_id to match this; OpenAI is no stricter.
CUSTOM_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

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

for _mod in ["backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

from backend.extensions import db  # noqa: E402
from backend.models import (  # noqa: E402
    User, UserProfile, ProfileBatchJob, APICostLog, Node)

# backend.tasks.profile_batch is imported lazily in the `app` fixture: an
# eager import at collection time trips over cross-file celery-mock ordering
# in the full suite. Matches how the other task-module tests import.
pb = None

MODELS = {"test-model": {
    "provider": "anthropic", "api_model": "claude-x",
    "input_price_per_mtok": 5.0, "output_price_per_mtok": 30.0}}


@pytest.fixture
def app(monkeypatch):
    global pb
    import backend.tasks.profile_batch as _pb
    pb = _pb
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["DEFAULT_LLM_MODEL"] = "test-model"
    app.config["SUPPORTED_MODELS"] = MODELS
    app.config["PROFILE_USE_BATCH"] = False
    app.config["PROFILE_BATCH_USER_IDS"] = set()
    app.config["OPENAI_API_KEY_BATCH"] = None
    db.init_app(app)
    # No real keys in the test app — the network boundary is mocked anyway.
    monkeypatch.setattr(pb, "get_api_keys_for_usage",
                        lambda *a, **k: {"anthropic": "k", "openai": "k"})
    # Pre-submit sizing count: default to "unavailable" so existing tests
    # exercise the fall-through (submit at the calibrated budget as before).
    import backend.llm_providers as lp
    monkeypatch.setattr(lp.LLMProvider, "count_tokens",
                        staticmethod(lambda model_id, messages, keys: None))
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


_N = [0]


def _user(**kw):
    _N[0] += 1
    u = User(username=f"u{_N[0]}", plan="alpha", twitter_id=None,
             approved=True, preferred_model="test-model", **kw)
    db.session.add(u)
    db.session.flush()
    return u


def _prev_profile(user, cutoff, source_tokens=1000, gen_type="update"):
    p = UserProfile(
        user_id=user.id, generated_by="test-model", tokens_used=0,
        generation_type=gen_type, source_tokens_used=source_tokens,
        source_data_cutoff=cutoff)
    p.set_content("PREVIOUS PROFILE")
    db.session.add(p)
    db.session.flush()
    return p


# ── gate ────────────────────────────────────────────────────────────────

def test_use_batch_for_user_gate(app):
    u = _user()
    db.session.commit()
    assert not pb.use_batch_for_user(
        u, {"PROFILE_USE_BATCH": False, "PROFILE_BATCH_USER_IDS": set()})
    assert pb.use_batch_for_user(
        u, {"PROFILE_USE_BATCH": True, "PROFILE_BATCH_USER_IDS": set()})
    assert pb.use_batch_for_user(
        u, {"PROFILE_USE_BATCH": False, "PROFILE_BATCH_USER_IDS": {u.id}})


# ── request builder ───────────────────────────────────────────────────────

def test_build_next_request_chunk(app, monkeypatch):
    u = _user()
    prev = _prev_profile(u, datetime(2026, 5, 1))
    db.session.commit()
    monkeypatch.setattr(pb._exports, "build_user_export_content", MagicMock(
        return_value={"content": "NEW DATA", "token_count": 90000,
                      "latest_node_created_at": datetime(2026, 6, 1)}))
    monkeypatch.setattr(pb._exports, "build_update_template", lambda uid: (
        "T {existing_profile}|{new_data}|{source_tokens_past}"
        "|{source_tokens_new}|{ratio_percent}"))

    req = pb._build_next_profile_request(u)

    assert req["provider"] == "anthropic"
    assert req["meta"]["kind"] == "chunk"
    assert req["meta"]["generation_type"] == "update"
    assert req["meta"]["prev_profile_id"] == prev.id
    assert req["meta"]["prev_cumulative"] == 1000
    assert req["meta"]["source_data_cutoff"] == "2026-06-01T00:00:00"
    text = req["request"]["messages"][0]["content"][0]["text"]
    assert "NEW DATA" in text and "PREVIOUS PROFILE" in text
    # Regression: Anthropic rejects custom_id with colons (must match pattern)
    assert CUSTOM_ID_RE.match(req["request"]["custom_id"])


def test_build_next_request_shrinks_oversized_chunk_before_submit(app, monkeypatch):
    """Pre-submit sizing: an over-limit token count shrinks the chunk
    budget and rebuilds BEFORE submitting, instead of burning a batch
    attempt on the provider's overflow rejection."""
    u = _user()
    _prev_profile(u, datetime(2026, 5, 1))
    db.session.commit()
    budgets = []

    def export(user, max_tokens=None, **kw):
        budgets.append(max_tokens)
        return {"content": f"DATA[{max_tokens}]", "token_count": 90000,
                "latest_node_created_at": datetime(2026, 6, 1)}

    monkeypatch.setattr(pb._exports, "build_user_export_content", export)
    monkeypatch.setattr(pb._exports, "build_update_template", lambda uid: (
        "T {existing_profile}|{new_data}|{source_tokens_past}"
        "|{source_tokens_new}|{ratio_percent}"))
    import backend.llm_providers as lp
    counts = iter([400_000, 100_000])  # over the 200k window, then fits
    monkeypatch.setattr(lp.LLMProvider, "count_tokens",
                        staticmethod(lambda m, msgs, k: next(counts)))

    req = pb._build_next_profile_request(u)

    assert len(budgets) == 2 and budgets[1] < budgets[0]
    text = req["request"]["messages"][0]["content"][0]["text"]
    assert f"DATA[{budgets[1]}]" in text  # the SHRUNK chunk is submitted


def test_build_next_request_uses_engaged_scope(app, monkeypatch):
    """Profile chunks read the anchor scope (own + addressed nodes, #110),
    not the legacy authored_threads scope that missed the user's replies
    in other users' threads and silently returned None whenever no
    thread root fit the budget window."""
    u = _user()
    _prev_profile(u, datetime(2026, 5, 1))
    db.session.commit()
    export = MagicMock(
        return_value={"content": "DATA", "token_count": 90000,
                      "latest_node_created_at": datetime(2026, 6, 1)})
    monkeypatch.setattr(pb._exports, "build_user_export_content", export)
    monkeypatch.setattr(pb._exports, "build_update_template", lambda uid: (
        "T {existing_profile}|{new_data}|{source_tokens_past}"
        "|{source_tokens_new}|{ratio_percent}"))

    pb._build_next_profile_request(u)

    assert export.call_args.kwargs["include_strategy"] == "engaged_threads"


def test_build_next_request_small_chunk_mid_corpus_still_chunks(
        app, monkeypatch):
    """A chunk that re-measures below MIN_CHUNK_TOKENS but has data
    remaining beyond it is a full budget window, not a tail — it must
    be processed, not deferred (the unit-mismatch starvation that froze
    a regen at a months-old cutoff with ~320k stored tokens left)."""
    u = _user()
    _prev_profile(u, datetime(2026, 5, 1))
    db.session.commit()
    monkeypatch.setattr(pb._exports, "build_user_export_content", MagicMock(
        return_value={"content": "SMALL RENDER", "token_count": 50000,
                      "latest_node_created_at": datetime(2026, 6, 1)}))
    monkeypatch.setattr(pb._exports, "build_update_template", lambda uid: (
        "T {existing_profile}|{new_data}|{source_tokens_past}"
        "|{source_tokens_new}|{ratio_percent}"))
    monkeypatch.setattr(pb._exports, "_has_more_source_after", lambda u, ts: True)

    req = pb._build_next_profile_request(u)

    assert req is not None and req["meta"]["kind"] == "chunk"


def test_build_next_request_small_tail_defers(app, monkeypatch):
    """A genuinely small tail (nothing beyond it) is deferred to the
    next update cycle — the threshold's original purpose."""
    u = _user()
    _prev_profile(u, datetime(2026, 5, 1))
    db.session.commit()
    monkeypatch.setattr(pb._exports, "build_user_export_content", MagicMock(
        return_value={"content": "TINY TAIL", "token_count": 5000,
                      "latest_node_created_at": datetime(2026, 6, 1)}))
    monkeypatch.setattr(pb._exports, "_has_more_source_after", lambda u, ts: False)
    monkeypatch.setattr(pb._exports, "build_integration_messages",
                        lambda uid, pid: (None, None))

    req = pb._build_next_profile_request(u)

    assert req is None  # not a chunk; single-version chain → no integration


def test_build_next_request_none_when_no_data(app, monkeypatch):
    u = _user()
    _prev_profile(u, datetime(2026, 5, 1))   # single version → no integration
    db.session.commit()
    monkeypatch.setattr(pb._exports, "build_user_export_content",
                        MagicMock(return_value=None))
    assert pb._build_next_profile_request(u) is None


# ── seed gate: null-cutoff (user-written) profiles ────────────────────────

def _age(obj, days):
    """Backdate a row so the inactivity/interval gates pass, isolating the
    token-threshold decision."""
    obj.created_at = datetime.utcnow() - timedelta(days=days)


def _seed_node(user, tokens):
    n = Node(user_id=user.id, node_type="user", ai_usage="chat",
             token_count=tokens)
    n.set_content("writing")
    db.session.add(n)
    db.session.flush()
    _age(n, 25)
    return n


def test_should_seed_null_cutoff_low_data_false(app):
    """A null-cutoff (user-written) profile with <80k tokens must NOT seed —
    the sentinel that used to force-seed it is gone."""
    u = _user()
    _age(_prev_profile(u, None, gen_type="initial"), 30)
    _seed_node(u, 2884)
    db.session.commit()
    assert pb._should_seed(u) is False


def test_should_seed_null_cutoff_high_data_true(app):
    """A null-cutoff profile WITH >=80k tokens still seeds, so the base gets
    folded into a data-grounded profile."""
    u = _user()
    _age(_prev_profile(u, None, gen_type="initial"), 30)
    _seed_node(u, 90000)
    db.session.commit()
    assert pb._should_seed(u) is True


# ── full-regen flag (profile_needs_full_regen) ────────────────────────────
# The batch pipeline used to be blind to the flag: the seeder's gates
# measure "new tokens since cutoff" (a cutoff the flag often exists to
# disavow) and the builder always resumed from the latest profile, so a
# requested full rebuild was silently downgraded to an incremental
# update and the flag swallowed.

def test_should_seed_full_regen_flag_overrides_gates(app):
    """Flag set → seed, even when interval/token gates would refuse."""
    u = _user()
    _prev_profile(u, datetime.utcnow())   # fresh profile → MIN_INTERVAL fails
    u.profile_needs_full_regen = True
    db.session.commit()
    assert pb._should_seed(u) is True


def test_build_next_request_full_regen_starts_from_scratch(app, monkeypatch):
    """Flag set → builder ignores the existing chain: from-scratch chunk 1
    (initial-generation prompt, no parent, cumulative from zero)."""
    u = _user()
    _prev_profile(u, datetime(2026, 5, 1))
    u.profile_needs_full_regen = True
    db.session.commit()
    export = MagicMock(
        return_value={"content": "ALL DATA", "token_count": 90000,
                      "latest_node_created_at": datetime(2026, 6, 1)})
    monkeypatch.setattr(pb._exports, "build_user_export_content", export)
    monkeypatch.setattr(pb._exports, "_load_prompt",
                        lambda *a, **k: "GEN {user_export}")

    req = pb._build_next_profile_request(u)

    # Export builds from the beginning of time, not from prev's cutoff.
    assert export.call_args.kwargs["created_after"] is None
    assert req["meta"]["prev_profile_id"] is None
    assert req["meta"]["generation_type"] == "iterative"
    assert req["meta"]["prev_cumulative"] == 0
    text = req["request"]["messages"][0]["content"][0]["text"]
    assert "ALL DATA" in text and "PREVIOUS PROFILE" not in text


def test_poll_clears_flag_only_for_from_scratch_chunk(app, monkeypatch):
    """A flag set while an incremental chunk is in flight survives that
    chunk (so the next build honors it); a from-scratch chunk commits the
    rebuild and clears it."""
    monkeypatch.setattr(pb._exports, "build_user_export_content",
                        MagicMock(return_value=None))
    monkeypatch.setattr(pb._exports, "build_integration_messages",
                        lambda uid, pid: (None, None))
    monkeypatch.setattr(pb, "batch_submit", MagicMock(return_value={}))

    # 1. incremental chunk (prev_profile_id set): flag survives
    u = _user()
    prev = _prev_profile(u, datetime(2026, 5, 1))
    job, item = _chunk_job(u, prev)
    u.profile_needs_full_regen = True
    db.session.commit()
    monkeypatch.setattr(pb, "batch_check_and_collect", lambda bids, keys: (
        {item["custom_id"]: {"content": "P", "input_tokens": 100,
                             "output_tokens": 50}}, {}, {}))
    pb._poll_profile_batches()
    assert User.query.get(u.id).profile_needs_full_regen is True

    # 2. from-scratch chunk (prev_profile_id None): flag cleared
    u2 = _user()
    u2.profile_needs_full_regen = True
    item2 = {
        "custom_id": f"profile_{u2.id}_0_chunk", "user_id": u2.id,
        "kind": "chunk", "prev_profile_id": None,
        "generation_type": "iterative", "prev_cumulative": 0,
        "source_data_cutoff": "2026-06-01T00:00:00",
        "model_id": "test-model",
    }
    db.session.add(ProfileBatchJob(
        provider_key="anthropic", batch_id="b9", status="pending",
        items=[item2], submitted_at=datetime.utcnow()))
    u2.profile_batch_pending = True
    db.session.commit()
    monkeypatch.setattr(pb, "batch_check_and_collect", lambda bids, keys: (
        {item2["custom_id"]: {"content": "P2", "input_tokens": 100,
                              "output_tokens": 50}}, {}, {}))
    pb._poll_profile_batches()
    u2_fresh = User.query.get(u2.id)
    assert u2_fresh.profile_needs_full_regen is False
    saved = UserProfile.query.filter_by(
        user_id=u2.id, generation_type="iterative").first()
    assert saved is not None and saved.parent_profile_id is None


def test_poll_saves_from_scratch_chunk_despite_historic_twin(app, monkeypatch):
    """A repeated from-scratch rebuild reproduces its predecessor's exact
    idempotency key (parent=None, same deterministic chunk-1 cutoff). The
    duplicate check must be scoped to this job's submission time, or the
    result is discarded, the flag survives, and the builder re-submits
    chunk 1 forever (observed in prod, user 27)."""
    u = _user()
    u.profile_needs_full_regen = True
    # Historic truncated regen: chunk-1 row with the SAME key tuple.
    old = UserProfile(
        user_id=u.id, generated_by="test-model", tokens_used=0,
        generation_type="iterative", source_tokens_used=100,
        source_data_cutoff=datetime(2024, 12, 13, 10, 52, 57),
        parent_profile_id=None)
    old.set_content("OLD TRUNCATED CHUNK 1")
    old.created_at = datetime.utcnow() - timedelta(days=30)
    db.session.add(old)
    item = {
        "custom_id": f"profile_{u.id}_0_chunk", "user_id": u.id,
        "kind": "chunk", "prev_profile_id": None,
        "generation_type": "iterative", "prev_cumulative": 0,
        "source_data_cutoff": "2024-12-13T10:52:57",
        "model_id": "test-model",
    }
    db.session.add(ProfileBatchJob(
        provider_key="anthropic", batch_id="b7", status="pending",
        items=[item], submitted_at=datetime.utcnow()))
    u.profile_batch_pending = True
    db.session.commit()

    monkeypatch.setattr(pb, "batch_check_and_collect", lambda bids, keys: (
        {item["custom_id"]: {"content": "REBUILT CHUNK 1",
                             "input_tokens": 100, "output_tokens": 50}},
        {}, {}))
    monkeypatch.setattr(pb._exports, "build_user_export_content",
                        MagicMock(return_value=None))
    monkeypatch.setattr(pb._exports, "build_integration_messages",
                        lambda uid, pid: (None, None))
    submit = MagicMock(return_value={})
    monkeypatch.setattr(pb, "batch_submit", submit)

    pb._poll_profile_batches()

    twins = UserProfile.query.filter_by(
        user_id=u.id, parent_profile_id=None,
        generation_type="iterative").order_by(UserProfile.id).all()
    assert len(twins) == 2                      # old row + the new save
    assert twins[1].get_content().endswith("REBUILT CHUNK 1")
    assert User.query.get(u.id).profile_needs_full_regen is False
    submit.assert_not_called()                  # no re-submit loop


# ── poll cycle ────────────────────────────────────────────────────────────

def _chunk_job(user, prev):
    item = {
        "custom_id": f"profile:{user.id}:{prev.id}:chunk",
        "user_id": user.id, "kind": "chunk", "prev_profile_id": prev.id,
        "generation_type": "update", "prev_cumulative": 1000,
        "source_data_cutoff": "2026-06-01T00:00:00", "model_id": "test-model",
    }
    job = ProfileBatchJob(
        provider_key="anthropic", batch_id="b1", status="pending",
        items=[item], submitted_at=datetime.utcnow())
    db.session.add(job)
    user.profile_batch_pending = True
    db.session.commit()
    return job, item


def test_poll_saves_chunk_then_enqueues_integration(app, monkeypatch):
    u = _user()
    prev = _prev_profile(u, datetime(2026, 5, 1))
    job, item = _chunk_job(u, prev)

    monkeypatch.setattr(pb, "batch_check_and_collect", lambda bids, keys: (
        {item["custom_id"]: {"content": "NEW PROFILE",
                             "input_tokens": 2000, "output_tokens": 500}},
        {}, {}))
    # After the chunk, no more raw data → the chain (prev + new) integrates.
    monkeypatch.setattr(pb._exports, "build_user_export_content",
                        MagicMock(return_value=None))
    monkeypatch.setattr(pb._exports, "build_integration_messages", lambda uid, pid: (
        [{"role": "user", "content": [{"type": "text", "text": "INTEG"}]}],
        [prev]))
    monkeypatch.setattr(pb, "batch_submit",
                        MagicMock(return_value={"anthropic": "b2"}))

    pb._poll_profile_batches()

    # chunk profile saved, cutoff advanced, batch-cost tagged
    newp = UserProfile.query.filter_by(
        user_id=u.id, parent_profile_id=prev.id,
        generation_type="update").first()
    assert newp is not None
    assert newp.source_data_cutoff == datetime(2026, 6, 1)
    assert newp.source_tokens_used == 1000 + 2000   # prev_cumulative + input
    log = (APICostLog.query.filter_by(user_id=u.id)
           .order_by(APICostLog.id.desc()).first())
    assert log.request_type == "profile_batch"

    # first job collected; an integration batch was submitted; user still busy
    assert ProfileBatchJob.query.get(job.id).status == "collected"
    integ = ProfileBatchJob.query.filter_by(batch_id="b2").first()
    assert integ is not None and integ.items[0]["kind"] == "integration"
    assert CUSTOM_ID_RE.match(integ.items[0]["custom_id"])   # no colons
    assert User.query.get(u.id).profile_batch_pending is True


def _integration_job(user, tip):
    item = {
        "custom_id": f"profile_{user.id}_{tip.id}_integration",
        "user_id": user.id, "kind": "integration", "prev_profile_id": tip.id,
        "prev_source_tokens": 5000, "source_data_cutoff": "2026-06-01T00:00:00",
        "model_id": "test-model",
    }
    job = ProfileBatchJob(
        provider_key="anthropic", batch_id="b9", status="pending",
        items=[item], submitted_at=datetime.utcnow())
    db.session.add(job)
    user.profile_batch_pending = True
    db.session.commit()
    return job, item


def _poll_integration(monkeypatch, item):
    monkeypatch.setattr(pb, "batch_check_and_collect", lambda bids, keys: (
        {item["custom_id"]: {"content": "INTEGRATED", "input_tokens": 100, "output_tokens": 50}},
        {}, {}))
    monkeypatch.setattr(pb, "batch_submit", MagicMock(return_value={}))
    import backend.utils.notifications as notif
    monkeypatch.setattr(notif, "notify_profile_ready", lambda uid: None)
    pb._poll_profile_batches()


def test_prefill_complete_emails_admin_once_for_initial_chain(app, monkeypatch):
    """Pre-filled account: chunk (root) → integration lands → one admin
    email with the whole chain's numbers. A later run on the same account
    (older versions exist below the new chain's root) stays silent, as
    does an organic account."""
    import backend.utils.email as em
    sent = []
    monkeypatch.setattr(em, "send_admin_prefill_complete_notification",
                        lambda *a: sent.append(a))
    u = _user(profile_force_batch=True, prefilled_handle="alice_x")
    u.approved = False
    root = _prev_profile(u, datetime(2026, 5, 1), source_tokens=70000, gen_type="iterative")
    job, item = _integration_job(u, root)
    _poll_integration(monkeypatch, item)
    assert len(sent) == 1
    username, handle, versions, source_tokens, approved = sent[0]
    assert (username, handle, versions, approved) == (u.username, "alice_x", 2, False)
    assert User.query.get(u.id).profile_batch_pending is False
    # Second build later: new root chunk above the old chain → not the first → silent.
    root2 = _prev_profile(u, datetime(2026, 7, 1), source_tokens=90000, gen_type="iterative")
    job2, item2 = _integration_job(u, root2)
    job2.batch_id = "b10"
    db.session.commit()
    _poll_integration(monkeypatch, item2)
    assert len(sent) == 1
    # Organic account (no prefilled_handle): never.
    o = _user(profile_force_batch=True)
    oroot = _prev_profile(o, datetime(2026, 5, 1), source_tokens=70000, gen_type="iterative")
    ojob, oitem = _integration_job(o, oroot)
    ojob.batch_id = "b11"
    db.session.commit()
    _poll_integration(monkeypatch, oitem)
    assert len(sent) == 1


def test_prefill_complete_email_failure_does_not_break_poll(app, monkeypatch):
    import backend.utils.email as em
    monkeypatch.setattr(em, "send_admin_prefill_complete_notification",
                        MagicMock(side_effect=RuntimeError("smtp down")))
    u = _user(profile_force_batch=True, prefilled_handle="bob_x")
    root = _prev_profile(u, datetime(2026, 5, 1), source_tokens=70000, gen_type="iterative")
    job, item = _integration_job(u, root)
    _poll_integration(monkeypatch, item)
    assert ProfileBatchJob.query.get(job.id).status == "collected"
    assert User.query.get(u.id).profile_batch_pending is False


def test_poll_routes_intentions_items_without_touching_profile_flags(app, monkeypatch):
    """A kind="intentions" job rides the same poller but must not touch
    profile_batch_pending / attempts, and its result goes to the
    intentions apply fn."""
    import backend.tasks.intentions as intentions_mod
    u = _user()
    item = {"custom_id": f"int-u{u.id}", "user_id": u.id, "kind": "intentions",
            "budget": 1_000_000, "resubmitted": False}
    job = ProfileBatchJob(provider_key="anthropic", batch_id="bi-1",
                          status="pending", items=[item],
                          submitted_at=datetime.utcnow())
    db.session.add(job)
    db.session.commit()
    applied = []
    monkeypatch.setattr(intentions_mod, "apply_intentions_item",
                        lambda user, itm, res: applied.append((user.id, res["content"])))
    monkeypatch.setattr(pb, "batch_check_and_collect", lambda bids, keys: (
        {item["custom_id"]: {"content": "# Endorsed", "input_tokens": 1, "output_tokens": 1}},
        {}, {}))
    monkeypatch.setattr(pb, "batch_submit", MagicMock(return_value={}))
    pb._poll_profile_batches()
    assert applied == [(u.id, "# Endorsed")]
    fresh = User.query.get(u.id)
    assert fresh.profile_batch_pending is False and (fresh.profile_batch_attempts or 0) == 0
    assert ProfileBatchJob.query.get(job.id).status == "collected"
    # Failed intentions item routes to the failure handler, not attempts.
    job2 = ProfileBatchJob(provider_key="anthropic", batch_id="bi-2",
                           status="pending", items=[dict(item)],
                           submitted_at=datetime.utcnow())
    db.session.add(job2)
    db.session.commit()
    failed = []
    monkeypatch.setattr(intentions_mod, "handle_failed_intentions_item",
                        lambda user, itm, job, keys: failed.append(user.id))
    monkeypatch.setattr(pb, "batch_check_and_collect", lambda bids, keys: ({}, {}, {}))
    pb._poll_profile_batches()
    assert failed == [u.id]
    assert (User.query.get(u.id).profile_batch_attempts or 0) == 0


def test_poll_failed_item_bumps_attempts_and_clears_pending(app, monkeypatch):
    u = _user()
    prev = _prev_profile(u, datetime(2026, 5, 1))
    job, item = _chunk_job(u, prev)

    # batch ended but this item is absent from results (failed/errored)
    monkeypatch.setattr(pb, "batch_check_and_collect",
                        lambda bids, keys: ({}, {}, {}))
    monkeypatch.setattr(pb, "batch_submit", MagicMock(return_value={}))

    pb._poll_profile_batches()

    u2 = User.query.get(u.id)
    assert u2.profile_batch_attempts == 1
    assert u2.profile_batch_pending is False
    assert ProfileBatchJob.query.get(job.id).status == "collected"
    # nothing saved
    assert UserProfile.query.filter_by(
        parent_profile_id=prev.id).first() is None


def test_poll_leaves_pending_job_untouched(app, monkeypatch):
    u = _user()
    prev = _prev_profile(u, datetime(2026, 5, 1))
    job, item = _chunk_job(u, prev)

    # batch still processing
    monkeypatch.setattr(pb, "batch_check_and_collect",
                        lambda bids, keys: ({}, {"anthropic": "b1"}, {}))
    submit = MagicMock(return_value={})
    monkeypatch.setattr(pb, "batch_submit", submit)

    pb._poll_profile_batches()

    assert ProfileBatchJob.query.get(job.id).status == "pending"
    assert User.query.get(u.id).profile_batch_pending is True
    submit.assert_not_called()


def test_seed_paused_is_noop(app, monkeypatch):
    app.config["PROFILE_UPDATES_PAUSED"] = True
    submit = MagicMock()
    monkeypatch.setattr(pb, "batch_submit", submit)
    pb._seed_profile_batches()
    submit.assert_not_called()
    assert ProfileBatchJob.query.count() == 0


def test_poll_is_not_paused(app, monkeypatch):
    # The pause kill-switch must NOT stop the poller — an in-flight batch
    # still gets collected so it can finish on its own.
    app.config["PROFILE_UPDATES_PAUSED"] = True
    u = _user()
    prev = _prev_profile(u, datetime(2026, 5, 1))
    job, item = _chunk_job(u, prev)
    monkeypatch.setattr(pb, "batch_check_and_collect", lambda bids, keys: (
        {item["custom_id"]: {"content": "P", "input_tokens": 100,
                             "output_tokens": 50}}, {}, {}))
    monkeypatch.setattr(pb._exports, "build_user_export_content",
                        MagicMock(return_value=None))
    monkeypatch.setattr(pb._exports, "build_integration_messages",
                        lambda uid, pid: (None, None))
    monkeypatch.setattr(pb, "batch_submit", MagicMock(return_value={}))

    pb._poll_profile_batches()

    assert ProfileBatchJob.query.get(job.id).status == "collected"
    assert UserProfile.query.filter_by(
        user_id=u.id, parent_profile_id=prev.id).first() is not None


def test_poll_fails_stale_job(app, monkeypatch):
    u = _user()
    prev = _prev_profile(u, datetime(2026, 5, 1))
    job, item = _chunk_job(u, prev)
    job.submitted_at = datetime.utcnow() - timedelta(hours=30)  # past SLA
    db.session.commit()
    monkeypatch.setattr(pb, "batch_submit", MagicMock(return_value={}))

    pb._poll_profile_batches()

    assert ProfileBatchJob.query.get(job.id).status == "failed"
    u2 = User.query.get(u.id)
    assert u2.profile_batch_pending is False
    assert u2.profile_batch_attempts == 1


# ── tokenizer-aware budgets (Community Archive pre-fill cost fixes) ───────

def test_use_batch_for_user_force_flag(app):
    u = _user(profile_force_batch=True)
    db.session.commit()
    assert pb.use_batch_for_user(
        u, {"PROFILE_USE_BATCH": False, "PROFILE_BATCH_USER_IDS": set()})


def test_chunk_budget_scales_with_multiplier_and_calibration(app):
    ex = pb._exports
    u = _user()
    app.config["SUPPORTED_MODELS"] = {
        **MODELS, "dense-model": {**MODELS["test-model"], "token_multiplier": 2.0}}
    # Older tokenizer: chars/4 stands as-is.
    assert ex.chunk_budget_for(u, "test-model") == (
        ex.CHUNK_BUDGET, ex.MIN_CHUNK_TOKENS)
    # New-generation tokenizer: chars/2 → half the stored-token budget,
    # and the min-chunk threshold shrinks with it.
    assert ex.chunk_budget_for(u, "dense-model") == (
        ex.CHUNK_BUDGET // 2, ex.MIN_CHUNK_TOKENS // 2)
    # In-loop calibration: the last chunk came in 1.6x over the multiplied
    # estimate → next budget shrinks by that residual.
    ratio = ex.record_token_ratio(u, "dense-model", 10000, 32000)
    assert ratio == 1.6
    assert ex.chunk_budget_for(u, "dense-model")[0] == int(ex.CHUNK_BUDGET / 3.2)
    assert ex.effective_chars_per_token(u, "dense-model") == 1.25
    # Clamped to [1, 5] chars/token so one odd chunk can't collapse or
    # explode the budget.
    ex.record_token_ratio(u, "dense-model", 10000, 200000)
    assert ex.effective_chars_per_token(u, "dense-model") == ex.CHARS_PER_TOKEN_MIN
    assert ex.chunk_budget_for(u, "dense-model")[0] == ex.CHUNK_BUDGET // 4
    ex.record_token_ratio(u, "test-model", 10000, 1000)
    assert ex.effective_chars_per_token(u, "test-model") == ex.CHARS_PER_TOKEN_MAX
    # No signal → no change.
    assert ex.record_token_ratio(u, "dense-model", 0, 5) is None


def test_build_next_request_uses_scaled_budget_and_records_estimate(
        app, monkeypatch):
    u = _user()
    app.config["SUPPORTED_MODELS"] = {
        "test-model": {**MODELS["test-model"], "token_multiplier": 2.0}}
    db.session.commit()
    export = MagicMock(return_value={
        "content": "NEW DATA", "token_count": 45000,
        "latest_node_created_at": datetime(2026, 6, 1)})
    monkeypatch.setattr(pb._exports, "build_user_export_content", export)
    monkeypatch.setattr(pb._exports, "_load_prompt",
                        lambda *a, **k: "GEN {user_export}")

    req = pb._build_next_profile_request(u)

    assert export.call_args.kwargs["max_tokens"] == pb._exports.CHUNK_BUDGET // 2
    assert req["meta"]["prompt_tokens_est"] > 0


def test_apply_result_calibrates_from_actual_tokens(app, monkeypatch):
    u = _user()
    db.session.commit()
    monkeypatch.setattr(pb._exports, "build_user_export_content",
                        MagicMock(return_value=None))
    item = {"custom_id": "x", "user_id": u.id, "kind": "chunk",
            "prev_profile_id": None, "generation_type": "iterative",
            "prev_cumulative": 0, "origin_stats": None,
            "source_data_cutoff": "2026-06-01T00:00:00",
            "model_id": "test-model", "prompt_tokens_est": 1000}
    result = {"content": "PROFILE", "input_tokens": 3200,
              "output_tokens": 10, "total_tokens": 3210}
    pb._apply_result(u, item, result, datetime.utcnow() - timedelta(minutes=1))
    assert u.profile_token_ratio == 3.2


def test_seed_single_user_submits_immediately(app, monkeypatch):
    u = _user(profile_force_batch=True, profile_needs_full_regen=True)
    other = _user(profile_force_batch=True, profile_needs_full_regen=True)
    db.session.commit()
    monkeypatch.setattr(pb._exports, "build_user_export_content", MagicMock(
        return_value={"content": "DATA", "token_count": 90000,
                      "latest_node_created_at": datetime(2026, 6, 1)}))
    monkeypatch.setattr(pb._exports, "_load_prompt", lambda *a, **k: "G {user_export}")
    submitted = []
    monkeypatch.setattr(pb, "batch_submit", lambda reqs, keys, kind: (
        submitted.append(reqs) or {k: f"b-{k}" for k in reqs}))

    assert pb._seed_profile_batches(users=[u]) == 1
    assert len(submitted) == 1
    ids = [r["custom_id"] for r in list(submitted[0].values())[0]]
    assert ids == [f"profile_{u.id}_0_chunk"]  # only the targeted user
    assert User.query.get(u.id).profile_batch_pending is True
    assert User.query.get(other.id).profile_batch_pending is False


def test_seed_reports_submitted_not_built(app, monkeypatch):
    u = _user(profile_force_batch=True, profile_needs_full_regen=True)
    db.session.commit()
    monkeypatch.setattr(pb._exports, "build_user_export_content", MagicMock(
        return_value={"content": "DATA", "token_count": 90000,
                      "latest_node_created_at": datetime(2026, 6, 1)}))
    monkeypatch.setattr(pb._exports, "_load_prompt", lambda *a, **k: "G {user_export}")
    monkeypatch.setattr(pb, "batch_submit", lambda reqs, keys, kind: {})  # provider rejected
    assert pb._seed_profile_batches(users=[u]) == 0
    assert User.query.get(u.id).profile_batch_pending is False
    assert User.query.get(u.id).profile_batch_attempts == 1


def test_force_batch_user_never_exhausts_to_sync(app, monkeypatch):
    """Pinned accounts keep being seeded past MAX_BATCH_ATTEMPTS; the
    sync last-resort in exports skips them too."""
    u = _user(profile_force_batch=True, profile_needs_full_regen=True,
              profile_batch_attempts=pb.MAX_BATCH_ATTEMPTS + 2)
    plain = _user(profile_needs_full_regen=True,
                  profile_batch_attempts=pb.MAX_BATCH_ATTEMPTS + 2)
    app.config["PROFILE_USE_BATCH"] = True
    db.session.commit()
    monkeypatch.setattr(pb._exports, "build_user_export_content", MagicMock(
        return_value={"content": "DATA", "token_count": 90000,
                      "latest_node_created_at": datetime(2026, 6, 1)}))
    monkeypatch.setattr(pb._exports, "_load_prompt", lambda *a, **k: "G {user_export}")
    submitted = []
    monkeypatch.setattr(pb, "batch_submit", lambda reqs, keys, kind: (
        submitted.append(reqs) or {k: f"b-{k}" for k in reqs}))
    assert pb._seed_profile_batches(users=[u, plain]) == 1  # only the pinned one
    ids = [r["custom_id"] for r in list(submitted[0].values())[0]]
    assert ids == [f"profile_{u.id}_0_chunk"]


def test_submit_drops_duplicate_users_and_custom_ids(app, monkeypatch):
    u = _user(profile_force_batch=True)
    db.session.commit()
    submitted = []
    monkeypatch.setattr(pb, "batch_submit", lambda reqs, keys, kind: (
        submitted.append(reqs) or {k: f"b-{k}" for k in reqs}))
    def req(cid):
        return {"provider": "anthropic",
                "request": {"custom_id": cid, "model_id": "test-model",
                            "api_model": "claude-x", "messages": [], "max_tokens": 10},
                "meta": {"custom_id": cid, "user_id": u.id, "kind": "chunk",
                         "prev_profile_id": None, "model_id": "test-model"}}
    n = pb._submit_requests([req("profile_1_290_chunk"), req("profile_1_290_chunk"),
                             req("profile_1_293_chunk")], {"anthropic": "k"})
    assert n == 1
    items = list(submitted[0].values())[0]
    assert [r["custom_id"] for r in items] == ["profile_1_290_chunk"]


def test_apply_duplicate_item_does_not_advance_chain(app, monkeypatch):
    """The 2026-08-27 cascade: a doubled cohort item, once its twin has
    been saved, must NOT return the next step again."""
    u = _user()
    db.session.commit()
    monkeypatch.setattr(pb._exports, "build_user_export_content", MagicMock(
        return_value={"content": "MORE", "token_count": 90000,
                      "latest_node_created_at": datetime(2026, 7, 1)}))
    monkeypatch.setattr(pb._exports, "build_update_template", lambda uid: (
        "T {existing_profile}|{new_data}|{source_tokens_past}|{source_tokens_new}|{ratio_percent}"))
    item = {"custom_id": "x", "user_id": u.id, "kind": "chunk",
            "prev_profile_id": None, "generation_type": "iterative",
            "prev_cumulative": 0, "origin_stats": None,
            "source_data_cutoff": "2026-06-01T00:00:00", "model_id": "test-model",
            "prompt_tokens_est": 100}
    result = {"content": "P1", "input_tokens": 100, "output_tokens": 5, "total_tokens": 105}
    submitted_at = datetime.utcnow() - timedelta(minutes=1)
    first = pb._apply_result(u, item, result, submitted_at)
    assert first is not None and first["meta"]["kind"] == "chunk"
    assert UserProfile.query.filter_by(user_id=u.id).count() == 1
    twin = pb._apply_result(u, item, result, submitted_at)
    assert twin is None
    assert UserProfile.query.filter_by(user_id=u.id).count() == 1


def test_batch_lock_skips_when_held(app, monkeypatch):
    class FakeRedis:
        held = {}
        @classmethod
        def from_url(cls, *a, **k): return cls()
        def set(self, key, val, nx=False, ex=None):
            if nx and key in self.held: return False
            self.held[key] = val; return True
        def delete(self, key): self.held.pop(key, None)
    import types, sys
    monkeypatch.setitem(sys.modules, "redis", types.SimpleNamespace(Redis=FakeRedis))
    with pb.batch_pipeline_lock() as a:
        assert a is True
        with pb.batch_pipeline_lock() as b:
            assert b is False
    with pb.batch_pipeline_lock() as c:
        assert c is True


def test_immediate_seed_defers_when_lock_held(app, monkeypatch):
    """A pre-fill's immediate seed must not give up on lock contention:
    the hourly seeder skips unapproved accounts, so a dropped immediate
    seed left Inactive pre-filled users without any profile (2026-08-28).
    The impl returns None while the lock is held (the task retries) and
    seeds with the user passed explicitly — bypassing the approved-only
    cohort query — once it is free."""
    u = _user(profile_force_batch=True, profile_needs_full_regen=True)
    u.approved = False
    db.session.commit()

    class Held:
        def __enter__(self): return False

        def __exit__(self, *a): return False

    class Free(Held):
        def __enter__(self): return True

    monkeypatch.setattr(pb, "batch_pipeline_lock", lambda: Held())
    seeded = []
    monkeypatch.setattr(pb, "_seed_profile_batches", lambda users=None: seeded.append([x.id for x in users]) or 1)
    assert pb._seed_profile_batch_for_user_impl(u.id) is None
    assert seeded == []
    monkeypatch.setattr(pb, "batch_pipeline_lock", lambda: Free())
    assert pb._seed_profile_batch_for_user_impl(u.id) == 1
    assert seeded == [[u.id]]
    assert pb._seed_profile_batch_for_user_impl(10 ** 9) == 0  # unknown user: no retry loop


def test_pinned_account_resumes_when_a_full_chunk_remains(app, monkeypatch):
    """A pre-filled account whose chunk 2 was lost (worker restart): root
    chunk saved, >= a minimum chunk of data beyond its cutoff, nothing in
    flight, inside MIN_INTERVAL → seeded again. A tail smaller than a
    minimum chunk waits for more data (chunks are never made uneven)."""
    u = _user(profile_force_batch=True)
    prev = _prev_profile(u, datetime(2026, 5, 1), source_tokens=70000, gen_type="iterative")
    prev.created_at = datetime.utcnow()  # inside MIN_INTERVAL
    db.session.commit()
    monkeypatch.setattr(pb, "_remaining_token_count", lambda user, cutoff: 85000)
    assert pb._should_seed(u) is True
    monkeypatch.setattr(pb, "_remaining_token_count", lambda user, cutoff: 30000)  # small tail
    assert pb._should_seed(u) is False
    # An organic (unpinned) user with a full chunk remaining is still gated
    # by MIN_INTERVAL.
    v = _user()
    pv = _prev_profile(v, datetime(2026, 5, 1), source_tokens=70000, gen_type="iterative")
    pv.created_at = datetime.utcnow()
    db.session.commit()
    monkeypatch.setattr(pb, "_new_token_count", lambda user, cutoff: 85000)
    assert pb._should_seed(v) is False


def test_small_tail_is_deferred_even_for_pinned_accounts(app, monkeypatch):
    u = _user(profile_force_batch=True)
    _prev_profile(u, datetime(2026, 5, 1), source_tokens=70000, gen_type="iterative")
    db.session.commit()
    monkeypatch.setattr(pb._exports, "_has_more_source_after", lambda user, ts: False)
    monkeypatch.setattr(pb._exports, "build_user_export_content", MagicMock(
        return_value={"content": "TAIL", "token_count": 6000,
                      "latest_node_created_at": datetime(2026, 6, 1)}))
    assert pb._build_next_profile_request(u) is None  # 1 version → no integration either


def test_remaining_token_count_uses_created_at_not_updated_at(app):
    """Imported tweets have updated_at = import time but created_at = tweet
    date; the remaining-data measure must follow the chunk cutoff (created_at)."""
    u = _user()
    db.session.flush()
    for day, tokens in ((1, 100), (10, 200), (20, 400)):
        n = Node(user_id=u.id, human_owner_id=u.id, privacy_level="private",
                 ai_usage="chat", token_count=tokens, created_at=datetime(2026, 1, day))
        n.set_content("x")
        db.session.add(n)
    db.session.commit()
    assert pb._remaining_token_count(u, datetime(2026, 1, 5)) == 600
    assert pb._remaining_token_count(u, datetime(2026, 1, 15)) == 400
    assert pb._remaining_token_count(u, datetime(2026, 1, 25)) == 0

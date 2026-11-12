"""Independent profile versions for large builds (#545).

The pipeline runs for real against SQLite (rows, job items, outcomes);
the network boundary (batch_submit / batch_check_and_collect / token
counting) and the export renderer are replaced by a synthetic corpus. No
model is called and every text is synthetic.
"""
import json
import os
import re
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

for _mod in ["backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

from backend.extensions import db  # noqa: E402
from backend.models import (  # noqa: E402
    User, UserProfile, ProfileBatchJob, APICostLog, Node)

pb = pw = ex = None

CUSTOM_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
MODEL_IDS = ("claude-opus-5.5", "claude-haiku-5.5", "gpt-6-astra",
             "gpt-6-luna", "gpt-6.1-sol")
T0 = datetime(2024, 1, 1)


def _good(events=True, tag="v"):
    """A synthetic profile with the three maps (and an events section)."""
    text = (f"### SURFACE MAP\nSynthetic surface {tag}.\n\n"
            f"### SHADOW MAP\nSynthetic shadow {tag}.\n\n"
            f"### LIGHT MAP\nSynthetic light {tag}.\n")
    if events:
        text += f"\n### EVENTS IN THIS WINDOW\n- 2024-01: synthetic {tag}\n"
    return text


class Corpus:
    """Synthetic writing: rows of ``units`` units, one an hour from T0.
    Stands in for count_remaining_units and the export renderer with the
    same window semantics (created_after exclusive, created_before
    exclusive, strict fit to the budget, ties at the edge included)."""

    def __init__(self, n, units=1000, start=T0, step=timedelta(hours=1)):
        self.rows = [(start + i * step, units) for i in range(n)]
        self.renders = []
        self.hot = set()   # row timestamps that render 10x their units
        self.silent = set()   # rows that are counted but render nothing
        self.origin = "loore"   # the rows' origin in the chunk's stats

    def add(self, n, units=1000, start=None, step=timedelta(hours=1)):
        start = start or (self.rows[-1][0] + step)
        self.rows += [(start + i * step, units) for i in range(n)]

    def _sel(self, after, before):
        return [(t, u) for t, u in self.rows
                if (after is None or t > after)
                and (before is None or t < before)]

    def count(self, uid, created_after=None, created_before=None):
        return sum(u for _t, u in self._sel(created_after, created_before))

    def export(self, user, max_tokens=None, *, filter_ai_usage=True,
               created_after=None, created_before=None, **kw):
        budget = (max_tokens or 10**12) - 100
        picked, total = [], 0
        for t, u in self._sel(created_after, created_before):
            if t in self.silent:
                continue
            if picked and total + u > budget:
                break
            picked.append((t, u))
            total += u
        if not picked:
            return None
        self.renders.append((created_after, max_tokens))
        text = "SYNTHETIC WRITING " + " ".join(
            ("QHOTROWQ" if t in self.hot else "QROWQ") for t, _u in picked)
        return {"content": text, "token_count": total, "unit_count": total,
                "latest_node_created_at": picked[-1][0],
                "earliest_node_created_at": picked[0][0],
                "node_count": len(picked), "node_ids": set(),
                "origin_stats": {self.origin: {"nodes": len(picked),
                                               "tokens": total}}}


class Provider:
    """batch_submit / batch_check_and_collect stand-in. Every submission is
    one batch id per provider key; results are produced per custom_id by
    ``respond`` when the poller collects."""

    def __init__(self):
        self.submits = []
        self.n = 0
        self.respond = lambda cid, req: {"content": _good(
            events="_w" in cid, tag=cid), "input_tokens": 1000,
            "output_tokens": 500, "batch": True}
        self.fail_submit = False

    def submit(self, requests_by_provider, keys, phase=None):
        from backend.utils.llm_batch import SubmittedBatches
        out = SubmittedBatches()
        self.submits.append(requests_by_provider)
        if self.fail_submit:
            return out
        from backend.utils.llm_batch import openai_batch_key
        for provider, reqs in requests_by_provider.items():
            keys = {"anthropic"} if provider == "anthropic" else {
                openai_batch_key(r["api_model"], r.get("endpoint"))
                for r in reqs}
            for key in sorted(keys):
                self.n += 1
                out[key] = f"batch_{self.n}"
        return out

    def requests(self, i=-1):
        if not self.submits:
            return []
        return [r for reqs in self.submits[i].values() for r in reqs]

    def collect(self, batch_ids, keys):
        results = {}
        for reqs in [r for s in self.submits for rs in s.values() for r in rs]:
            res = self.respond(reqs["custom_id"], reqs)
            if res is not None:
                results[reqs["custom_id"]] = res
        return results, {}, {}


@pytest.fixture
def app(monkeypatch):
    global pb, pw, ex
    import backend.tasks.profile_batch as _pb
    import backend.tasks.profile_windows as _pw
    import backend.tasks.exports as _ex
    pb, pw, ex = _pb, _pw, _ex
    from backend.config import Config
    app = Flask(__name__)
    app.config.update(
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:", SECRET_KEY="t",
        TESTING=True, DEFAULT_LLM_MODEL="claude-opus-5.5",
        SUPPORTED_MODELS={k: Config.SUPPORTED_MODELS[k] for k in MODEL_IDS},
        PROFILE_USE_BATCH=True, PROFILE_BATCH_USER_IDS=set(),
        OPENAI_API_KEY_BATCH=None, PROFILE_INDEPENDENT_VERSIONS=True,
        PROFILE_VERSION_MODEL_ANTHROPIC="claude-haiku-5.5",
        PROFILE_VERSION_MODEL_OPENAI="gpt-6-luna",
        PROFILE_MERGE_MODEL_ANTHROPIC="claude-opus-5.5",
        PROFILE_MERGE_MODEL_OPENAI="gpt-6.1-sol",
        PROFILE_MERGE_REASONING_EFFORT_OPENAI="",
        PROFILE_VERSION_MODEL_CROSS_PROVIDER="gpt-6-luna",
        PROFILE_MERGE_MODEL_CROSS_PROVIDER="claude-opus-5.5",
        PROFILE_MERGE_STRUCTURE="grouped", PROFILE_MERGE_GROUP_SIZE=30,
        PROFILE_VERSION_CONTENT_ATTEMPTS=2,
        PROFILE_VERSION_PROVIDER_ATTEMPTS=3,
        PROFILE_BATCH_MAX_BYTES=100_000_000,
        PROFILE_WINDOW_RENDER_SECONDS=2400)
    db.init_app(app)
    keys = lambda *a, **k: {"anthropic": "k", "openai": "k"}  # noqa: E731
    monkeypatch.setattr(pb, "get_api_keys_for_usage", keys)
    monkeypatch.setattr(pw, "get_api_keys_for_usage", keys)
    import backend.llm_providers as lp
    monkeypatch.setattr(lp.LLMProvider, "count_tokens",
                        staticmethod(lambda model_id, messages, k: None))
    monkeypatch.setattr(pw, "advance_profile_windows", MagicMock())
    monkeypatch.setattr(pw._exports, "_is_task_stale", lambda u: True)
    monkeypatch.setattr(pw._exports, "_first_writing_date",
                        lambda uid, until=None: T0)
    import backend.utils.notifications as notes
    monkeypatch.setattr(notes, "notify_profile_ready", MagicMock())
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def world(app, monkeypatch):
    """A corpus, a provider and the patches that route the pipeline to
    them. ``world.corpus`` can be replaced per test."""
    class W:
        pass
    w = W()
    w.corpus = Corpus(0)
    w.provider = Provider()
    monkeypatch.setattr(pw._exports, "count_remaining_units",
                        lambda uid, created_after=None, created_before=None:
                        w.corpus.count(uid, created_after, created_before))
    monkeypatch.setattr(pw._exports, "build_user_export_content",
                        lambda *a, **k: w.corpus.export(*a, **k))
    # No read of the (empty) node table: the pass counts and renders
    # through the two stand-ins above (test_window_scope runs the read).
    monkeypatch.setattr(pw._exports, "window_scope", lambda *a, **k: None)
    monkeypatch.setattr(pb, "batch_submit",
                        lambda *a, **k: w.provider.submit(*a, **k))
    monkeypatch.setattr(pb, "batch_check_and_collect",
                        lambda *a, **k: w.provider.collect(*a, **k))
    # The poller's listing shows nothing: each job is polled on its own.
    monkeypatch.setattr(pb, "list_batches", lambda *a, **k: {})
    # The continue rule on the synthetic corpus: rows after the cutoff
    # that existed when the window was rendered.
    monkeypatch.setattr(pw, "_unread_before", lambda user, cutoff, boundary: (
        cutoff is not None and boundary is not None and any(
            cutoff < t < boundary for t, _u in w.corpus.rows)))
    return w


_N = [0]


def _user(**kw):
    _N[0] += 1
    kw.setdefault("preferred_model", None)
    u = User(username=f"w{_N[0]}", plan="alpha", twitter_id=None,
             approved=True, **kw)
    db.session.add(u)
    db.session.commit()
    return u


def _profile(user, gen_type="integration", cutoff=T0, units=1000,
             generated_by="claude-opus-5.5", text=None, **kw):
    p = UserProfile(user_id=user.id, generated_by=generated_by,
                    tokens_used=0, generation_type=gen_type,
                    source_tokens_used=units, source_data_cutoff=cutoff,
                    ai_usage="chat", **kw)
    p.set_content(text or _good(events=False, tag="old"))
    db.session.add(p)
    db.session.commit()
    return p


def _advance(user):
    """Run the advance task's body as the dispatched task would."""
    task_id = pw.dispatch_advance(user)
    assert task_id is not None
    return pw.advance(user.id, task_id)


def _poll():
    pb._poll_profile_batches()


def _items(kind=None):
    out = []
    for job in ProfileBatchJob.query.order_by(ProfileBatchJob.id).all():
        out += [i for i in job.items if kind is None or i["kind"] == kind]
    return out


def _rows(user, gen_type):
    return (UserProfile.query.filter_by(user_id=user.id,
                                        generation_type=gen_type)
            .order_by(UserProfile.source_data_cutoff).all())


def _haiku_window_units():
    from backend.utils.chunk_plan import max_units_for_cap
    return max_units_for_cap(100_000, ex.TOKENS_PER_UNIT_PRIOR["threads"][
        "claude_new"])


def _windows_corpus(n_windows, units=1000):
    """A corpus that plans into about ``n_windows`` Haiku windows, and at
    least 140k units, so that the chat model's plan has two chunks."""
    rows = max(int(n_windows * _haiku_window_units() / units),
               140_000 // units)
    return Corpus(rows, units=units)


def _build(world, user, rounds=6):
    """Advance and collect until nothing more goes out."""
    for _ in range(rounds):
        _advance(user)
        if not ProfileBatchJob.query.filter_by(status="pending").count():
            break
        _poll()


# ── 1. routing ────────────────────────────────────────────────────────────

def _seed(user):
    return pb._seed_profile_batches(users=[user], ignore_backoff=True)


def test_one_chunk_build_takes_the_chained_path(world, monkeypatch):
    world.corpus = Corpus(50)                    # 50k units: one chunk
    monkeypatch.setattr(pb._exports, "count_remaining_units",
                        lambda uid, cutoff=None: world.corpus.count(uid, cutoff))
    u = _user(profile_needs_full_regen=True)
    _seed(u)
    reqs = world.provider.requests()
    assert len(reqs) == 1
    meta = _items()[0]
    assert meta["kind"] == "chunk" and meta["generation_type"] == "initial"
    text = reqs[0]["messages"][0]["content"][0]["text"]
    assert "EVENTS IN THIS WINDOW" not in text
    pw.advance_profile_windows.apply_async.assert_not_called()


def test_two_chunk_build_dispatches_the_advance(world):
    world.corpus = Corpus(400)                   # 400k units: 4 chunks
    u = _user(profile_needs_full_regen=True)
    _seed(u)
    assert world.provider.submits == []          # no chained chunk
    pw.advance_profile_windows.apply_async.assert_called_once()
    db.session.refresh(u)
    assert u.profile_batch_pending and u.profile_force_batch


def test_chained_update_with_one_chunk_stays_chained(world, monkeypatch):
    world.corpus = Corpus(85, start=T0 + timedelta(days=400))
    u = _user()
    _profile(u, gen_type="update", cutoff=T0 + timedelta(days=1))
    assert not pw.wants_window_build(u)


def test_a_set_tip_base_always_takes_the_window_path(world):
    u = _user()
    _profile(u, gen_type="window", window_set="s1", cutoff=T0)
    assert pw.wants_window_build(u)


def test_kill_switch_off_keeps_every_build_chained(app, world):
    app.config["PROFILE_INDEPENDENT_VERSIONS"] = False
    world.corpus = Corpus(400)
    u = _user(profile_needs_full_regen=True)
    assert not pw.wants_window_build(u)


def test_sync_task_hands_off_to_the_advance(world):
    world.corpus = Corpus(400)
    u = _user(profile_needs_full_regen=True)
    result = ex.window_hand_off(u, "sync-task-1")
    assert result is not None and result["status"] == "completed"
    assert len(world.provider.submits) == 1      # the windows went out
    assert ex.window_hand_off(u, "sync-task-2", integrate_only=True) is None
    db.session.refresh(u)
    assert u.profile_generation_task_id is None   # guard released


# ── 2. the events prompt ──────────────────────────────────────────────────

def test_events_paragraph_sits_where_the_input_line_was(app):
    u = _user()
    gen = ex._load_prompt("profile_generation.txt", user_id=u.id)
    win = ex.window_generation_template(u.id)
    para = ex.EVENTS_IN_WINDOW_PARAGRAPH
    assert win.count(para) == 1
    assert win.replace(para + "\n\n", "", 1) == gen
    i = win.index(para)
    assert win[i:].startswith(para + "\n\n## INPUT\n<user_export>")
    assert win[:i].endswith('Begin directly with "### SURFACE MAP"\n\n')


def test_own_prompt_without_input_line_gets_it_before_the_export(
        app, monkeypatch):
    own = "Write my profile.\nHere it is:\n{user_export}\nThanks."
    monkeypatch.setattr(ex, "_load_prompt", lambda name, user_id=None: own)
    win = ex.window_generation_template(1)
    assert win == ("Write my profile.\nHere it is:\n"
                   + ex.EVENTS_IN_WINDOW_PARAGRAPH
                   + "\n\n{user_export}\nThanks.")


def test_update_and_merge_prompts_never_carry_the_paragraph(app):
    u = _user()
    assert "EVENTS" not in ex.build_update_template(u.id)
    w = _profile(u, gen_type="window", window_set="s", covers_start=None)
    msgs = ex._integration_messages(u.id, [w, w], independent=True)
    assert "EVENTS IN THIS WINDOW" not in msgs[-1]["content"][0]["text"]


# ── 3. models ─────────────────────────────────────────────────────────────

def test_models_follow_the_users_provider(app):
    anth = _user()
    m = pw.profile_models_for(anth)
    assert (m.provider, m.version, m.merge) == (
        "anthropic", "claude-haiku-5.5", "claude-opus-5.5")
    oai = _user(preferred_model="gpt-6-astra")
    m = pw.profile_models_for(oai)
    assert (m.provider, m.version, m.merge) == (
        "openai", "gpt-6-luna", "gpt-6.1-sol")


def test_a_cross_provider_setting_raises(app):
    app.config["PROFILE_MERGE_MODEL_OPENAI"] = "claude-opus-5.5"
    with pytest.raises(pw.ProfileModelError):
        pw.profile_models_for(_user(preferred_model="gpt-6-astra"))


def test_boot_check_rejects_cross_provider_deprecated_and_unknown():
    from backend import validate_profile_models
    from backend.config import Config
    base = {"SUPPORTED_MODELS": Config.SUPPORTED_MODELS}
    validate_profile_models({
        **base, "PROFILE_VERSION_MODEL_ANTHROPIC": "claude-haiku-5.5",
        "PROFILE_VERSION_MODEL_OPENAI": "gpt-6-luna",
        "PROFILE_MERGE_MODEL_ANTHROPIC": "claude-opus-5.5",
        "PROFILE_MERGE_MODEL_OPENAI": "gpt-6.1-sol",
        "PROFILE_MERGE_REASONING_EFFORT_OPENAI": "high"})
    # The cross-provider pair may be of either provider.
    validate_profile_models({
        **base, "PROFILE_VERSION_MODEL_CROSS_PROVIDER": "gpt-6-luna",
        "PROFILE_MERGE_MODEL_CROSS_PROVIDER": "claude-opus-5.5"})
    for key, bad in (("PROFILE_MERGE_MODEL_OPENAI", "claude-opus-5.5"),
                     ("PROFILE_VERSION_MODEL_CROSS_PROVIDER", "gpt-7-nope"),
                     ("PROFILE_MERGE_MODEL_CROSS_PROVIDER",
                      "claude-sonnet-4.5"),
                     ("PROFILE_VERSION_MODEL_ANTHROPIC", "claude-sonnet-4.5"),
                     ("PROFILE_VERSION_MODEL_OPENAI", "gpt-7-nope"),
                     ("PROFILE_MERGE_REASONING_EFFORT_OPENAI", "max")):
        with pytest.raises(RuntimeError):
            validate_profile_models({**base, key: bad})


def test_the_shipped_profile_model_defaults_pass():
    from backend import validate_profile_models
    from backend.config import Config
    validate_profile_models({k: getattr(Config, k) for k in dir(Config)
                             if k.startswith("PROFILE_")
                             or k == "SUPPORTED_MODELS"})
    assert Config.PROFILE_VERSION_MODEL_ANTHROPIC == "claude-haiku-5.5"
    assert Config.PROFILE_MERGE_MODEL_OPENAI == "gpt-6.1-sol"
    assert Config.PROFILE_MERGE_REASONING_EFFORT_OPENAI == "high"
    assert Config.PROFILE_MERGE_STRUCTURE == "flat"
    assert Config.PROFILE_MERGE_GROUP_SIZE == 30
    assert Config.PROFILE_VERSION_MODEL_CROSS_PROVIDER == "gpt-6-luna"
    assert Config.PROFILE_MERGE_MODEL_CROSS_PROVIDER == "claude-opus-5.5"


def test_openai_merge_carries_the_configured_effort(app, world):
    app.config["PROFILE_MERGE_REASONING_EFFORT_OPENAI"] = "high"
    world.corpus = Corpus(800)                  # Luna: about 4 windows
    u = _user(preferred_model="gpt-6-astra", profile_needs_full_regen=True)
    _advance(u)
    assert all(r["api_model"] == "gpt-6-luna"
               and "reasoning_effort" not in r
               for r in world.provider.requests())
    _poll()
    _advance(u)
    merge = [r for r in world.provider.requests() if "_f_" in r["custom_id"]]
    assert merge and merge[0]["api_model"] == "gpt-6.1-sol"
    assert merge[0]["reasoning_effort"] == "high"
    assert merge[0]["endpoint"] == "/v1/responses"
    assert {j.provider_key for j in ProfileBatchJob.query.all()} == {
        "openai:gpt-6-luna", "openai:gpt-6.1-sol:responses"}
    _poll()
    final = _rows(u, "integration")
    assert len(final) == 1 and final[0].generated_by == "gpt-6.1-sol"


def test_an_openai_merge_without_an_effort_sends_none(world):
    world.corpus = Corpus(800)
    u = _user(preferred_model="gpt-6-astra", profile_needs_full_regen=True)
    _advance(u)
    _poll()
    _advance(u)
    merge = [r for r in world.provider.requests() if "_f_" in r["custom_id"]]
    assert merge and "reasoning_effort" not in merge[0]


# ── 4. planning ───────────────────────────────────────────────────────────

def test_windows_are_planned_at_haikus_cap_and_contiguous(world):
    world.corpus = _windows_corpus(8)
    u = _user(profile_needs_full_regen=True)
    _advance(u)
    assert len(world.provider.submits) == 1     # one submission
    reqs = world.provider.requests()
    items = _items("window")
    assert len(items) == len(reqs) >= 8
    assert all(r["api_model"] == "claude-haiku-5-5" for r in reqs)
    assert all(i["units"] <= _haiku_window_units() for i in items)
    assert len({r["custom_id"] for r in reqs}) == len(reqs)
    assert all(CUSTOM_ID_RE.match(r["custom_id"]) for r in reqs)
    assert items[0]["start"] is None
    for prev, nxt in zip(items, items[1:]):
        assert nxt["start"] == prev["cutoff"]
        assert nxt["cumulative"] == prev["cumulative"] + nxt["units"]
    assert items[-1]["cumulative"] == world.corpus.count(u.id)
    text = reqs[0]["messages"][0]["content"][0]["text"]
    assert text.count(ex.EVENTS_IN_WINDOW_PARAGRAPH) == 1


def test_a_window_over_the_cap_is_rendered_smaller_never_cut(
        world, monkeypatch):
    """Fitted by its counted tokens: a hot window (long node headers in
    a deep conversation) is rendered again smaller, ends at the last entry
    it holds, and the next window starts there."""
    import backend.llm_providers as lp
    world.corpus = _windows_corpus(4)
    hot = world.corpus.rows[5][0]
    world.corpus.hot = {hot}

    def count(model_id, messages, keys):
        text = messages[0]["content"][0]["text"]
        tokens = text.count("QROWQ") * 2000 + text.count("QHOTROWQ") * 62_000
        return tokens + 3000
    monkeypatch.setattr(lp.LLMProvider, "count_tokens", staticmethod(count))
    u = _user(profile_needs_full_regen=True)
    _advance(u)
    items = _items("window")
    for req in world.provider.requests():
        assert count(None, req["messages"], None) <= 100_000
    for prev, nxt in zip(items, items[1:]):
        assert nxt["start"] == prev["cutoff"]
    assert items[-1]["cutoff"] == world.corpus.rows[-1][0].isoformat()


def test_an_entry_larger_than_the_cap_is_skipped_loudly(world, monkeypatch):
    import backend.llm_providers as lp
    world.corpus = _windows_corpus(3)
    world.corpus.hot = {world.corpus.rows[0][0]}
    monkeypatch.setattr(lp.LLMProvider, "count_tokens", staticmethod(
        lambda m, msgs, k: 500_000 if "QHOTROWQ" in msgs[0]["content"][0][
            "text"] else 50_000))
    sent = []
    monkeypatch.setattr(pw, "_report", lambda title, **tags: sent.append(
        (title, tags)))
    u = _user(profile_needs_full_regen=True)
    _advance(u)
    skips = _rows(u, "window_skip")
    assert len(skips) == 1 and skips[0].covers_start is None
    assert any("over the version model's cap" in t for t, _ in sent)
    assert all("QHOTROWQ" not in r["messages"][0]["content"][0]["text"]
               for r in world.provider.requests())


def test_byte_slicing_sends_several_batches_in_one_run(app, world):
    world.corpus = _windows_corpus(6)
    app.config["PROFILE_BATCH_MAX_BYTES"] = 20_000
    u = _user(profile_needs_full_regen=True)
    _advance(u)
    assert len(world.provider.submits) >= 3
    assert ProfileBatchJob.query.count() == len(world.provider.submits)


def test_the_render_budget_submits_part_and_dispatches_the_rest(app, world):
    world.corpus = _windows_corpus(6)
    app.config["PROFILE_WINDOW_RENDER_SECONDS"] = 0
    u = _user(profile_needs_full_regen=True)
    _advance(u)
    first = len(_items("window"))
    assert first == 1
    assert pw.advance_profile_windows.apply_async.call_count == 2
    # The next task continues the same set from where the first stopped.
    _advance(u)
    items = _items("window")
    assert len(items) == 2 and items[1]["start"] == items[0]["cutoff"]
    assert {i["set"] for i in items} == {items[0]["set"]}


def test_a_range_that_renders_nothing_counts_as_covered(world, monkeypatch):
    """Units counted after the tip that render nothing (a thread holding
    only a system prompt): the set completes and its final goes out,
    without a Sentry alert."""
    u = _user()
    rows = _set_of(u, 5)
    _corpus_for(world, rows)
    quiet = rows[-1].source_data_cutoff + timedelta(hours=1)
    world.corpus.rows.append((quiet, 1000))
    world.corpus.silent = {quiet}
    sent = []
    monkeypatch.setattr(pw, "_report", lambda title, **tags: sent.append(
        title))
    assert pw.set_state(u, "set1", None).due
    _advance(u)
    assert not _items("window")
    assert len(_items("final")) == 1
    assert sent == []


# ── 5. _submit_requests ───────────────────────────────────────────────────

def test_submit_keeps_a_users_window_items_and_drops_a_second_chain_step(
        world):
    u = _user()

    def b(cid, set_id=None):
        meta = {"custom_id": cid, "user_id": u.id, "kind": "chunk"}
        if set_id:
            meta.update(kind="window", set=set_id)
        return {"provider": "anthropic", "meta": meta, "request": {
            "custom_id": cid, "model_id": "claude-haiku-5.5",
            "api_model": "claude-haiku-5-5", "messages": [],
            "max_tokens": 10}}
    assert pb._submit_requests([b("a", "s"), b("b", "s"), b("c", "s")],
                               {}) == 3
    assert pb._submit_requests([b("x"), b("y")], {}) == 1


# ── 6. collection ─────────────────────────────────────────────────────────

def test_collected_windows_are_saved_as_the_sets_rows(world):
    world.corpus = _windows_corpus(3)
    u = _user(profile_needs_full_regen=True)
    _advance(u)
    _poll()
    rows = _rows(u, "window")
    items = _items("window")
    assert len(rows) == len(items)
    assert {r.window_set for r in rows} == {items[0]["set"]}
    assert rows[0].covers_start is None
    assert rows[1].covers_start == rows[0].source_data_cutoff
    first = rows[0].get_content().split("\n", 1)[0]
    assert re.fullmatch(r"\[Covers \d{4}-\d\d-\d\d to \d{4}-\d\d-\d\d "
                        r"\(\S+ tokens\)\]", first)
    assert APICostLog.query.filter_by(
        user_id=u.id, request_type="profile_batch").count() >= len(rows)
    db.session.refresh(u)
    assert u.profile_needs_full_regen is False
    # A re-collect of the same job saves nothing twice.
    job = ProfileBatchJob.query.first()
    pw.collect(job, [dict(i) for i in job.items], world.provider.collect(
        None, None)[0])
    assert len(_rows(u, "window")) == len(rows)


def test_windows_are_hidden_while_the_build_runs(world):
    world.corpus = _windows_corpus(40)
    u = _user(profile_needs_full_regen=True)
    old = _profile(u, gen_type="integration", cutoff=T0 - timedelta(days=1))
    from backend.utils.profile_versions import (
        current_profile_version, visible_profiles_query)
    before = current_profile_version(u.id)
    _advance(u)
    _poll()          # the windows are in; the groups go out
    assert _rows(u, "window")
    from backend.tasks import llm_completion, recent_context
    from backend.utils import context_artifacts
    assert llm_completion.get_user_profile_content(u.id).id == old.id
    assert recent_context._get_latest_chat_profile(u.id).id == old.id
    assert context_artifacts._resolve_latest_artifact(
        "profile", u.id) == old.id
    from backend.routes.dashboard import get_latest_profile
    assert get_latest_profile(u)["id"] == old.id
    from backend.routes.export_data import _latest_profile_snapshot
    assert _latest_profile_snapshot(u)["id"] == old.id
    # Poll drafts' derived context (review of #589, finding 2).
    from backend.tasks import poll_draft
    context = poll_draft._derived_context(u)
    assert "Synthetic surface old." in context
    assert "EVENTS IN THIS WINDOW" not in context
    assert current_profile_version(u.id) == before
    assert [p.id for p in visible_profiles_query(u.id)] == [old.id]


def test_a_first_build_shows_no_profile_until_its_final(world):
    world.corpus = _windows_corpus(3)
    u = _user(profile_needs_full_regen=True)
    _advance(u)
    _poll()
    from backend.routes.dashboard import get_latest_profile
    assert _rows(u, "window") and get_latest_profile(u) is None


# ── 7. validation ─────────────────────────────────────────────────────────

def test_output_problem_rules():
    gen = "SURFACE MAP SHADOW MAP LIGHT MAP"
    assert ex.profile_output_problem(_good(), gen, events=True) is None
    variant = _good().replace("### SHADOW MAP", "### SECTION 2: SHADOW MAP")
    assert ex.profile_output_problem(variant, gen, events=True) is None
    assert ex.profile_output_problem(_good(events=False), gen) is None
    assert ex.profile_output_problem("I can't help with that.", gen)
    twice = _good() + "\n### EVENTS IN THIS WINDOW\n- again\n"
    assert ex.profile_output_problem(twice, gen, events=True)
    assert ex.profile_output_problem(_good(events=False), gen, events=True)
    # A prompt of the user's own without the maps is not checked for them.
    assert ex.profile_output_problem("free text", "my own prompt") is None
    assert "SYNTH" not in (ex.profile_output_problem(
        "SYNTHETIC SECRET", gen) or "")


def test_an_invalid_window_is_retried_unchanged_then_skipped(
        world, monkeypatch):
    world.corpus = _windows_corpus(3)
    bad = {}
    sent = []

    class Scope:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def set_tag(self, k, v):
            sent.append(("tag", k, v))
    import sentry_sdk
    monkeypatch.setattr(sentry_sdk, "new_scope", lambda: Scope())
    monkeypatch.setattr(sentry_sdk, "capture_message",
                        lambda title, level=None: sent.append(("msg", title)))

    def respond(cid, req):
        if "_w2_" in cid:
            bad.setdefault(cid, req["messages"][0]["content"][0]["text"])
            return {"content": "I would rather not read this.",
                    "input_tokens": 900, "output_tokens": 9, "batch": True}
        return {"content": _good(events="_w" in cid, tag=cid),
                "input_tokens": 1000, "output_tokens": 500, "batch": True}
    world.provider.respond = respond
    u = _user(profile_needs_full_regen=True)
    _advance(u)
    _poll()
    assert not [r for r in _rows(u, "window") if r.covers_start
                == datetime.fromisoformat(_items("window")[1]["start"])]
    costs = APICostLog.query.filter_by(user_id=u.id).count()
    assert costs == len(_items("window"))      # the failed call is billed
    assert APICostLog.query.filter_by(
        user_id=u.id, request_ref="refused:truncated").count() == 0
    # The next advance retries that one window, with the same bytes.
    _advance(u)
    retry = [i for i in _items("window") if i["attempt"] == 2]
    assert len(retry) == 1
    req = world.provider.requests()
    assert len(req) == 1
    assert req[0]["messages"][0]["content"][0]["text"] == list(
        bad.values())[0]
    world.provider.respond = lambda cid, r: (
        {"content": "No.", "input_tokens": 9, "output_tokens": 1,
         "batch": True} if cid == req[0]["custom_id"] else None)
    _poll()
    skips = _rows(u, "window_skip")
    assert len(skips) == 1
    assert ex.WINDOW_SKIPPED_TEXT in skips[0].get_content()
    msgs = [x for x in sent if x[0] == "msg"]
    assert msgs and "skipped" in msgs[0][1]
    flat = json.dumps(sent)
    assert "SYNTHETIC" not in flat and "rather not" not in flat \
        and "No." not in flat


def test_an_old_path_merge_without_the_maps_is_refused(app):
    u = _user()
    with pytest.raises(ex.EmptyTruncatedOutputError):
        ex.refuse_truncated_profile(
            u, "claude-opus-5.5", {"content": "sorry", "input_tokens": 1,
                                   "output_tokens": 1},
            "batch final", batch=True,
            problem=ex.profile_output_problem("sorry", "SURFACE MAP"))
    assert APICostLog.query.filter_by(
        user_id=u.id, request_ref="refused:truncated").count() == 1


# ── 8. provider failures ──────────────────────────────────────────────────

def test_a_missing_window_is_retried_and_the_third_failure_waits(
        world, monkeypatch):
    world.corpus = _windows_corpus(2)
    sent = []
    monkeypatch.setattr(pw, "_report", lambda title, **tags: sent.append(
        (title, tags)))
    world.provider.respond = lambda cid, req: (
        None if "_w1_" in cid else {"content": _good(tag=cid),
                                    "input_tokens": 1, "output_tokens": 1,
                                    "batch": True})
    u = _user(profile_needs_full_regen=True)
    for attempt in (1, 2, 3):
        _advance(u)
        firsts = [i for i in _items("window") if i["start"] is None]
        assert len(firsts) == attempt
        _poll()
    db.session.refresh(u)
    assert u.profile_seed_error.startswith(pw.STOP_NOTE)
    assert any("provider" in t for t, _ in sent)
    _advance(u)                                  # waits: nothing more
    assert len([i for i in _items("window") if i["start"] is None]) == 3
    assert not pw.set_work_due(u, ex.profile_update_base(u.id))
    # A day later it gets one more try.
    later = datetime.utcnow() + timedelta(hours=25)
    history = pw.attempt_history(u.id)
    assert pw.provider_stop_until(history, ("window", None), later) is None


def test_a_stale_job_counts_its_windows_as_missing(world, monkeypatch):
    world.corpus = _windows_corpus(2)
    u = _user(profile_needs_full_regen=True)
    _advance(u)
    job = ProfileBatchJob.query.first()
    job.submitted_at = datetime.utcnow() - timedelta(hours=26)
    db.session.commit()
    _poll()
    db.session.refresh(job)
    assert job.status == "failed"
    assert {i["outcome"] for i in job.items} == {"missing"}
    assert pw.advance_profile_windows.apply_async.call_count == 2


def test_refused_submissions_stop_rendering_after_the_third(world):
    """Review of #589, finding 3: a submission the provider refuses (no
    batch id, e.g. a spend limit) is recorded in the attempt history, so
    the set is not rendered again every hour while the refusal lasts."""
    world.corpus = _windows_corpus(3)
    world.provider.fail_submit = True
    u = _user(profile_needs_full_regen=True)
    renders = []
    for _ in range(4):
        before = len(world.corpus.renders)
        assert _advance(u) in ("failed", "idle")
        renders.append(len(world.corpus.renders) - before)
    assert all(renders[:3]) and renders[3] == 0
    failed = ProfileBatchJob.query.filter_by(status="failed").all()
    assert len(failed) == 3
    assert {i["outcome"] for job in failed for i in job.items} == {"missing"}
    assert not ProfileBatchJob.query.filter_by(status="pending").count()
    # No immediate re-dispatch: one advance per call above.
    assert pw.advance_profile_windows.apply_async.call_count == 4
    db.session.refresh(u)
    assert u.profile_seed_error.startswith(pw.STOP_NOTE)
    assert u.profile_batch_pending is False
    # A day later it gets one more try.
    later = datetime.utcnow() + timedelta(hours=25)
    assert pw.provider_stop_until(pw.attempt_history(u.id), ("window", None),
                                  later) is None


# ── 9. merges ─────────────────────────────────────────────────────────────

# The coverage of one window of a large build in these fixtures: a Haiku
# window is about 43k units, a Luna window up to about 108k.
WINDOW_UNITS = 40_000


def _set_of(user, n, set_id="set1", base=None, start=T0, step=None,
            created=None, units=WINDOW_UNITS):
    """``n`` saved windows of one set, contiguous, a day each, each
    covering ``units`` (cumulative coverage, as a large build's windows
    have: a set below CHUNK_TARGET_UNITS is provisional, and the ladder,
    not the 80k gate, rebuilds it)."""
    step = step or timedelta(days=1)
    rows, cum = [], (base.source_tokens_used if base else 0)
    prev = base.source_data_cutoff if base else None
    for i in range(n):
        cutoff = start + step * (i + 1)
        cum += units
        row = UserProfile(
            user_id=user.id, generated_by="claude-haiku-5.5", tokens_used=0,
            generation_type="window", window_set=set_id, covers_start=prev,
            source_tokens_used=cum, source_data_cutoff=cutoff,
            source_rendered_at=datetime.utcnow(), ai_usage="chat",
            parent_profile_id=base.id if base else None,
            created_at=created or datetime.utcnow())
        row.set_content(ex.format_date_metadata(
            covers_start=prev or start, covers_end=cutoff, tokens=1000)
            + _good(tag=f"w{i}"))
        db.session.add(row)
        rows.append(row)
        prev = cutoff
    db.session.commit()
    return rows


def _corpus_for(world, rows):
    """A corpus whose rows end at the set's tip (nothing left to read)."""
    world.corpus = Corpus(0)
    world.corpus.rows = [(r.source_data_cutoff, 1000) for r in rows]


def _merge_reqs(world):
    return [r for r in world.provider.requests()
            if "_g" in r["custom_id"] or "_f_" in r["custom_id"]]


LABEL_RE = re.compile(r"Profile No\. \d+\n- Covers the writing from "
                      r"\d{4}-\d\d-\d\d to \d{4}-\d\d-\d\d")
DATE_LINE_RE = re.compile(r"\[Covers \d{4}-\d\d-\d\d to \d{4}-\d\d-\d\d "
                          r"\(\S+ tokens\)\]\n")


def test_flat_merge_labels_match_the_experiment(app, world):
    app.config["PROFILE_MERGE_STRUCTURE"] = "flat"
    u = _user()
    rows = _set_of(u, 26)
    _corpus_for(world, rows)
    _advance(u)
    reqs = _merge_reqs(world)
    assert len(reqs) == 1 and "_f_" in reqs[0]["custom_id"]
    msgs = reqs[0]["messages"]
    assert len(msgs) == 27
    labels = [m["content"][0]["text"].split("\n\n", 1)[0] for m in msgs[:-1]]
    assert all(LABEL_RE.fullmatch(lab) for lab in labels)
    assert labels[0] == ("Profile No. 1\n- Covers the writing from "
                         "2024-01-01 to 2024-01-02")
    assert all(DATE_LINE_RE.match(m["content"][0]["text"].split(
        "\n\n", 1)[1]) for m in msgs[:-1])
    # The integration prompt is #586's, unchanged.
    integ = ex._integration_messages(u.id, rows)[-1]
    assert msgs[-1] == integ
    assert "{N_MONTHS}" not in msgs[-1]["content"][0]["text"]


def test_flat_over_the_cap_merges_in_groups(app, world, monkeypatch):
    app.config["PROFILE_MERGE_STRUCTURE"] = "flat"
    app.config["PROFILE_MERGE_GROUP_SIZE"] = 10
    u = _user()
    rows = _set_of(u, 25)
    _corpus_for(world, rows)
    monkeypatch.setattr(pw, "merge_over_cap", lambda m, msgs, k: (
        len(msgs) > 12, len(msgs) * 1000))
    _advance(u)
    ids = [r["custom_id"] for r in _merge_reqs(world)]
    assert len(ids) == 3 and all("_g" in c for c in ids)


def test_thirty_windows_merge_in_one_call(world):
    u = _user()
    rows = _set_of(u, 30)
    _corpus_for(world, rows)
    _advance(u)
    reqs = _merge_reqs(world)
    assert len(reqs) == 1 and "_f_" in reqs[0]["custom_id"]
    assert len(reqs[0]["messages"]) == 31


def test_sixty_five_windows_merge_in_groups_then_the_final(world):
    u = _user()
    rows = _set_of(u, 65)
    _corpus_for(world, rows)
    _advance(u)
    groups = [i for i in _items("group")]
    assert [len(g["members"]) for g in groups] == [30, 30, 5]
    assert not _items("final")
    _poll()
    assert len(_rows(u, "group")) == 3
    _advance(u)
    final_item = _items("final")
    assert len(final_item) == 1
    final_req = [r for r in world.provider.requests()
                 if "_f_" in r["custom_id"]][0]
    labels = [m["content"][0]["text"].split("\n\n", 1)[0]
              for m in final_req["messages"][:-1]]
    assert len(labels) == 3
    assert labels[0].endswith("2024-01-01 to 2024-01-31")
    assert labels[2].endswith("2024-03-01 to 2024-03-06")
    _poll()
    final = _rows(u, "integration")
    assert len(final) == 1
    f = final[0]
    assert f.window_set == "set1" and f.parent_profile_id == rows[-1].id
    assert f.source_data_cutoff == rows[-1].source_data_cutoff
    assert f.source_tokens_used == rows[-1].source_tokens_used
    assert f.merged_profile_id is None
    from backend.routes.dashboard import get_latest_profile
    assert get_latest_profile(u)["id"] == f.id
    # Done: the next advance sends nothing.
    n = len(world.provider.submits)
    _advance(u)
    assert len(world.provider.submits) == n


def test_a_merge_over_the_cap_stops_once(world, monkeypatch):
    u = _user()
    rows = _set_of(u, 65)
    _corpus_for(world, rows)
    sent = []
    monkeypatch.setattr(pw, "_report", lambda title, **tags: sent.append(
        title))
    monkeypatch.setattr(pw, "merge_over_cap", lambda m, msgs, k: (
        True, 2_000_000))
    _advance(u)
    db.session.refresh(u)
    assert u.profile_seed_error.startswith(pw.MERGE_STOP_NOTE)
    assert _merge_reqs(world) == [] and len(sent) == 1
    assert not pw.set_work_due(u, ex.profile_update_base(u.id))
    _advance(u)
    assert len(sent) == 1                       # not retried, not re-alerted


def test_a_window_set_with_a_base_merges_it_as_profile_one(world):
    u = _user()
    base = _profile(u, gen_type="update", cutoff=T0, units=5000)
    rows = _set_of(u, 3, base=base)
    _corpus_for(world, rows)
    _advance(u)
    msgs = _merge_reqs(world)[0]["messages"]
    first = msgs[0]["content"][0]["text"].split("\n\n", 1)[0]
    assert first == ("Profile No. 1\n- Covers all the writing from "
                     "2024-01-01 to 2024-01-01")
    assert len(msgs) == 5


# ── 10. updates ───────────────────────────────────────────────────────────

def _finish(world, user):
    for _ in range(4):
        _advance(user)
        if not ProfileBatchJob.query.filter_by(status="pending").count():
            return
        _poll()


def test_an_update_redoes_only_the_newest_group_and_the_final(world):
    u = _user()
    rows = _set_of(u, 65)
    _corpus_for(world, rows)
    _finish(world, u)
    assert len(_rows(u, "integration")) == 1
    groups_before = {g.id for g in _rows(u, "group")}
    # 85k units of new writing after the build.
    future = datetime.utcnow() + timedelta(days=2)
    world.corpus.add(85, start=future)
    n = len(world.provider.submits)
    assert pw.wants_window_build(u)
    _finish(world, u)
    reqs = [r for s in world.provider.submits[n:]
            for rs in s.values() for r in rs]
    sent = [r["custom_id"] for r in reqs]
    # A regular update (Peter, 2026-11-05): one chained version by the
    # user's chat model, and the merges after it by the same model.
    assert not [c for c in sent if "_w" in c]
    assert len([c for c in sent if "_u" in c]) == 1
    assert {r["model_id"] for r in reqs} == {"claude-opus-5.5"}
    groups = [c for c in sent if "_g" in c]
    assert len(groups) == 1                      # the newest group only
    assert sum(1 for c in sent if "_f_" in c) == 1
    new_groups = [g for g in _rows(u, "group") if g.id not in groups_before]
    assert len(new_groups) == 1
    assert new_groups[0].covers_start == rows[59].source_data_cutoff
    assert len(_rows(u, "integration")) == 2


def test_successive_updates_never_redo_a_full_group(world):
    u = _user()
    rows = _set_of(u, 62)
    _corpus_for(world, rows)
    _finish(world, u)
    full = {(g.covers_start, g.source_data_cutoff) for g in _rows(u, "group")
            if g.covers_start in (None, rows[29].source_data_cutoff)}
    assert len(full) == 2
    for k in range(3):
        start = datetime.utcnow() + timedelta(days=2 + k * 10)
        world.corpus.add(85, start=start)
        last_job = db.session.query(db.func.max(ProfileBatchJob.id)).scalar()
        _finish(world, u)
        group_meta = [i for job in ProfileBatchJob.query.filter(
                      ProfileBatchJob.id > last_job).all()
                      for i in job.items if i["kind"] == "group"]
        assert len(group_meta) == 1             # the newest group only
        for g in group_meta:
            key = (datetime.fromisoformat(g["start"]) if g["start"] else None,
                   datetime.fromisoformat(g["cutoff"]))
            assert key not in full


def test_a_gap_fill_invalidates_only_its_group(world):
    u = _user()
    rows = _set_of(u, 65)
    _corpus_for(world, rows)
    _finish(world, u)
    # Window 40 is built again (a gap filled after a retry): newer than
    # its group, so only the second group is redone.
    w = rows[39]
    again = UserProfile(
        user_id=u.id, generated_by=w.generated_by, tokens_used=0,
        generation_type="window", window_set=w.window_set,
        covers_start=w.covers_start, source_tokens_used=w.source_tokens_used,
        source_data_cutoff=w.source_data_cutoff, ai_usage="chat",
        source_rendered_at=datetime.utcnow())
    again.set_content(_good(tag="again"))
    db.session.add(again)
    db.session.commit()
    state = pw.set_state(u, "set1", None)
    plan = pw.merge_plan(u, state)
    assert len(plan.groups) == 1
    assert plan.groups[0][0].covers_start == rows[29].source_data_cutoff


def test_no_group_reaches_past_a_missing_window(world):
    """Review of #589, finding 4: with window 40 of 65 missing, the group
    1-30 goes out with the retry, and no group takes windows 31-39 and
    41-61 in the gap's place."""
    u = _user()
    rows = _set_of(u, 65)
    _corpus_for(world, rows)
    db.session.delete(rows[39])
    db.session.commit()
    state = pw.set_state(u, "set1", None)
    assert state.prefix == 39 and not state.complete
    plan = pw.merge_plan(u, state)
    assert [(g[0].covers_start, g[-1].source_data_cutoff)
            for g in plan.groups] == [(None, rows[29].source_data_cutoff)]
    _advance(u)
    windows, groups = _items("window"), _items("group")
    assert [w["start"] for w in windows] == [
        rows[38].source_data_cutoff.isoformat()]
    assert [(g["start"], g["cutoff"]) for g in groups] == [
        (None, rows[29].source_data_cutoff.isoformat())]
    assert not _items("final")



# ── 11. the user's edits ──────────────────────────────────────────────────

def _edit(user, of, when=None):
    from backend.utils.profile_versions import coverage_of
    e = UserProfile(user_id=user.id, generated_by="user", tokens_used=0,
                    ai_usage="chat", parent_profile_id=of.id,
                    created_at=when or datetime.utcnow(),
                    **coverage_of(user, of))
    e.set_content(_good(events=False, tag="mine"))
    db.session.add(e)
    db.session.commit()
    return e


def test_an_edit_during_the_build_is_merged_in_the_final(world):
    u = _user()
    old = _profile(u, gen_type="integration", cutoff=T0 + timedelta(days=5))
    rows = _set_of(u, 10)
    edit = _edit(u, old)
    _corpus_for(world, rows)
    assert ex.profile_update_base(u.id).id == rows[-1].id
    _advance(u)
    msgs = _merge_reqs(world)[0]["messages"]
    labels = [m["content"][0]["text"].split("\n\n", 1)[0] for m in msgs[:-1]]
    mine = [i for i, lab in enumerate(labels) if "Written by the user" in lab]
    assert len(mine) == 1
    # At its date: after the windows up to its cutoff (day 5).
    assert mine[0] == 5
    assert labels[5].startswith("Profile No. 6\n- Covers all the writing")
    _poll()
    final = _rows(u, "integration")[-1]
    assert final.window_set == "set1" and final.merged_profile_id == edit.id


def test_an_edit_while_the_final_is_in_flight_rebuilds_it(world):
    u = _user()
    rows = _set_of(u, 5)
    _corpus_for(world, rows)
    _advance(u)
    edit = _edit(u, rows[-1])          # saved while the final is in flight
    costs = APICostLog.query.filter_by(user_id=u.id).count()
    _poll()
    assert not [f for f in _rows(u, "integration") if f.window_set]
    assert APICostLog.query.filter_by(user_id=u.id).count() == costs + 1
    assert _items("final")[0]["outcome"] == "superseded"
    _advance(u)
    _poll()
    final = [f for f in _rows(u, "integration") if f.window_set]
    assert len(final) == 1 and final[0].merged_profile_id == edit.id


def test_an_edit_after_the_final_is_updated_on_the_chained_path(world):
    u = _user()
    rows = _set_of(u, 5)
    _corpus_for(world, rows)
    _finish(world, u)
    final = [f for f in _rows(u, "integration") if f.window_set][0]
    edit = _edit(u, final, when=datetime.utcnow() + timedelta(seconds=5))
    assert ex.profile_update_base(u.id).id == edit.id
    world.corpus.add(85, start=datetime.utcnow() + timedelta(days=2))
    assert not pw.wants_window_build(u)          # one chunk: chained
    n = len(world.provider.submits)
    _advance(u)                                  # the set is not extended
    assert len(world.provider.submits) == n


def _update_in_flight(world, user):
    """A finished set of five windows, then 85k units of new writing whose
    update window is in the batch. Returns (rows, final)."""
    rows = _set_of(user, 5)
    _corpus_for(world, rows)
    _finish(world, user)
    final = [f for f in _rows(user, "integration") if f.window_set][0]
    world.corpus.add(85, start=datetime.utcnow() + timedelta(days=2))
    _advance(user)
    assert [i for i in _items("window") if "outcome" not in i]
    return rows, final


def test_an_edit_after_the_final_discards_an_update_window_in_flight(world):
    """Review of #589, finding 1: the window collected after the edit is
    not saved (its cost row is), the edit is the base, and the set gets
    nothing more."""
    u = _user()
    rows, final = _update_in_flight(world, u)
    edit = _edit(u, final)
    costs = APICostLog.query.filter_by(user_id=u.id).count()
    _poll()
    assert {i["outcome"] for i in _items("window") if i["set"] == "set1"
            and i["start"] == rows[-1].source_data_cutoff.isoformat()} == {
        "superseded"}
    assert len(_rows(u, "window")) == 5
    assert APICostLog.query.filter_by(user_id=u.id).count() > costs
    assert ex.profile_update_base(u.id).id == edit.id
    assert not pw.wants_window_build(u)          # one chunk: chained
    n = len(world.provider.submits)
    _advance(u)
    assert len(world.provider.submits) == n


def test_a_set_in_flight_after_an_edit_on_its_final_sends_nothing_more(
        world):
    u = _user()
    _rows_, final = _update_in_flight(world, u)
    _edit(u, final)
    # More writing while the window is in the batch.
    world.corpus.add(85, start=datetime.utcnow() + timedelta(days=20))
    n = len(world.provider.submits)
    assert _advance(u) == "idle"
    assert len(world.provider.submits) == n


# #421, option (c) (Peter, voice review 2026-11-10): a rebuild's final
# merges every round of the user's edits at its date, an older round only
# for the lines no newer round changed. Invented texts.
BASE421 = ("### SURFACE MAP\nLikes long walks.\nWRONG GUESS\n\n"
           "### SHADOW MAP\nAvoids crowds.")
CELLO421 = "Plays the cello on weekends."


def _rebuild_with_two_rounds(world, first_text, update_text, second_text):
    """An old chain built before the set: an integration (BASE421, day 3),
    the first round (an edit of it), an update and its integration
    (``update_text``, day 6), and the second round (an edit of that). Then
    a from-scratch set of ten windows (days 1 to 10) whose final is due,
    with the second round as the merge the rebuild still has to make (as
    next_work plans it for a fresh full rebuild). Returns (user, first,
    second)."""
    u = _user()
    ago = datetime.utcnow() - timedelta(days=20)

    def version(gen_type, day, parent, text, hours):
        return _profile(u, gen_type=gen_type, cutoff=T0 + timedelta(days=day),
                        text=text, parent_profile_id=(
                            parent.id if parent else None),
                        created_at=ago + timedelta(hours=hours))

    def edit(of, text, hours):
        e = _edit(u, of, when=ago + timedelta(hours=hours))
        e.set_content(text)
        db.session.commit()
        return e
    old = version("integration", 3, None, BASE421, 1)
    first = edit(old, first_text, 2)
    update = version("update", 6, first, update_text, 3)
    again = version("integration", 6, update, update_text, 4)
    second = edit(again, second_text, 5)
    u.profile_pending_merge_id = second.id
    db.session.commit()
    rows = _set_of(u, 10)
    _corpus_for(world, rows)
    return u, first, second


def _final_texts(world):
    msgs = _merge_reqs(world)[0]["messages"]
    return [m["content"][0]["text"] for m in msgs[:-1]]


def test_421c_a_rebuilds_final_merges_an_older_round_reduced(world):
    """Round 1 corrects WRONG GUESS and adds a sentence, the update keeps
    both, round 2 deletes the sentence. The final reads round 1 at its
    date (after day 3) without the sentence and with the correction, and
    round 2 whole at its date (after day 6)."""
    first_text = BASE421.replace("WRONG GUESS", "RIGHT GUESS") \
        + f"\n{CELLO421}"
    update_text = first_text.replace("RIGHT GUESS", "Right guess, reworded.")
    u, first, second = _rebuild_with_two_rounds(
        world, first_text, update_text,
        update_text.replace(f"\n{CELLO421}", ""))

    _advance(u)

    texts = _final_texts(world)
    mine = [i for i, t in enumerate(texts) if "Written by the user" in t]
    assert mine == [3, 7]
    assert texts[3].endswith(
        "\n\n### SURFACE MAP\nLikes long walks.\nRIGHT GUESS\n\n"
        "### SHADOW MAP\nAvoids crowds.")
    assert texts[7].endswith(f"\n\n{second.get_content()}")
    assert all(CELLO421 not in t for t in texts)
    final_item = _items("final")[0]
    assert final_item["user_version_id"] == second.id
    assert first.id in final_item["inputs"]
    _poll()
    final = _rows(u, "integration")[-1]
    assert final.window_set == "set1" and final.merged_profile_id == second.id
    db.session.refresh(u)
    assert u.profile_pending_merge_id is None


def test_421c_a_rebuilds_final_leaves_out_a_round_with_nothing_left(world):
    with_cello = f"{BASE421}\n{CELLO421}"
    u, first, second = _rebuild_with_two_rounds(
        world, with_cello, with_cello, BASE421)

    _advance(u)

    texts = _final_texts(world)
    assert [i for i, t in enumerate(texts)
            if "Written by the user" in t] == [6]
    assert all(CELLO421 not in t for t in texts)
    assert first.id not in _items("final")[0]["inputs"]


def test_421c_the_seeders_check_reads_no_profile_text(world, monkeypatch):
    """set_work_due runs for every window-set user on the seeder's ticks:
    it plans the final from metadata, and the older rounds are read only
    when the final goes out."""
    first_text = BASE421.replace("WRONG GUESS", "RIGHT GUESS")
    u, first, second = _rebuild_with_two_rounds(
        world, first_text, first_text, first_text + "\nA NEW LINE")

    def no_text(*a, **k):
        raise AssertionError("read a version's text")
    monkeypatch.setattr(pw._exports, "user_merge_input", no_text)
    tip = _rows(u, "window")[-1]
    assert pw.set_work_due(u, tip)
    plan = pw.merge_plan(u, pw.set_state(u, "set1", None))
    assert plan.older_versions == [first.id]
    assert plan.user_version.id == second.id


# ── 13. spend ─────────────────────────────────────────────────────────────
# Peter, 2026-10-22: "send it and let the cap stop it halfway. It will
# self-heal the following month": the per-user cap holds the next
# submission once the user is over the limit. Peter, 2026-11-05: windows go
# out only when the expected cost of the windows and the merges after them
# fits into what is left of the limit; else the build waits.

def _block_email(monkeypatch):
    import backend.utils.email as em
    sent = MagicMock()
    monkeypatch.setattr(em, "send_user_spend_block_email", sent)
    return sent


def _spend(user, usd, created_at=None):
    row = APICostLog(user_id=user.id, model_id="claude-haiku-5.5",
                     request_type="profile_batch", input_tokens=1,
                     output_tokens=1,
                     cost_microdollars=int(round(usd * 1_000_000)))
    if created_at is not None:
        row.created_at = created_at
    db.session.add(row)
    db.session.commit()


def _last_month():
    from backend.utils.spend import month_start
    return month_start() - timedelta(days=1)


def _fixed_estimate(monkeypatch, usd):
    monkeypatch.setattr(pw, "estimate_cost_usd", lambda *a, **k: usd)


def test_a_build_over_the_headroom_waits_and_sends_nothing(world):
    world.corpus = _windows_corpus(10)
    u = _user(profile_needs_full_regen=True, monthly_spend_limit_usd=0.01)
    assert _advance(u) == "held"
    assert world.provider.submits == []
    db.session.refresh(u)
    assert u.profile_cap_held is True              # the seeder resumes it
    assert u.profile_seed_error.startswith(pw.HOLD_NOTE)
    assert "$0.00 of $0.01 used this month" in u.profile_seed_error
    assert u.profile_batch_pending is False


@pytest.mark.parametrize("limit, sent", [(4.99, False), (5.00, True)])
def test_the_estimate_must_fit_into_the_headroom(world, monkeypatch,
                                                 limit, sent):
    """$2 spent this month and an estimate of $3: a limit of $5 leaves
    exactly enough and the windows go out; $4.99 leaves a cent too little
    and the build waits, with the amount in the admin column."""
    _fixed_estimate(monkeypatch, 3.00)
    world.corpus = _windows_corpus(4)
    u = _user(profile_needs_full_regen=True, monthly_spend_limit_usd=limit)
    _spend(u, 2.00)
    outcome = _advance(u)
    db.session.refresh(u)
    if sent:
        assert outcome == "submitted"
        assert len(world.provider.submits) == 1
        assert all("_w" in r["custom_id"] for r in world.provider.requests())
        assert u.profile_cap_held is False
        assert u.profile_seed_error is None
    else:
        assert outcome == "held"
        assert world.provider.submits == []
        assert u.profile_seed_error == (
            f"{pw.HOLD_NOTE}: the build needs about $3.00; "
            "$2.00 of $4.99 used this month")


def test_a_user_without_a_limit_is_not_gated(world, monkeypatch):
    _fixed_estimate(monkeypatch, 1000.0)
    world.corpus = _windows_corpus(4)
    u = _user(profile_needs_full_regen=True)        # no limit of their own
    _spend(u, 50.0)
    assert _advance(u) == "submitted"
    assert len(world.provider.submits) == 1


@pytest.mark.parametrize("limit, sent", [(4.99, False), (5.00, True)])
def test_the_headroom_is_the_limit_less_the_carry(world, monkeypatch,
                                                  limit, sent):
    """#594: $2 of last month's overflow carried into this month, nothing
    spent yet, and an estimate of $3: a limit of $5 leaves exactly enough;
    $4.99 leaves a cent too little, and the note names the carry."""
    from backend.utils.spend import current_month
    _fixed_estimate(monkeypatch, 3.00)
    world.corpus = _windows_corpus(4)
    u = _user(profile_needs_full_regen=True, monthly_spend_limit_usd=limit,
              spend_carry_month=current_month(),
              spend_carry_microdollars=2_000_000)
    outcome = _advance(u)
    db.session.refresh(u)
    if sent:
        assert outcome == "submitted"
        assert len(world.provider.submits) == 1
        assert u.profile_seed_error is None
    else:
        assert outcome == "held"
        assert world.provider.submits == []
        assert u.profile_seed_error == (
            f"{pw.HOLD_NOTE}: the build needs about $3.00; "
            "$2.00 of $4.99 used this month ($2.00 carried over)")


def test_a_build_held_at_the_cap_stays_held_while_the_carry_fills_the_limit(
        app, world, monkeypatch):
    """#594: a build held at the cap is not resumed when the month turns
    while last month's overflow alone fills the new month's limit: the
    hourly seeder rolls the carry first. Clearing the carry (the admin's
    action) resumes it on the next pass."""
    from backend.utils.spend import (
        clear_spend_carry, current_month, month_start)
    _block_email(monkeypatch)
    world.corpus = _windows_corpus(4)
    u = _user(profile_needs_full_regen=True, monthly_spend_limit_usd=1)
    _advance(u)
    _poll()                                      # the windows are saved
    _spend(u, 1.0)                               # the cap is reached
    assert _advance(u) == "held"
    n = len(world.provider.submits)

    # The month turns: everything so far was last month's, $2.50 over the
    # $1 limit, and the carry was last rolled then.
    last = _last_month()
    APICostLog.query.filter_by(user_id=u.id).update({"created_at": last})
    _spend(u, 1.5, created_at=last)
    u = db.session.get(User, u.id)
    u.spend_blocked_month = current_month(last)
    u.spend_carry_month = current_month(last)
    u.spend_carry_microdollars = 0
    db.session.commit()

    dispatches = pw.advance_profile_windows.apply_async.call_count
    pb._seed_profile_batches()                   # hourly pass
    assert pw.advance_profile_windows.apply_async.call_count == dispatches
    assert len(world.provider.submits) == n
    u = db.session.get(User, u.id)
    assert u.spend_carry_month == current_month()
    assert u.spend_carry_microdollars >= 1_000_000
    assert u.spend_blocked_month == current_month()
    assert u.profile_cap_held is True
    assert month_start() > last

    clear_spend_carry(u, app.config)
    db.session.commit()
    pb._seed_profile_batches()                   # the next hourly pass
    assert (pw.advance_profile_windows.apply_async.call_count
            == dispatches + 1)
    u = db.session.get(User, u.id)
    assert pw.advance(u.id, u.profile_generation_task_id) == "submitted"
    assert len(world.provider.submits) == n + 1


@pytest.mark.parametrize("lift", ["next_month", "raised_limit"])
def test_a_build_waiting_on_its_estimate_goes_out_when_there_is_room(
        app, world, monkeypatch, lift):
    """The build waits; the hourly seeder tries it again and it waits
    again; once the month turns (or the admin raises the limit) the next
    hourly pass submits its windows and the note is gone."""
    _fixed_estimate(monkeypatch, 3.00)
    world.corpus = _windows_corpus(4)
    u = _user(profile_needs_full_regen=True, monthly_spend_limit_usd=4.99)
    _spend(u, 2.00)
    assert _advance(u) == "held"

    def hourly():
        before = pw.advance_profile_windows.apply_async.call_count
        pb._seed_profile_batches()
        assert pw.advance_profile_windows.apply_async.call_count == before + 1
        user = db.session.get(User, u.id)
        return pw.advance(user.id, user.profile_generation_task_id)

    assert hourly() == "held"                       # still no room
    assert world.provider.submits == []
    user = db.session.get(User, u.id)
    assert user.profile_seed_error.startswith(pw.HOLD_NOTE)

    if lift == "next_month":
        APICostLog.query.filter_by(user_id=u.id).update(
            {"created_at": _last_month()})
    else:
        user.monthly_spend_limit_usd = 10.0
    db.session.commit()

    assert hourly() == "submitted"
    assert len(world.provider.submits) == 1
    assert all("_w" in r["custom_id"] for r in world.provider.requests())
    user = db.session.get(User, u.id)
    assert user.profile_cap_held is False
    assert user.profile_seed_error is None


def test_a_final_over_the_headroom_under_the_cap_goes_out(world):
    """Merges with no window to read are held by the spend cap only, not
    by the estimate (Peter, 2026-10-22: "let the cap stop it halfway")."""
    u = _user(monthly_spend_limit_usd=1)
    rows = _set_of(u, 5)
    _corpus_for(world, rows)
    _spend(u, 0.999)                  # under the cap, short of the final
    _advance(u)
    assert len(_merge_reqs(world)) == 1
    db.session.refresh(u)
    assert u.profile_cap_held is False


def test_a_cap_reached_between_windows_and_final_holds_the_final(
        world, monkeypatch):
    _block_email(monkeypatch)
    u = _user(monthly_spend_limit_usd=1)
    rows = _set_of(u, 5)
    _corpus_for(world, rows)
    _spend(u, 1.0)
    assert _advance(u) == "held"
    assert world.provider.submits == []
    db.session.refresh(u)
    assert u.profile_cap_held is True
    assert u.profile_seed_error is None          # no note of its own
    assert u.profile_batch_pending is False


@pytest.mark.parametrize("lift", ["next_month", "raised_limit"])
def test_a_build_held_at_the_cap_resumes_when_the_cap_lifts(
        app, world, monkeypatch, lift):
    """Windows collected and the user over the cap: the groups and the
    final are held. Once the month turns (or the admin raises the limit)
    the hourly seeder resumes the set from its saved windows, and the
    final is saved."""
    from backend.utils.spend import current_month, reconcile_user_spend_block
    _block_email(monkeypatch)
    app.config["PROFILE_MERGE_GROUP_SIZE"] = 2
    world.corpus = _windows_corpus(4)
    u = _user(profile_needs_full_regen=True, monthly_spend_limit_usd=1)
    _advance(u)
    windows_sent = [r["custom_id"] for r in world.provider.requests()]
    assert len(windows_sent) >= 3
    assert all("_w" in c for c in windows_sent)
    _poll()                                      # the windows are saved
    assert len(_rows(u, "window")) == len(windows_sent)
    _spend(u, 1.0)                               # the cap is reached
    n = len(world.provider.submits)

    assert _advance(u) == "held"                 # the poller's advance
    assert len(world.provider.submits) == n
    u = db.session.get(User, u.id)
    assert u.profile_cap_held is True
    assert u.profile_batch_pending is False
    dispatches = pw.advance_profile_windows.apply_async.call_count
    assert pb._seed_profile_batches() == 0       # hourly pass, still capped
    assert len(world.provider.submits) == n
    assert pw.advance_profile_windows.apply_async.call_count == dispatches
    assert db.session.get(User, u.id).profile_cap_held is True

    u = db.session.get(User, u.id)
    if lift == "next_month":
        APICostLog.query.filter_by(user_id=u.id).update(
            {"created_at": _last_month()})
        u.spend_blocked_month = current_month(_last_month())
    else:
        u.monthly_spend_limit_usd = 100.0
        assert reconcile_user_spend_block(u, app.config)["blocked"] is False
    db.session.commit()

    pb._seed_profile_batches()                   # hourly pass
    assert (pw.advance_profile_windows.apply_async.call_count
            == dispatches + 1)
    u = db.session.get(User, u.id)
    assert pw.advance(u.id, u.profile_generation_task_id) == "submitted"
    resumed = [r["custom_id"] for r in world.provider.requests()]
    assert len(world.provider.submits) == n + 1
    assert resumed and not any("_w" in c for c in resumed)
    assert any("_g" in c for c in resumed)       # windows are not re-read
    assert db.session.get(User, u.id).profile_cap_held is False
    _poll()
    _finish(world, u)
    finals = [f for f in _rows(u, "integration") if f.window_set]
    assert len(finals) == 1
    assert len(_rows(u, "window")) == len(windows_sent)
    assert pw._visible_profile(u.id).id == finals[0].id   # the profile


def test_a_window_build_waits_while_the_user_imports(world):
    """#598: while an import's quiet period runs, the advance sends
    nothing (no windows, no merges); once it is over, the windows go out."""
    world.corpus = _windows_corpus(4)
    u = _user(profile_needs_full_regen=True,
              profile_import_quiet_from=datetime.utcnow())
    assert _advance(u) == "waiting"
    assert world.provider.submits == []
    assert pw.advance_profile_windows.apply_async.call_count == 1
    u = db.session.get(User, u.id)
    u.profile_import_quiet_from = datetime.utcnow() - timedelta(minutes=30)
    db.session.commit()
    assert _advance(u) == "submitted"
    assert len(world.provider.submits) == 1


def test_the_admins_window_build_does_not_wait_for_the_imports(world):
    world.corpus = _windows_corpus(4)
    u = _user(profile_needs_full_regen=True,
              profile_import_quiet_from=datetime.utcnow())
    _seed(u)                                   # admin Build profile
    dispatch = pw.advance_profile_windows.apply_async.call_args
    assert dispatch.args[0] == (u.id, False)
    u = db.session.get(User, u.id)
    assert pw.advance(u.id, u.profile_generation_task_id,
                      *dispatch.args[0][1:]) == "submitted"
    assert len(world.provider.submits) == 1


# ── 14. R7, R11 ───────────────────────────────────────────────────────────

def test_a_stuck_pending_flag_is_cleared_and_a_live_one_kept(
        app, world, monkeypatch):
    stuck = _user(profile_batch_pending=True)
    live = _user(profile_batch_pending=True,
                 profile_generation_task_id="t-live",
                 profile_generation_task_dispatched_at=datetime.utcnow())
    monkeypatch.setattr(pb._exports, "_is_task_stale",
                        lambda u: u.profile_generation_task_id != "t-live")
    assert pb._clear_stuck_pending() == 1
    db.session.refresh(stuck)
    db.session.refresh(live)
    assert stuck.profile_batch_pending is False
    assert live.profile_batch_pending is True


def test_imported_nodes_do_not_count_as_new_writing(app):
    u = _user()
    cutoff = datetime(2026, 1, 1)
    n = Node(user_id=u.id, node_type="user", ai_usage="chat",
             token_count=90_000, created_at=datetime(2020, 1, 1),
             updated_at=datetime(2026, 10, 1))
    n.set_content("synthetic imported text")
    db.session.add(n)
    db.session.commit()
    assert pb._new_token_count(u, cutoff) == 0


# ── 15. an import on a set ────────────────────────────────────────────────

def test_an_import_on_a_set(app):
    u = _user()
    rows = _set_of(u, 3)
    tip = rows[-1]
    action, version = ex.revert_profile_for_import(
        u.id, tip.source_data_cutoff + timedelta(days=1))
    assert (action, version.id) == ("none", tip.id)
    action, _v = ex.revert_profile_for_import(u.id, T0)
    assert action == "full" and u.profile_needs_full_regen is True


# #625 (Peter, 2026-11-11): an import inside a set's history regenerates
# the profile from the import's oldest change on; what comes before stays.

def _import_inside(world, rows, k):
    """A synthetic entry imported inside window ``k`` (0-based) of
    ``rows``; returns its date."""
    inside = rows[k].source_data_cutoff - timedelta(hours=12)
    world.corpus.rows = sorted(world.corpus.rows + [(inside, 1000)])
    return inside


def test_an_import_inside_a_set_keeps_the_windows_before_it(world):
    u = _user()
    rows = _set_of(u, 10)
    _corpus_for(world, rows)
    _finish(world, u)
    [old_final] = _finals(u)
    inside = _import_inside(world, rows, 5)
    action, tip = ex.revert_profile_for_import(u.id, inside)
    db.session.commit()
    assert action == "revert" and u.profile_needs_full_regen is not True
    # Windows 1-5 end before the import: copied into a new set, unchanged.
    assert tip.window_set != "set1"
    kept = pw.set_windows(u.id, tip.window_set)
    assert [(w.covers_start, w.source_data_cutoff) for w in kept] == [
        (r.covers_start, r.source_data_cutoff) for r in rows[:5]]
    assert [w.get_content() for w in kept] == [
        r.get_content() for r in rows[:5]]
    assert ex.profile_update_base(u.id).id == tip.id
    # The profile in use stays the old final until the new one is saved.
    from backend.utils.profile_versions import current_profile_query
    assert current_profile_query(u.id).first().id == old_final.id

    before = len(_items("window"))
    _finish(world, u)
    new = _items("window")[before:]
    # Only the writing from window 6 on is read again.
    assert new and {i["start"] for i in new} == {
        rows[4].source_data_cutoff.isoformat()}
    final = [f for f in _finals(u) if f.window_set == tip.window_set]
    assert len(final) == 1
    assert current_profile_query(u.id).first().id == final[0].id
    [final_item] = [i for i in _items("final")
                    if i["set"] == tip.window_set]
    assert {w.id for w in kept} <= set(final_item["inputs"])


def test_an_import_inside_a_set_keeps_its_full_groups(app, world):
    """65 windows merged in groups of 30: an import in window 50 keeps the
    group of windows 1-30; only windows from 50 on are read again."""
    u = _user()
    rows = _set_of(u, 65)
    _corpus_for(world, rows)
    _finish(world, u)
    first = [g for g in _rows(u, "group") if g.covers_start is None]
    assert first
    inside = _import_inside(world, rows, 49)
    action, tip = ex.revert_profile_for_import(u.id, inside)
    db.session.commit()
    assert action == "revert"
    groups = [g for g in _rows(u, "group") if g.window_set == tip.window_set]
    assert [(g.covers_start, g.source_data_cutoff) for g in groups] == [
        (None, rows[29].source_data_cutoff)]
    before_groups = len(_items("group"))
    _finish(world, u)
    sent = _items("group")[before_groups:]
    assert sent and all(g["start"] is not None for g in sent)


def test_an_import_inside_a_set_keeps_the_users_edit(world):
    """The user edited the set's final: the import re-tips inside the set
    (not a rebuild from scratch), and the new final merges the edit."""
    u = _user()
    rows = _set_of(u, 6)
    _corpus_for(world, rows)
    _finish(world, u)
    [final] = _finals(u)
    edit = _edit(u, final)
    inside = _import_inside(world, rows, 3)
    action, tip = ex.revert_profile_for_import(u.id, inside)
    db.session.commit()
    assert action == "revert" and tip.window_set != "set1"
    assert len(pw.set_windows(u.id, tip.window_set)) == 3
    _finish(world, u)
    new_final = [f for f in _finals(u) if f.window_set == tip.window_set]
    assert len(new_final) == 1 and new_final[0].merged_profile_id == edit.id


def test_an_import_inside_a_sets_first_window_retips_its_base(world):
    """No window ends before the import: the profile the set continues
    is re-tipped as a chain is, and a new set builds on it."""
    u = _user()
    base = _profile(u, gen_type="update", cutoff=T0, units=WINDOW_UNITS)
    rows = _set_of(u, 4, base=base)
    _corpus_for(world, rows)
    _finish(world, u)
    action, copy = ex.revert_profile_for_import(
        u.id, rows[0].source_data_cutoff - timedelta(hours=2))
    db.session.commit()
    assert action == "revert"
    assert copy.generation_type == "revert" and copy.parent_profile_id == base.id
    assert ex.profile_update_base(u.id).id == copy.id


def _covering(items, when):
    """The window items whose range holds ``when``."""
    return [i for i in items
            if (i["start"] is None or pw._dt(i["start"]) < when)
            and pw._dt(i["cutoff"]) >= when]


def test_an_import_while_an_advance_renders_waits_and_retips_the_set(
        world, redis, monkeypatch):
    """Review of #637: an advance renders its windows into the set it chose
    for minutes before anything is in the batch, so an import that arrives
    then is not re-tipped into a new set the advance would leave behind.
    Since #639 its date waits until the advance's windows are collected,
    and the set is then re-tipped from it: a window reads the import, and
    nothing is rebuilt from scratch."""
    u = _user()
    base = _profile(u, gen_type="update", cutoff=T0, units=WINDOW_UNITS)
    rows = _set_of(u, 6, base=base)
    _corpus_for(world, rows)
    _finish(world, u)
    world.corpus.add(200)                 # new writing after the set
    real = pw._render_windows
    race = {}

    def import_while_rendering(user, state, *a, **k):
        if not race:
            race["inside"] = _import_inside(world, rows, 2)
            race["action"] = ex.revert_profile_for_import(
                user.id, race["inside"])[0]
        return real(user, state, *a, **k)

    monkeypatch.setattr(pw, "_render_windows", import_while_rendering)
    before = len(_items("window"))
    _build(world, u, rounds=10)
    inside = race["inside"]
    later = _items("window")[before:]
    assert _covering(later, inside)      # a window read the imported entry
    assert all(i["start"] is not None for i in later)   # not from scratch
    assert race["action"] == "deferred"
    u = db.session.get(User, u.id)
    assert u.profile_needs_full_regen is False
    assert u.profile_import_retip_from is None


def test_a_synchronous_tasks_guard_alone_does_not_hold_the_retip(
        world, monkeypatch):
    """A profile task that holds the user's guard without having claimed
    a window build (a synchronous task chooses its base when it starts,
    and claims the build before it renders any window): the set is
    re-tipped at once (#639; before, a rebuild from scratch)."""
    u = _user()
    rows = _set_of(u, 6)
    _corpus_for(world, rows)
    _finish(world, u)
    u.profile_generation_task_id = "t-running"
    db.session.commit()
    # The module revert_profile_for_import runs in (``ex``), which may be
    # another object than ``pw._exports`` after other test files reloaded
    # the package.
    for module in (ex, pw._exports):
        monkeypatch.setattr(module, "_is_task_stale", lambda user: False)
    action, _v = ex.revert_profile_for_import(
        u.id, rows[3].source_data_cutoff - timedelta(hours=2))
    assert action == "revert" and u.profile_needs_full_regen is False


def test_an_import_while_an_advance_has_claimed_the_build_waits(
        world, monkeypatch):
    """An advance dispatched and not yet run (its guard, and the claim's
    pending flag): the date waits, and that advance applies it before it
    chooses its set (#639)."""
    u = _user()
    rows = _set_of(u, 6)
    _corpus_for(world, rows)
    _finish(world, u)
    u.profile_generation_task_id = "t-claimed"
    u.profile_batch_pending = True
    db.session.commit()
    for module in (ex, pw._exports):
        monkeypatch.setattr(module, "_is_task_stale", lambda user: False)
    inside = _import_inside(world, rows, 3)
    action, _v = ex.revert_profile_for_import(u.id, inside)
    db.session.commit()
    assert pw.advance(u.id, "t-claimed") == "submitted"
    windows = [i for i in _items("window") if i["set"] != "set1"]
    assert windows and _covering(windows, inside)
    # Read from the last window that ends before the import, not from
    # scratch.
    assert {i["start"] for i in windows} == {
        rows[2].source_data_cutoff.isoformat()}
    assert action == "deferred"
    u = db.session.get(User, u.id)
    assert u.profile_import_retip_from is None
    assert u.profile_needs_full_regen is False


def test_an_import_while_the_set_is_in_flight_waits_for_it(world):
    """A set with work in the batch does not see an import (a known limit
    of #545): the date waits on the user instead of flagging a rebuild
    from scratch (#639), and the oldest of several imports' dates is
    kept."""
    u = _user()
    rows, _final = _update_in_flight(world, u)
    later = rows[3].source_data_cutoff - timedelta(hours=2)
    older = rows[2].source_data_cutoff - timedelta(hours=2)
    assert ex.revert_profile_for_import(u.id, later)[0] == "deferred"
    assert ex.revert_profile_for_import(u.id, older)[0] == "deferred"
    assert ex.revert_profile_for_import(u.id, later)[0] == "deferred"
    db.session.commit()
    u = db.session.get(User, u.id)
    assert u.profile_needs_full_regen is False
    assert u.profile_import_retip_from == older


# ── 15b. an import while the set is in flight (#639) ─────────────────────

def _import_hand_off(monkeypatch, user, earliest):
    """The import's profile hand-off (routes/import_data), as every
    importer calls it. The seed it schedules is not run, and the import's
    quiet period (#598) is over at once."""
    from backend.routes import import_data
    monkeypatch.setattr(pb.seed_profile_batch_for_user, "apply_async",
                        MagicMock(), raising=False)
    import_data._hand_off_profile_update_after_import(user, earliest, 1000)
    user = db.session.get(User, user.id)
    user.profile_import_quiet_from = None
    db.session.commit()
    return user


def test_an_import_during_a_first_build_in_flight_is_read_after_it(
        world, monkeypatch):
    """#639: a first build's windows are in the batch when an import adds
    an entry inside the range they cover. They were rendered before it, so
    none of them reads it. Once they are collected, the set is re-tipped
    from the entry's date: a window reads it, the windows that end before
    it are kept, and nothing is rebuilt from scratch."""
    world.corpus = _windows_corpus(4)
    u = _user()
    _advance(u)                               # the first build goes out
    first = _items("window")
    assert len(first) >= 3 and not _rows(u, "window")
    inside = pw._dt(first[2]["cutoff"]) - timedelta(minutes=30)
    world.corpus.rows = sorted(world.corpus.rows + [(inside, 1000)])
    u = _import_hand_off(monkeypatch, u, inside)
    _build(world, u, rounds=8)
    later = _items("window")[len(first):]
    assert _covering(later, inside)           # a window read the import
    assert all(i["start"] is not None for i in later)   # not from scratch
    assert {i["start"] for i in later if i.get("outcome") == "saved"} == {
        first[1]["cutoff"]}                   # windows 1-2 are kept
    final = _items("final")[-1]
    assert final["set"] != first[0]["set"]
    assert pw._visible_profile(u.id).window_set == final["set"]
    u = db.session.get(User, u.id)
    assert u.profile_import_retip_from is None
    assert u.profile_needs_full_regen is False


def test_the_import_result_says_the_profile_waits_for_the_imports(
        world, monkeypatch):
    """A control (passes on main, where the rebuild flag said it): the
    import's result still tells the user that the profile is built from
    the import once the imports stop, when its date waits for the update
    window in flight (#598, #639)."""
    from backend.routes import import_data
    u = _user()
    rows, _final = _update_in_flight(world, u)
    monkeypatch.setattr(pb.seed_profile_batch_for_user, "apply_async",
                        MagicMock(), raising=False)
    import_data._hand_off_profile_update_after_import(
        u, rows[2].source_data_cutoff - timedelta(hours=2), 1000)
    assert import_data._profile_waits_for_imports(u.id) is True


def test_a_first_builds_window_collected_after_an_import_leaves_it_read(
        world, monkeypatch):
    """#639, the issue's second half: a first build whose last window is
    sent again (its first result was missing) when an import adds an entry
    inside an earlier window. Before, the import flagged a rebuild from
    scratch and the window collected next, a window of the first build,
    cleared the flag: the set's final merged windows that never read the
    entry, and nothing told the user."""
    world.corpus = _windows_corpus(4)
    u = _user()
    _advance(u)
    first = _items("window")
    lost = first[-1]["custom_id"]
    good = world.provider.respond
    world.provider.respond = lambda cid, req: (
        None if cid == lost else good(cid, req))
    _poll()
    assert len(_rows(u, "window")) == len(first) - 1
    _advance(u)                               # the last window again
    assert [i for i in _items("window") if "outcome" not in i]
    inside = pw._dt(first[1]["cutoff"]) - timedelta(minutes=30)
    world.corpus.rows = sorted(world.corpus.rows + [(inside, 1000)])
    u = _import_hand_off(monkeypatch, u, inside)
    before = len(_items("window"))
    _build(world, u, rounds=8)
    later = _items("window")[before:]
    assert _covering(later, inside)           # a window read the import
    assert all(i["start"] is not None for i in later)   # not from scratch
    finals = [f for f in _finals(u) if f.window_set]
    assert finals
    final_set = finals[-1].window_set
    # The final in use merges windows that read the import.
    assert final_set == pw._visible_profile(u.id).window_set
    assert _covering([i for i in later if i["set"] == final_set], inside)
    assert db.session.get(User, u.id).profile_needs_full_regen is False


def _many_rows(world, rows, units=30_000):
    """A corpus of one row of ``units`` per window of ``rows``: re-read,
    each row is about a window of its own."""
    world.corpus = Corpus(0)
    world.corpus.rows = [(r.source_data_cutoff, units) for r in rows]


def test_a_second_import_during_a_partial_rebuild_retips_after_it(world):
    """#639, the issue's first half: an import re-tips a set (#625), and
    while the re-tipped set's windows are in the batch a second import
    lands later in the writing. Its date waits; once the windows are
    collected the set is re-tipped from it, keeping the windows the first
    re-tip read before it. Nothing is rebuilt from scratch. Before, the
    import was compared with the set's saved windows only, which end
    before it: "nothing to invalidate", and the windows in flight, rendered
    before it, never read it."""
    u = _user()
    base = _profile(u, gen_type="update", cutoff=T0, units=WINDOW_UNITS)
    rows = _set_of(u, 8, base=base)
    _many_rows(world, rows)
    _finish(world, u)
    first = rows[2].source_data_cutoff - timedelta(hours=12)
    world.corpus.rows = sorted(world.corpus.rows + [(first, 1000)])
    action, tip = ex.revert_profile_for_import(u.id, first)
    db.session.commit()
    assert action == "revert"
    before = len(_items("window"))
    _advance(u)                           # the re-tipped set's windows
    retip_windows = _items("window")[before:]
    assert len(retip_windows) >= 3
    assert {i["set"] for i in retip_windows} == {tip.window_set}
    second = rows[5].source_data_cutoff - timedelta(hours=12)
    world.corpus.rows = sorted(world.corpus.rows + [(second, 1000)])
    action, _v = ex.revert_profile_for_import(u.id, second)
    db.session.commit()
    before = len(_items("window"))
    _build(world, u, rounds=8)
    later = _items("window")[before:]
    assert _covering(later, second)       # a window read the second import
    assert all(i["start"] is not None
               and pw._dt(i["start"]) >= rows[2].source_data_cutoff
               for i in later)            # what the first re-tip read stays
    assert action == "deferred"
    final_set = pw._visible_profile(u.id).window_set
    assert final_set not in ("set1", tip.window_set)
    kept = pw.set_windows(u.id, final_set)
    # The windows before the second import: the first re-tip's copies and
    # the windows it read again, kept as they are.
    assert [w.source_data_cutoff for w in kept[:2]] == [
        r.source_data_cutoff for r in rows[:2]]
    assert any(w.source_data_cutoff > first
               and w.source_data_cutoff < second for w in kept)
    u = db.session.get(User, u.id)
    assert u.profile_needs_full_regen is False
    assert u.profile_import_retip_from is None


def test_the_seeders_gate_applies_an_import_date_that_waited(world):
    """The advance that would apply the date may not run (it waited for
    the imports to stop, or for the spend cap): the seeder's gate applies
    it, and opens for the re-tipped set's unread range."""
    u = _user()
    rows = _set_of(u, 6)
    _corpus_for(world, rows)
    _finish(world, u)
    inside = _import_inside(world, rows, 3)
    u.profile_import_retip_from = inside   # waited for windows since collected
    db.session.commit()
    assert pb._should_seed(u, ignore_backoff=True)
    u = db.session.get(User, u.id)
    assert u.profile_import_retip_from is None
    tip = ex.profile_update_base(u.id)
    assert tip.window_set != "set1"
    assert tip.source_data_cutoff == rows[2].source_data_cutoff


def test_the_synchronous_check_applies_an_import_date_that_waited(
        app, world, monkeypatch):
    """An account off the batch path: the hourly synchronous check applies
    the date and starts the update, as the import's hand-off would have."""
    app.config["PROFILE_USE_BATCH"] = False
    u = _user()
    rows = _set_of(u, 6)
    _corpus_for(world, rows)
    _finish(world, u)
    inside = _import_inside(world, rows, 3)
    u.profile_import_retip_from = inside
    u.profile_force_batch = False         # the advance pinned it
    db.session.commit()
    trigger = MagicMock(return_value="t-sync")
    monkeypatch.setattr(ex, "maybe_trigger_profile_update", trigger)
    assert ex.maybe_trigger_incremental_profile_update(u) == "t-sync"
    assert trigger.call_args.kwargs == {"force_full_regen": False}
    assert db.session.get(User, u.id).profile_import_retip_from is None
    assert ex.profile_update_base(u.id).window_set != "set1"


def test_an_import_date_without_a_profile_version_is_dropped(world):
    """Nothing was saved of the build the date waited for (every window
    failed): the next build is from scratch and reads everything anyway,
    so the date flags nothing."""
    u = _user()
    u.profile_import_retip_from = T0
    db.session.commit()
    assert ex.apply_waiting_import_date(u) == "none"
    assert u.profile_import_retip_from is None
    assert u.profile_needs_full_regen is False


def test_an_import_date_under_a_flagged_rebuild_is_dropped(world):
    """A rebuild from scratch is already flagged (Build profile, the
    ladder): it reads everything, so the date re-tips nothing."""
    u = _user()
    rows = _set_of(u, 6)
    _corpus_for(world, rows)
    _finish(world, u)
    u.profile_import_retip_from = _import_inside(world, rows, 3)
    u.profile_needs_full_regen = True
    db.session.commit()
    assert ex.apply_waiting_import_date(u) == "none"
    assert u.profile_import_retip_from is None
    assert ex.profile_update_base(u.id).window_set == "set1"


# ── 16. restarts ──────────────────────────────────────────────────────────

def test_an_exception_mid_render_submits_nothing(world, monkeypatch):
    world.corpus = _windows_corpus(4)
    real = world.corpus.export
    calls = [0]

    def flaky(*a, **k):
        calls[0] += 1
        if calls[0] == 3:
            raise RuntimeError("synthetic render failure")
        return real(*a, **k)
    monkeypatch.setattr(pw._exports, "build_user_export_content", flaky)
    u = _user(profile_needs_full_regen=True)
    assert _advance(u) == "error"
    assert world.provider.submits == []
    db.session.refresh(u)
    assert u.profile_batch_pending is False
    assert "synthetic" not in (u.profile_seed_error or "")
    assert _advance(u) == "submitted"
    assert _items("window")[0]["start"] is None


def test_a_set_whose_second_slice_never_went_out_continues(app, world):
    world.corpus = _windows_corpus(6)
    app.config["PROFILE_WINDOW_RENDER_SECONDS"] = 0
    u = _user(profile_needs_full_regen=True)
    _advance(u)                                  # one window, then stops
    _poll()                                      # its row lands
    app.config["PROFILE_WINDOW_RENDER_SECONDS"] = 2400
    first = _rows(u, "window")
    assert len(first) == 1
    _advance(u)
    items = [i for i in _items("window") if i.get("outcome") is None]
    assert items and items[0]["start"] == first[0].source_data_cutoff.isoformat()
    assert {i["set"] for i in items} == {first[0].window_set}


# ── #613: an advance killed again and again ───────────────────────────────

def test_an_advance_killed_again_and_again_is_a_failed_build(app):
    """HeavyTask's stop for the advance: recorded as a failed build in the
    admin's Profile column, the guard cleared, nothing dispatched; the
    hourly seeder starts the build again."""
    u = _user(profile_generation_task_id="t-killed",
              profile_generation_task_dispatched_at=datetime.utcnow(),
              profile_batch_pending=True)
    pw.stop_killed_advance(u.id, "t-killed")
    db.session.refresh(u)
    assert u.profile_seed_error == "Profile window build failed: HeavyJobKilled"
    assert u.profile_generation_task_id is None
    assert u.profile_generation_task_dispatched_at is None
    assert u.profile_batch_pending is False   # nothing in flight
    assert not pw.advance_profile_windows.apply_async.called


def test_a_killed_copy_leaves_a_newer_builds_guard_alone(app):
    u = _user(profile_generation_task_id="t-new",
              profile_generation_task_dispatched_at=datetime.utcnow())
    pw.stop_killed_advance(u.id, "t-old")
    db.session.refresh(u)
    assert u.profile_generation_task_id == "t-new"
    assert u.profile_seed_error is None


# ── 17. progress and holds ────────────────────────────────────────────────

def test_progress_labels_for_the_three_kinds(app):
    from backend.routes import export_data as ed
    u = _user(profile_batch_pending=True)
    for kind, message, progress in (
            ("window", "Generating profile: reading all 157 chunks", 20),
            ("group", "Integrating profile versions", 80),
            ("final", "Integrating profile versions", 95)):
        ProfileBatchJob.query.delete()
        db.session.add(ProfileBatchJob(
            provider_key="anthropic", batch_id="b", status="pending",
            items=[{"custom_id": "c", "user_id": u.id, "kind": kind,
                    "total": 157}]))
        db.session.commit()
        out = ed._batch_progress(u)
        assert (out["message"], out["progress"]) == (message, progress)


def test_groups_and_the_final_carry_a_read_time_taken_before_the_read(
        world, monkeypatch):
    """Review of #630: a group's or the final's read time must not come
    after the read (the job's submission and a stamp after the build both
    do), or work that read content deleted meanwhile is saved. The pass
    stamps it before it reads anything."""
    u = _user()
    rows = _set_of(u, 65)
    _corpus_for(world, rows)
    built = []
    real = pw._merge_messages

    def merge_messages(*a, **k):
        built.append(datetime.utcnow())
        return real(*a, **k)
    monkeypatch.setattr(pw, "_merge_messages", merge_messages)
    _advance(u)
    groups = _items("group")
    assert len(groups) == 3 and built
    assert all(datetime.fromisoformat(g["read_at"]) <= min(built)
               for g in groups)
    _poll()
    built.clear()
    _advance(u)
    final = _items("final")[0]
    assert datetime.fromisoformat(final["built_at"]) <= min(built)


def test_windows_read_before_a_content_deletion_are_dropped_and_costed(
        world, monkeypatch):
    """#590: the windows were rendered before "Delete all my content": no
    row is saved, each billed result gets its cost row, and the items
    are marked dropped for the window pipeline's history."""
    from backend.models import APICostLog, ProfileBatchJob, UserDataPurge
    world.corpus = _windows_corpus(2)
    u = _user(profile_needs_full_regen=True)
    _advance(u)
    now = datetime.utcnow() + timedelta(seconds=1)
    db.session.add(UserDataPurge(
        user_id=u.id, source="self", scope="hidden", status="scheduled",
        requested_at=now, first_requested_at=now,
        scheduled_for=now + timedelta(days=30)))
    db.session.commit()
    sent = [i for j in ProfileBatchJob.query.all() for i in j.items
            if i.get("user_id") == u.id]
    _poll()
    assert _rows(u, "window") == []
    db.session.refresh(u)
    assert u.profile_batch_pending is False
    assert APICostLog.query.filter_by(user_id=u.id).count() == len(sent)
    outcomes = [i.get("outcome") for j in ProfileBatchJob.query.all()
                for i in j.items if i.get("user_id") == u.id]
    assert outcomes and set(outcomes) == {"dropped"}


def test_kill_switch_off_bases_updates_outside_any_set(app, world):
    u = _user()
    chain = _profile(u, gen_type="update", cutoff=T0)
    rows = _set_of(u, 3, start=T0 + timedelta(days=1))
    assert ex.profile_update_base(u.id).id == rows[-1].id
    app.config["PROFILE_INDEPENDENT_VERSIONS"] = False
    assert ex.profile_update_base(u.id).id == chain.id


def test_kill_switch_off_updates_a_sets_final_on_the_chained_path(
        app, world):
    """Review of #589, finding 5: with the switch off, a user whose profile
    is a set's final keeps it as the base: no from-scratch chained
    rebuild, and the next update chains on the final."""
    u = _user()
    rows = _set_of(u, 5, created=datetime.utcnow() - timedelta(hours=3))
    final = _profile(u, gen_type="integration",
                     cutoff=rows[-1].source_data_cutoff, units=14_100_000,
                     window_set="set1", parent_profile_id=rows[-1].id,
                     created_at=datetime.utcnow() - timedelta(hours=2))
    n = Node(user_id=u.id, node_type="user", ai_usage="chat",
             token_count=90_000, created_at=T0 + timedelta(hours=1))
    n.set_content("synthetic writing")
    db.session.add(n)
    db.session.commit()
    app.config["PROFILE_INDEPENDENT_VERSIONS"] = False
    assert ex.profile_update_base(u.id).id == final.id
    assert pb._should_seed(u) is False
    db.session.refresh(u)
    assert not u.profile_needs_full_regen
    world.corpus = Corpus(85, start=rows[-1].source_data_cutoff
                          + timedelta(hours=1))
    req = pb._build_next_profile_request(u)
    assert req["meta"]["kind"] == "chunk"
    assert req["meta"]["prev_profile_id"] == final.id
    assert req["meta"]["generation_type"] == "update"
    # A version outside the set that is newer than the final is the base.
    later = _profile(u, gen_type="update", cutoff=rows[-1].source_data_cutoff
                     + timedelta(days=1), parent_profile_id=final.id)
    assert ex.profile_update_base(u.id).id == later.id


def test_the_seeder_gate_opens_for_a_set_whose_final_is_due(world):
    u = _user()
    rows = _set_of(u, 5, created=datetime.utcnow())
    _corpus_for(world, rows)
    # Within the hourly interval and with no new writing: only the set's
    # own work (its final) opens the gate.
    assert pb._should_seed(u) is True
    _advance(u)
    _poll()
    assert pb._should_seed(u) is False


def test_the_estimate_for_a_large_anthropic_build(app, world):
    """At the expected sizes, about $2.5 of Haiku windows (each under its
    100k tier) plus about $3.8 of Opus merges for 14.1M units: the
    estimate prices every request in its own tier."""
    u = _user()
    models = pw.profile_models_for(u)
    state = pw.SetState("s", None, [], [], [])
    n = 330
    est = pw.estimate_cost_usd(u, state, models, (n, 14_100_000))
    assert 5.5 < est < 7
    # Flat falls back to groups above the merge model's cap, and the
    # estimate with it.
    app.config["PROFILE_MERGE_STRUCTURE"] = "flat"
    assert pw.estimate_cost_usd(u, state, models, (n, 14_100_000)) == est


# ── 14. a from-scratch build that fits one chunk (Peter, 2026-11-03) ──────
# One window version (with the events paragraph) and one integration of it,
# where the provider's PROFILE_ONE_CHUNK_WINDOW_* is on. Private writing:
# the user's chat model for both steps (Peter, 2026-11-05).

def _one_chunk_user(app, **kw):
    """An OpenAI user with a from-scratch build due, the OpenAI switch on
    and the shipped merge structure."""
    app.config["PROFILE_ONE_CHUNK_WINDOW_OPENAI"] = True
    app.config["PROFILE_MERGE_STRUCTURE"] = "flat"
    kw.setdefault("profile_needs_full_regen", True)
    return _user(preferred_model="gpt-6-astra", **kw)


def _finals(user):
    return [f for f in _rows(user, "integration") if f.window_set]


def _all_requests(world):
    return [r for s in world.provider.submits for rs in s.values()
            for r in rs]


def test_a_one_chunk_build_is_one_window_and_its_integration(app, world):
    world.corpus = Corpus(50)                    # 50k units: one chunk
    u = _one_chunk_user(app)
    assert pw.wants_window_build(u)
    _advance(u)
    reqs = world.provider.requests()
    assert len(reqs) == 1 and reqs[0]["api_model"] == "gpt-6-astra"
    assert ex.EVENTS_IN_WINDOW_PARAGRAPH in (
        reqs[0]["messages"][0]["content"][0]["text"])
    assert _items("window")[0]["total"] == 1
    _poll()
    windows = _rows(u, "window")
    assert len(windows) == 1 and windows[0].covers_start is None
    from backend.routes.dashboard import get_latest_profile
    assert get_latest_profile(u) is None         # no profile until the final
    _advance(u)
    final_reqs = [r for r in _all_requests(world) if "_f_" in r["custom_id"]]
    assert len(final_reqs) == 1
    req = final_reqs[0]
    assert req["api_model"] == "gpt-6-astra"
    assert req["endpoint"] == "/v1/responses"
    # No reasoning effort on the chat model, as the live replies send.
    assert "reasoning_effort" not in req
    msgs = req["messages"]
    assert msgs == pw._exports.single_integration_messages(u.id, windows[0])
    assert len(msgs) == 2
    label, stored = msgs[0]["content"][0]["text"].split("\n\n", 1)
    assert label == ("Profile No. 1\n- Covers the writing from 2024-01-01 "
                     "to 2024-01-03")
    assert stored == windows[0].get_content()
    assert DATE_LINE_RE.match(stored)
    assert msgs[1]["content"][0]["text"] == (
        pw._exports.single_integration_prompt(u.id))
    _poll()
    finals = _finals(u)
    assert len(finals) == 1
    f = finals[0]
    assert (f.window_set, f.parent_profile_id) == (
        windows[0].window_set, windows[0].id)
    assert f.generated_by == "gpt-6-astra"
    assert f.source_tokens_used == windows[0].source_tokens_used == 50_000
    assert get_latest_profile(u)["id"] == f.id
    # Done: nothing more goes out.
    n = len(world.provider.submits)
    _advance(u)
    assert len(world.provider.submits) == n
    # The user's writing reached their own chat model only.
    assert {r["api_model"] for r in _all_requests(world)} == {"gpt-6-astra"}


def test_the_one_chunk_switch_is_per_provider(app, world):
    world.corpus = Corpus(50)
    oai = _user(preferred_model="gpt-6-astra", profile_needs_full_regen=True)
    anth = _user(profile_needs_full_regen=True)
    # A config without the keys: the chained path for both.
    assert not pw.wants_window_build(oai)
    assert not pw.wants_window_build(anth)
    app.config["PROFILE_ONE_CHUNK_WINDOW_OPENAI"] = True
    assert pw.wants_window_build(oai) and not pw.wants_window_build(anth)
    app.config["PROFILE_ONE_CHUNK_WINDOW_ANTHROPIC"] = True
    assert pw.wants_window_build(anth)
    app.config["PROFILE_ONE_CHUNK_WINDOW_OPENAI"] = False
    assert not pw.wants_window_build(oai)
    # The kill switch turns it off with the rest of the window path.
    app.config["PROFILE_INDEPENDENT_VERSIONS"] = False
    assert not pw.wants_window_build(anth)


def test_the_shipped_one_chunk_defaults(app, world, monkeypatch):
    """Both providers on (Peter, 2026-11-03: "yes, we should do the same
    for Ant"): the seeder hands a one-chunk build of either provider to
    the window advance instead of a chained chunk."""
    from backend.config import Config
    assert Config.PROFILE_ONE_CHUNK_WINDOW_OPENAI is True
    assert Config.PROFILE_ONE_CHUNK_WINDOW_ANTHROPIC is True
    for key in ("PROFILE_ONE_CHUNK_WINDOW_OPENAI",
                "PROFILE_ONE_CHUNK_WINDOW_ANTHROPIC"):
        app.config[key] = getattr(Config, key)
    world.corpus = Corpus(50)
    monkeypatch.setattr(pb._exports, "count_remaining_units",
                        lambda uid, cutoff=None: world.corpus.count(uid, cutoff))
    for n, model in enumerate((None, "gpt-6-astra"), 1):
        u = _user(preferred_model=model, profile_needs_full_regen=True)
        _seed(u)
        assert world.provider.submits == []      # no chained chunk
        assert pw.advance_profile_windows.apply_async.call_count == n
        db.session.refresh(u)
        assert u.profile_batch_pending and u.profile_force_batch


@pytest.mark.parametrize("units_k, ratio", [
    (30, 3.1), (31, 3.1), (100, 3.1),
    (134, 3.1),        # the largest one-chunk build on Opus
    (40, None),        # the threads prior
])
def test_an_anthropic_one_chunk_build_is_one_window_on_the_chat_model(
        app, world, units_k, ratio):
    """A build that fits one chunk on the chat model (Opus 5.5, rounded at
    the planner's 90k target) is one window on Opus and Opus's
    single-version integration (Peter, 2026-11-05: the user's model
    writes both), at any size up to that chunk; no Haiku window."""
    app.config["PROFILE_ONE_CHUNK_WINDOW_ANTHROPIC"] = True
    app.config["PROFILE_MERGE_STRUCTURE"] = "flat"
    kw = ({"profile_token_ratio": ratio,
           "profile_token_ratio_family": "claude_new"} if ratio else {})
    u = _user(profile_needs_full_regen=True, **kw)
    world.corpus = Corpus(units_k)
    # One chunk on the chat model (Opus): the switch decides the path.
    assert pw.wants_window_build(u)
    app.config["PROFILE_ONE_CHUNK_WINDOW_ANTHROPIC"] = False
    assert not pw.wants_window_build(u)
    app.config["PROFILE_ONE_CHUNK_WINDOW_ANTHROPIC"] = True
    _advance(u)
    window_reqs = world.provider.requests()
    assert len(window_reqs) == 1
    assert window_reqs[0]["api_model"] == "claude-opus-5-5"
    assert _items("window")[0]["units"] == units_k * 1000
    _poll()
    [row] = _rows(u, "window")
    _advance(u)
    final = [r for r in _all_requests(world) if "_f_" in r["custom_id"]]
    assert len(final) == 1 and final[0]["api_model"] == "claude-opus-5-5"
    assert final[0]["messages"] == (
        pw._exports.single_integration_messages(u.id, row))
    _poll()
    assert len(_finals(u)) == 1
    assert {r["api_model"] for r in _all_requests(world)} == {
        "claude-opus-5-5"}


def test_a_build_of_two_chat_model_chunks_takes_the_providers_pair(
        app, world):
    """Just above one Opus chunk (136k units round to two) the build has
    more than one window: Haiku windows at its 100k tier and the Opus
    merge of several versions (#589's rule for private writing)."""
    app.config["PROFILE_ONE_CHUNK_WINDOW_ANTHROPIC"] = True
    app.config["PROFILE_MERGE_STRUCTURE"] = "flat"
    u = _user(profile_needs_full_regen=True, profile_token_ratio=3.1,
              profile_token_ratio_family="claude_new")
    world.corpus = Corpus(136)
    _finish(world, u)
    windows = [r for r in _all_requests(world) if "_w" in r["custom_id"]]
    assert len(windows) == 5
    assert {r["api_model"] for r in windows} == {"claude-haiku-5-5"}
    [final] = [r for r in _all_requests(world) if "_f_" in r["custom_id"]]
    assert final["api_model"] == "claude-opus-5-5"
    rows = _rows(u, "window")
    assert final["messages"] == pw._exports._integration_messages(
        u.id, rows, independent=True)


def test_the_sync_task_hands_a_one_chunk_build_to_the_advance(app, world):
    world.corpus = Corpus(50)
    u = _one_chunk_user(app)
    result = ex.window_hand_off(u, "sync-task-1")
    assert result is not None and result["status"] == "completed"
    assert [r["api_model"] for r in world.provider.requests()] == [
        "gpt-6-astra"]


def test_a_chained_one_chunk_update_stays_chained_with_the_switch_on(
        app, world):
    world.corpus = Corpus(85, start=T0 + timedelta(days=400))
    u = _one_chunk_user(app, profile_needs_full_regen=False)
    _profile(u, gen_type="update", cutoff=T0 + timedelta(days=1),
             units=100_000)
    assert not pw.wants_window_build(u)


def test_the_single_version_prompt(app):
    u = _user()
    prompt = ex.single_integration_prompt(u.id)
    gen = ex._load_prompt("profile_generation.txt", user_id=u.id)
    structure = ex.profile_structure_instructions(gen)
    assert prompt.count(structure) == 1
    own = prompt.replace(structure, "")
    assert "{" not in own and "}" not in own
    assert own.startswith("The above is a profile of the user")
    assert prompt.endswith(
        '\n\n## OUTPUT\nGenerate the profile now. No preamble, no '
        'meta-commentary. Begin directly with "### SURFACE MAP"')
    # (a) none of the many-version wording that leaked into finals.
    for phrase in ("series", "update", "80k", "110k", "unif", "trajectory",
                   "evolution", "months", "Profile No"):
        assert phrase not in own, phrase
    # (b) the events list is named, moved into the Surface Map, left out.
    assert '"### EVENTS IN THIS WINDOW"' in own
    assert "into the Surface Map, each with its date" in own
    assert "with no events section" in own
    # (c) no claims of the merge model's own.
    assert "add no fact, outcome or trend of your own" in own
    assert ex.EVENTS_IN_WINDOW_PARAGRAPH not in prompt


def test_the_single_version_prompt_embeds_the_users_own_structure(
        app, monkeypatch):
    """The user's own generation prompt shapes the profile, as in every
    merge; their own integration prompt (written for many versions) does
    not replace this one."""
    own_gen = "My own maps.\n\n## OUTPUT\nGo.\n\n## INPUT\n{user_export}"
    own_integ = ("MY OWN INTEGRATION {profile_generation_prompt}\n\n"
                 "## OUTPUT\nGo.")
    import backend.utils.prompts as prompts
    monkeypatch.setattr(prompts, "get_user_prompt", lambda uid, key: {
        "profile_generation": own_gen,
        "profile_integration": own_integ}.get(key))
    u = _user()
    prompt = ex.single_integration_prompt(u.id)
    assert "My own maps." in prompt and "## INPUT" not in prompt
    assert "MY OWN INTEGRATION" not in prompt
    w = _profile(u, gen_type="window", window_set="s", covers_start=None)
    many = ex._integration_messages(u.id, [w, w], independent=True)
    assert many[-1]["content"][0]["text"].startswith("MY OWN INTEGRATION")


def test_the_many_version_prompt_is_unchanged(app, world):
    """#586's prompt file, byte for byte, and a set of two windows (a
    one-window build after its first update) is merged with it."""
    import hashlib
    path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "prompts", "profile_integration.txt")
    with open(path, "rb") as fh:
        assert hashlib.sha256(fh.read()).hexdigest() == (
            "2d12819e3c450c74c0c65db1b3a70516eb1a9272b99c0eb786c47568f27a4e52")
    app.config["PROFILE_MERGE_STRUCTURE"] = "flat"
    u = _user(preferred_model="gpt-6-astra")
    rows = _set_of(u, 2)
    _corpus_for(world, rows)
    _advance(u)
    msgs = _merge_reqs(world)[0]["messages"]
    assert msgs == pw._exports._integration_messages(
        u.id, rows, independent=True)
    assert msgs[-1]["content"][0]["text"] != ex.single_integration_prompt(
        u.id)
    assert not pw.single_version(rows) and pw.single_version(rows[:1])


def test_output_problem_rejects_an_events_section_in_a_final():
    gen = "SURFACE MAP SHADOW MAP LIGHT MAP"
    assert ex.profile_output_problem(_good(events=False), gen,
                                     final=True) is None
    assert ex.profile_output_problem(_good(events=True), gen, final=True) == (
        "an EVENTS IN THIS WINDOW heading in a final profile")
    lower = _good(events=False) + "\n## Events in this window\n- x\n"
    assert ex.profile_output_problem(lower, gen, final=True)
    # A group or a chained step is not checked for it; a window has one.
    assert ex.profile_output_problem(_good(events=True), gen) is None
    assert ex.profile_output_problem(_good(events=True), gen,
                                     events=True) is None


def test_a_final_with_an_events_section_is_refused(app, world):
    world.corpus = Corpus(50)
    u = _one_chunk_user(app)
    world.provider.respond = lambda cid, req: {
        "content": _good(events=True, tag=cid), "input_tokens": 1000,
        "output_tokens": 500, "batch": True}
    _advance(u)
    _poll()
    assert len(_rows(u, "window")) == 1
    _advance(u)
    costs = APICostLog.query.filter_by(user_id=u.id).count()
    _poll()
    assert _items("final")[0]["outcome"] == "refused"
    assert not _finals(u)
    assert APICostLog.query.filter_by(user_id=u.id).count() == costs + 1
    # The integration rule: no retry within the hour.
    n = len(world.provider.submits)
    _advance(u)
    assert len(world.provider.submits) == n


def test_an_edit_during_a_one_window_build_is_merged_in_its_final(
        app, world):
    """The user's edit wins: a version the user saves while the window is
    in the batch is merged into the final, with the many-version prompt
    (two inputs) and its "Written by the user" label."""
    world.corpus = Corpus(50)
    u = _one_chunk_user(app)
    old = _profile(u, gen_type="initial", cutoff=T0 + timedelta(hours=10),
                   units=5_000)
    _advance(u)                                  # the window goes out
    edit = _edit(u, old)
    _poll()
    windows = _rows(u, "window")
    assert len(windows) == 1
    _advance(u)
    msgs = _merge_reqs(world)[0]["messages"]
    assert len(msgs) == 3
    labels = [m["content"][0]["text"].split("\n\n", 1)[0] for m in msgs[:-1]]
    assert "Written by the user" in labels[0]
    assert msgs == pw._exports._integration_messages(
        u.id, [edit, windows[0]], independent=True)
    _poll()
    finals = _finals(u)
    assert len(finals) == 1 and finals[0].merged_profile_id == edit.id


def test_a_provisional_one_window_set_is_rebuilt_from_scratch(
        app, world, monkeypatch):
    """The ladder, not the 80k gate, replaces a provisional one-window
    set, as it replaces a chained provisional profile."""
    world.corpus = Corpus(5)                     # 5k units: the first rung
    u = _one_chunk_user(app)
    _finish(world, u)
    first = _finals(u)
    assert len(first) == 1
    tip = ex.profile_update_base(u.id)
    assert tip.generation_type == "window" and ex.profile_is_provisional(tip)
    world.corpus.add(85, start=datetime.utcnow() + timedelta(days=2))
    assert not pw.set_work_due(u, tip)
    tip.created_at = datetime.utcnow() - timedelta(hours=2)
    db.session.commit()
    for mod in (ex, pw._exports, pb._exports):
        monkeypatch.setattr(mod, "_estimate_source_tokens",
                            lambda user: world.corpus.count(user.id))
    assert pb._should_seed(u) is True
    db.session.refresh(u)
    assert u.profile_needs_full_regen
    assert pw.wants_window_build(u)
    _finish(world, u)
    finals = _finals(u)
    assert len(finals) == 2
    assert finals[-1].window_set != first[0].window_set
    newest = [w for w in _rows(u, "window")
              if w.window_set == finals[-1].window_set]
    assert len(newest) == 1 and newest[0].covers_start is None
    assert newest[0].source_tokens_used == 90_000


def test_a_one_window_set_past_the_ladder_is_updated_as_a_set(app, world):
    """Its later updates are the set's. A regular one-window update is a
    chained update by the user's chat model (Peter, 2026-11-05), which
    stands in the window's place, and the final over the window and the
    chained version runs on the chat model with the many-version prompt."""
    world.corpus = Corpus(100)                   # one chunk, past the ladder
    u = _one_chunk_user(app)
    _finish(world, u)
    assert len(_rows(u, "window")) == 1 and len(_finals(u)) == 1
    world.corpus.add(85, start=datetime.utcnow() + timedelta(days=2))
    assert pw.wants_window_build(u)
    _finish(world, u)
    [window] = _rows(u, "window")
    [chained] = _rows(u, "window_update")
    assert chained.covers_start == window.source_data_cutoff
    final_req = [r for r in _all_requests(world)
                 if "_f_" in r["custom_id"]][-1]
    assert final_req["messages"] == pw._exports._integration_messages(
        u.id, [window, chained], independent=True)
    assert final_req["api_model"] == "gpt-6-astra"
    assert len(_finals(u)) == 2


def test_a_one_window_build_whose_window_is_given_up_sends_no_final(
        app, world, monkeypatch):
    """Nothing was read, so nothing is merged: a merge of the skip note
    alone would be saved as the user's profile."""
    monkeypatch.setattr(pw, "_report", lambda title, **tags: None)
    world.corpus = Corpus(50)
    u = _one_chunk_user(app)
    world.provider.respond = lambda cid, req: {
        "content": "I can't help with that.", "input_tokens": 1000,
        "output_tokens": 20, "batch": True}
    _finish(world, u)
    assert len(_rows(u, "window_skip")) == 1 and not _rows(u, "window")
    assert not _items("final") and not _finals(u)
    db.session.refresh(u)
    assert u.profile_seed_error == pw.NOTHING_READ_NOTE
    assert not pw.set_work_due(u, ex.profile_update_base(u.id))


def _other_batch(provider_key="openai:gpt-6-astra"):
    """Another account's batch of ours, pending in the same queue (the
    one-chunk user's chat model, GPT-6 Astra, reads the window)."""
    other = _user()
    job = ProfileBatchJob(
        provider_key=provider_key, batch_id="other", status="pending",
        items=[{"custom_id": f"profile_{other.id}_x", "user_id": other.id,
                "kind": "window", "set": "zz", "start": None}],
        submitted_at=datetime.utcnow())
    db.session.add(job)
    db.session.commit()
    return job


def _queue_full_except_other(bids, keys, known=None):
    """A collect stand-in: every batch failed on a full queue, except the
    other account's batch ("other"), which is still in progress."""
    from backend.utils.llm_batch import CollectedResults
    out = CollectedResults()
    if "other" in bids.values():
        return out, dict(bids), {}
    out.queue_full.update(bids)
    return out, {}, {}


def test_a_window_refused_on_a_full_queue_waits_and_is_never_stopped(
        app, world, monkeypatch):
    """The provider refuses the create (its batch queue is full, other
    batches of ours wait in it): nothing is recorded against the window,
    so no number of such refusals stops or skips it; once the queue has
    room the build goes through."""
    from backend.utils.llm_batch import SubmittedBatches
    world.corpus = Corpus(50)
    u = _one_chunk_user(app)
    other = _other_batch()

    def full(*a, **k):
        out = SubmittedBatches()
        out.queue_full.update(world.provider.submit(*a, **k))
        return out
    monkeypatch.setattr(pb, "batch_submit", full)
    for _ in range(pb.MAX_BATCH_ATTEMPTS + 2):
        _advance(u)
    assert ProfileBatchJob.query.count() == 1     # no "missing" records
    assert pw.attempt_history(u.id) == {}
    db.session.refresh(u)
    assert u.profile_needs_full_regen and not u.profile_batch_pending
    assert (u.profile_batch_attempts or 0) == 0
    other.status = "collected"
    db.session.commit()
    monkeypatch.setattr(pb, "batch_submit",
                        lambda *a, **k: world.provider.submit(*a, **k))
    _finish(world, u)
    assert len(_finals(u)) == 1


def test_a_window_batch_failed_on_a_full_queue_waits(app, world,
                                                     monkeypatch):
    """OpenAI fails the batch on its enqueued-token limit while other
    batches of ours are queued: the items get the outcome "queue_full",
    which neither stops nor skips the window, and no advance is
    dispatched at once (the queue is still full)."""
    world.corpus = Corpus(50)
    u = _one_chunk_user(app)
    other = _other_batch()
    monkeypatch.setattr(pb, "batch_check_and_collect",
                        _queue_full_except_other)
    for _ in range(pw._content_attempts() + 2):
        _advance(u)
        pw.advance_profile_windows.apply_async.reset_mock()
        _poll()
        pw.advance_profile_windows.apply_async.assert_not_called()
    jobs = [j for j in ProfileBatchJob.query.all() if j.id != other.id]
    assert {j.status for j in jobs} == {"failed"}
    assert {i["outcome"] for j in jobs for i in j.items} == {"queue_full"}
    assert not _rows(u, "window_skip")
    state = pw.set_state(u, pw.resolve_set(u)[0], None)
    assert state.due and not state.stopped
    other.status = "collected"
    db.session.commit()
    monkeypatch.setattr(pb, "batch_check_and_collect",
                        lambda *a, **k: world.provider.collect(*a, **k))
    _finish(world, u)
    assert len(_finals(u)) == 1


def test_a_queue_full_window_batch_clears_the_pending_flag(app, world,
                                                           monkeypatch):
    """Review of #596 (its reproduction): after a window batch fails on a
    full queue nothing of the user's is in flight, so the flag is
    cleared: the seeder (and "Build profile") reaches the user at once,
    with no hourly clean-up of a stuck flag."""
    world.corpus = Corpus(50)
    u = _one_chunk_user(app)
    _other_batch()
    monkeypatch.setattr(pb, "batch_check_and_collect",
                        _queue_full_except_other)
    _advance(u)
    _poll()
    db.session.refresh(u)
    assert pw.inflight_items(u.id) == []
    assert u.profile_batch_pending is False
    pw.advance_profile_windows.apply_async.reset_mock()
    _seed(u)
    pw.advance_profile_windows.apply_async.assert_called_once()


def test_an_unactivated_prefills_window_sent_back_by_a_full_queue_is_resent(
        app, world, monkeypatch):
    """Peter, 2026-11-03 ("yes, implement it pls"): a pre-filled account
    not yet activated is outside the hourly cohort; a window batch a full
    queue sent back marks it (profile_work_held), the hourly seeder
    dispatches its advance, and the successful submit clears the mark."""
    world.corpus = Corpus(50)
    u = _one_chunk_user(app, profile_force_batch=True,
                        prefilled_handle="someone")
    u.approved = False
    db.session.commit()
    assert u.id not in {x.id for x in User.profile_eligible_query().all()}
    other = _other_batch()
    monkeypatch.setattr(pb, "batch_check_and_collect",
                        _queue_full_except_other)
    _advance(u)
    _poll()
    db.session.refresh(u)
    assert u.profile_work_held is True and not u.profile_batch_pending
    other.status = "collected"
    db.session.commit()
    pw.advance_profile_windows.apply_async.reset_mock()
    pb._seed_profile_batches()                           # the hourly seed
    calls = pw.advance_profile_windows.apply_async.call_args_list
    assert [c.args[0] for c in calls] == [(u.id,)]
    pw.advance(u.id, calls[0].kwargs["task_id"])         # the task it sent
    db.session.refresh(u)
    assert u.profile_work_held is False and u.profile_batch_pending
    assert [r["api_model"] for r in world.provider.requests()] == [
        "gpt-6-astra"]


def _unactivated_prefill(app):
    u = _one_chunk_user(app, profile_force_batch=True,
                        prefilled_handle="someone")
    u.approved = False
    db.session.commit()
    return u


def _respond_final_with(world, final_text):
    """Windows answer with a good version; a final with
    ``final_text(custom_id)`` (the fake provider answers every request it
    has seen on each poll, so the answer depends on the attempt's id)."""
    def respond(cid, req):
        content = (_good(tag=cid) if "_w" in cid else final_text(cid))
        return {"content": content, "input_tokens": 1000,
                "output_tokens": 500, "batch": True}
    world.provider.respond = respond


def _hour_later():
    """The refusal backoff's hour has passed: the cost rows are moved two
    hours back, and the saved versions three, so the refusals still count
    as a streak after the last saved version."""
    for row in APICostLog.query.all():
        row.created_at = row.created_at - timedelta(hours=2)
    for row in UserProfile.query.all():
        row.created_at = row.created_at - timedelta(hours=3)
    db.session.commit()


def _hourly_seed_dispatches(u):
    pw.advance_profile_windows.apply_async.reset_mock()
    pb._seed_profile_batches()
    calls = pw.advance_profile_windows.apply_async.call_args_list
    return [c for c in calls if c.args[0] == (u.id,)]


def test_an_unactivated_prefills_refused_final_is_retried_after_the_backoff(
        app, world):
    """Peter, 2026-11-03 ("yes pls"), the reviewer's case: the refused
    final waits out the backoff hour, and then the hourly seeder (whose
    cohort leaves the account out) sends it again because the account is
    held. The backoff itself is unchanged."""
    world.corpus = Corpus(50)
    u = _unactivated_prefill(app)
    _respond_final_with(world, lambda cid: _good(
        events=cid.endswith("_f_1"), tag=cid))   # the first one refused
    _advance(u)
    _poll()                                       # the window
    _advance(u)
    _poll()                                       # the final, refused
    assert _items("final")[0]["outcome"] == "refused"
    _advance(u)                                   # the advance it dispatched
    db.session.refresh(u)
    assert u.profile_work_held is True and not _finals(u)
    assert _hourly_seed_dispatches(u) == []       # within the backoff hour
    _hour_later()
    calls = _hourly_seed_dispatches(u)
    assert len(calls) == 1
    pw.advance(u.id, calls[0].kwargs["task_id"])  # the final goes out again
    _poll()
    db.session.refresh(u)
    assert len(_finals(u)) == 1 and u.profile_work_held is False


def test_an_unactivated_prefills_failed_window_is_resent_by_the_seeder(
        app, world):
    """A window of an unactivated prefill that failed (the advance that
    would resend it at once may be lost): the account is held, and the
    hourly seeder dispatches its advance."""
    world.corpus = Corpus(50)
    u = _unactivated_prefill(app)
    world.provider.respond = lambda cid, req: {
        "content": "I can't help with that.", "input_tokens": 1000,
        "output_tokens": 20, "batch": True}
    _advance(u)
    _poll()
    assert _items("window")[0]["outcome"] == "invalid"
    db.session.refresh(u)
    assert u.profile_work_held is True
    assert len(_hourly_seed_dispatches(u)) == 1


def test_a_deactivated_account_with_held_work_stays_paused(app, world):
    world.corpus = Corpus(50)
    u = _unactivated_prefill(app)
    u.profile_work_held = True
    u.deactivated_at = datetime.utcnow()
    db.session.commit()
    assert _hourly_seed_dispatches(u) == []


def test_giving_up_after_the_refusal_limit_releases_the_hold(app, world):
    """A final refused twice in a row stops (the refusal rule, unchanged):
    nothing waits any more, the hold goes, and the stop shows."""
    world.corpus = Corpus(50)
    u = _unactivated_prefill(app)
    _respond_final_with(world, lambda cid: _good(events=True, tag=cid))
    _advance(u)
    _poll()
    _advance(u)
    _poll()                                       # refused once
    _advance(u)
    db.session.refresh(u)
    assert u.profile_work_held is True
    _hour_later()
    calls = _hourly_seed_dispatches(u)
    pw.advance(u.id, calls[0].kwargs["task_id"])
    _poll()                                       # refused again: stopped
    _advance(u)
    db.session.refresh(u)
    assert u.profile_seed_error.startswith("Stopped")
    assert u.profile_work_held is False
    assert _hourly_seed_dispatches(u) == []


def _a_day_later():
    """The provider stop's day has passed: every job is moved 25 h back."""
    for job in ProfileBatchJob.query.all():
        job.submitted_at = job.submitted_at - timedelta(hours=25)
        if job.collected_at:
            job.collected_at = job.collected_at - timedelta(hours=25)
    db.session.commit()


def _missing_three_times(u, marker):
    """Collect rounds until the merge whose custom id holds ``marker`` has
    failed at the provider three times (no result), running each advance
    the collector dispatched."""
    for _ in range(int(pw._setting("PROFILE_VERSION_PROVIDER_ATTEMPTS", 3))):
        _advance(u)
        _poll()
    _advance(u)                       # finds the merge stopped: idle
    outcomes = [i["outcome"] for i in _items()
                if marker in i["custom_id"]]
    assert outcomes == ["missing"] * 3


def test_an_unactivated_prefills_stopped_final_is_resent_after_the_day(
        app, world, monkeypatch):
    """Review of #596 (its reproduction): a final failed at the provider
    three times waits out the day's stop with the hold kept, and the
    hourly seeder then sends it again for an account not yet activated.
    The stop itself is unchanged."""
    monkeypatch.setattr(pw, "_report", lambda title, **tags: None)
    world.corpus = Corpus(50)
    u = _unactivated_prefill(app)
    world.provider.respond = lambda cid, req: None if "_f_" in cid else {
        "content": _good(tag=cid), "input_tokens": 1000,
        "output_tokens": 500, "batch": True}
    _advance(u)
    _poll()                                       # the window is saved
    _missing_three_times(u, "_f_")
    db.session.refresh(u)
    assert u.profile_work_held is True
    # Within the day the stop holds: an hourly pass sends nothing, and the
    # hold stays.
    n = len(world.provider.submits)
    for c in _hourly_seed_dispatches(u):
        pw.advance(u.id, c.kwargs["task_id"])
    db.session.refresh(u)
    assert len(world.provider.submits) == n and u.profile_work_held is True
    _a_day_later()
    calls = _hourly_seed_dispatches(u)
    assert len(calls) == 1
    assert pw.advance(u.id, calls[0].kwargs["task_id"]) == "submitted"
    assert [r["custom_id"] for r in world.provider.requests()][-1].endswith(
        "_f_4")


def test_an_unactivated_prefills_stopped_group_is_resent_after_the_day(
        app, world, monkeypatch):
    """The same for a group of a grouped set."""
    monkeypatch.setattr(pw, "_report", lambda title, **tags: None)
    app.config["PROFILE_MERGE_GROUP_SIZE"] = 2       # structure "grouped"
    u = _user(profile_force_batch=True, prefilled_handle="someone")
    u.approved = False
    db.session.commit()
    rows = _set_of(u, 3)
    _corpus_for(world, rows)
    world.provider.respond = lambda cid, req: None if "_g" in cid else {
        "content": _good(events=False, tag=cid), "input_tokens": 1000,
        "output_tokens": 500, "batch": True}
    _missing_three_times(u, "_g1_")
    db.session.refresh(u)
    assert u.profile_work_held is True
    _a_day_later()
    calls = _hourly_seed_dispatches(u)
    assert len(calls) == 1
    assert pw.advance(u.id, calls[0].kwargs["task_id"]) == "submitted"
    assert [r["custom_id"] for r in world.provider.requests()][-1].endswith(
        "_g1_4")


def test_a_held_set_with_nothing_due_is_released_by_its_advance(
        app, world):
    world.corpus = Corpus(50)
    u = _one_chunk_user(app)
    _finish(world, u)
    assert len(_finals(u)) == 1
    u.profile_work_held = True
    db.session.commit()
    assert _advance(u) == "idle"
    db.session.refresh(u)
    assert u.profile_work_held is False


def test_a_window_batch_over_the_limit_by_itself_stops_and_shows(
        app, world, monkeypatch):
    """Review of #596: a batch failed on the enqueued-token limit with no
    other batch of ours queued for that model is over the limit by
    itself: its window counts provider failures as before the queue rule,
    so it stops after three and the admin column says so."""
    world.corpus = Corpus(50)
    u = _one_chunk_user(app)
    _other_batch("openai:gpt-6.1-sol:responses")   # another model's queue
    monkeypatch.setattr(pb, "batch_check_and_collect",
                        _queue_full_except_other)
    monkeypatch.setattr(pw, "_report", lambda title, **tags: None)
    for _ in range(int(app.config["PROFILE_VERSION_PROVIDER_ATTEMPTS"])):
        _advance(u)
        _poll()
    outcomes = [i["outcome"] for j in ProfileBatchJob.query.all()
                for i in j.items if i["user_id"] == u.id]
    assert outcomes == ["missing"] * 3
    db.session.refresh(u)
    assert u.profile_seed_error.startswith(pw.STOP_NOTE)
    state = pw.set_state(u, pw.resolve_set(u)[0], None)
    assert state.stopped and not state.due


def test_progress_labels_for_a_one_window_build(app):
    from backend.routes import export_data as ed
    u = _user(profile_batch_pending=True)
    for kind, extra, message, progress in (
            ("window", {"total": 1}, "Generating profile", 20),
            ("final", {"inputs": [7]}, "Generating profile", 95),
            ("final", {"inputs": [7, 8]}, "Integrating profile versions",
             95)):
        ProfileBatchJob.query.delete()
        db.session.add(ProfileBatchJob(
            provider_key="openai:gpt-6-luna", batch_id="b", status="pending",
            items=[dict({"custom_id": "c", "user_id": u.id, "kind": kind},
                        **extra)]))
        db.session.commit()
        out = ed._batch_progress(u)
        assert (out["message"], out["progress"]) == (message, progress)


# ── content-policy blocks (2026-11-04) ───────────────────────────────────
# An item the provider's content-policy filter blocked is never sent
# again automatically. A blocked window is skipped (a window_skip row in
# its place) and the profile is finished from the other windows (Peter:
# "skip the flagged windows, but finish their profile"). A blocked group
# or final cannot be skipped: the account's build stops until the admin's
# confirmed Build profile lifts it.

def _blocking(world, monkeypatch, marker):
    """batch_check_and_collect that reports every item whose custom_id
    contains ``marker`` as blocked (bio_policy), as llm_batch does."""
    from backend.utils.llm_batch import CollectedResults

    def collect(batch_ids, keys, known=None):
        results, pending, durations = world.provider.collect(batch_ids, keys)
        out = CollectedResults()
        for cid, res in results.items():
            if marker in cid:
                out.blocked[cid] = "bio_policy"
            else:
                out[cid] = res
        return out, pending, durations
    monkeypatch.setattr(pb, "batch_check_and_collect", collect)


def _policy_reports(monkeypatch):
    from backend.utils import refusal_backoff
    reports = []
    monkeypatch.setattr(refusal_backoff, "report_policy_block",
                        lambda *a: reports.append(a))
    return reports


def _window_requests(world):
    return [r["custom_id"] for s in world.provider.submits
            for reqs in s.values() for r in reqs if "_w" in r["custom_id"]]


def test_a_policy_blocked_window_is_skipped_and_the_profile_finished(
        world, monkeypatch):
    from backend.models import APICostLog
    world.corpus = _windows_corpus(3)
    reports = _policy_reports(monkeypatch)
    _blocking(world, monkeypatch, "_w1_")
    u = _user(profile_needs_full_regen=True)

    _advance(u)
    windows_sent = len(_items("window"))
    assert windows_sent >= 2
    # Every request names its user (the OpenAI safety identifier).
    assert {r["user_id"] for r in world.provider.requests()} == {u.id}
    _poll()

    [blocked] = [i for i in _items("window") if i.get("outcome") == "blocked"]
    assert blocked["start"] is None
    # A skip row in the blocked window's place, over its real range.
    [skip] = _rows(u, "window_skip")
    assert skip.covers_start is None
    assert skip.source_data_cutoff == pw._dt(blocked["cutoff"])
    assert skip.get_content().endswith(ex.WINDOW_SKIPPED_TEXT)
    assert len(_rows(u, "window")) == windows_sent - 1
    db.session.refresh(u)
    assert u.profile_seed_error == (
        f"Skipped window start..{blocked['cutoff']}: provider policy block "
        f"(bio_policy)")
    # Recorded ($0) and reported, but nothing is stopped.
    row = APICostLog.query.filter_by(
        user_id=u.id, request_ref="policy_block:bio_policy").one()
    assert row.request_type == pw.WINDOW_BLOCK_REQUEST_TYPE
    assert row.cost_microdollars == 0
    assert [r[:2] for r in reports] == [(u.id, "bio_policy")]
    from backend.utils import refusal_backoff as rb
    assert rb.policy_block_for(u.id, rb.PROFILE_REQUEST_TYPES) is None

    # The next passes finish the profile over the other windows (the skip
    # row is one of the final's inputs) and never resend the window.
    windows_before = len(_window_requests(world))
    _build(world, u)
    assert len(_window_requests(world)) == windows_before
    [final] = _rows(u, "integration")
    final_item = [i for i in _items("final") if i.get("outcome") == "saved"][-1]
    assert skip.id in final_item["inputs"]
    assert final.window_set == skip.window_set
    # Days later nothing resends it either.
    for job in ProfileBatchJob.query.all():
        job.submitted_at -= timedelta(days=2)
    db.session.commit()
    assert _advance(u) == "idle"
    assert len(_window_requests(world)) == windows_before


def test_every_window_blocked_builds_no_profile(world, monkeypatch):
    world.corpus = _windows_corpus(2)
    _policy_reports(monkeypatch)
    _blocking(world, monkeypatch, "_w")
    u = _user(profile_needs_full_regen=True)
    _advance(u)
    windows_sent = len(_items("window"))
    _poll()
    assert len(_rows(u, "window_skip")) == windows_sent
    assert not _rows(u, "window")
    submits = len(world.provider.submits)
    assert _advance(u) == "idle"
    # No final over skip rows alone; the account shows why.
    assert len(world.provider.submits) == submits
    assert not _items("final") and not _rows(u, "integration")
    db.session.refresh(u)
    assert u.profile_seed_error == pw.NOTHING_READ_NOTE
    assert pb._should_seed(u) is False


def test_a_blocked_window_of_an_account_without_ai_writes_no_skip_row(
        world, monkeypatch):
    world.corpus = _windows_corpus(3)
    _policy_reports(monkeypatch)
    _blocking(world, monkeypatch, "_w1_")
    u = _user(profile_needs_full_regen=True)
    _advance(u)
    u.default_ai_usage = "none"       # switched while the batch ran
    db.session.commit()
    _poll()
    [blocked] = [i for i in _items("window") if i.get("outcome") == "blocked"]
    assert blocked["start"] is None
    assert not _rows(u, "window_skip")
    submits = len(world.provider.submits)
    assert _advance(u) == "off"
    assert len(world.provider.submits) == submits


def _age_jobs(days):
    for job in ProfileBatchJob.query.all():
        job.submitted_at -= timedelta(days=days)
        if job.collected_at:
            job.collected_at -= timedelta(days=days)
    db.session.commit()


def _unblock(world, monkeypatch):
    monkeypatch.setattr(pb, "batch_check_and_collect",
                        lambda *a, **k: world.provider.collect(*a, **k))


def _overlaps(item, blocked):
    """Whether a window item's range (start, cutoff] meets the blocked
    window's."""
    s, c = pw._dt(item["start"]), pw._dt(item["cutoff"])
    bs, bc = pw.blocked_range_of(blocked)
    lo = max(s or datetime.min, bs or datetime.min)
    hi = min(c or datetime.max, bc or datetime.max)
    return lo < hi


def _blocked_build(world, monkeypatch, marker, corpus=None, user=None):
    """Build a set whose window ``marker`` is blocked, to its final; then
    the provider blocks nothing more. Returns (user, blocked item)."""
    world.corpus = corpus or _windows_corpus(3)
    _policy_reports(monkeypatch)
    _blocking(world, monkeypatch, marker)
    u = user or _user(profile_needs_full_regen=True)
    _build(world, u)
    [blocked] = [i for i in _items("window") if i.get("outcome") == "blocked"]
    _unblock(world, monkeypatch)
    return u, blocked


def _range(blocked):
    """The blocked range a blocked window item leaves: from just before
    its earliest entry to its cutoff."""
    return pw.blocked_range_of(blocked)


def _never_resent(u, blocked, before):
    """No window sent after ``before`` meets the blocked range, and the
    newest set skips exactly that range without a request."""
    new = _items("window")[before:]
    assert not [i for i in new if _overlaps(i, blocked)], [
        (i["start"], i["cutoff"]) for i in new]
    tip_set = ex.profile_update_base(u.id).window_set
    skips = [r for r in _rows(u, "window_skip") if r.window_set == tip_set]
    assert [(pw._iso(r.covers_start), pw._iso(r.source_data_cutoff))
            for r in skips] == [tuple(pw._iso(x) for x in _range(blocked))]
    return new


def test_the_blocked_range_is_remembered_for_good(world, monkeypatch):
    from backend.models import ProfileBlockedRange
    u, blocked = _blocked_build(world, monkeypatch, "_w2_")
    [row] = ProfileBlockedRange.query.filter_by(user_id=u.id).all()
    # From just before the window's earliest entry, not its start.
    assert (row.range_start, row.range_end) == (
        pw._dt(blocked["earliest"]) - timedelta(microseconds=1),
        pw._dt(blocked["cutoff"]))
    assert row.code == "bio_policy" and row.lifted_at is None
    assert pw.blocked_ranges(u.id) == [(row.range_start, row.range_end)]


def test_cut_around_blocked():
    d = [datetime(2024, 1, i) for i in range(1, 10)]
    cut = pw.cut_around_blocked
    assert cut(d[0], d[8], []) == [(False, d[0], d[8])]
    assert cut(d[0], d[8], [(d[2], d[4])]) == [
        (False, d[0], d[2]), (True, d[2], d[4]), (False, d[4], d[8])]
    assert cut(None, None, [(None, d[3])]) == [
        (True, None, d[3]), (False, d[3], None)]
    # Clipped to the range; a range that ends inside a blocked one.
    assert cut(d[3], d[5], [(d[2], d[7])]) == [(True, d[3], d[5])]
    assert cut(d[3], None, [(d[1], d[2]), (d[4], d[5])]) == [
        (False, d[3], d[4]), (True, d[4], d[5]), (False, d[5], None)]


def test_a_rebuilt_set_skips_a_blocked_window_without_a_request(
        world, monkeypatch):
    u, blocked = _blocked_build(world, monkeypatch, "_w1_")
    before = len(_items("window"))
    u.profile_needs_full_regen = True      # e.g. Build profile
    db.session.commit()
    _build(world, u)
    assert _never_resent(u, blocked, before)
    assert len({r.window_set for r in _rows(u, "window_skip")}) == 2


def test_an_import_of_older_writing_does_not_resend_a_blocked_range(
        world, monkeypatch):
    """Review of #596: an import of older writing rebuilds from scratch
    and shifts every window start; the blocked range is still skipped
    exactly."""
    u, blocked = _blocked_build(world, monkeypatch, "_w2_")
    assert blocked["start"] is not None
    older = [(T0 - timedelta(hours=30 - i), 1000) for i in range(30)]
    world.corpus.rows = older + world.corpus.rows
    action, _v = ex.revert_profile_for_import(u.id, older[0][0])
    assert action == "full"
    db.session.commit()
    before = len(_items("window"))
    _build(world, u)
    assert _never_resent(u, blocked, before)
    assert _finals(u)


@pytest.mark.parametrize("days", [0, 8])
def test_new_writing_and_a_rebuild_do_not_resend_a_blocked_range(
        world, monkeypatch, days):
    """Review of #596: new writing resizes the windows of a rebuild (also
    one Build profile starts without the dialog), within the attempt
    history or long after it."""
    u, blocked = _blocked_build(world, monkeypatch, "_w2_")
    world.corpus.add(len(world.corpus.rows) // 5)
    _age_jobs(days)
    u.profile_needs_full_regen = True
    db.session.commit()
    before = len(_items("window"))
    _build(world, u)
    assert _never_resent(u, blocked, before)


def test_an_import_inside_a_blocked_range_is_skipped_with_it(
        world, monkeypatch):
    u, blocked = _blocked_build(world, monkeypatch, "_w2_")
    bs, bc = pw._dt(blocked["start"]), pw._dt(blocked["cutoff"])
    inside = bs + (bc - bs) / 2 + timedelta(minutes=7)
    world.corpus.rows = sorted(world.corpus.rows + [(inside, 1000)])
    action, _v = ex.revert_profile_for_import(u.id, inside)
    db.session.commit()
    u.profile_needs_full_regen = True
    db.session.commit()
    before = len(_items("window"))
    _build(world, u)
    assert _never_resent(u, blocked, before)


def test_ai_turned_back_on_long_after_does_not_resend_a_blocked_range(
        world, monkeypatch):
    """Review of #596: AI usage switched off while the batch ran (no skip
    row) and back on more than the attempt history later: the range is
    remembered all the same and skipped without a request."""
    world.corpus = _windows_corpus(3)
    _policy_reports(monkeypatch)
    _blocking(world, monkeypatch, "_w1_")
    u = _user(profile_needs_full_regen=True)
    _advance(u)
    u.default_ai_usage = "none"
    db.session.commit()
    _poll()
    [blocked] = [i for i in _items("window") if i.get("outcome") == "blocked"]
    assert not _rows(u, "window_skip")
    assert pw.blocked_ranges(u.id)          # recorded with AI off
    _unblock(world, monkeypatch)
    u.default_ai_usage = "chat"
    db.session.commit()
    _age_jobs(8)
    before = len(_items("window"))
    _build(world, u)
    assert _never_resent(u, blocked, before)
    assert _finals(u)


def _ladder_ready(world, monkeypatch, u, days):
    world.corpus.add(20, start=datetime.utcnow() + timedelta(days=2))
    for p in UserProfile.query.filter_by(user_id=u.id).all():
        p.created_at = datetime.utcnow() - timedelta(days=days)
    db.session.commit()
    for mod in (ex, pw._exports, pb._exports):
        monkeypatch.setattr(mod, "_estimate_source_tokens",
                            lambda user: world.corpus.count(user.id))
    monkeypatch.setattr(pb, "MIN_INACTIVITY", timedelta(0))


@pytest.mark.parametrize("days", [2, 8])
def test_the_ladder_rebuild_skips_only_the_blocked_range(
        app, world, monkeypatch, days):
    """Review of #596: a one-window set whose window was blocked built no
    profile; new writing takes it up the provisional ladder, within the
    attempt history or after it. The rebuild skips exactly the blocked
    range and reads the new writing, so a profile is built from it."""
    u, blocked = _blocked_build(world, monkeypatch, "_w", corpus=Corpus(5),
                                user=_one_chunk_user(app))
    assert not _finals(u)
    _ladder_ready(world, monkeypatch, u, days)
    if days > 7:
        _age_jobs(days)
    assert pb._should_seed(u)
    before = len(_items("window"))
    _build(world, u)
    new = _never_resent(u, blocked, before)
    assert new and all(pw._dt(i["start"]) >= pw._dt(blocked["cutoff"])
                       for i in new)
    assert _finals(u)


def test_lifting_the_blocked_ranges_reads_them_again(world, monkeypatch):
    """The one way to resend a blocked window: lift_blocked_ranges from a
    shell. No button does it."""
    u, blocked = _blocked_build(world, monkeypatch, "_w2_")
    assert pw.lift_blocked_ranges(u.id) == 1
    assert pw.blocked_ranges(u.id) == []
    u.profile_needs_full_regen = True
    db.session.commit()
    before = len(_items("window"))
    _advance(u)
    assert [i for i in _items("window")[before:] if _overlaps(i, blocked)]


def test_a_blocked_chained_chunk_is_remembered_as_a_blocked_range(
        app, monkeypatch):
    """A chained step's chunk blocked by the filter (#597 stops the
    account) is remembered as a range too, so a later window build cuts
    around it."""
    from backend.models import ProfileBlockedRange
    u = _user()
    prev = _profile(u, gen_type="update", cutoff=T0)
    item = {"custom_id": "c1", "user_id": u.id, "kind": "chunk",
            "prev_profile_id": prev.id, "model_id": "claude-opus-5.5",
            "source_data_cutoff": (T0 + timedelta(days=3)).isoformat()}
    _policy_reports(monkeypatch)
    pb._stop_for_policy_block(u, item, "bio_policy")
    [row] = ProfileBlockedRange.query.filter_by(user_id=u.id).all()
    assert (row.range_start, row.range_end) == (T0, T0 + timedelta(days=3))


def test_a_policy_blocked_final_stops_the_account(world, monkeypatch):
    reports = _policy_reports(monkeypatch)
    u = _user()
    rows = _set_of(u, 5, created=datetime.utcnow())
    _corpus_for(world, rows)
    _blocking(world, monkeypatch, "_f_")
    _advance(u)
    _poll()
    [item] = _items("final")
    assert item["outcome"] == "blocked"
    db.session.refresh(u)
    # The real cutoff of the final, not "..end".
    assert u.profile_seed_error.startswith(
        f"{pw.POLICY_STOP_NOTE} final merge start..{item['cutoff']} "
        f"(bio_policy)")
    assert [r[1] for r in reports] == ["bio_policy"]
    submits = len(world.provider.submits)
    assert _advance(u) == "blocked"
    assert pb._should_seed(u) is False
    assert len(world.provider.submits) == submits
    # A later save clears the column; the advance restores the note with
    # the real cutoff.
    u.profile_seed_error = None
    db.session.commit()
    assert _advance(u) == "blocked"
    db.session.refresh(u)
    assert f"start..{item['cutoff']}" in u.profile_seed_error


def test_a_block_collected_while_the_advance_runs_is_not_resent(
        world, monkeypatch):
    """Review of #596: the poller collects a blocked final after the
    advance's first block check but before it reads what is in flight.
    The final is no longer in flight, so without a second check it would
    be submitted again."""
    _policy_reports(monkeypatch)
    u = _user()
    rows = _set_of(u, 5, created=datetime.utcnow())
    _corpus_for(world, rows)
    _blocking(world, monkeypatch, "_f_")
    _advance(u)
    assert ProfileBatchJob.query.filter_by(status="pending").count()
    submits = len(world.provider.submits)

    real_history = pw.attempt_history
    polled = []

    def poll_meanwhile(user_id, now=None):
        if not polled:
            polled.append(user_id)
            # The poller's dispatch finds the guard held by this advance.
            monkeypatch.setattr(pb, "_advance_users", lambda ids: None)
            _poll()
        return real_history(user_id, now)
    monkeypatch.setattr(pw, "attempt_history", poll_meanwhile)

    assert _advance(u) == "blocked"
    assert polled == [u.id]
    assert not ProfileBatchJob.query.filter_by(status="pending").count()
    assert len(world.provider.submits) == submits
    assert [i["outcome"] for i in _items("final")] == ["blocked"]


# ── the lab backfill (scripts/backfill_policy_blocks.py) ─────────────────

def _backfill_module():
    import importlib.util
    path = os.path.join(os.path.dirname(__file__), "..", "scripts",
                        "backfill_policy_blocks.py")
    spec = importlib.util.spec_from_file_location("backfill_policy_blocks",
                                                  path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _openai_world(app, world):
    """A user on an OpenAI chat model (windows on Luna), a 3-window corpus."""
    u = _user(preferred_model="gpt-6-astra", profile_needs_full_regen=True)
    world.corpus = _windows_corpus(3)
    return u


def test_the_backfill_records_old_missing_blocks_and_the_set_finishes(
        app, world, monkeypatch):
    """Under the old code a blocked first window came back "missing" three
    times. The backfill reads each batch's error file: the policy-coded
    attempts become "blocked" with one skip row, an ordinary 500 stays
    "missing"; the next pass sends the final, never the window."""
    import json as _json
    from types import SimpleNamespace
    from backend.models import APICostLog
    mod = _backfill_module()
    _policy_reports(monkeypatch)
    u = _openai_world(app, world)
    world.provider.respond = lambda cid, req: (
        None if "_w1_" in cid else {"content": _good(tag=cid),
                                    "input_tokens": 1, "output_tokens": 1,
                                    "batch": True})
    for _ in range(3):
        _advance(u)
        _poll()
    attempts = [i for i in _items("window") if i["start"] is None]
    assert [i["outcome"] for i in attempts] == ["missing"] * 3
    assert all(j.provider_key.startswith("openai:")
               for j in ProfileBatchJob.query.all())
    # The provider's error files: the first two attempts were blocked,
    # the third was an outage.
    codes = {attempts[0]["custom_id"]: "bio_policy",
             attempts[1]["custom_id"]: "bio_policy",
             attempts[2]["custom_id"]: "server_error"}
    by_batch = {}
    for job in ProfileBatchJob.query.all():
        for item in job.items:
            if item["custom_id"] in codes:
                by_batch[job.batch_id] = item["custom_id"]

    class _Files:
        def content(self, file_id):
            cid = file_id[len("ef-"):]
            code = codes[cid]
            line = _json.dumps({"custom_id": cid, "response": {
                "status_code": 500 if code == "server_error" else 400,
                "body": {"error": {"code": code, "message": "x",
                                   "type": "invalid_request_error"}}}})
            return SimpleNamespace(content=(line + "\n").encode())

    class _Batches:
        def retrieve(self, batch_id):
            cid = by_batch.get(batch_id)
            return SimpleNamespace(
                status="completed", error_file_id=f"ef-{cid}" if cid else None)

    client = SimpleNamespace(batches=_Batches(), files=_Files())
    lines = []

    found = mod.find_blocks(client_for=lambda: client, out=lines.append)
    assert [(item["custom_id"], code) for _j, item, code in found] == [
        (attempts[0]["custom_id"], "bio_policy"),
        (attempts[1]["custom_id"], "bio_policy")]
    # Dry run: printed, nothing written.
    assert mod.record_blocks(found, apply=False, out=lines.append) == 2
    assert not _rows(u, "window_skip")
    assert {i["outcome"] for i in _items("window")
            if i["start"] is None} == {"missing"}
    assert all("bio_policy" in line for line in lines)
    assert not any("message" in line for line in lines)

    mod.record_blocks(found, apply=True, out=lines.append)
    assert [i["outcome"] for i in _items("window") if i["start"] is None] \
        == ["blocked", "blocked", "missing"]
    assert len(_rows(u, "window_skip")) == 1
    assert APICostLog.query.filter_by(
        user_id=u.id, request_ref="policy_block:bio_policy").count() == 2
    # The durable range record a later window plan cuts around.
    assert pw.blocked_ranges(u.id) == [_range(attempts[0])]
    assert _range(attempts[0])[0] is not None
    # Idempotent: a second run finds nothing more.
    again = mod.find_blocks(client_for=lambda: client, out=lines.append)
    assert again == []
    assert len(_rows(u, "window_skip")) == 1

    # The new code finishes the profile without the window.
    world.provider.respond = lambda cid, req: {
        "content": _good(events="_w" in cid, tag=cid), "input_tokens": 1,
        "output_tokens": 1, "batch": True}
    windows_before = sum(1 for i in _items("window") if i["start"] is None)
    _build(world, u)
    assert sum(1 for i in _items("window") if i["start"] is None) \
        == windows_before
    assert len(_rows(u, "integration")) == 1


def test_the_backfill_leaves_accounts_without_ai_without_a_skip_row(
        app, world, monkeypatch):
    import json as _json
    from types import SimpleNamespace
    mod = _backfill_module()
    _policy_reports(monkeypatch)
    u = _openai_world(app, world)
    world.provider.respond = lambda cid, req: (
        None if "_w1_" in cid else {"content": _good(tag=cid),
                                    "input_tokens": 1, "output_tokens": 1,
                                    "batch": True})
    _advance(u)
    _poll()
    [attempt] = [i for i in _items("window") if i["start"] is None]
    job = [j for j in ProfileBatchJob.query.all()
           if any(i["custom_id"] == attempt["custom_id"] for i in j.items)][0]
    u.default_ai_usage = "none"
    db.session.commit()
    line = _json.dumps({"custom_id": attempt["custom_id"], "response": {
        "status_code": 400, "body": {"error": {"code": "bio_policy"}}}})
    client = SimpleNamespace(
        batches=SimpleNamespace(retrieve=lambda bid: SimpleNamespace(
            status="completed",
            error_file_id="ef-1" if bid == job.batch_id else None)),
        files=SimpleNamespace(content=lambda fid: SimpleNamespace(
            content=(line + "\n").encode())))
    found = mod.find_blocks(client_for=lambda: client, out=lambda s: None)
    mod.record_blocks(found, apply=True, out=lambda s: None)
    [attempt] = [i for i in _items("window") if i["start"] is None]
    assert attempt["outcome"] == "blocked"
    assert not _rows(u, "window_skip")
    # The range is remembered all the same.
    assert pw.blocked_ranges(u.id) == [_range(attempt)]


# ── review of 07a3f1d7 ────────────────────────────────────────────────────

def test_older_writing_imported_before_a_blocked_first_window_is_read(
        world, monkeypatch):
    """A blocked first window is remembered from just before its earliest
    entry, not "from the first writing": older writing imported later,
    which was never sent, is read by the rebuild; the blocked range is
    still skipped exactly."""
    u, blocked = _blocked_build(world, monkeypatch, "_w1_")
    assert blocked["start"] is None
    older = [(T0 - timedelta(days=300 - i), 1000) for i in range(30)]
    world.corpus.rows = older + world.corpus.rows
    action, _v = ex.revert_profile_for_import(u.id, older[0][0])
    assert action == "full"
    db.session.commit()
    before = len(_items("window"))
    _build(world, u)
    new = _never_resent(u, blocked, before)
    read_older = [i for i in new if i["start"] is None
                  or pw._dt(i["start"]) < T0]
    assert read_older
    assert sum(i["units"] for i in new
               if pw._dt(i["cutoff"]) < T0) == 30_000
    assert _finals(u)


def test_a_superseded_update_window_block_never_reaches_a_chained_chunk(
        world, monkeypatch):
    """Review of 07a3f1d7: the user saves a version on the set's final
    while an update window is in the batch, and that window is blocked.
    Its set is superseded (no skip row), and the rest fits one chunk on
    the user's version; the build must still take the window path, which
    cuts around the blocked range, never a chained chunk holding it."""
    _policy_reports(monkeypatch)
    u = _user()
    rows, final = _update_in_flight(world, u)
    w = [i for i in _items("window") if "outcome" not in i][0]
    _blocking(world, monkeypatch, w["custom_id"])
    _edit(u, final)
    _poll()
    [w] = [i for i in _items("window") if i["custom_id"] == w["custom_id"]]
    assert w["outcome"] == "blocked"
    assert pw.blocked_ranges(u.id) == [_range(w)]
    assert pw.wants_window_build(u) is True
    _unblock(world, monkeypatch)
    for p in UserProfile.query.filter_by(user_id=u.id).all():
        p.created_at -= timedelta(days=8)
    db.session.commit()
    monkeypatch.setattr(pb, "MIN_INACTIVITY", timedelta(0))
    monkeypatch.setattr(pb, "_new_token_count",
                        lambda user, cutoff: world.corpus.count(user.id,
                                                                cutoff))
    n_jobs = ProfileBatchJob.query.count()
    before = len(_items("window"))
    for _ in range(4):
        _seed(u)
        db.session.refresh(u)
        if u.profile_generation_task_id:
            pw.advance(u.id, u.profile_generation_task_id)
        if ProfileBatchJob.query.filter_by(status="pending").count():
            _poll()
    # The chained builder refuses it outright too.
    assert pb._build_next_profile_request(u) is None or \
        pb._build_next_profile_request(u)["meta"]["kind"] != "chunk"
    jobs = ProfileBatchJob.query.order_by(ProfileBatchJob.id).all()[n_jobs:]
    assert not [i for j in jobs for i in j.items if i.get("kind") == "chunk"]
    assert not [i for i in _items("window")[before:] if _overlaps(i, w)]


def test_no_chained_chunk_or_sync_build_meets_a_blocked_range(
        app, world, monkeypatch):
    """With the window path switched off, nothing that would hold a
    blocked range is built: the chained batch builder returns no chunk,
    and the synchronous task's hand-off builds nothing."""
    from backend.utils import refusal_backoff  # noqa: F401
    world.corpus = Corpus(50)
    monkeypatch.setattr(pb._exports, "count_remaining_units",
                        lambda uid, cutoff=None, **k: world.corpus.count(
                            uid, cutoff))
    u = _user(profile_needs_full_regen=True)
    pw.record_blocked_range(u.id, T0 + timedelta(hours=5),
                            T0 + timedelta(hours=9), "bio_policy")
    app.config["PROFILE_INDEPENDENT_VERSIONS"] = False
    assert pw.wants_window_build(u) is False
    assert pb._build_next_profile_request(u) is None
    result = ex.window_hand_off(u, "task-1")
    assert result is not None and result["profile_id"] is None
    assert not world.provider.submits


# ── backfill: a blocked window another attempt saved (lab dry run) ────────

def _error_file_client(errors):
    """A fake OpenAI client whose batches' error files hold ``errors``
    {batch_id: (custom_id, code)}."""
    import json as _json
    from types import SimpleNamespace

    def retrieve(batch_id):
        return SimpleNamespace(
            status="completed",
            error_file_id=f"ef-{batch_id}" if batch_id in errors else None)

    def content(file_id):
        cid, code = errors[file_id[len("ef-"):]]
        line = _json.dumps({"custom_id": cid, "response": {
            "status_code": 400, "body": {"error": {
                "code": code, "type": "invalid_request_error"}}}})
        return SimpleNamespace(content=(line + "\n").encode())
    return SimpleNamespace(batches=SimpleNamespace(retrieve=retrieve),
                           files=SimpleNamespace(content=content))


def _job_of(custom_id):
    return [j for j in ProfileBatchJob.query.all()
            if any(i["custom_id"] == custom_id for i in j.items)][0]


def _assert_saved_window_kept(u, mod, errors, blocked_cid):
    """The backfill marks the blocked attempt and records its range, but
    writes no skip row, keeps the saved version, and starts nothing."""
    windows = {r.id for r in _rows(u, "window")}
    finals = len(_finals(u))
    lines = []
    found = mod.find_blocks(client_for=lambda: _error_file_client(errors),
                            out=lines.append)
    assert [item["custom_id"] for _j, item, _c in found] == [blocked_cid]
    mod.record_blocks(found, apply=False, out=lines.append)
    assert "+ skip row" not in lines[-1]
    assert "another attempt was saved" in lines[-1]
    mod.record_blocks(found, apply=True, out=lambda s: None)
    blocked = [i for i in _items("window") if i["custom_id"] == blocked_cid][0]
    assert blocked["outcome"] == "blocked"
    assert not _rows(u, "window_skip")
    assert {r.id for r in _rows(u, "window")} == windows
    assert pw.blocked_ranges(u.id) == [_range(blocked)]
    assert len(_finals(u)) == finals
    return blocked


def test_the_backfill_keeps_a_window_saved_after_its_blocked_attempt(
        app, world, monkeypatch):
    """Lab dry run: the old poller resent a blocked window and the resend
    was saved ("missing", then "saved", same set). No skip row, the saved
    version and the profile stay; the range is recorded; nothing is
    re-merged or sent."""
    mod = _backfill_module()
    _policy_reports(monkeypatch)
    u = _openai_world(app, world)
    world.provider.respond = lambda cid, req: (
        None if ("_w1_" in cid and cid.endswith("_1")) else {
            "content": _good(events="_w" in cid, tag=cid),
            "input_tokens": 1, "output_tokens": 1, "batch": True})
    _build(world, u)
    attempts = [i for i in _items("window") if i["start"] is None]
    assert [i["outcome"] for i in attempts] == ["missing", "saved"]
    assert len(_finals(u)) == 1
    first = attempts[0]
    _assert_saved_window_kept(
        u, mod, {_job_of(first["custom_id"]).batch_id:
                 (first["custom_id"], "bio_policy")}, first["custom_id"])
    submits = len(world.provider.submits)
    assert _advance(u) == "idle"
    assert len(world.provider.submits) == submits
    assert [i["outcome"] for i in _items("window")
            if i["start"] is None] == ["blocked", "saved"]


def test_the_backfill_keeps_a_saved_window_when_a_later_duplicate_was_blocked(
        app, world, monkeypatch):
    """The other order: the window was saved, and a later duplicate of it
    in the same set was blocked."""
    mod = _backfill_module()
    _policy_reports(monkeypatch)
    u = _openai_world(app, world)
    _build(world, u)
    [saved] = [i for i in _items("window") if i["start"] is None]
    assert saved["outcome"] == "saved" and len(_finals(u)) == 1
    dup = dict(saved, custom_id=saved["custom_id"] + "_dup",
               outcome="missing")
    db.session.add(ProfileBatchJob(
        provider_key=_job_of(saved["custom_id"]).provider_key,
        batch_id="batch_dup", status="collected", items=[dup],
        submitted_at=datetime.utcnow(), collected_at=datetime.utcnow()))
    db.session.commit()
    _assert_saved_window_kept(
        u, mod, {"batch_dup": (dup["custom_id"], "bio_policy")},
        dup["custom_id"])
    submits = len(world.provider.submits)
    assert _advance(u) == "idle"
    assert len(world.provider.submits) == submits


# ── which models read the writing (Peter, 2026-11-05) ────────────────────
# More than one window: the user's provider's pair (Haiku / Opus, Luna /
# Sol), or Luna / Opus when the writing is public only or the user agreed
# to both providers. A regular one-window update: a chained version by the
# user's chat model, and the merges after it by the same model.

def _node(user, provenance=None, privacy="private", parent=None,
          owner=None, deleted=False):
    n = Node(user_id=(owner or user).id, node_type="user", ai_usage="chat",
             token_count=10, privacy_level=privacy, provenance=provenance,
             parent_id=parent.id if parent else None, created_at=T0)
    if deleted:
        n.deleted_at = datetime.utcnow()
    n.set_content("synthetic")
    db.session.add(n)
    db.session.commit()
    return n


def _prefilled(**kw):
    u = _user(**kw)
    _node(u, provenance="prefill_ca")
    _node(u, provenance="prefill_x")
    return u


def test_public_only_writing_is_read_from_metadata(app):
    assert pw.writing_is_public_only(_user()) is False      # nothing yet
    assert pw.writing_is_public_only(_prefilled()) is True

    u = _prefilled()
    _node(u)                                   # one private entry
    assert pw.writing_is_public_only(u) is False
    u = _prefilled()
    _node(u, deleted=True)                     # a deleted private one too
    assert pw.writing_is_public_only(u) is False
    u = _prefilled()
    _node(u, provenance="archive_upload")      # an own export: not vouched
    assert pw.writing_is_public_only(u) is False

    u = _prefilled(public_sharing_enabled=True)
    _node(u, privacy="public")
    assert pw.writing_is_public_only(u) is True
    u.public_sharing_enabled = False           # not public any more
    db.session.commit()
    assert pw.writing_is_public_only(u) is False

    other = _user()
    u = _prefilled()
    _node(other, provenance="prefill_ca", parent=_node(u, "prefill_ca"))
    assert pw.writing_is_public_only(u) is False   # someone's reply
    u = _prefilled()
    _node(u, provenance="prefill_ca", parent=_node(other, "prefill_ca"))
    assert pw.writing_is_public_only(u) is False   # a reply to someone

    u = _prefilled()
    _profile(u, generated_by="user", gen_type="initial")
    assert pw.writing_is_public_only(u) is False   # the user's own text
    from backend.models import UserPrompt
    u = _prefilled()
    db.session.add(UserPrompt(user_id=u.id, prompt_key="profile_generation",
                              title="t", content="my prompt"))
    db.session.commit()
    assert pw.writing_is_public_only(u) is False


def test_the_model_table(app):
    anth, oai = _user(), _user(preferred_model="gpt-6-astra")
    for user, own in ((anth, ("claude-haiku-5.5", "claude-opus-5.5")),
                      (oai, ("gpt-6-luna", "gpt-6.1-sol"))):
        assert pw.cross_provider_allowed(user) is False
        m = pw.profile_models_for(user, cross=False)
        assert (m.version, m.merge, m.cross) == own + (False,)
        m = pw.profile_models_for(user, cross=True)
        assert (m.version, m.merge, m.cross) == (
            "gpt-6-luna", "claude-opus-5.5", True)
        assert m.merge_effort is None          # Opus takes none
    c = pw.chained_models_for(oai)
    assert (c.version, c.merge, c.chained) == ("gpt-6-astra", "gpt-6-astra",
                                               True)
    c = pw.chained_models_for(anth)
    assert (c.version, c.merge) == ("claude-opus-5.5", "claude-opus-5.5")
    # Consent, or public-only writing, allows the cross-provider pair.
    assert pw.cross_provider_allowed(_user(profile_both_providers=True))
    assert pw.cross_provider_allowed(_prefilled())


def _all_reqs(world, since=0):
    return [(provider, r) for sub in world.provider.submits[since:]
            for provider, reqs in sub.items() for r in reqs]


def _build_then_update(world, user):
    """A large build, then a regular update of 85k units after it."""
    world.corpus = _windows_corpus(4)
    _finish(world, user)
    assert [f for f in _rows(user, "integration") if f.window_set]
    n = len(world.provider.submits)
    world.corpus.add(85, start=datetime.utcnow() + timedelta(days=2))
    _finish(world, user)
    return n


@pytest.mark.parametrize("chat, provider, version, merge", [
    ("gpt-6-astra", "openai", "gpt-6-luna", "gpt-6.1-sol"),
    (None, "anthropic", "claude-haiku-5.5", "claude-opus-5.5")])
def test_private_writing_never_reaches_the_other_provider(
        world, chat, provider, version, merge):
    """The hard line (Peter, 2026-11-05): an OpenAI user's private writing
    never gets an Anthropic call, and an Anthropic user's never an OpenAI
    one: not for a large build's windows and merges, not for a regular
    update and its merges."""
    u = _user(preferred_model=chat, profile_needs_full_regen=True)
    _node(u)                                   # one private entry
    n = _build_then_update(world, u)
    assert {p for p, _r in _all_reqs(world)} == {provider}
    build = {r["model_id"] for sub in world.provider.submits[:n]
             for reqs in sub.values() for r in reqs}
    assert build == {version, merge}
    chat_model = chat or "claude-opus-5.5"
    update = _all_reqs(world, n)
    assert {r["model_id"] for _p, r in update} == {chat_model}
    assert any("_u" in r["custom_id"] for _p, r in update)
    assert any("_f_" in r["custom_id"] for _p, r in update)
    assert len([f for f in _rows(u, "integration") if f.window_set]) == 2


@pytest.mark.parametrize("how", ["public_only", "consent"])
def test_public_or_agreed_writing_takes_luna_and_opus(world, how):
    u = (_prefilled(profile_needs_full_regen=True) if how == "public_only"
         else _user(profile_needs_full_regen=True, profile_both_providers=True))
    if how == "consent":
        _node(u)                               # private, but agreed
    world.corpus = _windows_corpus(4)
    _finish(world, u)
    by_kind = {}
    for provider, r in _all_reqs(world):
        kind = "window" if "_w" in r["custom_id"] else "merge"
        by_kind.setdefault(kind, set()).add((provider, r["model_id"]))
    assert by_kind["window"] == {("openai", "gpt-6-luna")}
    assert by_kind["merge"] == {("anthropic", "claude-opus-5.5")}


def test_a_request_to_a_provider_not_allowed_is_never_sent(world):
    sub = pw._Submission({}, {"openai"})
    with pytest.raises(pw.ProfileModelError):
        sub.add("anthropic", {"custom_id": "c"}, {"user_id": 1})
    assert sub.built == []


def test_a_regular_update_chains_on_the_newest_profile(world):
    """The base: after a windows build the set's final; after a regular
    update its chained version. The chained version is the update prompt
    over that profile and the new window, saved as a window_update with
    "Covers data through" and merged in the window's place, labelled as a
    profile of all the writing."""
    u = _user(profile_needs_full_regen=True)
    _build_then_update(world, u)
    first_final = [f for f in _rows(u, "integration") if f.window_set][0]
    [chained] = _rows(u, "window_update")
    [item] = [i for i in _items("window") if "_u" in i["custom_id"]]
    assert item["chained_base_id"] == first_final.id
    # Its parent is the set's base, as a window's: the merges after it do
    # not read the earlier final as a base.
    assert chained.parent_profile_id is None
    assert chained.get_content().startswith("[Covers data through ")
    [update_req] = [r for _p, r in _all_reqs(world)
                    if "_u" in r["custom_id"]]
    prompt = update_req["messages"][0]["content"][0]["text"]
    assert first_final.get_content() in prompt          # {existing_profile}
    assert ex.EVENTS_IN_WINDOW_PARAGRAPH not in prompt
    final = [f for f in _rows(u, "integration") if f.window_set][-1]
    merge = [r for s in world.provider.submits for rs in s.values()
             for r in rs if r["custom_id"].endswith("_f_1")][-1]
    labels = [m["content"][0]["text"].split("\n\n")[0]
              for m in merge["messages"][:-1]]
    assert labels[-1].startswith("Profile No. ")
    assert "- Covers all the writing from" in labels[-1]
    assert "- Updates a profile of all the writing up to" in labels[-1]
    assert final.parent_profile_id == chained.id

    # The next update chains on the chained version.
    world.corpus.add(85, start=datetime.utcnow() + timedelta(days=20))
    _finish(world, u)
    second = [i for i in _items("window") if "_u" in i["custom_id"]][-1]
    assert second["chained_base_id"] == chained.id
    assert len(_rows(u, "window_update")) == 2


def test_a_retried_window_of_a_build_is_not_a_chained_update(world):
    """A set still being built (no final over its last window) reads its
    windows independently, with the build's models."""
    u = _user()
    rows = _set_of(u, 3)
    _corpus_for(world, rows)
    world.corpus.add(85)
    state = pw.set_state(u, "set1", None)
    assert state.due
    assert pw.chained_update_base(u, state, False) is None


def test_the_estimate_prices_the_chained_update_at_the_chat_model(
        world, monkeypatch):
    import backend.utils.cost as cost
    u = _user(preferred_model="gpt-6-astra", profile_needs_full_regen=True)
    world.corpus = _windows_corpus(4)
    _finish(world, u)
    world.corpus.add(85, start=datetime.utcnow() + timedelta(days=2))
    priced = []
    real = cost.calculate_llm_cost_microdollars
    monkeypatch.setattr(cost, "calculate_llm_cost_microdollars",
                        lambda model_id, *a, **k: (
                            priced.append(model_id) or real(model_id, *a, **k)))
    _advance(u)
    assert priced and set(priced) == {"gpt-6-astra"}


def test_every_chained_version_keeps_its_place_in_the_merges(world):
    """Peter, 2026-11-05: "The last version of a chain is heavily biased
    towards the last window. All chained versions should go to the
    merge." After each of ten regular updates the final reads the build's
    windows and every chained version so far, each in its window's place,
    and never an earlier final as a base: a chained row's parent is the
    set's base, as a window's."""
    u = _user()
    rows = _set_of(u, 5)
    _corpus_for(world, rows)
    _finish(world, u)
    for k in range(10):
        world.corpus.add(85, start=datetime.utcnow() + timedelta(days=2 + 10 * k))
        _finish(world, u)
        final_item = _items("final")[-1]
        inputs = [db.session.get(UserProfile, pid)
                  for pid in final_item["inputs"]]
        assert [p.generation_type for p in inputs] == (
            ["window"] * 5 + ["window_update"] * (k + 1))
        assert [p.id for p in inputs[5:]] == [
            r.id for r in _rows(u, "window_update")]
    assert len([f for f in _rows(u, "integration") if f.window_set]) == 11


# ── public-only, after review (2026-11-05) ────────────────────────────────
# "A user is public-only only if nothing private was ever read for them."

def test_an_entry_edited_through_the_node_route_is_private(app, monkeypatch):
    """Review finding 1: PUT /api/nodes/<id> keeps a pre-filled entry's
    provenance; the NodeVersion row the edit writes makes it private."""
    from flask_login import LoginManager
    import backend.routes.nodes as nodes_module
    monkeypatch.setattr(
        "backend.utils.context_artifacts.sync_context_artifacts",
        lambda *a, **k: None)
    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    app.register_blueprint(nodes_module.nodes_bp, url_prefix="/api/nodes")
    u = _prefilled(prefilled_handle="alice")
    tweet = Node.query.filter_by(user_id=u.id).first()
    assert pw.writing_is_public_only(u) is True
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(u.id)
        sess["_fresh"] = True
    res = client.put(f"/api/nodes/{tweet.id}",
                     json={"content": "my own words now"})
    assert res.status_code == 200
    db.session.expire_all()
    assert db.session.get(Node, tweet.id).provenance == "prefill_ca"
    assert pw.writing_is_public_only(db.session.get(User, u.id)) is False


def test_a_deleted_fetched_entry_whose_text_was_wiped_is_private(app):
    u = _prefilled(prefilled_handle="alice")
    gone = _node(u, provenance="prefill_x", deleted=True)
    assert pw.writing_is_public_only(u) is True     # its history is there
    gone.content = None                             # the cleanup's wipe
    db.session.commit()
    assert pw.writing_is_public_only(u) is False


def test_profiles_count_only_when_they_read_pre_filled_tweets(app):
    from backend.utils.profile_providers import profiles_public_only
    tweets = {"twitter": {"nodes": 2, "tokens": 20}}
    u = _prefilled(prefilled_handle="alice")
    _profile(u, source_origin_stats=tweets)
    assert pw.writing_is_public_only(u) is True
    _profile(u, source_origin_stats=None)           # no record: private
    assert profiles_public_only(u) is False
    v = _prefilled(prefilled_handle="bob")
    _profile(v, source_origin_stats={**tweets, "loore": {"nodes": 1,
                                                          "tokens": 5}})
    assert pw.writing_is_public_only(v) is False    # read Loore writing
    w = _prefilled()                                # not a pre-fill
    _profile(w, source_origin_stats=tweets)         # (an own upload?)
    assert pw.writing_is_public_only(w) is False


def test_a_purged_private_base_never_reaches_the_other_provider(world):
    """Review finding 2: an OpenAI user's profile was built by GPT-6 Astra
    from private Loore entries, which were deleted and purged; only
    pre-filled tweets remain. The next build of two or more windows takes
    that profile as its base: it must stay with OpenAI."""
    u = _user(preferred_model="gpt-6-astra", prefilled_handle="alice")
    _node(u, provenance="prefill_ca")
    base = _profile(u, gen_type="update", cutoff=T0,
                    generated_by="gpt-6-astra", text=_good(
                        events=False, tag="PRIVATE-BASE"),
                    source_origin_stats={"loore": {"nodes": 9,
                                                   "tokens": 900}})
    world.corpus = _windows_corpus(4)
    _finish(world, u)
    assert {p for p, _r in _all_reqs(world)} == {"openai"}
    merges = [r for _p, r in _all_reqs(world) if "_f_" in r["custom_id"]]
    assert merges and all(r["model_id"] == "gpt-6.1-sol" for r in merges)
    texts = [m["content"][0]["text"] for r in merges for m in r["messages"]]
    assert any("PRIVATE-BASE" in t for t in texts)    # merged, on Sol


@pytest.mark.parametrize("consent", [False, True])
def test_a_pass_over_private_writing_marks_the_user_for_good(world, consent):
    """The sticky mark: a pass that read private writing (with consent,
    too) leaves the user private after those entries are gone and the
    consent is withdrawn."""
    u = _user(prefilled_handle="alice", profile_needs_full_regen=True,
              profile_both_providers=consent)
    _node(u, provenance="prefill_ca")
    private = _node(u)
    world.corpus = _windows_corpus(4)
    _advance(u)
    u = db.session.get(User, u.id)
    assert u.profile_private_read is True
    db.session.delete(private)                      # purged
    u.profile_both_providers = False
    db.session.commit()
    from backend.utils.profile_providers import entries_public_only
    assert entries_public_only(u) is True
    assert pw.writing_is_public_only(u) is False
    assert pw.cross_provider_allowed(u) is False


def _withdraw(user):
    """The user's no (#599), written as another request would: a Core
    UPDATE, so the pass's loaded User object still says yes."""
    db.session.execute(User.__table__.update()
                       .where(User.__table__.c.id == user.id)
                       .values(profile_both_providers=False,
                               profile_both_providers_answered_at=(
                                   datetime.utcnow())))


def _consenting_build_with_windows_in(world, chat):
    """A user who said yes, whose GPT-6 Luna windows of a 4-window build
    are collected; the merges are due next. Returns the user and the
    number of submissions so far."""
    u = _user(preferred_model=chat, profile_needs_full_regen=True,
              profile_both_providers=True,
              profile_both_providers_answered_at=datetime.utcnow())
    _node(u)                                   # private, but agreed
    world.corpus = _windows_corpus(4)
    _advance(u)
    windows = _all_reqs(world)
    assert windows and {(p, r["model_id"]) for p, r in windows} == {
        ("openai", "gpt-6-luna")}
    _poll()
    return u, len(world.provider.submits)


def _no_before_the_pass_picks_models(monkeypatch, user):
    """The user's no, committed elsewhere just before the next pass
    computes its models, while this pass's session still holds the User
    it loaded with the yes (merge_plan runs right before the models are
    picked, with no commit in between)."""
    real = pw.merge_plan
    said = []

    def merge_plan_then_no(*a, **k):
        if not said:
            said.append(True)
            _withdraw(user)
        return real(*a, **k)
    monkeypatch.setattr(pw, "merge_plan", merge_plan_then_no)
    return said


@pytest.mark.parametrize("chat, provider, merge", [
    ("gpt-6-astra", "openai", "gpt-6.1-sol"),
    (None, "anthropic", "claude-opus-5.5")])
def test_a_withdrawn_yes_applies_from_the_next_pass(
        world, monkeypatch, chat, provider, merge):
    """#599: a user who said yes and then no gets their own provider from
    the next pass on, also when the pass's session still holds the User it
    loaded with the yes (the consent is read from the database). The
    windows already sent under the yes are merged by their provider's
    merge model, and nothing more reaches the other provider."""
    u, n = _consenting_build_with_windows_in(world, chat)
    said = _no_before_the_pass_picks_models(monkeypatch, u)
    assert u.profile_both_providers is True    # the loaded object
    _finish(world, u)
    assert said
    after = _all_reqs(world, n)
    assert after, "the merges went out"
    assert {p for p, _r in after} == {provider}
    assert {r["model_id"] for _p, r in after} == {merge}
    assert [f for f in _rows(u, "integration") if f.window_set]


def test_a_no_during_a_pass_sends_no_token_count_to_the_other_provider(
        world, monkeypatch):
    """#599, review finding 1: an OpenAI user said yes, their Luna windows
    are in, and the merges are due on Opus 5.5. A no given after the pass
    picked its models must stop the merge prompts (written from the
    user's writing) before they reach Anthropic, also the token count
    that checks them against the merge model's cap."""
    u, n = _consenting_build_with_windows_in(world, "gpt-6-astra")
    counted = []
    import backend.llm_providers as lp
    monkeypatch.setattr(lp.LLMProvider, "count_tokens", staticmethod(
        lambda model_id, messages, k: counted.append(model_id)))
    real = pw.chained_update_base

    def models_then_no(*a, **k):
        out = real(*a, **k)
        _withdraw(u)                   # after the pass computed `cross`
        return out
    monkeypatch.setattr(pw, "chained_update_base", models_then_no)
    assert _advance(u) == "partial"
    assert "claude-opus-5.5" not in counted
    assert len(world.provider.submits) == n       # nothing sent
    monkeypatch.setattr(pw, "chained_update_base", real)
    _finish(world, u)
    after = _all_reqs(world, n)
    assert after and {p for p, _r in after} == {"openai"}
    assert {r["model_id"] for _p, r in after} == {"gpt-6.1-sol"}
    assert "claude-opus-5.5" not in counted


def test_a_no_given_while_a_pass_renders_stops_it_before_sending(
        world, monkeypatch):
    """#599: rendering a pass can take many minutes. A no given meanwhile
    is read again before the slice is sent: nothing rendered under the yes
    goes to the other provider, and the next pass renders the windows for
    the user's own provider."""
    u = _user(profile_needs_full_regen=True, profile_both_providers=True,
              profile_both_providers_answered_at=datetime.utcnow())
    _node(u)
    world.corpus = _windows_corpus(4)
    export = world.corpus.export
    said_no = []

    def render_then_no(*a, **k):
        out = export(*a, **k)
        if not said_no:
            said_no.append(True)
            _withdraw(u)
        return out
    monkeypatch.setattr(pw._exports, "build_user_export_content",
                        render_then_no)
    assert _advance(u) == "partial"
    assert world.provider.submits == []
    assert not _items()                        # nothing in flight
    _finish(world, u)
    reqs = _all_reqs(world)
    assert reqs and {p for p, _r in reqs} == {"anthropic"}
    by_kind = {("window" if "_w" in r["custom_id"] else "merge",
                r["model_id"]) for _p, r in reqs}
    assert by_kind == {("window", "claude-haiku-5.5"),
                       ("merge", "claude-opus-5.5")}


def test_a_submission_without_a_recheck_sends_as_before(world):
    sub = pw._Submission({}, {"anthropic"})
    assert sub.recheck is None and not sub.withdrawn


def test_a_public_only_pass_does_not_mark_the_user(world):
    from backend.utils.profile_providers import note_private_read
    u = _prefilled(prefilled_handle="alice")
    note_private_read(u)
    assert u.profile_private_read is False
    _node(u)
    note_private_read(u)
    assert u.profile_private_read is True


# ── accounts someone signed into; the backfill of the mark ───────────────
# An account that ever uploaded its own archive is private: public-only
# needs an account nobody has signed into (utils/activity.sign_in_status).

@pytest.mark.parametrize("record", ["last_seen", "terms", "older"])
def test_an_account_someone_signed_into_is_private(app, record):
    tweets = {"twitter": {"nodes": 2, "tokens": 20}}
    u = _prefilled(prefilled_handle="alice")
    _profile(u, source_origin_stats=tweets)
    assert pw.writing_is_public_only(u) is True
    if record == "last_seen":
        u.last_seen_at = datetime.utcnow()
    elif record == "terms":
        u.accepted_terms_at = datetime.utcnow()
    else:                       # older than the sign-in record: unknown
        u.created_at = datetime(2026, 8, 1)
    db.session.commit()
    assert pw.writing_is_public_only(u) is False
    assert pw.cross_provider_allowed(u) is False


def test_before_the_backfill_only_verified_pre_fills_are_public(app):
    """The mark starts false for every account from before it: until the
    backfill runs, an account that someone signed into, with pre-filled
    tweets and profiles that read only them, is private; a pre-fill
    nobody signed into is public."""
    tweets = {"twitter": {"nodes": 2, "tokens": 20}}
    used = _prefilled(prefilled_handle="alice")
    _profile(used, source_origin_stats=tweets)
    used.last_seen_at = datetime(2026, 9, 1)
    waiting = _prefilled(prefilled_handle="bob")
    _profile(waiting, source_origin_stats=tweets)
    db.session.commit()
    assert used.profile_private_read is False      # not backfilled yet
    assert pw.writing_is_public_only(used) is False
    assert pw.writing_is_public_only(waiting) is True


def test_the_backfill_marks_what_it_cannot_vouch_for(app):
    from backend.scripts import backfill_profile_private_read as bf
    tweets = {"twitter": {"nodes": 2, "tokens": 20}}
    signed_in = _prefilled(prefilled_handle="a", last_seen_at=T0)
    deleted_private = _prefilled(prefilled_handle="b")
    _node(deleted_private, deleted=True)
    public_loore = _prefilled(prefilled_handle="c",
                              public_sharing_enabled=True)
    _node(public_loore, privacy="public")      # public now; before? unknown
    loore_profile = _prefilled(prefilled_handle="d")
    _profile(loore_profile, source_origin_stats={"loore": {"nodes": 1,
                                                           "tokens": 5}})
    clean = _prefilled(prefilled_handle="e")
    _profile(clean, source_origin_stats=tweets)
    empty = _user()                            # a pre-fill still to come
    already = _user(profile_private_read=True)
    out = []
    marked = bf.run(apply=False, out=out.append)
    assert set(marked) == {signed_in.id, deleted_private.id,
                           public_loore.id, loore_profile.id}
    assert "dry run" in out[-1]
    assert not User.query.filter(User.id.in_(marked),
                                 User.profile_private_read.is_(True)).count()
    assert bf.run(apply=True, out=out.append) == marked
    db.session.expire_all()
    for uid in marked + [already.id]:
        assert db.session.get(User, uid).profile_private_read is True
    for user in (clean, empty):
        assert db.session.get(User, user.id).profile_private_read is False
    assert pw.writing_is_public_only(db.session.get(User, clean.id))
    assert bf.run(apply=True, out=out.append) == []    # idempotent


# ── #596's paths under the rules of 2026-11-05: the hard line ────────────
# A one-window build from scratch and a small build of more than one
# window, for an Anthropic and an OpenAI user with private writing: every
# request goes to the user's own provider. A public-only pre-fill: GPT-6
# Luna versions and the Opus 5.5 integration, at any size. When in doubt
# (the owner signed in before the final), private.

def _one_chunk_switches(app):
    app.config["PROFILE_ONE_CHUNK_WINDOW_OPENAI"] = True
    app.config["PROFILE_ONE_CHUNK_WINDOW_ANTHROPIC"] = True
    app.config["PROFILE_MERGE_STRUCTURE"] = "flat"


@pytest.mark.parametrize("chat, provider", [
    ("gpt-6-astra", "openai"), (None, "anthropic")])
def test_a_private_one_window_build_stays_on_the_chat_model(
        app, world, chat, provider):
    _one_chunk_switches(app)
    u = _user(preferred_model=chat, profile_needs_full_regen=True)
    _node(u)                                   # one private entry
    assert not pw.cross_provider_allowed(u)
    world.corpus = Corpus(50)
    _finish(world, u)
    chat_model = chat or "claude-opus-5.5"
    reqs = _all_reqs(world)
    assert {p for p, _r in reqs} == {provider}
    assert {r["model_id"] for _p, r in reqs} == {chat_model}
    [window] = [r for _p, r in reqs if "_w" in r["custom_id"]]
    assert ex.EVENTS_IN_WINDOW_PARAGRAPH in (
        window["messages"][0]["content"][0]["text"])
    [final] = [r for _p, r in reqs if "_f_" in r["custom_id"]]
    [row] = _rows(u, "window")
    assert final["messages"] == ex.single_integration_messages(u.id, row)
    assert "reasoning_effort" not in final
    [profile] = _finals(u)
    assert profile.generated_by == chat_model
    db.session.refresh(u)
    assert u.profile_private_read is True


@pytest.mark.parametrize("chat, provider, version, merge", [
    ("gpt-6-astra", "openai", "gpt-6-luna", "gpt-6.1-sol"),
    (None, "anthropic", "claude-haiku-5.5", "claude-opus-5.5")])
def test_a_private_small_multi_window_build_stays_on_its_provider(
        app, world, chat, provider, version, merge):
    """Two chunks on the chat model (140k units): the provider's pair."""
    _one_chunk_switches(app)
    u = _user(preferred_model=chat, profile_needs_full_regen=True)
    _node(u)
    world.corpus = Corpus(140)
    assert pw.wants_window_build(u)
    assert not pw.one_window_pass(u, pw.set_state(u, "s", None))
    _finish(world, u)
    reqs = _all_reqs(world)
    assert {p for p, _r in reqs} == {provider}
    windows = [r for _p, r in reqs if "_w" in r["custom_id"]]
    merges = [r for _p, r in reqs if "_w" not in r["custom_id"]]
    assert len(windows) >= 2
    assert {r["model_id"] for r in windows} == {version}
    assert {r["model_id"] for r in merges} == {merge}
    assert len(_finals(u)) == 1


def test_a_no_during_a_consented_one_window_build_keeps_its_final_home(
        app, world, monkeypatch):
    """#596 with #599: an OpenAI user who said yes gets a one-window
    build's window on GPT-6 Luna and its integration on Opus 5.5. A no
    given after the final's pass picked its models sends nothing to
    Anthropic, not even the integration's token count; the next pass
    writes the integration with the user's preferred model."""
    _one_chunk_switches(app)
    u = _user(preferred_model="gpt-6-astra", profile_needs_full_regen=True,
              profile_both_providers=True,
              profile_both_providers_answered_at=datetime.utcnow())
    _node(u)                                   # private, but agreed
    world.corpus = Corpus(50)
    _advance(u)
    [(provider, window)] = _all_reqs(world)
    assert (provider, window["model_id"]) == ("openai", "gpt-6-luna")
    _poll()
    n = len(world.provider.submits)
    counted = []
    import backend.llm_providers as lp
    monkeypatch.setattr(lp.LLMProvider, "count_tokens", staticmethod(
        lambda model_id, messages, k: counted.append(model_id)))
    real = pw.chained_update_base

    def models_then_no(*a, **k):
        out = real(*a, **k)
        _withdraw(u)                   # after the pass computed `cross`
        return out
    monkeypatch.setattr(pw, "chained_update_base", models_then_no)
    assert _advance(u) == "partial"
    assert "claude-opus-5.5" not in counted
    assert len(world.provider.submits) == n       # nothing sent
    monkeypatch.setattr(pw, "chained_update_base", real)
    _finish(world, u)
    after = _all_reqs(world, n)
    assert [(p, r["model_id"]) for p, r in after] == [
        ("openai", "gpt-6-astra")]
    assert "_f_" in after[0][1]["custom_id"]
    assert "claude-opus-5.5" not in counted
    assert len(_finals(u)) == 1


def _public_prefill(world, monkeypatch, units_k, **kw):
    """A pre-filled account nobody signed into, whose writing is tweets
    Loore fetched (public only), with ``units_k`` thousand units."""
    import backend.utils.email as email
    monkeypatch.setattr(email, "send_admin_prefill_complete_notification",
                        lambda *a, **k: None)
    u = _prefilled(profile_needs_full_regen=True, prefilled_handle="someone",
                   **kw)
    world.corpus = Corpus(units_k)
    world.corpus.origin = "twitter"            # pre-filled tweets
    return u


@pytest.mark.parametrize("chat, units_k, ratio, n_windows", [
    ("gpt-6-astra", 50, None, 1),   # one window: Opus, single-version
    (None, 50, None, 1),
    (None, 130, 2.4, 2),            # one Opus chunk, two Luna windows
])
def test_a_public_only_prefill_takes_luna_and_opus(
        app, world, monkeypatch, chat, units_k, ratio, n_windows):
    """Rule 2 names pre-fills and the lab: GPT-6 Luna for versions, Opus
    5.5 for merges, also when the writing fits one chunk on the chat
    model (no Sol, and not the chat model). The windows are sized for
    Luna."""
    _one_chunk_switches(app)
    kw = ({"profile_token_ratio": ratio,
           "profile_token_ratio_family": "o200k"} if ratio else {})
    u = _public_prefill(world, monkeypatch, units_k, preferred_model=chat,
                        **kw)
    assert pw.cross_provider_allowed(u)
    assert pw.wants_window_build(u)
    _finish(world, u)
    reqs = _all_reqs(world)
    windows = {(p, r["model_id"]) for p, r in reqs if "_w" in r["custom_id"]}
    assert windows == {("openai", "gpt-6-luna")}
    assert len(_rows(u, "window")) == n_windows
    [final] = [(p, r) for p, r in reqs if "_f_" in r["custom_id"]]
    assert (final[0], final[1]["model_id"]) == ("anthropic",
                                                "claude-opus-5.5")
    assert "reasoning_effort" not in final[1]
    rows = _rows(u, "window")
    expected = (ex.single_integration_messages(u.id, rows[0])
                if n_windows == 1 else
                ex._integration_messages(u.id, rows, independent=True))
    assert final[1]["messages"] == expected
    db.session.refresh(u)
    assert u.profile_private_read is False


def test_a_prefill_signed_into_before_its_final_is_integrated_privately(
        app, world, monkeypatch):
    """When in doubt, private: the owner of an OpenAI pre-fill signs in
    after Luna read the window, so the integration runs on their chat
    model, never on Opus."""
    _one_chunk_switches(app)
    u = _public_prefill(world, monkeypatch, 50, preferred_model="gpt-6-astra")
    _advance(u)
    assert {(p, r["model_id"]) for p, r in _all_reqs(world)} == {
        ("openai", "gpt-6-luna")}
    _poll()
    u.last_seen_at = datetime.utcnow()         # someone signed in
    db.session.commit()
    assert not pw.cross_provider_allowed(u)
    n = len(world.provider.submits)
    _advance(u)
    [final] = _all_reqs(world, n)
    assert final[0] == "openai" and final[1]["model_id"] == "gpt-6-astra"
    assert "_f_" in final[1]["custom_id"]


# ── one run per user (2026-11-07: a redelivered build and a second dispatch
# rendered at the same time) ───────────────────────────────────────────────

@pytest.fixture
def real_is_task_stale():
    """The real exports._is_task_stale, taken before ``app`` replaces it:
    list this fixture before ``app`` / ``world``."""
    import backend.tasks.exports as _ex
    fn = _ex._is_task_stale
    assert fn.__name__ == "_is_task_stale", "list this fixture first"
    return fn


@pytest.fixture
def redis(monkeypatch):
    from backend.tests.fake_redis import install
    return install(monkeypatch)


def _run_lock(user, token):
    from backend.utils import run_locks
    return run_locks.RunLock(run_locks.profile_run_key(user.id), token,
                             run_locks.PROFILE_RUN_TTL)


def _result_state(monkeypatch, state):
    """What the result backend says of every task id."""
    import backend.celery_app as ca
    from types import SimpleNamespace
    monkeypatch.setattr(ca.celery, "AsyncResult",
                        lambda task_id: SimpleNamespace(state=state),
                        raising=False)


def test_a_second_copy_of_a_running_advance_renders_nothing(world, redis):
    """Its own message delivered again while it runs (the same task id):
    the copy exits at once and leaves the guard to the running copy."""
    world.corpus = _windows_corpus(3)
    u = _user(profile_needs_full_regen=True)
    task_id = pw.dispatch_advance(u)
    assert _run_lock(u, task_id).acquire()        # the first copy runs
    assert pw.advance(u.id, task_id) == pw.RUNNING_ELSEWHERE
    assert world.corpus.renders == [] and world.provider.submits == []
    db.session.refresh(u)
    assert u.profile_generation_task_id == task_id
    assert u.profile_batch_pending is True


def test_no_dispatch_while_a_run_holds_the_lock_even_without_a_guard(
        world, redis):
    world.corpus = _windows_corpus(3)
    u = _user(profile_needs_full_regen=True)
    assert _run_lock(u, "t-running").acquire()
    assert pw.dispatch_advance(u) is None
    pw.advance_profile_windows.apply_async.assert_not_called()
    out = pw.run_advance_inline(u, "t-sync")
    assert out["message"] == "A build is running"
    assert world.corpus.renders == []


def test_a_dispatch_that_finds_the_lock_held_hands_the_guard_back(
        world, redis):
    """A copy started under a new guard while another run held the lock
    (a path that took the guard for stale): it exits, and the guard names
    the run that holds the lock again, whose exit then clears it and
    dispatches what is left."""
    world.corpus = _windows_corpus(3)
    u = _user(profile_needs_full_regen=True)
    assert _run_lock(u, "t-running").acquire()
    u.profile_generation_task_id = "t-second"
    db.session.commit()
    assert pw.advance(u.id, "t-second") == pw.RUNNING_ELSEWHERE
    db.session.refresh(u)
    assert u.profile_generation_task_id == "t-running"
    assert world.corpus.renders == []


def test_the_seeder_keeps_a_failed_task_s_guard_while_its_run_beats(
        real_is_task_stale, world, redis, monkeypatch):
    """The 2026-11-07 sequence: the hard limit killed the task (FAILURE in
    the result backend), its message was delivered again and the copy
    rendered. The hourly seeder must neither clear the guard nor start a
    second run while the copy's heartbeat is fresh; once a dead run's
    lock has expired, it resumes the build."""
    monkeypatch.setattr(pw._exports, "_is_task_stale", real_is_task_stale)
    _result_state(monkeypatch, "FAILURE")
    world.corpus = _windows_corpus(3)
    u = _user(profile_needs_full_regen=True)
    task_id = pw.dispatch_advance(u)
    assert _run_lock(u, task_id).acquire()        # the copy renders
    pw.advance_profile_windows.apply_async.reset_mock()

    assert pw._exports._is_task_stale(u) is False
    pb._seed_profile_batches()
    db.session.refresh(u)
    assert u.profile_generation_task_id == task_id
    assert u.profile_batch_pending is True
    pw.advance_profile_windows.apply_async.assert_not_called()

    # The copy is killed too: no beat, and its lock expires on its own.
    from backend.utils import run_locks
    redis.advance(run_locks.PROFILE_RUN_TTL + 1)
    assert pw._exports._is_task_stale(u) is True
    pb._seed_profile_batches()
    db.session.refresh(u)
    pw.advance_profile_windows.apply_async.assert_called_once()
    assert u.profile_generation_task_id not in (None, task_id)


def test_a_run_beats_before_each_render_and_releases_at_the_end(
        world, redis):
    world.corpus = _windows_corpus(4)
    from backend.utils import run_locks
    u = _user(profile_needs_full_regen=True)
    key = run_locks.profile_run_key(u.id)
    ttls = []
    real = world.corpus.export

    def export(*a, **k):
        ttls.append(redis.ttl(key))
        redis.advance(120)     # each render takes two minutes
        return real(*a, **k)
    world.corpus.export = export
    assert _advance(u) == "submitted"
    # Ten minutes of rendering under a 15-minute TTL: each render found
    # the key refreshed just before it.
    assert len(ttls) >= 4
    assert all(t == run_locks.PROFILE_RUN_TTL for t in ttls)
    assert redis.get(key) is None


def test_a_run_releases_its_lock_before_it_dispatches_the_rest(
        app, world, redis):
    """The render budget used: the rest goes to a new task, which must
    not find the lock still held and take itself for a second copy."""
    world.corpus = _windows_corpus(6)
    app.config["PROFILE_WINDOW_RENDER_SECONDS"] = 0
    u = _user(profile_needs_full_regen=True)
    assert _advance(u) == "partial"
    assert pw.advance_profile_windows.apply_async.call_count == 2
    db.session.refresh(u)
    assert pw.advance(u.id, u.profile_generation_task_id) != (
        pw.RUNNING_ELSEWHERE)


def test_a_run_that_lost_its_lock_while_frozen_sends_nothing_more(
        world, redis):
    """A run frozen past the lock's TTL finds another run holding the
    lock at its next beat: it stops, sends nothing of what it built, and
    leaves the guard and the lock to the other run."""
    world.corpus = _windows_corpus(4)
    from backend.utils import run_locks
    u = _user(profile_needs_full_regen=True)
    key = run_locks.profile_run_key(u.id)
    real = world.corpus.export
    calls = [0]

    def export(*a, **k):
        calls[0] += 1
        if calls[0] == 2:
            # Frozen: the lock expires and another run takes it.
            redis.advance(run_locks.PROFILE_RUN_TTL + 1)
            redis.set(key, "t-other", nx=True, ex=run_locks.PROFILE_RUN_TTL)
        return real(*a, **k)
    world.corpus.export = export
    task_id = pw.dispatch_advance(u)
    assert pw.advance(u.id, task_id) == "superseded"
    assert world.provider.submits == []
    assert ProfileBatchJob.query.count() == 0
    db.session.refresh(u)
    assert u.profile_generation_task_id == task_id   # not cleared by it
    assert redis.get(key) == b"t-other"


def test_the_soft_time_limit_in_a_token_count_stops_the_run(
        world, redis, monkeypatch):
    """A token count lets the task's soft time limit through (it used to
    come back as None, and the run went on rendering to the hard limit):
    the run stops before it sends anything, its handler records the stop,
    clears the guard and releases the lock."""
    from billiard.exceptions import SoftTimeLimitExceeded
    from backend.utils import run_locks
    import backend.llm_providers as lp
    world.corpus = _windows_corpus(4)
    counts = [0]

    def count(model_id, messages, keys):
        counts[0] += 1
        if counts[0] == 2:
            raise SoftTimeLimitExceeded()
        return None
    monkeypatch.setattr(lp.LLMProvider, "count_tokens", staticmethod(count))
    u = _user(profile_needs_full_regen=True)
    assert _advance(u) == "error"
    assert counts[0] == 2
    assert world.provider.submits == []
    db.session.refresh(u)
    assert u.profile_generation_task_id is None
    assert "SoftTimeLimitExceeded" in (u.profile_seed_error or "")
    assert redis.get(run_locks.profile_run_key(u.id)) is None
    pw.advance_profile_windows.apply_async.assert_called_once()  # no rerun


def test_the_byte_and_render_defaults_without_config(app, world,
                                                     monkeypatch):
    """15 MB of prompts per slice and 600 s of rendering per task when the
    config has neither (as config.py's defaults)."""
    from backend.config import Config
    app.config.pop("PROFILE_BATCH_MAX_BYTES")
    app.config.pop("PROFILE_WINDOW_RENDER_SECONDS")
    assert pw._Submission({}, {"anthropic"}).max_bytes == 15_000_000
    deadlines = []
    monkeypatch.setattr(pw.time, "monotonic", lambda: 1000.0)
    monkeypatch.setattr(pw, "next_work", lambda user, keys, deadline, w,
                        task_id=None: (deadlines.append(deadline) or "idle"))
    u = _user(profile_needs_full_regen=True)
    _advance(u)
    assert deadlines == [1600.0]
    if not os.environ.get("PROFILE_BATCH_MAX_BYTES"):
        assert Config.PROFILE_BATCH_MAX_BYTES == 15_000_000
    if not os.environ.get("PROFILE_WINDOW_RENDER_SECONDS"):
        assert Config.PROFILE_WINDOW_RENDER_SECONDS == 600


# ── review of #611: builds of accounts outside the hourly cohort ─────────

def _waiting_prefill(**kw):
    """An account outside the hourly seeder's cohort: a pre-fill waiting
    for its owner (not approved, never deactivated)."""
    u = _user(**kw)
    u.approved = False
    db.session.commit()
    assert u.id not in {x.id for x in User.profile_eligible_query()}
    return u


def test_a_killed_run_s_build_resumes_outside_the_cohort(
        real_is_task_stale, world, redis, monkeypatch):
    """The first delivery is killed for memory and its message requeued at
    once: the copy finds the dead run's lock and exits. Once the lock
    expires, the hourly seeder resumes the build, also for an account it
    reaches only through the work hold."""
    from backend.utils import run_locks
    monkeypatch.setattr(pw._exports, "_is_task_stale", real_is_task_stale)
    _result_state(monkeypatch, "SUCCESS")       # the copy's own result
    world.corpus = _windows_corpus(3)
    u = _waiting_prefill(profile_needs_full_regen=True)
    task_id = pw.dispatch_advance(u)
    assert _run_lock(u, task_id).acquire()        # then killed
    assert pw.advance(u.id, task_id) == pw.RUNNING_ELSEWHERE
    db.session.refresh(u)
    assert u.profile_work_held is True
    pw.advance_profile_windows.apply_async.reset_mock()
    pb._seed_profile_batches()                    # the dead lock lives
    pw.advance_profile_windows.apply_async.assert_not_called()
    redis.advance(run_locks.PROFILE_RUN_TTL + 1)
    pb._seed_profile_batches()
    pw.advance_profile_windows.apply_async.assert_called_once()


def test_a_soft_limit_stop_resumes_outside_the_cohort(
        world, redis, monkeypatch):
    from billiard.exceptions import SoftTimeLimitExceeded
    import backend.llm_providers as lp
    world.corpus = _windows_corpus(4)
    counts = [0]

    def count(model_id, messages, keys):
        counts[0] += 1
        if counts[0] == 2:
            raise SoftTimeLimitExceeded()
        return None
    monkeypatch.setattr(lp.LLMProvider, "count_tokens", staticmethod(count))
    u = _waiting_prefill(profile_needs_full_regen=True)
    assert _advance(u) == "error"
    db.session.refresh(u)
    assert u.profile_work_held is True
    assert u.profile_generation_task_id is None
    pw.advance_profile_windows.apply_async.reset_mock()
    pb._seed_profile_batches()
    pw.advance_profile_windows.apply_async.assert_called_once()


def test_a_soft_limit_in_a_set_s_submit_resumes_outside_the_cohort(
        world, redis, monkeypatch):
    """The soft limit inside the submission of a window set's slice: the
    advance stops, nothing is counted against the windows, and the hourly
    seeder resumes the build of an account outside its cohort."""
    from billiard.exceptions import SoftTimeLimitExceeded
    world.corpus = _windows_corpus(3)

    def submit(*a, **k):
        raise SoftTimeLimitExceeded()
    monkeypatch.setattr(pb, "batch_submit", submit)
    u = _waiting_prefill(profile_needs_full_regen=True)
    assert _advance(u) == "error"
    assert ProfileBatchJob.query.count() == 0      # no "missing" outcome
    db.session.refresh(u)
    assert u.profile_work_held is True
    assert u.profile_batch_pending is False
    monkeypatch.setattr(pb, "batch_submit",
                        lambda *a, **k: world.provider.submit(*a, **k))
    pw.advance_profile_windows.apply_async.reset_mock()
    pb._seed_profile_batches()
    pw.advance_profile_windows.apply_async.assert_called_once()

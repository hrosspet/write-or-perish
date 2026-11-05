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
                "origin_stats": {"loore": {"nodes": len(picked),
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
    monkeypatch.setattr(pb, "batch_submit",
                        lambda *a, **k: w.provider.submit(*a, **k))
    monkeypatch.setattr(pb, "batch_check_and_collect",
                        lambda *a, **k: w.provider.collect(*a, **k))
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

def _set_of(user, n, set_id="set1", base=None, start=T0, step=None,
            created=None):
    """``n`` saved windows of one set, contiguous, a day each."""
    step = step or timedelta(days=1)
    rows, cum = [], (base.source_tokens_used if base else 0)
    prev = base.source_data_cutoff if base else None
    for i in range(n):
        cutoff = start + step * (i + 1)
        cum += 1000
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


def test_held_writing_drops_window_results(world, monkeypatch):
    world.corpus = _windows_corpus(2)
    u = _user(profile_needs_full_regen=True)
    _advance(u)
    monkeypatch.setattr(pb, "writing_on_hold", lambda uid: True)
    _poll()
    assert _rows(u, "window") == []
    db.session.refresh(u)
    assert u.profile_batch_pending is False


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


# ── content-policy blocks (2026-11-04) ───────────────────────────────────
# A window, group or final the provider's content-policy filter blocked
# is final: its own outcome, never a provider failure, never resent by
# the seeder, the poll path or the 24 h stop expiry, and the account
# builds nothing further until the admin's confirmed Build profile lifts
# the stop.

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


def test_a_policy_blocked_window_stops_the_account_for_good(
        world, monkeypatch):
    from backend.models import APICostLog
    from backend.utils import refusal_backoff as rb
    world.corpus = _windows_corpus(3)
    reports = _policy_reports(monkeypatch)
    _blocking(world, monkeypatch, "_w1_")
    u = _user(profile_needs_full_regen=True)

    _advance(u)
    windows_sent = len(_items("window"))
    assert windows_sent >= 2
    # Every OpenAI or Anthropic request names its user.
    assert {r["user_id"] for r in world.provider.requests()} == {u.id}
    _poll()

    [blocked] = [i for i in _items("window") if i.get("outcome") == "blocked"]
    assert blocked["start"] is None
    # The other windows of the batch are saved.
    assert len(_rows(u, "window")) == windows_sent - 1
    db.session.refresh(u)
    assert u.profile_seed_error.startswith(pw.POLICY_STOP_NOTE + " window")
    assert "bio_policy" in u.profile_seed_error
    row = APICostLog.query.filter_by(
        user_id=u.id, request_ref="policy_block:bio_policy").one()
    assert row.cost_microdollars == 0
    assert [r[:2] for r in reports] == [(u.id, "bio_policy")]
    # Not a provider failure: no stop that expires.
    history = pw.attempt_history(u.id)
    assert pw.provider_stop_until(history, ("window", None)) is None

    # Nothing further goes out: not the window, not the rest of the set,
    # not a merge; not from the advance, the hourly seeder or an import's
    # seed; not a day later.
    submits = len(world.provider.submits)
    assert _advance(u) == "blocked"
    assert pb._should_seed(u) is False
    assert _seed(u) == 0
    for job in ProfileBatchJob.query.all():
        job.submitted_at -= timedelta(days=2)
        job.collected_at = (job.collected_at or job.submitted_at) \
            - timedelta(days=2)
    db.session.commit()
    assert _advance(u) == "blocked"
    assert _seed(u) == 0
    assert len(world.provider.submits) == submits
    assert not _rows(u, "integration")
    assert not pw.set_work_due(u, ex.profile_update_base(u.id))
    # The note stays (a saved window clears profile_seed_error).
    db.session.refresh(u)
    assert u.profile_seed_error.startswith(pw.POLICY_STOP_NOTE)

    # The admin's confirmed Build profile lifts the stop: the next pass
    # sends the blocked range again (and a block again stops it again).
    rb.lift_policy_block(u.id, "profile_batch", rb.PROFILE_REQUEST_TYPES)
    u.profile_needs_full_regen = False
    db.session.commit()
    assert _advance(u) == "submitted"
    resent = [r for r in world.provider.requests() if "_w" in r["custom_id"]]
    assert resent
    assert any(i["start"] is None and i.get("outcome") is None
               for i in _items("window"))


@pytest.mark.parametrize("kind", ["final"])
def test_a_policy_blocked_merge_is_its_own_outcome(world, monkeypatch, kind):
    reports = _policy_reports(monkeypatch)
    u = _user()
    rows = _set_of(u, 5, created=datetime.utcnow())
    _corpus_for(world, rows)
    _blocking(world, monkeypatch, "_f_")
    _advance(u)
    _poll()
    [item] = _items(kind)
    assert item["outcome"] == "blocked"
    db.session.refresh(u)
    assert u.profile_seed_error.startswith(
        pw.POLICY_STOP_NOTE + " final merge")
    assert [r[1] for r in reports] == ["bio_policy"]
    submits = len(world.provider.submits)
    assert _advance(u) == "blocked"
    assert pb._should_seed(u) is False
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

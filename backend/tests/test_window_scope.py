"""One metadata read per profile pass (#612): the same windows as before.

A profile pass cuts its windows from one read of the metadata
(routes/export_data.WindowScope, tasks/profile_windows._PassScope) where
each window used to read the metadata of all the writing after its start,
once for its unit count and once per render. These tests run both on the
same synthetic corpora:

- ``WindowScope.rows`` against ``_select_incremental_rows`` (which every
  count and render of the old path calls) for many date bounds: the same
  rows in the same order;
- whole builds through the advance task and the poller, once with the
  old reads (``exports.window_scope`` returns None: every count and
  render reads the database) and once with the one read: the same
  requests byte for byte (prompts, models, custom ids), the same job
  items (window bounds, units, cumulative units, origin stats) and the
  same saved rows;
- changes made while a pass runs, the reviewer's cases of #617 among
  them, and window ends inside runs of rows that share a timestamp.

On SQLite (the default) the old walk returns rows that share a timestamp
in a fixed order, which the read reproduces. Set
WINDOW_SCOPE_TEST_DATABASE_URL to a Postgres database to run them there:
the old walk's order for such rows is its query plan's, so there the
rows are compared as sets, and the old path of a whole build is given
the read's order for them (``_canonical``) after its rows are checked
to be the same.

No model is called (the batch and token-count boundaries are stand-ins)
and every text is synthetic.
"""
import os
import random
import re
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace
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
    User, UserProfile, ProfileBatchJob, Node)

pb = pw = ex = ed = None
REAL = {}
TEST_DB = os.environ.get("WINDOW_SCOPE_TEST_DATABASE_URL",
                         "sqlite:///:memory:")
SQLITE = TEST_DB.startswith("sqlite")

MODEL_IDS = ("claude-opus-5.5", "claude-haiku-5.5", "gpt-6-astra",
             "gpt-6-luna", "gpt-6.1-sol")
T0 = datetime(2024, 1, 1)
WORDS = ("river stone ledger quiet morning signal thread window garden "
         "letter lantern harbor orbit meadow cipher compass ember").split()


def _good(events=True, tag="v"):
    text = (f"### SURFACE MAP\nSynthetic surface {tag}.\n\n"
            f"### SHADOW MAP\nSynthetic shadow {tag}.\n\n"
            f"### LIGHT MAP\nSynthetic light {tag}.\n")
    if events:
        text += f"\n### EVENTS IN THIS WINDOW\n- 2024-01: synthetic {tag}\n"
    return text


class Provider:
    """batch_submit / batch_check_and_collect stand-in. ``skip`` names
    custom ids that get no result when first collected (a provider that
    lost them), so their windows are rendered again in a later pass."""

    def __init__(self, skip=()):
        self.submits = []
        self.n = 0
        self.skip = set(skip)

    def submit(self, requests_by_provider, keys, phase=None):
        from backend.utils.llm_batch import SubmittedBatches, openai_batch_key
        out = SubmittedBatches()
        self.submits.append(requests_by_provider)
        for provider, reqs in requests_by_provider.items():
            keys = {"anthropic"} if provider == "anthropic" else {
                openai_batch_key(r["api_model"], r.get("endpoint"))
                for r in reqs}
            for key in sorted(keys):
                self.n += 1
                out[key] = f"batch_{self.n}"
        return out

    def collect(self, batch_ids, keys):
        results = {}
        for submit in self.submits:
            for reqs in submit.values():
                for req in reqs:
                    cid = req["custom_id"]
                    if cid in self.skip:
                        continue
                    results[cid] = {
                        "content": _good(events="_w" in cid, tag=cid),
                        "input_tokens": 1000, "output_tokens": 500,
                        "batch": True}
        return results, {}, {}


def _count_tokens(model_id, messages, keys):
    """A deterministic count of the real prompt: a character per two
    tokens, so the rendered scaffolding counts and a window can be over
    the cap and fitted again."""
    text = "".join(part.get("text", "") for m in messages
                   for part in (m["content"] if isinstance(m["content"], list)
                                else [{"text": m["content"]}]))
    return len(text) // 2


@pytest.fixture
def app(monkeypatch):
    global pb, pw, ex, ed
    import backend.tasks.profile_batch as _pb
    import backend.tasks.profile_windows as _pw
    import backend.tasks.exports as _ex
    import backend.routes.export_data as _ed
    pb, pw, ex, ed = _pb, _pw, _ex, _ed
    REAL.update(select=ed._select_incremental_rows, scope=ex.window_scope,
                units=pw._PassScope.units)
    # The old path as the tests run it: the walk itself on SQLite, the
    # walk in the read's order for tied rows elsewhere (_canonical).
    REAL["old"] = (REAL["select"] if SQLITE
                   else _canonical(REAL["select"]))
    monkeypatch.setattr(ed, "_select_incremental_rows", REAL["old"])
    from backend.config import Config
    from backend.tests.fake_redis import install
    models = {k: dict(Config.SUPPORTED_MODELS[k]) for k in MODEL_IDS}
    app = Flask(__name__)
    app.config.update(
        SQLALCHEMY_DATABASE_URI=TEST_DB, SECRET_KEY="t",
        TESTING=True, DEFAULT_LLM_MODEL="claude-opus-5.5",
        SUPPORTED_MODELS=models,
        PROFILE_USE_BATCH=True, PROFILE_BATCH_USER_IDS=set(),
        OPENAI_API_KEY_BATCH=None, PROFILE_INDEPENDENT_VERSIONS=True,
        PROFILE_VERSION_MODEL_ANTHROPIC="claude-haiku-5.5",
        PROFILE_VERSION_MODEL_OPENAI="gpt-6-luna",
        PROFILE_MERGE_MODEL_ANTHROPIC="claude-opus-5.5",
        PROFILE_MERGE_MODEL_OPENAI="gpt-6.1-sol",
        PROFILE_MERGE_REASONING_EFFORT_OPENAI="",
        PROFILE_VERSION_MODEL_CROSS_PROVIDER="gpt-6-luna",
        PROFILE_MERGE_MODEL_CROSS_PROVIDER="claude-opus-5.5",
        PROFILE_MERGE_STRUCTURE="grouped", PROFILE_MERGE_GROUP_SIZE=8,
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
                        staticmethod(_count_tokens))
    monkeypatch.setattr(pw, "advance_profile_windows", MagicMock())
    monkeypatch.setattr(pw._exports, "_is_task_stale", lambda u: True)
    import backend.utils.notifications as notes
    monkeypatch.setattr(notes, "notify_profile_ready", MagicMock())
    monkeypatch.setattr(pb, "list_batches", lambda *a, **k: {})
    install(monkeypatch)
    with app.app_context():
        db.drop_all()
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


def _by_id_fields(rows):
    return {r.id: (r.parent_id, r.created_at, r.token_count, r.deleted_at,
                   r.origin) for r in rows}


def _canonical(select):
    """``_select_incremental_rows`` with the read's order for rows that
    share a timestamp, for databases whose order for them is the plan's:
    the walk's rows, checked to be the read's rows field for field, in
    the read's order. Everything else of the old path stays the old
    path's."""
    def walk(user_id, filter_ai_usage=False, created_before=None,
             created_after=None):
        old = select(user_id, filter_ai_usage=filter_ai_usage,
                     created_before=created_before,
                     created_after=created_after)
        scope = ed.WindowScope(user_id, filter_ai_usage=bool(filter_ai_usage),
                               floor=created_after)
        new = scope.rows(created_after, created_before)
        assert _by_id_fields(old) == _by_id_fields(new)
        return new
    return walk


def _same_rows(new, old):
    """The read's rows against the walk's: the same rows with the same
    fields, and on SQLite also in the same order."""
    assert _by_id_fields(new) == _by_id_fields(old)
    if SQLITE:
        assert _key(new) == _key(old)


# ── a synthetic corpus ────────────────────────────────────────────────────

def _text(rng, units):
    words = []
    size = 0
    while size < units * 4:
        w = rng.choice(WORDS)
        words.append(w)
        size += len(w) + 1
    return " ".join(words)[:max(units * 4, 1)]


class _Corpus:
    """Synthetic writing in the shapes the export walks: conversations of
    the writer and the model (the model's rows addressed to the writer),
    flat tweets, other people's public threads the writer replied deep
    inside, other people's replies under the writer's rows (public and
    private), late replies to old threads, rows sharing a timestamp, a
    reply dated before its parent, tombstones (soft-deleted rows of the
    writer), deleted threads, and rows marked 'none'."""

    def __init__(self, seed, units, *, start=T0, big=0, skew=True):
        self.rng = random.Random(seed)
        self.t = start
        self.total = 0
        self.writer = _user("writer")
        self.model = _user("model")
        self.friend = _user("friend")
        self.mine = []
        self.add(units, skew=skew)
        if big:
            self._chain(2, units=big)
        db.session.commit()

    def _node(self, author, parent, units, *, kind="user", ai="chat",
              privacy="private", origin=None, at=None, deleted=False):
        rng = self.rng
        owner = self.writer if author is not self.friend else self.friend
        n = Node(user_id=author.id, human_owner_id=owner.id,
                 parent_id=parent.id if parent is not None else None,
                 node_type=kind,
                 llm_model="synthetic-model" if kind == "llm" else None,
                 privacy_level=privacy, ai_usage=ai, origin=origin)
        n.set_content(_text(rng, units))
        n.token_count = units
        n.created_at = at or self.t
        if deleted:
            n.deleted_at = n.created_at + timedelta(days=30)
        db.session.add(n)
        db.session.flush()
        self.total += units
        if author is self.writer:
            self.mine.append(n)
        return n

    def _step(self, lo=1, hi=90):
        self.t += timedelta(minutes=self.rng.randint(lo, hi))

    def _size(self):
        return max(1, min(6000, int(self.rng.lognormvariate(5.6, 0.9))))

    def _chain(self, n, units=None, origin=None, skew=False):
        rng = self.rng
        parent = None
        dead = rng.random() < 0.04         # a deleted conversation
        for i in range(n):
            author = self.writer if i % 2 == 0 else self.model
            ai = "none" if rng.random() < 0.05 else rng.choice(
                ("chat", "chat", "train"))
            at = self.t
            if skew and i and rng.random() < 0.05:
                at = self.t - timedelta(minutes=5)   # before its parent
            parent = self._node(
                author, parent, units or self._size(),
                kind="user" if author is self.writer else "llm", ai=ai,
                origin=origin, at=at,
                deleted=dead or (author is self.writer
                                 and rng.random() < 0.06))
            if rng.random() < 0.15:
                continue                        # the next shares its time
            self._step(1, 20)

    def add(self, units, skew=True):
        rng = self.rng
        goal = self.total + units
        while self.total < goal:
            r = rng.random()
            if r < 0.5:
                self._chain(rng.randint(2, 14),
                            origin=rng.choice((None, None, "chatgpt")),
                            skew=skew)
            elif r < 0.62:
                for _ in range(rng.randint(1, 6)):   # tweets, ties
                    self._node(self.writer, None, rng.randint(8, 70),
                               origin="twitter",
                               ai="none" if rng.random() < 0.05 else "chat")
                    if rng.random() < 0.6:
                        self.t += timedelta(seconds=1)
            elif r < 0.74:
                # Someone else's public thread the writer replied deep in.
                root = self._node(self.friend, None, self._size(),
                                  privacy="public")
                cur = root
                for _ in range(rng.randint(1, 4)):
                    self._step(1, 30)
                    cur = self._node(self.friend, cur, self._size(),
                                     privacy="public")
                self._step(1, 30)
                mine = self._node(self.writer, cur, self._size())
                self._step(1, 30)
                self._node(self.friend, mine, self._size(),
                           privacy=rng.choice(("public", "private")))
                self._step(1, 30)
                self._node(self.model, mine, self._size(), kind="llm")
            elif r < 0.84 and self.mine:
                # A late reply to an old thread, and someone's answer.
                old = rng.choice(self.mine)
                reply = self._node(self.writer, old, self._size())
                if rng.random() < 0.5:
                    self._step(1, 10)
                    self._node(self.friend, reply, self._size(),
                               privacy="public")
            elif r < 0.92 and self.mine:
                # Someone else's reply under an old row of the writer.
                old = rng.choice(self.mine)
                self._node(self.friend, old, self._size(),
                           privacy="public",
                           ai="none" if rng.random() < 0.1 else "chat")
            else:
                self._chain(1)
            self._step()
        db.session.commit()


def _user(name, **kw):
    u = User(username=name, plan="alpha", twitter_id=None, approved=True,
             preferred_model=None, **kw)
    db.session.add(u)
    db.session.commit()
    return u


# ── 1. the rows of a window ───────────────────────────────────────────────

def _key(rows):
    return [(r.id, r.parent_id, r.created_at, r.token_count, r.deleted_at,
             r.origin) for r in rows]


def _bounds(corpus, rng, floor):
    dates = sorted({n.created_at for n in Node.query.all()})
    if floor is not None:
        dates = [d for d in dates if d >= floor]
    picks = rng.sample(dates, min(25, len(dates)))
    afters = ([None] if floor is None else [floor]) + picks + [
        d + timedelta(microseconds=1) for d in picks[:5]] + [
        d - timedelta(microseconds=1) for d in picks[5:10]
        if floor is None or d - timedelta(microseconds=1) >= floor]
    out = []
    for after in afters:
        out.append((after, None))
        later = [d for d in dates if after is None or d > after]
        for d in rng.sample(later, min(3, len(later))):
            out.append((after, d + timedelta(microseconds=1)))
            out.append((after, d))
    return out


@pytest.mark.parametrize("seed", [1, 2, 3])
@pytest.mark.parametrize("filter_ai_usage", [True, False])
def test_scope_rows_are_the_incremental_rows(app, seed, filter_ai_usage):
    """Every date pair a pass can ask for, from the floor on: the rows,
    their fields and, on SQLite, their order (which decides ties at a
    window's edge and the order of entries sharing a timestamp)."""
    corpus = _Corpus(seed, 60_000)
    uid = corpus.writer.id
    rng = random.Random(seed)
    dates = sorted({n.created_at for n in Node.query.all()})
    for floor in (None, dates[len(dates) // 3]):
        scope = ed.WindowScope(uid, filter_ai_usage=filter_ai_usage,
                               floor=floor)
        for after, before in _bounds(corpus, rng, floor):
            old = REAL["select"](
                uid, filter_ai_usage=filter_ai_usage,
                created_before=before, created_after=after)
            _same_rows(scope.rows(after, before), old)
            assert scope.units(after, before) == ed.count_remaining_units(
                uid, after, filter_ai_usage=filter_ai_usage,
                created_before=before)
        assert scope.reads == 1


def test_a_reply_under_a_parent_the_climb_up_found(app):
    """The old walk's corner: someone's row X is the parent of an anchor
    (found by the climb up) and the reply to an anchor above it. A window
    that holds the lower anchor does not climb down through X, so X's
    other replies stay out; a window that ends before the lower anchor
    climbs down through X and holds them. The read keeps both."""
    w, f = _user("writer"), _user("friend")

    def node(author, parent, at, privacy="public"):
        n = Node(user_id=author.id, human_owner_id=author.id,
                 parent_id=parent.id if parent else None, node_type="user",
                 privacy_level=privacy, ai_usage="chat")
        n.set_content("synthetic")
        n.token_count = 10
        n.created_at = T0 + timedelta(hours=at)
        db.session.add(n)
        db.session.flush()
        return n

    top = node(w, None, 1)
    x = node(f, top, 2)
    node(w, x, 10)                 # the lower anchor
    other = node(f, x, 3)
    db.session.commit()
    scope = ed.WindowScope(w.id)
    for after, before in [(None, None), (None, T0 + timedelta(hours=5)),
                          (T0, T0 + timedelta(hours=11))]:
        old = REAL["select"](w.id, True, before, after)
        _same_rows(scope.rows(after, before), old)
    assert other.id not in {r.id for r in scope.rows()}
    assert other.id in {r.id for r in scope.rows(
        None, T0 + timedelta(hours=5))}


def test_a_window_below_the_floor_is_refused(app):
    corpus = _Corpus(4, 5_000)
    scope = ed.WindowScope(corpus.writer.id, floor=T0 + timedelta(days=1))
    with pytest.raises(ValueError):
        scope.rows(None)
    with pytest.raises(ValueError):
        scope.rows(T0)


def _render_same(user, scope, units):
    via = ex.render_profile_window(user, units, None, scope=scope)
    direct = ex.render_profile_window(user, units, None)
    assert _normal(via.pop("content")) == _normal(direct.pop("content"))
    assert via == direct
    return via


@pytest.mark.parametrize("whose", ["writer", "friend"])
def test_a_changed_row_is_not_rendered_from_the_read(app, whose):
    """A row of the window hidden after the pass read the metadata: the
    render raises ScopeChanged before any row is loaded, and the window
    rendered from a new read is the database's. A change to the user's
    own rows also shows in fresh(), checked before each window; one to
    someone else's row in the window only in the render's check."""
    corpus = _Corpus(5, 20_000, skew=False)
    user = corpus.writer
    scope = ex.window_scope(user.id)
    first = _render_same(user, scope, 5_000)
    assert scope.fresh()
    owner = corpus.writer if whose == "writer" else corpus.friend
    hidden = (Node.query.filter(Node.id.in_(first["node_ids"]),
                                Node.user_id == owner.id)
              .order_by(Node.id).first())
    assert hidden is not None
    hidden.ai_usage = "none"
    db.session.commit()
    assert scope.fresh() is (whose == "friend")
    with pytest.raises(ed.ScopeChanged):
        ex.render_profile_window(user, 5_000, None, scope=scope)
    scope.reload()
    again = _render_same(user, scope, 5_000)
    assert hidden.id not in again["node_ids"]


# ── 2. whole builds: the old reads and the one read ───────────────────────

def _normal(text):
    """The text without the export's render time (one line)."""
    out, n = re.subn(r"\*\*Export Date:\*\* [^\n]*", "**Export Date:** -",
                     text)
    assert n <= 1
    return out


def _texts(messages):
    """Every text part of a request's messages, the export time taken
    out, in order: (role, text)."""
    out = []
    for m in messages:
        parts = (m["content"] if isinstance(m["content"], list)
                 else [{"type": "text", "text": m["content"]}])
        for part in parts:
            out.append((m["role"], part.get("type"),
                        _normal(part.get("text", ""))))
    return tuple(out)


def _capture(provider, user):
    requests = []
    for submit in provider.submits:
        for prov in sorted(submit):
            for req in submit[prov]:
                requests.append((prov, req["custom_id"], req["model_id"],
                                 req.get("endpoint"), req.get("max_tokens"),
                                 repr(_texts(req["messages"]))))
    items = []
    for job in ProfileBatchJob.query.order_by(ProfileBatchJob.id).all():
        for item in job.items or []:
            items.append({k: v for k, v in item.items()
                          if k not in ("rendered_at", "built_at", "read_at")})
    rows = [(p.generation_type, p.window_set, p.covers_start,
             p.source_data_cutoff, p.source_tokens_used,
             p.source_origin_stats, p.generated_by, p.parent_profile_id,
             _normal(p.get_content()))
            for p in UserProfile.query.order_by(UserProfile.id).all()]
    return requests, items, rows


class _Clock:
    """time.monotonic for profile_windows: a second per call, so a render
    budget runs out after the same windows in both runs."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        self.now += 1.0
        return self.now


def _run(app, monkeypatch, one_read, build, *, skip=(), clock=False,
         rounds=12, config=None, hook=None):
    """Build a corpus (``build`` returns the writer), run advance + poll
    until nothing is pending, and return what went out and was saved.
    ``one_read`` False: every count and render reads the database, as
    before #612. ``hook(n)`` runs before the pass's n-th window count (a
    change to the writing while the pass runs)."""
    db.session.remove()
    db.drop_all()
    db.create_all()
    sets = iter(f"set{i:04d}" for i in range(1, 100))
    monkeypatch.setattr(pw, "new_set_id", lambda: next(sets))
    provider = Provider(skip=skip)
    monkeypatch.setattr(pb, "batch_submit", provider.submit)
    monkeypatch.setattr(pb, "batch_check_and_collect", provider.collect)
    if clock:
        monkeypatch.setattr(pw, "time", SimpleNamespace(
            monotonic=_Clock().monotonic))
    else:
        monkeypatch.setattr(pw, "time", __import__("time"))
    for key, value in (config or {}).items():
        monkeypatch.setitem(app.config, key, value)
    reads = {"select": 0, "scope": 0}
    # The functions as the modules define them (an earlier run of the
    # same test replaced the module attributes with its own counters).
    real_select = REAL["old"]
    real_scope = REAL["scope"]

    def counted_select(*a, **k):
        reads["select"] += 1
        return real_select(*a, **k)

    def scope_or_none(user_id, created_after=None):
        if not one_read:
            return None
        scope = real_scope(user_id, created_after)
        reads["scope"] += 1
        scopes.append(scope)
        return scope

    scopes = []

    monkeypatch.setattr(ed, "_select_incremental_rows", counted_select)
    monkeypatch.setattr(pw._exports, "window_scope", scope_or_none)
    real_units = REAL["units"]
    counts = [0]

    def units(self, start, end):
        counts[0] += 1
        if hook is not None:
            hook(counts[0])
        return real_units(self, start, end)

    monkeypatch.setattr(pw._PassScope, "units", units)

    def drive(user):
        for _ in range(rounds):
            task_id = pw.dispatch_advance(user)
            assert task_id is not None
            pw.advance(user.id, task_id)
            if not ProfileBatchJob.query.filter_by(status="pending").count():
                if not pw.set_work_due(user, ex.profile_update_base(user.id)):
                    break
                continue
            pb._poll_profile_batches()

    user = build(drive)
    out = _capture(provider, user)
    # A static corpus: each pass read its metadata once, never again.
    reads["reloads"] = sum(s.reads - 1 for s in scopes)
    return out, reads


def _same(app, monkeypatch, build, changes=False, **kw):
    old, old_reads = _run(app, monkeypatch, False, build, **kw)
    new, new_reads = _run(app, monkeypatch, True, build, **kw)
    requests, items, rows = new
    # Whole prompts are compared: every window's export, to its end.
    reads = [r for r in requests if re.search(r"_[wu]\d+_\d+$", r[1])]
    assert reads and all("*End of Export*" in r[-1] for r in reads)
    assert requests == old[0]
    assert items == old[1]
    assert rows == old[2]
    assert old_reads["scope"] == 0
    assert new_reads["scope"] >= 1
    assert (new_reads["reloads"] >= 1) if changes else (
        new_reads["reloads"] == 0)
    # The old path read the rows for every count and render of a window;
    # the new one only for each pass's plan (set_state, the routing).
    assert new_reads["select"] < old_reads["select"]
    return new, old_reads, new_reads


def _windows(items):
    return [i for i in items if i["kind"] == "window"]


def _fresh_build(units, seed, **corpus_kw):
    def build(drive):
        corpus = _Corpus(seed, units, **corpus_kw)
        user = corpus.writer
        user.profile_needs_full_regen = True
        db.session.commit()
        drive(user)
        return user
    return build


def _small_cap(cap):
    from backend.config import Config
    models = {k: dict(Config.SUPPORTED_MODELS[k]) for k in MODEL_IDS}
    models["claude-haiku-5.5"]["long_context_threshold"] = cap
    return {"SUPPORTED_MODELS": models}


def test_a_small_build_is_the_same(app, monkeypatch):
    """170k units at Haiku's cap: a few windows, a group, the final."""
    (requests, items, rows), old_reads, new_reads = _same(
        app, monkeypatch, _fresh_build(170_000, 11))
    assert len(_windows(items)) >= 3
    assert any(r[0] == "integration" for r in rows)


def test_a_build_of_many_windows_is_the_same(app, monkeypatch):
    """The same kind of corpus at an 11k-token cap: dozens of windows,
    most fitted again after their first count, threads that run across
    window ends, tombstones, rows marked 'none', other people's threads
    and replies, tweets sharing a second."""
    (requests, items, rows), old_reads, new_reads = _same(
        app, monkeypatch, _fresh_build(170_000, 12),
        config=_small_cap(11_000))
    windows = _windows(items)
    assert len(windows) >= 20
    assert old_reads["select"] > 2 * len(windows)
    assert new_reads["select"] < len(windows)
    assert any(r[0] == "integration" for r in rows)


def test_a_one_window_build_is_the_same(app, monkeypatch):
    """A from-scratch build that fits one window on the chat model (the
    one-chunk window path): the chat model reads it whole."""
    (requests, items, rows), _o, _n = _same(
        app, monkeypatch, _fresh_build(20_000, 13),
        config={"PROFILE_ONE_CHUNK_WINDOW_ANTHROPIC": True})
    assert len(_windows(items)) == 1
    assert _windows(items)[0]["model_id"] == "claude-opus-5.5"


def test_an_entry_larger_than_a_window_is_the_same(app, monkeypatch):
    """An entry over the version model's cap is never cut: its window is
    given up (a window_skip row), the same one on both paths."""
    (requests, items, rows), _o, _n = _same(
        app, monkeypatch, _fresh_build(170_000, 14, big=20_000),
        config=_small_cap(11_000))
    assert any(r[0] == "window_skip" for r in rows)


def test_a_resumed_set_is_the_same(app, monkeypatch):
    """Windows lost at the provider and a render budget that runs out:
    the next passes render the gaps between saved windows (bounded on
    both sides) and the rest after the last one."""
    skip = {f"profile_1_set0001_w{n}_1" for n in (2, 5, 9)}
    (requests, items, rows), _o, _n = _same(
        app, monkeypatch, _fresh_build(170_000, 15), skip=skip, clock=True,
        config=dict(_small_cap(11_000), PROFILE_WINDOW_RENDER_SECONDS=12))
    windows = _windows(items)
    assert any(w["attempt"] == 2 for w in windows)
    assert any(r[0] == "window" for r in rows)


def test_a_blocked_range_is_the_same(app, monkeypatch):
    """A range the provider's policy filter blocked for good: the windows
    end where it starts and begin where it ends, and its units are counted
    for its skip row."""
    def build(drive):
        corpus = _Corpus(16, 170_000)
        user = corpus.writer
        dates = sorted(n.created_at for n in corpus.mine)
        pw.record_blocked_range(user.id, dates[len(dates) // 3],
                                dates[len(dates) // 3 + 20], "bio_policy")
        user.profile_needs_full_regen = True
        db.session.commit()
        drive(user)
        return user
    (requests, items, rows), _o, _n = _same(app, monkeypatch, build,
                                            config=_small_cap(11_000))
    assert any(r[0] == "window_skip" for r in rows)


def test_a_chained_one_window_update_is_the_same(app, monkeypatch):
    """A merged set, then new writing after its last window that fits one
    chunk on the chat model: the chat model writes a chained update of
    the set's final, and merges after it."""
    def build(drive):
        corpus = _Corpus(17, 170_000)
        user = corpus.writer
        user.profile_needs_full_regen = True
        db.session.commit()
        drive(user)
        corpus.t += timedelta(days=3)
        corpus.add(30_000)
        drive(user)
        return user
    (requests, items, rows), _o, _n = _same(app, monkeypatch, build)
    assert any(i.get("generation_type") == pw.CHAINED_TYPE for i in items)


# ── 3. writing that changes while a pass runs ─────────────────────────────

def _late(author, share=2 / 3):
    """A readable row of ``author`` about ``share`` into the writing."""
    rows = sorted((n for n in Node.query.filter_by(user_id=author.id)
                   if n.ai_usage in ("chat", "train")
                   and n.deleted_at is None),
                  key=lambda n: (n.created_at, n.id))
    return rows[int(len(rows) * share)]


def _changed_build(state, author="writer"):
    """170k units at an 11k cap with one marked row of ``author`` two
    thirds in; ``state`` gets its id and date and the writer's id."""
    def build(drive):
        corpus = _Corpus(18, 170_000)
        user = corpus.writer
        who = corpus.writer if author == "writer" else corpus.friend
        target = _late(who)
        target.set_content("QMARKQ " * max(1, target.token_count // 2))
        state.update(id=target.id, at=target.created_at, writer=user.id)
        user.profile_needs_full_regen = True
        db.session.commit()
        drive(user)
        return user
    return build


def _hide_at(state, n):
    def hook(count):
        if count == n:
            node = db.session.get(Node, state["id"])
            node.ai_usage = "none"
            db.session.commit()
    return hook


def test_a_row_hidden_during_the_pass_is_never_sent(app, monkeypatch):
    """The writer marks a later entry None while the pass renders: the
    next window's count sees the change (fresh), the pass reads its
    metadata again, and the windows are the ones the per-window reads
    make; the entry reaches no model."""
    state = {}
    (requests, items, rows), _o, _n = _same(
        app, monkeypatch, _changed_build(state), changes=True,
        hook=_hide_at(state, 5), config=_small_cap(11_000))
    assert requests
    assert not any("QMARKQ" in r[-1] for r in requests)


def test_a_row_added_during_the_pass_is_read(app, monkeypatch):
    """An entry dated inside a later window is added while the pass
    renders (an import): read again, the pass's windows hold it, as the
    per-window reads do."""
    state = {}

    def hook(count):
        if count == 5:
            node = Node(user_id=state["writer"],
                        human_owner_id=state["writer"], node_type="user",
                        privacy_level="private", ai_usage="chat")
            node.set_content("QADDEDQ " * 40)
            node.token_count = 80
            node.created_at = state["at"] + timedelta(seconds=1)
            db.session.add(node)
            db.session.commit()

    (requests, items, rows), _o, _n = _same(
        app, monkeypatch, _changed_build(state), changes=True, hook=hook,
        config=_small_cap(11_000))
    assert sum("QADDEDQ" in r[-1] for r in requests) == 1


def test_someone_elses_row_hidden_during_the_pass_is_never_sent(
        app, monkeypatch):
    """A reply by someone else, marked None after the pass read the
    metadata: the user's own rows did not change (fresh), so the pass
    goes on with its read until the window that holds the reply, whose
    render finds it changed (check), reads again and renders without
    it."""
    state = {}
    runs = [_run(app, monkeypatch, one_read, _changed_build(state, "friend"),
                 hook=_hide_at(state, 5), config=_small_cap(11_000))
            for one_read in (False, True)]
    for (requests, _items, _rows), _reads in runs:
        assert requests
        assert not any("QMARKQ" in r[-1] for r in requests)
    assert runs[1][1]["reloads"] >= 1


def test_rows_that_keep_changing_read_the_database(app, monkeypatch):
    """A window whose rows differ from the read twice in a row (an edit
    burst, or a read that differs from the database's walk): that window
    and the rest of the pass read the database per window, as before,
    and the fallback is reported."""
    def changed(self, ids, created_after=None, created_before=None, **kw):
        raise ed.ScopeChanged("synthetic")

    reports = []
    monkeypatch.setattr(pw, "_report",
                        lambda title, **tags: reports.append(title))
    old, _o = _run(app, monkeypatch, False, _fresh_build(170_000, 19),
                   config=_small_cap(11_000))
    monkeypatch.setattr(ed.WindowScope, "check", changed)
    new, reads = _run(app, monkeypatch, True, _fresh_build(170_000, 19),
                      config=_small_cap(11_000))
    assert new == old
    fallbacks = [t for t in reports if "two reads" in t]
    assert fallbacks and reads["reloads"] == len(fallbacks)


# ── 4. what decides a window beyond its own rows (review of #617) ─────────

def _purge(user):
    from backend.utils import user_purge
    user_purge.schedule_purge(user, requested_by_id=user.id, source="self")
    db.session.commit()


def _restore(user):
    from backend.utils import user_purge
    assert user_purge.cancel_purge(user.id, user.id)
    db.session.commit()


def _tombstone_thread(own_reply=False):
    """E: the writer's entry, deleted before the pass, 10 units; C: a
    5,000-unit reply under it nine hours later (a friend's public one, or
    the writer's own). A 300-unit window selects E alone, and keeps it as
    a tombstone because C below it is live (§5a)."""
    w, f = _user("writer"), _user("friend")

    def node(author, parent, hours, units, privacy):
        n = Node(user_id=author.id, human_owner_id=author.id,
                 parent_id=parent.id if parent else None, node_type="user",
                 privacy_level=privacy, ai_usage="chat")
        n.set_content("synthetic " * max(1, units // 2))
        n.token_count = units
        n.created_at = T0 + timedelta(hours=hours)
        db.session.add(n)
        db.session.flush()
        return n

    e = node(w, None, 1, 10, "private")
    e.deleted_at = T0 + timedelta(days=2)
    c = node(w if own_reply else f, e, 10, 5_000,
             "private" if own_reply else "public")
    db.session.commit()
    return w, f, e, c


def _change(kind, w, f, c):
    if kind == "deleted":
        c.deleted_at = datetime.utcnow()
    elif kind == "private":
        c.set_privacy_level("private")
    elif kind == "ai_none":
        c.ai_usage = "none"
    elif kind == "account":
        f.deleted_at = datetime.utcnow()
    elif kind == "purge":
        _purge(w)
        return
    db.session.commit()


@pytest.mark.parametrize("kind", ["deleted", "private", "ai_none", "account",
                                  "purge"])
def test_a_thread_left_without_live_rows_is_left_out(app, kind):
    """The reviewer's cases: after the pass read the metadata, the only
    live row under a tombstone entry goes (someone's reply deleted, made
    private, marked None, gone with its author's account; or the writer's
    own reply, by "Delete all my writing"). The database walk then sends
    nothing for the window; from the old read the thread would go out as
    tombstone lines. The render's check finds the change (ScopeChanged),
    and the pass's render, read again, sends nothing either."""
    w, f, e, c = _tombstone_thread(own_reply=(kind == "purge"))
    stale = ex.window_scope(w.id)
    pass_scope = pw._PassScope(w, [None])
    before = ex.render_profile_window(w, 300, None)
    assert before is not None and before["node_ids"] == {e.id}
    via = pass_scope.render(300, None, None)
    assert _normal(via.pop("content")) == _normal(before.pop("content"))
    assert via == before
    _change(kind, w, f, c)
    assert ex.render_profile_window(w, 300, None) is None
    with pytest.raises(ed.ScopeChanged):
        ex.render_profile_window(w, 300, None, scope=stale)
    assert pass_scope.render(300, None, None) is None
    assert pass_scope.index is not None and pass_scope.index.reads == 2


def test_a_reply_made_visible_after_the_read_is_rendered(app):
    """The other way: a reply under a window's entry that was private when
    the pass read the metadata is made public, so the database walk
    takes it in; the read is taken again and the window holds it too."""
    w, f, e, c = _tombstone_thread()
    e.deleted_at = None
    c.set_privacy_level("private")
    c.token_count = 50
    db.session.commit()
    stale = ex.window_scope(w.id)
    c.set_privacy_level("public")
    db.session.commit()
    direct = ex.render_profile_window(w, 5_000, None)
    assert c.id in direct["node_ids"]
    with pytest.raises(ed.ScopeChanged):
        ex.render_profile_window(w, 5_000, None, scope=stale)
    stale.reload()
    _render_same(w, stale, 5_000)


def test_delete_all_my_writing_and_its_restore_are_seen(app):
    """Both set or clear deleted_at on every row of the writer and leave
    updated_at as it was: the deleted count and the newest deletion in
    fresh() see them."""
    corpus = _Corpus(20, 8_000)
    user = corpus.writer
    scope = ex.window_scope(user.id)
    assert scope.fresh()
    _purge(user)
    assert not scope.fresh()
    scope.reload()
    assert scope.fresh()
    _restore(user)
    assert not scope.fresh()


def test_delete_and_restore_during_the_pass_are_the_same(app, monkeypatch):
    """"Delete all my writing" before the 5th window count and "Restore my
    writing" before the 15th: each is seen before the next window, and
    the build is the one the per-window reads make."""
    state = {}

    def hook(count):
        user = db.session.get(User, state["writer"])
        if count == 5:
            _purge(user)
        elif count == 15:
            _restore(user)

    _same(app, monkeypatch, _changed_build(state), changes=True, hook=hook,
          config=_small_cap(11_000))


# ── 5. window ends inside runs of rows that share a timestamp ─────────────

def _tied_build(drive_phases):
    """170k units of tweets in bursts that share one second and of the
    writer's messages and the model's replies stamped alike, at an
    11k-token cap: many window ends fall inside such runs."""
    def build(drive):
        corpus = _Corpus(21, 0)
        rng = corpus.rng
        while corpus.total < 170_000:
            if rng.random() < 0.5:
                for _ in range(rng.randint(2, 8)):
                    corpus._node(corpus.writer, None, rng.randint(40, 600),
                                 origin="twitter")
            else:
                mine = corpus._node(corpus.writer, None,
                                    rng.randint(40, 900))
                corpus._node(corpus.model, mine, rng.randint(40, 900),
                             kind="llm")
            corpus._step(1, 5)
        user = corpus.writer
        user.profile_needs_full_regen = True
        db.session.commit()
        drive_phases(drive, user)
        return user
    return build


def _shuffled(select, seed):
    """The walk with its rows in a shuffled order, as a query plan may
    return them: rows that share a timestamp come in any order."""
    rng = random.Random(seed)

    def walk(*a, **k):
        rows = list(select(*a, **k))
        rng.shuffle(rows)
        return rows
    return walk


@pytest.mark.parametrize("phases", ["old", "new", "old_then_new"])
def test_window_ends_never_split_a_timestamp(app, monkeypatch, phases):
    """Whatever order rows that share a timestamp come in (the old walk
    on Postgres; here shuffled), and when a set the old path started is
    resumed by the new one: every window starts at the previous window's
    end, no row is in two windows, and every row of the writer is in
    exactly one."""
    windows = []
    real_fit = pw.fit_window

    def fit(*a, **k):
        fitted = real_fit(*a, **k)
        if fitted is not None:
            windows.append((a[4], fitted[0]["latest_node_created_at"],
                            set(fitted[0]["node_ids"])))
        return fitted

    monkeypatch.setattr(pw, "fit_window", fit)
    monkeypatch.setattr(ed, "_select_incremental_rows",
                        _shuffled(REAL["select"], 7))
    use_read = {"on": phases == "new"}
    monkeypatch.setattr(pw._exports, "window_scope",
                        lambda uid, after=None: (REAL["scope"](uid, after)
                                                 if use_read["on"] else None))
    monkeypatch.setattr(pw, "time", SimpleNamespace(
        monotonic=_Clock().monotonic))
    monkeypatch.setitem(app.config, "PROFILE_WINDOW_RENDER_SECONDS", 10)
    for key, value in _small_cap(11_000).items():
        monkeypatch.setitem(app.config, key, value)
    provider = Provider()
    monkeypatch.setattr(pb, "batch_submit", provider.submit)
    monkeypatch.setattr(pb, "batch_check_and_collect", provider.collect)

    def drive_phases(drive, user):
        for _ in range(30):
            task_id = pw.dispatch_advance(user)
            pw.advance(user.id, task_id)
            if phases == "old_then_new":
                use_read["on"] = True       # the next pass resumes the set
            if ProfileBatchJob.query.filter_by(status="pending").count():
                pb._poll_profile_batches()
            elif not pw.set_work_due(user, ex.profile_update_base(user.id)):
                break

    user = _tied_build(drive_phases)(None)
    assert len(windows) >= 12
    ties = {}
    for n in Node.query.all():
        ties[n.created_at] = ties.get(n.created_at, 0) + 1
    ends = [end for _start, end, _ids in windows]
    assert sum(ties.get(end, 0) > 1 for end in ends) >= 3   # ends in runs
    seen = {}
    for i, (start, end, ids) in enumerate(windows):
        assert start == (windows[i - 1][1] if i else None)
        for node_id in ids:
            assert node_id not in seen, (node_id, seen.get(node_id), i)
            seen[node_id] = i
    from sqlalchemy import or_
    mine = {n.id for n in Node.query.filter(
        or_(Node.user_id == user.id, Node.human_owner_id == user.id),
        *ed._scope_filter(user.id, True, None, None))}
    assert mine <= set(seen)

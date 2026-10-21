"""Every row a profile window selects is in the text sent to the model,
once, in order (#552).

A profile window's resume cursor (``latest_node_created_at``) and unit
count come from the rows the budget selection takes; the next window
starts at ``created_at > cursor``. The text used to come from the rows
the quote resolver kept under the same budget, and the two disagreed:

- a quote embed counted against the budget, so the window's newest rows
  were left out of the text;
- a run of rows sharing the boundary timestamp is taken whole by the
  selection (#285), and the resolver then dropped the part over the
  budget;
- a row larger than the whole budget was selected alone and dropped, so
  the window rendered nothing and the chain stopped;
- a node the resolver embedded counted as part of the window: a reply
  under a quoted older parent was not rendered (its parent "was in the
  export"), and a quoted newer row was rendered as its own entry here and
  again in its own window.

The cursor moved past every row left out, so no later window read it.
Now the selection is the budget: the resolver embeds quotes and drops no
selected row, and an embedded node appears only inside its quote. Which
rows a window selects, its units and its cursor do not change.

In-memory SQLite, ENCRYPTION_DISABLED, celery mocked so the task modules
import; the export runs for real, the model calls are scripted.
"""
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

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

from backend.extensions import db as _db  # noqa: E402
from backend.models import Node, User, UserProfile  # noqa: E402

MODEL = {"provider": "anthropic", "api_model": "claude-x",
         "context_window": 1_000_000, "tokenizer_family": "claude_new",
         "input_price_per_mtok": 5.0, "output_price_per_mtok": 25.0}

CUTOFF = datetime(2026, 4, 1)
T0 = datetime(2026, 4, 10, 9, 0, 0)
OLD = datetime(2026, 3, 1, 8, 0, 0)       # before the cutoff

UPDATE_TEMPLATE = ("U {existing_profile}\n{new_data}|{source_tokens_past}"
                   "|{source_tokens_new}|{ratio_percent}")

QUOTE_BLOCK = re.compile(r"\n--- Quoted from .*?\n--- End quote ---\n", re.S)


@pytest.fixture
def app(monkeypatch):
    # Warm celery_app first: it resolves backend.tasks.exports and
    # backend.tasks.profile_batch in the safe import order (see
    # test_profile_regen_resume).
    import backend.celery_app  # noqa: F401
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["SUPPORTED_MODELS"] = {"m": dict(MODEL)}
    app.config["DEFAULT_LLM_MODEL"] = "m"
    _db.init_app(app)
    import backend.llm_providers as lp
    monkeypatch.setattr(lp.LLMProvider, "count_tokens",
                        staticmethod(lambda model_id, messages, keys: None))
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()


def _alice():
    alice = User(username="alice", plan="alpha", approved=True,
                 twitter_id=None, preferred_model="m")
    _db.session.add(alice)
    _db.session.flush()
    return alice


def _node(owner, text, when, units=100, parent=None, origin=None):
    n = Node(user_id=owner.id, human_owner_id=owner.id,
             parent_id=parent.id if parent else None, node_type="user",
             privacy_level="private", ai_usage="chat", origin=origin,
             created_at=when, updated_at=when, token_count=units)
    n.set_content(text)
    _db.session.add(n)
    _db.session.flush()
    return n


def _window(user, units, cutoff=CUTOFF):
    from backend.tasks.exports import render_profile_window
    return render_profile_window(user, units, cutoff)


def _entries(text):
    """The text without its quote blocks: what the window renders as its
    own entries."""
    return QUOTE_BLOCK.sub("\n", text)


# ── The two causes measured in #552 ──────────────────────────────────────

def test_a_quote_embed_keeps_the_newest_entries(app):
    """Five 100-unit entries at 600 units (500 after the header
    allowance), the first quoting an older 1,000-character entry: the
    embed was charged 250, and the last three entries were left out of
    the text while the cursor moved past the fifth."""
    alice = _alice()
    old = _node(alice, "OLDQUOTED " + "x" * 990, OLD, units=250)
    entries = [
        _node(alice, f"ENTRY{i} " + (f"{{quote:{old.id}}}" if i == 1 else ""),
              T0 + timedelta(minutes=i))
        for i in range(1, 6)
    ]
    _db.session.commit()

    w = _window(alice, 600)

    assert w["node_ids"] == {e.id for e in entries}
    assert w["unit_count"] == 500
    assert w["latest_node_created_at"] == entries[-1].created_at
    text = w["content"]
    positions = [text.index(f"ENTRY{i}") for i in range(1, 6)]
    assert positions == sorted(positions)
    # The quoted entry is in the text once, inside the quote.
    assert text.count("OLDQUOTED") == 1
    assert "OLDQUOTED" not in _entries(text)


def test_a_tie_run_at_the_window_end_is_rendered_whole(app):
    """Three 100-unit entries, then four on one second, at 550 units (450
    after the allowance): the selection takes the whole run so that no
    row on that second is left behind, and the text now holds it too."""
    alice = _alice()
    before = [_node(alice, f"BEFORE{i}", T0 + timedelta(minutes=i))
              for i in range(3)]
    tie_ts = T0 + timedelta(minutes=10)
    tie = [_node(alice, f"TIED{i}", tie_ts) for i in range(4)]
    after = _node(alice, "AFTER", T0 + timedelta(minutes=11))
    _db.session.commit()

    w = _window(alice, 550)

    assert w["node_ids"] == {n.id for n in before + tie}
    assert w["latest_node_created_at"] == tie_ts
    for marker in [f"BEFORE{i}" for i in range(3)] + [f"TIED{i}" for i in range(4)]:
        assert w["content"].count(marker) == 1, marker
    assert "AFTER" not in w["content"]

    nxt = _window(alice, 550, w["latest_node_created_at"])
    assert nxt["node_ids"] == {after.id}
    assert "TIED" not in nxt["content"]


def test_tied_tweets_render_each_once_across_windows(app):
    """Imported tweets carry second precision. Walking a flat tweet corpus
    with several tie runs window by window, each tweet reaches the text
    exactly once, oldest first."""
    alice = _alice()
    seconds = [0, 0, 0, 1, 2, 2, 3, 3, 3, 3, 4, 5]
    tweets = [_node(alice, f"TWEET{i:02d}", T0 + timedelta(seconds=s),
                    origin="twitter")
              for i, s in enumerate(seconds)]
    _db.session.commit()

    texts, cutoff, covered = [], CUTOFF, 0
    while True:
        w = _window(alice, 350, cutoff)    # 250 units after the allowance
        if not w:
            break
        texts.append(w["content"])
        covered += w["unit_count"]
        cutoff = w["latest_node_created_at"]
    assert len(texts) > 1

    joined = "\n".join(texts)
    for t in tweets:
        assert joined.count(t.get_content()) == 1, t.get_content()
    assert covered == 100 * len(tweets)
    order = sorted(tweets, key=lambda t: joined.index(t.get_content()))
    assert [t.created_at for t in order] == sorted(t.created_at for t in tweets)


# ── More ways a selected row was left out ────────────────────────────────

def test_a_row_larger_than_the_budget_is_rendered(app):
    """The selection always takes a window's first row; the resolver
    dropped one larger than the budget, so the window rendered nothing and
    the build stopped at that cutoff (the stall utils/node_split.py
    describes)."""
    alice = _alice()
    big = _node(alice, "BIGENTRY", T0, units=300)
    nxt = _node(alice, "NEXTENTRY", T0 + timedelta(minutes=1))
    _db.session.commit()

    w = _window(alice, 200)

    assert w["node_ids"] == {big.id}
    assert "BIGENTRY" in w["content"] and "NEXTENTRY" not in w["content"]
    assert _window(alice, 200, w["latest_node_created_at"])["node_ids"] \
        == {nxt.id}


def test_a_reply_under_a_quoted_older_parent_is_rendered(app):
    """A reply in an old thread that quotes the message it answers: the
    embedded parent counted as part of the window, so the reply was not an
    entry point and was never rendered."""
    alice = _alice()
    parent = _node(alice, "PARENTTEXT from before the cutoff", OLD)
    reply = _node(alice, f"REPLYTEXT on {{quote:{parent.id}}}", T0,
                  parent=parent)
    plain = _node(alice, "PLAINTEXT", T0 + timedelta(minutes=1))
    _db.session.commit()

    w = _window(alice, 100_000)

    assert w["node_ids"] == {reply.id, plain.id}
    text = w["content"]
    assert "REPLYTEXT" in text and "PLAINTEXT" in text
    assert text.index("REPLYTEXT") < text.index("PLAINTEXT")
    assert "Continuation of thread started 2026-03-01" in text
    assert text.count("PARENTTEXT") == 1
    assert "PARENTTEXT" not in _entries(text)


def test_a_quoted_newer_row_renders_in_its_own_window_only(app):
    """An entry quoting a newer one (an edit can add the quote later): the
    newer row is embedded in the quote, and it is an entry of its own
    window only, not also of this one."""
    alice = _alice()
    newer_ts = T0 + timedelta(hours=1)
    quoting = _node(alice, "QUOTINGTEXT", T0)
    newer = _node(alice, "NEWERTEXT", newer_ts)
    quoting.set_content(f"QUOTINGTEXT {{quote:{newer.id}}}")
    _db.session.commit()

    first = _window(alice, 250)            # 150 units: the first entry only
    assert first["node_ids"] == {quoting.id}
    assert first["content"].count("NEWERTEXT") == 1
    assert "NEWERTEXT" not in _entries(first["content"])
    assert "# Thread 2" not in first["content"]

    second = _window(alice, 250, first["latest_node_created_at"])
    assert second["node_ids"] == {newer.id}
    assert "NEWERTEXT" in _entries(second["content"])


def test_a_whole_window_needs_the_incremental_export(app):
    """The authored-threads path has its own selection; the flag is not
    silently ignored there."""
    from backend.routes.export_data import build_user_export_content
    alice = _alice()
    _node(alice, "ENTRY", T0)
    _db.session.commit()

    with pytest.raises(ValueError):
        build_user_export_content(alice, max_tokens=1000,
                                  filter_ai_usage=True, whole_window=True)


# ── The chain a profile build walks ──────────────────────────────────────

def test_a_chunked_build_sends_every_row_once_in_order(app, monkeypatch):
    """The synchronous chunk loop at a 300-unit chunk target over a corpus
    with a quote embed, tie runs, a reply under a quoted older parent and a
    quoted newer row: every row of the remainder is in exactly one prompt,
    as an entry, oldest first, and the chain's coverage is the sum of the
    rows."""
    import backend.tasks.exports as exports
    from backend.utils import chunk_plan

    alice = _alice()
    old_parent = _node(alice, "OLDPARENT", OLD)
    rows = []
    t = T0
    rows.append(_node(alice, f"R00 {{quote:{old_parent.id}}}", t,
                      parent=old_parent))
    for i in range(1, 6):
        t += timedelta(minutes=1)
        rows.append(_node(alice, f"R{i:02d}", t))
    t += timedelta(minutes=1)
    for i in range(6, 10):                  # a run on one second
        rows.append(_node(alice, f"R{i:02d}", t, origin="twitter"))
    for i in range(10, 14):
        t += timedelta(minutes=1)
        rows.append(_node(alice, f"R{i:02d}", t))
    t += timedelta(minutes=1)
    for i in range(14, 17):                 # another run
        rows.append(_node(alice, f"R{i:02d}", t))
    t += timedelta(minutes=1)
    newer = _node(alice, "R18", t + timedelta(minutes=1))
    rows.append(_node(alice, f"R17 {{quote:{newer.id}}}", t))
    rows.append(newer)
    base = UserProfile(user_id=alice.id, generated_by="m", ai_usage="chat",
                       generation_type="update", source_tokens_used=1000,
                       source_data_cutoff=CUTOFF,
                       created_at=datetime(2026, 4, 5))
    base.set_content("BASEPROFILE")
    _db.session.add(base)
    _db.session.commit()

    def small_target(remaining, max_units=None):
        return chunk_plan.next_window_budget(remaining, max_units=max_units,
                                             target=300)

    prompts = []

    def llm(task, model_id, prompt, user_id, keys, **k):
        prompts.append(prompt)
        return {"content": f"PROFILE{len(prompts)}", "input_tokens": 1000,
                "output_tokens": 5, "total_tokens": 1005}

    monkeypatch.setattr(exports, "next_window_budget", small_target)
    monkeypatch.setattr(exports, "_call_llm_with_retries", llm)
    _pid, chunks, covered = exports._chunked_profile_loop(
        MagicMock(), alice, "m", update_template=UPDATE_TEMPLATE,
        api_keys={}, initial_profile_content=base.get_content(),
        initial_profile_id=base.id,
        initial_source_tokens=base.source_tokens_used,
        initial_cutoff=base.source_data_cutoff, generation_type="update")

    assert chunks == len(prompts) > 3
    assert covered == base.source_tokens_used + 100 * len(rows)
    joined = "\n".join(_entries(p) for p in prompts)
    markers = [f"R{i:02d}" for i in range(len(rows))]
    for marker in markers:
        assert joined.count(marker) == 1, marker
    assert "OLDPARENT" not in joined
    by_position = sorted(range(len(rows)),
                         key=lambda i: joined.index(markers[i]))
    times = [rows[i].created_at for i in by_position]
    assert times == sorted(times)

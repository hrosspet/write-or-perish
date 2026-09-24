"""Read picks as one row per tweet, and the recommendation log (#352).

Covers: a pick's row (a new READ_PICK_SOURCE row, or the user's own row
for the tweet), saving a picked tweet in place (clip, bookmark upsert),
picks out of the references list, Delete keeping a pick, the action log
(opens, marks, verdicts, with the reply), which verdicts count for which
recommendation (parallel Reads, re-rating, quotes per thread, a
recommendation made after a verdict), quotes logged as recommendations,
the per-recommendation fields on the thread page, and the one-off
migration script.
"""
import importlib.util
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

import flask_login as _real_flask_login  # noqa: E402
from backend.extensions import db as _db  # noqa: E402
import backend.models as _real_backend_models  # noqa: E402
from backend.models import (  # noqa: E402
    ExternalItem, FeedPick, FeedRender, Node, READ_PICK_SOURCE,
    ReferenceAction, User,
)
from backend.utils import reference_log  # noqa: E402
from backend.utils.reference_log import (  # noqa: E402
    KIND_QUOTE, KIND_READ, log_quotes, outcomes, record_action,
)

# Glue-import the upsert helper (identity celery, like test_external_content)
_GLUE = ("backend.celery_app", "backend.tasks.external_sync")
_saved = {k: sys.modules.get(k) for k in _GLUE}
sys.modules["backend.celery_app"] = MagicMock()
sys.modules.pop("backend.tasks.external_sync", None)
from backend.tasks.external_sync import _upsert_items  # noqa: E402
for _k, _v in _saved.items():
    if _v is None:
        sys.modules.pop(_k, None)
    else:
        sys.modules[_k] = _v

T0 = datetime(2026, 9, 24, 8, 0)


@pytest.fixture
def app():
    _affected = lambda k: (  # noqa: E731
        k == "flask_login"
        or k.startswith("backend.routes")
        or k == "backend.models"
    )
    saved = {k: sys.modules[k] for k in list(sys.modules) if _affected(k)}
    sys.modules["flask_login"] = _real_flask_login
    sys.modules["backend.models"] = _real_backend_models
    for _k in [k for k in list(sys.modules) if k.startswith("backend.routes")]:
        del sys.modules[_k]

    from flask_login import LoginManager
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    _db.init_app(app)
    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from backend.routes.external import external_bp
    from backend.routes.nodes import nodes_bp
    app.register_blueprint(external_bp, url_prefix="/api/external")
    app.register_blueprint(nodes_bp, url_prefix="/api/nodes")

    with app.app_context():
        _db.create_all()
        _db.session.add_all([
            User(username="alice", external_content_enabled=True),
            User(username="gpt", twitter_id="llm-gpt"),
            User(username="bob"),
        ])
        _db.session.commit()
        yield app
        _db.session.remove()
        _db.drop_all()

    for k in [k for k in list(sys.modules) if _affected(k)]:
        if k not in saved:
            del sys.modules[k]
    for k, mod in saved.items():
        sys.modules[k] = mod


def _user(name):
    return User.query.filter_by(username=name).first()


@pytest.fixture
def client(app):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(_user("alice").id)
        sess["_fresh"] = True
    return client


def _node(parent=None, llm=False, at=T0):
    alice = _user("alice")
    n = Node(user_id=_user("gpt").id if llm else alice.id,
             human_owner_id=alice.id,
             parent_id=parent.id if parent else None,
             node_type="llm" if llm else "user",
             llm_model="gpt-5" if llm else None, created_at=at)
    n.set_content("x")
    _db.session.add(n)
    _db.session.commit()
    return n


def _item(ext_id="111", source=READ_PICK_SOURCE, text="a tweet", **kw):
    item = ExternalItem(user_id=_user("alice").id, source=source,
                        external_id=ext_id, author_handle="carol",
                        url=f"https://x.com/carol/status/{ext_id}", **kw)
    item.set_content(text)
    _db.session.add(item)
    _db.session.commit()
    return item


def _pick(node, item, kind=KIND_READ, decided_at=T0, prior_verdict=None,
          model="gpt-5"):
    row = FeedPick(user_id=_user("alice").id, node_id=node.id,
                   external_item_id=item.id, kind=kind, rank=1,
                   picked_by=model, decided_at=decided_at,
                   prior_read=False, prior_verdict=prior_verdict)
    _db.session.add(row)
    _db.session.commit()
    return row


def _read_reply(at=T0):
    """A home-page Read: its own thread (prompt root, reply under it)."""
    root = _node(at=at)
    reply = _node(parent=root, llm=True, at=at)
    _db.session.add(FeedRender(node_id=reply.id, created_at=at))
    _db.session.commit()
    return reply


# ── A pick's row ─────────────────────────────────────────────────────────

def _ref(tweet_id, text="picked tweet"):
    return {"tweet_id": tweet_id, "username": "carol", "text": text,
            "posted_at": T0}


def _picks(*tweet_ids):
    return [{"ref": _ref(t), "rank": i + 1, "relevance": 50,
             "recommend": True, "qt": "why"} for i, t in enumerate(tweet_ids)]


def test_a_pick_of_an_unsaved_tweet_is_not_a_reference(app, client):
    from backend.utils.ca_feed import save_feed_picks
    reply = _read_reply(at=T0)
    rows = save_feed_picks(_user("alice").id, reply, _picks("111"))
    _db.session.commit()
    item = rows[0].item
    assert item.source == READ_PICK_SOURCE
    assert not item.is_saved
    # The model chose at the read's submit, not at the collect.
    assert rows[0].decided_at == T0
    assert rows[0].kind == KIND_READ
    assert (rows[0].prior_read, rows[0].prior_verdict) == (False, None)
    # Not in the references list, nor in its counts.
    body = client.get("/api/external/items").get_json()
    assert body["items"] == [] and body["counts"] == {}


def test_a_pick_of_a_saved_tweet_reuses_its_row(app):
    from backend.utils.ca_feed import save_feed_picks
    saved = _item("111", source="twitter_bookmark",
                  read_at=T0 - timedelta(days=1), feedback="good",
                  feedback_at=T0 - timedelta(days=1))
    reply = _read_reply(at=T0)
    rows = save_feed_picks(_user("alice").id, reply, _picks("111"))
    _db.session.commit()
    assert rows[0].external_item_id == saved.id
    assert ExternalItem.query.count() == 1
    # The model chose knowing the reader's marks: not blind.
    assert (rows[0].prior_read, rows[0].prior_verdict) == (True, "good")


# ── Saving a picked tweet ────────────────────────────────────────────────

def test_clipping_a_picked_tweet_saves_its_row_in_place(app, client):
    pick_row = _item("111", read_at=T0, feedback="good", feedback_at=T0)
    r = client.post("/api/external/clip", json={
        "url": "https://x.com/carol/status/111",
        "content": "the full rendered tweet, longer than the archive text"})
    assert r.status_code == 201
    assert r.get_json()["id"] == pick_row.id
    row = ExternalItem.query.get(pick_row.id)
    assert row.source == "twitter_bookmark"
    assert (row.read_at, row.feedback) == (T0, "good")
    assert row.get_content().startswith("the full rendered tweet")
    assert ExternalItem.query.count() == 1
    assert client.get("/api/external/items").get_json()["counts"] == {
        "twitter_bookmark": 1}


def test_bookmark_sync_saves_a_picked_tweet_in_place(app):
    pick_row = _item("111", read_at=T0)
    created, skipped = _upsert_items(_user("alice").id, "twitter_bookmark", [
        {"external_id": "111", "content": "api text", "author_handle": "carol"},
        {"external_id": "222", "content": "new one", "author_handle": "dan"},
    ])
    # The picked tweet counts as created: new to the references, and not
    # a known bookmark that would stop the sync early.
    assert (created, skipped) == (2, 0)
    row = ExternalItem.query.get(pick_row.id)
    assert row.source == "twitter_bookmark"
    assert row.read_at == T0
    assert row.get_content() == "a tweet"  # the archive text is kept
    assert ExternalItem.query.count() == 2


# ── Delete ───────────────────────────────────────────────────────────────

def test_delete_keeps_a_read_pick_and_removes_the_rest(app, client):
    picked = _item("111", source="twitter_bookmark")
    _pick(_read_reply(), picked)
    plain = _item("222", source="twitter_bookmark")

    r = client.delete(f"/api/external/items/{picked.id}")
    assert r.get_json()["kept_as_pick"] is True
    assert ExternalItem.query.get(picked.id).source == READ_PICK_SOURCE
    assert FeedPick.query.count() == 1

    client.delete(f"/api/external/items/{plain.id}")
    assert ExternalItem.query.get(plain.id) is None


# ── The action log ───────────────────────────────────────────────────────

def test_marks_and_verdicts_are_logged_with_the_reply(app, client):
    item = _item("111")
    reply = _read_reply()
    client.post(f"/api/external/items/{item.id}/read",
                json={"node_id": reply.id, "via": "open"})
    client.post(f"/api/external/items/{item.id}/feedback",
                json={"feedback": "good", "node_id": reply.id})
    client.delete(f"/api/external/items/{item.id}/read",
                  json={"node_id": reply.id})
    # A node that is not the reader's is logged as outside any reply.
    bob_node = Node(user_id=_user("bob").id, node_type="user")
    bob_node.set_content("b")
    _db.session.add(bob_node)
    _db.session.commit()
    client.post(f"/api/external/items/{item.id}/feedback",
                json={"feedback": "bad", "node_id": bob_node.id})

    acts = ReferenceAction.query.order_by(ReferenceAction.id).all()
    assert [(a.kind, a.value, a.node_id) for a in acts] == [
        ("open", None, reply.id),
        ("verdict", "good", reply.id),
        ("unread", None, reply.id),
        ("verdict", "bad", None),
    ]
    item = ExternalItem.query.get(item.id)
    # A verdict marks the reference read again.
    assert item.feedback == "bad" and item.read_at is not None


def test_the_first_action_carries_over_older_marks(app):
    item = _item("111", read_at=T0 - timedelta(days=2), feedback="good",
                 feedback_at=T0 - timedelta(days=1))
    record_action(item, reference_log.ACTION_OPEN, node_id=None, at=T0)
    _db.session.commit()
    acts = ReferenceAction.query.order_by(ReferenceAction.created_at).all()
    assert [(a.kind, a.value, a.created_at) for a in acts] == [
        ("read", None, T0 - timedelta(days=2)),
        ("verdict", "good", T0 - timedelta(days=1)),
        ("open", None, T0),
    ]


def test_mark_all_as_read_logs_each_unread_pick(app, client):
    reply = _read_reply()
    a, b = _item("111"), _item("222", read_at=T0)
    _pick(reply, a)
    _pick(reply, b)
    client.post(f"/api/nodes/{reply.id}/feed-picks/read")
    acts = ReferenceAction.query.filter_by(kind="read").all()
    # b was already read: marking the list changed nothing there.
    assert [(x.item_id, x.node_id) for x in acts] == [(a.id, reply.id)]


# ── Which verdicts count ─────────────────────────────────────────────────

def test_parallel_reads_share_the_verdict_and_the_open(app):
    item = _item("111")
    luna, sol = _read_reply(at=T0), _read_reply(at=T0 + timedelta(minutes=1))
    p1, p2 = _pick(luna, item, decided_at=T0, model="luna"), _pick(
        sol, item, decided_at=T0 + timedelta(minutes=1), model="sol")
    record_action(item, "open", node_id=luna.id, at=T0 + timedelta(hours=1))
    record_action(item, "verdict", "good", node_id=luna.id,
                  at=T0 + timedelta(hours=1))
    _db.session.commit()
    out = outcomes([p1, p2])
    assert (out[p1.id].verdict, out[p1.id].verdict_shared) == ("good", False)
    assert (out[p2.id].verdict, out[p2.id].verdict_shared) == ("good", True)
    assert out[p2.id].opened and out[p2.id].opened_shared
    assert out[p2.id].read and out[p2.id].read_shared

    # Re-rating in either batch changes it for both: the latest counts.
    record_action(item, "verdict", "bad", node_id=sol.id,
                  at=T0 + timedelta(hours=2))
    _db.session.commit()
    out = outcomes([p1, p2])
    assert (out[p1.id].verdict, out[p1.id].verdict_shared) == ("bad", True)
    assert (out[p2.id].verdict, out[p2.id].verdict_shared) == ("bad", False)


def test_a_parallel_batch_collected_after_the_verdict_still_shares_it(app):
    """The second batch's rows are written after the reader rated the
    first; what decides is when its model chose (the submit)."""
    from backend.utils.ca_feed import save_feed_picks
    luna, sol = _read_reply(at=T0), _read_reply(at=T0 + timedelta(minutes=1))
    (p1,) = save_feed_picks(_user("alice").id, luna, _picks("111"))
    _db.session.commit()
    item = p1.item
    record_action(item, "verdict", "good", node_id=luna.id,
                  at=datetime.utcnow())
    item.feedback, item.feedback_at = "good", datetime.utcnow()
    _db.session.commit()
    (p2,) = save_feed_picks(_user("alice").id, sol, _picks("111"))
    _db.session.commit()
    assert p2.prior_verdict is None
    assert outcomes([p2])[p2.id].verdict == "good"


def test_a_recommendation_made_after_the_verdict_is_judged_on_its_own(app):
    item = _item("111", source="twitter_bookmark")
    reply = _read_reply(at=T0)
    p1 = _pick(reply, item, decided_at=T0)
    record_action(item, "verdict", "good", node_id=reply.id,
                  at=T0 + timedelta(hours=1))
    _db.session.commit()
    chat = _node(parent=_node(), llm=True, at=T0 + timedelta(days=1))
    q = _pick(chat, item, kind=KIND_QUOTE, decided_at=T0 + timedelta(days=1),
              prior_verdict="good")
    out = outcomes([p1, q])
    assert out[p1.id].verdict == "good"
    assert out[q.id].verdict is None and not out[q.id].blind
    assert reference_log.rated_before(q, item) == {
        "verdict": "good", "at": T0 + timedelta(hours=1)}
    record_action(item, "verdict", "bad", node_id=chat.id,
                  at=T0 + timedelta(days=1, hours=1))
    _db.session.commit()
    out = outcomes([p1, q])
    assert out[q.id].verdict == "bad"
    assert out[p1.id].verdict == "good"  # not the quote's verdict


def test_quotes_share_only_within_their_thread(app):
    item = _item("111", source="twitter_bookmark")
    thread = _node()
    first = _node(parent=thread, llm=True)
    regenerated = _node(parent=thread, llm=True)
    elsewhere = _node(parent=_node(), llm=True)
    q1, q2, q3 = (_pick(n, item, kind=KIND_QUOTE)
                  for n in (first, regenerated, elsewhere))
    record_action(item, "verdict", "good", node_id=first.id,
                  at=T0 + timedelta(hours=1))
    # Outside any reply: counts for Read picks, not for quotes.
    record_action(item, "open", node_id=None, at=T0 + timedelta(hours=2))
    _db.session.commit()
    out = outcomes([q1, q2, q3])
    assert out[q2.id].verdict == "good" and out[q2.id].verdict_shared
    assert out[q3.id].verdict is None
    assert not out[q3.id].opened


def test_actions_before_the_model_chose_do_not_count(app):
    item = _item("111")
    early = _read_reply(at=T0)
    p1 = _pick(early, item, decided_at=T0)
    record_action(item, "open", node_id=early.id, at=T0 + timedelta(hours=1))
    record_action(item, "unread", node_id=early.id, at=T0 + timedelta(hours=1))
    late = _read_reply(at=T0 + timedelta(days=1))
    p2 = _pick(late, item, decided_at=T0 + timedelta(days=1))
    _db.session.commit()
    out = outcomes([p1, p2])
    assert out[p1.id].opened
    assert not out[p2.id].opened


def test_verdicts_outside_any_reply_count_for_read_picks(app):
    item = _item("111", source="twitter_bookmark")
    p = _pick(_read_reply(), item)
    record_action(item, "verdict", "bad", node_id=None,
                  at=T0 + timedelta(hours=1))
    _db.session.commit()
    out = outcomes([p])[p.id]
    assert (out.verdict, out.verdict_shared) == ("bad", True)


# ── Quotes logged as recommendations ─────────────────────────────────────

def test_quotes_are_logged_once_per_turn(app):
    a = _item("111", source="twitter_bookmark", read_at=T0)
    b = _item("222", source="twitter_bookmark")
    bob_item = ExternalItem(user_id=_user("bob").id, source="web_clip",
                            external_id="f" * 64)
    bob_item.set_content("not alice's")
    _db.session.add(bob_item)
    _db.session.commit()
    interim = _node(parent=_node(), llm=True, at=T0)
    final = _node(parent=interim, llm=True, at=T0)
    logged = set()
    log_quotes(interim, f"see {{quote_ext:{b.id}}}", _user("alice").id,
               T0, "gpt-5", logged)
    log_quotes(final, f"{{quote_ext:{a.id}}} and {{quote_ext:{b.id}}} "
               f"{{quote_ext:{bob_item.id}}}", _user("alice").id, T0,
               "gpt-5", logged)
    _db.session.commit()
    rows = FeedPick.query.order_by(FeedPick.id).all()
    assert [(r.node_id, r.external_item_id, r.kind, r.rank) for r in rows] == [
        (interim.id, b.id, KIND_QUOTE, 1), (final.id, a.id, KIND_QUOTE, 1)]
    assert rows[1].prior_read is True and rows[1].picked_by == "gpt-5"


def test_a_read_replys_own_picks_are_not_logged_again(app):
    item = _item("111")
    reply = _read_reply()
    _pick(reply, item)
    log_quotes(reply, f"{{quote_ext:{item.id}}}", _user("alice").id, T0,
               "gpt-5", set())
    _db.session.commit()
    assert FeedPick.query.count() == 1


# ── The thread page ──────────────────────────────────────────────────────

def test_resolve_quotes_carries_the_verdict_that_counts(app, client):
    item = _item("111")
    luna, sol = _read_reply(at=T0), _read_reply(at=T0)
    _pick(luna, item)
    _pick(sol, item)
    for n in (luna, sol):
        n.set_content(f"{{quote_ext:{item.id}}}")
    _db.session.commit()
    client.post(f"/api/external/items/{item.id}/feedback",
                json={"feedback": "good", "node_id": luna.id})
    body = client.get(f"/api/nodes/{sol.id}/resolve-quotes").get_json()
    quote = body["external_quotes"][str(item.id)]
    assert (quote["feedback"], quote["feedback_shared"]) == ("good", True)
    assert quote["rated_before"] is None


# ── The one-off migration ────────────────────────────────────────────────

def _load_script():
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "scripts", "backfill_read_picks.py")
    spec = importlib.util.spec_from_file_location("_backfill_read_picks", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_relabels_merges_and_carries_marks(app):
    script = _load_script()
    reply = _read_reply(at=T0)
    # A pick-made archive row (written with its pick) ...
    made = _item("111", source="community_archive", read_at=T0 + timedelta(hours=1))
    made.fetched_at = T0
    # ... one the reader imported before a Read picked it ...
    imported = _item("222", source="community_archive")
    imported.fetched_at = T0 - timedelta(days=3)
    # ... and a picked tweet the reader then clipped: two rows.
    dup_pick = _item("333", source="community_archive")
    dup_pick.fetched_at = T0
    clip = _item("333", source="twitter_bookmark", feedback="good",
                 feedback_at=T0 + timedelta(hours=2), text="a longer clipped text")
    for it in (made, imported, dup_pick):
        p = _pick(reply, it, decided_at=None)
        p.created_at = T0
    reply.set_content(" ".join(f"{{quote_ext:{i.id}}}"
                               for i in (made, imported, dup_pick)))
    # An agentic reply that quoted the clipped copy.
    quoting = _node(parent=_node(), llm=True, at=T0 + timedelta(hours=3))
    quoting.set_content(f"look {{quote_ext:{clip.id}}}")
    clip.surfaced_count = 1
    clip.fetched_at = T0 + timedelta(hours=2)
    clip.last_surfaced_at = T0 + timedelta(hours=4)
    _db.session.commit()

    script.relabel_pick_rows(None, apply=True)
    _db.session.flush()
    script.carry_over_marks(None, apply=True)
    _db.session.flush()
    script.merge_copies(None, apply=True)
    _db.session.flush()
    script.stamp_old_picks(None, apply=True)
    _db.session.commit()

    assert ExternalItem.query.get(made.id).source == READ_PICK_SOURCE
    assert ExternalItem.query.get(imported.id).source == "community_archive"
    kept = ExternalItem.query.get(dup_pick.id)
    assert ExternalItem.query.get(clip.id) is None
    assert kept.source == "twitter_bookmark"
    assert kept.feedback == "good"
    assert kept.get_content() == "a longer clipped text"
    assert Node.query.get(quoting.id).get_content() == (
        f"look {{quote_ext:{kept.id}}}")
    # The clipped copy's carried-over verdict moved to the kept row.
    assert ReferenceAction.query.filter_by(
        item_id=kept.id, kind="verdict").count() == 1
    assert all(p.decided_at == T0 for p in FeedPick.query.all())
    # A second run finds nothing to do.
    assert script._saved_copy_pairs(None) == ([], 0)


def test_report_counts_per_model_with_shared_verdicts(app):
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "scripts", "recommendation_report.py")
    spec = importlib.util.spec_from_file_location("_rec_report", path)
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)

    item, other = _item("111"), _item("222")
    luna, sol = _read_reply(at=T0), _read_reply(at=T0)
    _pick(luna, item, model="luna")
    _pick(sol, item, model="sol")
    _pick(sol, other, model="sol")
    record_action(item, "verdict", "good", node_id=luna.id,
                  at=T0 + timedelta(hours=1))
    chat = _node(parent=_node(), llm=True)
    chat.tool_calls_meta = '[{"name": "_mode", "source_mode": "voice"}]'
    _pick(chat, _item("333", source="twitter_bookmark"), kind=KIND_QUOTE,
          model="gpt-5")
    _db.session.commit()

    rows = script.report()
    luna_row = rows[(KIND_READ, "luna", "-")]
    sol_row = rows[(KIND_READ, "sol", "-")]
    assert (luna_row["shown"], luna_row["good"], luna_row["good_shared"]) == (1, 1, 0)
    assert (sol_row["shown"], sol_row["good"], sol_row["good_shared"],
            sol_row["untouched"]) == (2, 1, 1, 1)
    assert rows[(KIND_QUOTE, "gpt-5", "voice")]["untouched"] == 1


def test_marks_from_before_the_log_count_until_carried_over(app):
    """Between the deploy and the migration run, an old pick's verdict
    still shows: the reference's own marks stand in for its log."""
    item = _item("111", read_at=T0 + timedelta(hours=1), feedback="good",
                 feedback_at=T0 + timedelta(hours=1))
    p = _pick(_read_reply(at=T0), item, decided_at=None)
    out = outcomes([p])[p.id]
    assert (out.verdict, out.verdict_shared, out.read) == ("good", True, True)


def test_a_reply_that_quoted_references_is_no_read_reply(app):
    from backend.utils.ca_feed import (
        has_read_picks, is_feed_node, is_read_reply, read_reply_ids)
    item = _item("111", source="twitter_bookmark")
    chat = _node(parent=_node(), llm=True)
    log_quotes(chat, f"{{quote_ext:{item.id}}}", _user("alice").id, T0,
               "gpt-5", set())
    _db.session.commit()
    chat = Node.query.get(chat.id)
    assert FeedPick.query.filter_by(node_id=chat.id).count() == 1
    assert not has_read_picks(chat)
    assert not is_read_reply(chat)
    assert not is_feed_node(chat)
    assert read_reply_ids([chat]) == frozenset()
    reply = _read_reply()
    _pick(reply, item)
    reply = Node.query.get(reply.id)
    assert is_read_reply(reply) and read_reply_ids([reply]) == {reply.id}


def test_a_verdict_on_the_turns_final_node_counts_for_its_interim_quote(app):
    item = _item("111", source="twitter_bookmark")
    interim = _node(parent=_node(), llm=True)
    final = _node(parent=interim, llm=True)
    interim.continuation_node_id = final.id
    _db.session.commit()
    q = _pick(interim, item, kind=KIND_QUOTE)
    record_action(item, "verdict", "good", node_id=final.id,
                  at=T0 + timedelta(hours=1))
    _db.session.commit()
    out = outcomes([q])[q.id]
    assert (out.verdict, out.verdict_shared) == ("good", False)


def test_migration_ignores_quote_rows_and_keeps_the_bookmark_source(app):
    """A quote row on the saved copy (an agentic reply quoted it after the
    deploy) must not make the copy look picked, and when one reply has
    rows for both copies the copy's is dropped, not re-pointed into a
    unique-constraint clash. The merged row stays an X bookmark."""
    script = _load_script()
    reply = _read_reply(at=T0)
    pick_row = _item("333", source="community_archive")
    pick_row.fetched_at = T0
    copy = _item("333", source="twitter_bookmark")
    p = _pick(reply, pick_row, decided_at=None)
    p.created_at = T0
    chat = _node(parent=_node(), llm=True)
    _pick(chat, copy, kind=KIND_QUOTE)
    _pick(chat, pick_row, kind=KIND_QUOTE)
    _db.session.commit()

    pairs, skipped = script._saved_copy_pairs(None)
    assert [(k.id, [c.id for c in cs]) for k, cs in pairs] == [
        (pick_row.id, [copy.id])]
    script.relabel_pick_rows(None, apply=True)
    _db.session.flush()
    script.merge_copies(None, apply=True)
    _db.session.commit()
    kept = ExternalItem.query.get(pick_row.id)
    assert kept.source == "twitter_bookmark"
    assert ExternalItem.query.get(copy.id) is None
    assert sorted((r.node_id, r.kind) for r in FeedPick.query.all()) == sorted(
        [(reply.id, KIND_READ), (chat.id, KIND_QUOTE)])


def test_migration_finds_a_quote_made_before_a_reclip_moved_fetched_at(app):
    script = _load_script()
    reply = _read_reply(at=T0)
    pick_row = _item("333", source="community_archive")
    pick_row.fetched_at = T0
    p = _pick(reply, pick_row, decided_at=None)
    p.created_at = T0
    copy = _item("333", source="twitter_bookmark")
    quoting = _node(parent=_node(), llm=True, at=T0 + timedelta(days=2))
    quoting.set_content(f"see {{quote_ext:{copy.id}}}")
    # Re-clipped with longer text a week later: fetched_at moved past
    # the quote.
    copy.fetched_at = T0 + timedelta(days=9)
    copy.surfaced_count = 1
    copy.last_surfaced_at = T0 + timedelta(days=2, minutes=1)
    _db.session.commit()
    script.relabel_pick_rows(None, apply=True)
    _db.session.flush()
    script.merge_copies(None, apply=True)
    _db.session.commit()
    assert Node.query.get(quoting.id).get_content() == (
        f"see {{quote_ext:{pick_row.id}}}")

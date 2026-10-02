"""Read tracking (voice review, 2026-10-02): whether a Read reply was
opened, and Reads that showed nothing counted in the report.

FeedRender.opened_at is set the first time the reply's owner fetches the
finished reply (GET /nodes/<id>, or an llm-status poll that returns it
and carries ?visible=1, the web page's flag while its tab is visible: a
background tab keeps polling and does not count), never for another user,
an admin or a reply still being made. The
report's Reads table counts every completed Read, the pick-less ones
split into "the model picked nothing" and "every pick was dropped"
(FeedRender.dropped_picks). The batch side (dropped_picks set on
collect, the cost row of an unusable reply) is in test_read_batch_poll.
"""
from datetime import timedelta

from flask import g

from backend.tests.test_reference_log import (  # noqa: F401 (fixtures)
    app, client, T0, _item, _node, _pick, _read_reply, _report_script,
    _user,
)
from backend.extensions import db as _db
from backend.models import FeedRender, User
from backend.utils.reference_log import KIND_QUOTE


def _render(node):
    return FeedRender.query.filter_by(node_id=node.id).one()


def _completed_read(**node_fields):
    reply = _read_reply(at=T0)
    reply.llm_task_status = "completed"
    for k, v in node_fields.items():
        setattr(reply, k, v)
    _db.session.commit()
    return reply


def _login_as(test_client, user):
    # The fixture's app context outlives each request: Flask-Login would
    # keep serving the first user from g.
    g.pop("_login_user", None)
    with test_client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


# ── opened_at ────────────────────────────────────────────────────────────

def test_owner_opening_a_finished_read_sets_opened_at_once(app, client):  # noqa: F811
    reply = _completed_read()
    assert _render(reply).opened_at is None

    assert client.get(f"/api/nodes/{reply.id}").status_code == 200
    first = _render(reply).opened_at
    assert first is not None

    # A later open keeps the first time.
    assert client.get(f"/api/nodes/{reply.id}").status_code == 200
    assert client.get(
        f"/api/nodes/{reply.id}/llm-status?visible=1").status_code == 200
    _db.session.expire_all()
    assert _render(reply).opened_at == first


def test_pending_read_is_not_opened(app, client):  # noqa: F811
    reply = _read_reply(at=T0)
    reply.llm_task_status = "processing"
    _db.session.commit()

    assert client.get(f"/api/nodes/{reply.id}").status_code == 200
    assert client.get(f"/api/nodes/{reply.id}/llm-status").status_code == 200
    assert client.get(
        f"/api/nodes/{reply.id}/llm-status?visible=1").status_code == 200
    assert _render(reply).opened_at is None


def test_visible_llm_status_returning_the_finished_read_sets_opened_at(app, client):  # noqa: F811
    """A thread page left open while the batch ran: the poll that brings
    the finished reply counts as the open when its tab is visible."""
    reply = _read_reply(at=T0)
    reply.llm_task_status = "processing"
    _db.session.commit()
    body = client.get(
        f"/api/nodes/{reply.id}/llm-status?visible=1").get_json()
    assert "content" not in body
    assert _render(reply).opened_at is None

    reply.llm_task_status = "completed"
    _db.session.commit()
    body = client.get(
        f"/api/nodes/{reply.id}/llm-status?visible=1").get_json()
    assert body["status"] == "completed" and "content" in body
    _db.session.expire_all()
    assert _render(reply).opened_at is not None


def test_llm_status_without_the_visible_flag_is_not_an_open(app, client):  # noqa: F811
    """A background tab's poll, an older client, the iPhone app today:
    the reply is returned, and nothing is recorded. Only visible=1
    counts, so not visible=0 or any other value either."""
    reply = _read_reply(at=T0)
    reply.llm_task_status = "completed"
    _db.session.commit()

    for query in ("", "?visible=0", "?visible=", "?visible=true",
                  "?visible=yes", "?other=1"):
        body = client.get(
            f"/api/nodes/{reply.id}/llm-status{query}").get_json()
        assert body["status"] == "completed" and "content" in body
        _db.session.expire_all()
        assert _render(reply).opened_at is None, query

    # The tab is shown again: the page's one extra poll counts.
    client.get(f"/api/nodes/{reply.id}/llm-status?visible=1")
    _db.session.expire_all()
    assert _render(reply).opened_at is not None


def test_a_hidden_polls_reply_is_opened_by_a_later_node_fetch(app, client):  # noqa: F811
    """Loading the reply (GET of the node) is still an open, as before."""
    reply = _read_reply(at=T0)
    reply.llm_task_status = "completed"
    _db.session.commit()
    client.get(f"/api/nodes/{reply.id}/llm-status")
    assert _render(reply).opened_at is None
    client.get(f"/api/nodes/{reply.id}")
    _db.session.expire_all()
    assert _render(reply).opened_at is not None


def test_another_user_or_an_admin_opening_it_does_not_count(app, client):  # noqa: F811
    """Both can see a public reply; neither is its owner."""
    reply = _completed_read(privacy_level="public")
    admin = User(username="root", is_admin=True)
    _db.session.add(admin)
    _db.session.commit()

    for viewer in (_user("bob"), admin):
        _login_as(client, viewer)
        assert client.get(f"/api/nodes/{reply.id}").status_code == 200
        assert client.get(
            f"/api/nodes/{reply.id}/llm-status?visible=1").status_code == 200
        _db.session.expire_all()
        assert _render(reply).opened_at is None

    _login_as(client, _user("alice"))
    assert client.get(f"/api/nodes/{reply.id}").status_code == 200
    _db.session.expire_all()
    assert _render(reply).opened_at is not None


def test_opening_the_read_prompt_is_not_opening_the_reply(app, client):  # noqa: F811
    """Only a fetch of the reply itself counts: its parent's page lists
    it as a child, which says nothing about reading it."""
    reply = _completed_read()
    assert client.get(f"/api/nodes/{reply.parent_id}").status_code == 200
    assert _render(reply).opened_at is None


def test_a_reply_with_no_render_is_left_alone(app, client):  # noqa: F811
    """An ordinary LLM reply: no FeedRender row, nothing to set."""
    chat = _node(parent=_node(), llm=True)
    chat.llm_task_status = "completed"
    _db.session.commit()
    assert client.get(f"/api/nodes/{chat.id}").status_code == 200
    assert client.get(f"/api/nodes/{chat.id}/llm-status").status_code == 200
    assert FeedRender.query.count() == 0


def test_put_does_not_count_as_an_open(app, client):  # noqa: F811
    """PUT shares the focal fields with GET; only GET counts."""
    reply = _completed_read()
    resp = client.put(f"/api/nodes/{reply.id}", json={"content": "edited"})
    assert resp.status_code == 200
    assert resp.get_json()["node"]["read_reply"] is True
    assert _render(reply).opened_at is None


# ── the report's Reads table ─────────────────────────────────────────────

def _read_under(prompt, model, at=T0, status="completed", dropped=0,
                opened=False):
    reply = _node(parent=prompt, llm=True, at=at)
    reply.llm_model = model
    reply.llm_task_status = status
    _db.session.add(FeedRender(
        node_id=reply.id, created_at=at, dropped_picks=dropped,
        opened_at=at + timedelta(hours=1) if opened else None))
    _db.session.commit()
    return reply


def test_report_counts_empty_reads_and_splits_them(app):  # noqa: F811
    script = _report_script()
    home = _node()
    home.prompt_key = "read"
    convo = _node()
    thread_prompt = _node(parent=convo)
    thread_prompt.prompt_key = "read_thread"
    _db.session.commit()

    picked = _read_under(home, "luna", opened=True)
    _pick(picked, _item("1"), model="luna")
    _read_under(home, "luna", opened=True)               # picked nothing
    _read_under(home, "luna", dropped=2)                 # all dropped
    _read_under(home, "luna", status="failed")           # not a Read
    quoted = _read_under(home, "luna")                   # a quote is no pick
    _pick(quoted, _item("2", source="twitter_bookmark"), kind=KIND_QUOTE,
          model="luna")
    _read_under(thread_prompt, "sol", dropped=1, opened=True)

    rows = script.read_report()
    luna = rows[("luna", "uncond", "first")]
    assert {k: luna[k] for k in ("reads", "picked", "empty", "nothing",
                                 "dropped", "opened")} == {
        "reads": 4, "picked": 1, "empty": 3, "nothing": 2, "dropped": 1,
        "opened": 2}
    sol = rows[("sol", "cond", "first")]
    assert (sol["reads"], sol["empty"], sol["dropped"], sol["opened"]) == (
        1, 1, 1, 1)
    assert set(rows) == {("luna", "uncond", "first"), ("sol", "cond", "first")}

    # The pick table is unchanged: the one Read pick (and the quote), no
    # row for the empty Reads.
    picks = script.report()
    assert [k for k in picks if k[0] == "read"] == [
        ("read", "luna", "uncond", "first")]
    assert picks[("read", "luna", "uncond", "first")]["shown"] == 1


def test_report_reads_filter_by_user_and_submit_time(app):  # noqa: F811
    script = _report_script()
    home = _node()
    home.prompt_key = "read"
    _db.session.commit()
    _read_under(home, "luna", at=T0)
    _read_under(home, "luna", at=T0 + timedelta(days=10))

    assert script.read_report(user_id=_user("bob").id) == {}
    rows = script.read_report(since=T0 + timedelta(days=5))
    assert rows[("luna", "uncond", "first")]["reads"] == 1
    assert script.read_report(
        user_id=_user("alice").id)[("luna", "uncond", "first")]["reads"] == 2


def test_report_prints_the_reads_table(app, capsys):  # noqa: F811
    script = _report_script()
    home = _node()
    home.prompt_key = "read"
    _db.session.commit()
    _read_under(home, "luna", opened=True)
    _read_under(home, "luna", dropped=1)

    script.print_reads(script.read_report())
    out = capsys.readouterr().out
    header = out.splitlines()[0].split()
    assert header == ["model", "variant", "turn", "reads", "picked",
                      "empty", "nothing", "dropped", "opened", "opened%"]
    assert out.splitlines()[2].split() == [
        "luna", "uncond", "first", "2", "0", "2", "1", "1", "1", "50%"]

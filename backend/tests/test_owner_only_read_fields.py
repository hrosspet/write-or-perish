"""A Read reply's owner-only fields (2026-10-09).

What a read covered is counted after the owner's read tweets were left
out (FeedRender.excluded_count, and the tweet and account counts that
remained), and the picks the reply quotes carry the owner's read marks
and verdicts. The owner gets both; another user who can see the reply (a
public thread) gets the reply without them.
"""
from datetime import timedelta

from flask import g

from backend.tests.test_reference_log import (  # noqa: F401 (fixtures)
    app, client, T0, _item, _read_reply, _user,
)
from backend.extensions import db as _db
from backend.models import FeedRender


def _login_as(test_client, user):
    # The fixture's app context outlives each request: Flask-Login would
    # keep serving the first user from g.
    g.pop("_login_user", None)
    with test_client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def _public_read_reply():
    reply = _read_reply(at=T0)
    reply.llm_task_status = "completed"
    reply.privacy_level = "public"
    render = FeedRender.query.filter_by(node_id=reply.id).one()
    render.window_start = T0 - timedelta(days=1)
    render.window_end = T0
    render.tweet_count = 4812
    render.account_count = 410
    render.excluded_count = 37
    _db.session.commit()
    return reply


def test_read_window_goes_to_the_replys_owner_only(app, client):  # noqa: F811
    reply = _public_read_reply()

    body = client.get(f"/api/nodes/{reply.id}").get_json()
    assert body["read_reply"] is True
    assert body["read_window"]["excluded"] == 37
    assert body["read_window"]["tweets"] == 4812

    _login_as(client, _user("bob"))
    resp = client.get(f"/api/nodes/{reply.id}")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["read_reply"] is True
    assert "read_window" not in body


def test_quoted_picks_carry_read_marks_to_the_owner_only(app, client):  # noqa: F811
    reply = _public_read_reply()
    item = _item("111", read_at=T0 + timedelta(hours=1), feedback="good")
    reply.set_content(f"The verdict.\n\n{{quote_ext:{item.id}}}")
    _db.session.commit()
    url = f"/api/nodes/{reply.id}/resolve-quotes"

    quote = client.get(url).get_json()["external_quotes"][str(item.id)]
    assert quote["read_at"] is not None
    assert quote["feedback"] == "good"

    _login_as(client, _user("bob"))
    quote = client.get(url).get_json()["external_quotes"][str(item.id)]
    assert quote["content"] == "a tweet"
    assert "read_at" not in quote
    assert "feedback" not in quote

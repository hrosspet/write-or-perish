"""A gleaning pick's two verdicts (#502, option B, Peter 2026-10-14).

Each pick of a gleaning is judged in two parts: Loore's intro to the
tweet (the paragraph above the card) and the tweet. The tweet's verdict
is the reference's verdict as before (ExternalItem.feedback, a
ReferenceAction against the gleaning, read on a verdict); the intro's is
the pick row's (FeedPick.intro_verdict), its value only: the intro is
written by a model that sees the user's profile and intentions, so none
of its text goes with the verdict (Peter: "we shouldn't be tracking the
contents of the point").

Covers POST /api/nodes/<id>/feed-picks/<item_id>/verdict (both parts in
one request, one part alone, clearing, owner only, a gleaning's own pick
only, no text stored), the intro verdict on resolve-quotes and
feed-picks (owner only), and a pick hidden by a waiting deletion.
"""
from datetime import datetime

from backend.tests.test_reference_log import (  # noqa: F401 (fixtures)
    app, client, _db, _item, _pick, _read_reply, _user, KIND_QUOTE, T0,
)
from backend.models import (
    ExternalItem, FeedPick, Node, ReferenceAction, UserDataPurge,
    UserDataPurgeHidden,
)

INTRO = "You said you keep restarting your morning pages; PRIVATE-INTRO"


def _gleaning(item):
    """Alice's gleaning quoting *item* as its pick, with its intro."""
    reply = _read_reply(at=T0)
    reply.set_content(f"One today.\n\n{INTRO}\n\n{{quote_ext:{item.id}}}")
    pick = _pick(reply, item)
    pick.set_why(INTRO)
    _db.session.commit()
    return reply, pick


def _url(reply, item):
    return f"/api/nodes/{reply.id}/feed-picks/{item.id}/verdict"


def _bob_client(flask_app):
    from flask import g
    g.pop("_login_user", None)
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(_user("bob").id)
        sess["_fresh"] = True
    return c


def test_the_pick_level_tap_sets_both_parts_in_one_request(app, client):  # noqa: F811
    item = _item("111")
    reply, pick = _gleaning(item)
    r = client.post(_url(reply, item), json={"intro": "good", "tweet": "good"})
    assert r.status_code == 200
    body = r.get_json()
    assert (body["intro_verdict"], body["feedback"]) == ("good", "good")
    assert body["feedback_shared"] is False
    assert body["recommendation_kind"] == "read"
    assert body["read_at"] is not None

    pick = FeedPick.query.get(pick.id)
    assert pick.intro_verdict == "good" and pick.intro_verdict_at is not None
    item = ExternalItem.query.get(item.id)
    # The tweet's verdict is the reference's, as from /feedback: on the
    # item, logged against the gleaning, and it marks the tweet read.
    assert item.feedback == "good" and item.feedback_at is not None
    assert item.read_at is not None
    acts = ReferenceAction.query.all()
    assert [(a.kind, a.value, a.node_id) for a in acts] == [
        ("verdict", "good", reply.id)]


def test_a_part_changes_or_clears_alone(app, client):  # noqa: F811
    item = _item("111")
    reply, pick = _gleaning(item)
    client.post(_url(reply, item), json={"intro": "good", "tweet": "good"})

    body = client.post(_url(reply, item), json={"intro": "bad"}).get_json()
    assert (body["intro_verdict"], body["feedback"]) == ("bad", "good")
    assert ReferenceAction.query.count() == 1   # the tweet was not touched

    body = client.post(_url(reply, item), json={"intro": None}).get_json()
    assert (body["intro_verdict"], body["feedback"]) == (None, "good")
    pick = FeedPick.query.get(pick.id)
    assert pick.intro_verdict is None and pick.intro_verdict_at is None

    read_at = ExternalItem.query.get(item.id).read_at
    body = client.post(_url(reply, item), json={"tweet": None}).get_json()
    assert (body["intro_verdict"], body["feedback"]) == (None, None)
    item = ExternalItem.query.get(item.id)
    # Clearing the tweet's verdict leaves the read mark alone.
    assert item.feedback is None and item.read_at == read_at
    assert [(a.kind, a.value) for a in ReferenceAction.query.order_by(
        ReferenceAction.id)] == [("verdict", "good"), ("verdict", None)]


def test_an_intro_verdict_alone_does_not_mark_the_tweet_read(app, client):  # noqa: F811
    item = _item("111")
    reply, pick = _gleaning(item)
    body = client.post(_url(reply, item), json={"intro": "bad"}).get_json()
    assert body["intro_verdict"] == "bad" and body["read_at"] is None
    item = ExternalItem.query.get(item.id)
    assert item.read_at is None and item.feedback is None
    assert ReferenceAction.query.count() == 0


def test_no_text_of_the_intro_is_stored_with_the_verdict(app, client):  # noqa: F811
    item = _item("111")
    reply, pick = _gleaning(item)
    client.post(_url(reply, item), json={"intro": "good", "tweet": "bad"})
    client.post(_url(reply, item), json={"intro": "bad"})

    # The pick row: only the value and the time are new; the intro text
    # is where it was before (`why`, the model's own words), nowhere else.
    row = FeedPick.query.get(pick.id)
    values = {c.name: getattr(row, c.name) for c in FeedPick.__table__.columns
              if c.name != "why"}
    assert row.intro_verdict == "bad"
    assert isinstance(row.intro_verdict_at, datetime)
    assert not any("PRIVATE-INTRO" in str(v) for v in values.values())
    assert not any(str(v).startswith("You said") for v in values.values())
    # The log of the tweet's verdict carries no text either.
    for a in ReferenceAction.query.all():
        for c in ReferenceAction.__table__.columns:
            assert "PRIVATE-INTRO" not in str(getattr(a, c.name))


def test_bad_bodies_are_refused(app, client):  # noqa: F811
    item = _item("111")
    reply, _ = _gleaning(item)
    assert client.post(_url(reply, item), json={}).status_code == 400
    assert client.post(_url(reply, item),
                       json={"intro": "meh"}).status_code == 400
    assert client.post(_url(reply, item),
                       json={"intro": "good", "tweet": "great"}).status_code == 400
    # Nothing was changed by the refused ones.
    assert FeedPick.query.one().intro_verdict is None
    assert ExternalItem.query.get(item.id).feedback is None


def test_only_the_owner_rates_her_gleanings_picks(app, client):  # noqa: F811
    item = _item("111")
    reply, pick = _gleaning(item)
    reply.privacy_level = "public"
    _db.session.commit()
    bob = _bob_client(app)
    # Bob may see her public gleaning; he may not rate its picks.
    assert bob.post(_url(reply, item), json={"intro": "bad"}).status_code == 403
    # Nor through a gleaning of his own naming her tweet.
    bob_reply = Node(user_id=_user("gpt").id, human_owner_id=_user("bob").id,
                     node_type="llm", llm_model="gpt-5")
    bob_reply.set_content(f"{{quote_ext:{item.id}}}")
    _db.session.add(bob_reply)
    _db.session.commit()
    assert bob.post(_url(bob_reply, item),
                    json={"intro": "bad"}).status_code == 404
    assert FeedPick.query.get(pick.id).intro_verdict is None
    assert ExternalItem.query.get(item.id).feedback is None


def test_a_node_the_user_cannot_see_is_not_found(app, client):  # noqa: F811
    item = _item("111")
    reply, _ = _gleaning(item)
    reply.deleted_at = datetime.utcnow()
    _db.session.commit()
    assert client.post(_url(reply, item), json={"intro": "good"}).status_code == 404
    assert client.post("/api/nodes/999999/feed-picks/1/verdict",
                       json={"intro": "good"}).status_code == 404


def test_only_a_gleanings_own_pick_has_an_intro_verdict(app, client):  # noqa: F811
    item, other = _item("111"), _item("222")
    reply, _ = _gleaning(item)
    # A tweet the gleaning did not pick.
    assert client.post(_url(reply, other), json={"intro": "good"}).status_code == 404
    # A reference an agentic reply quoted (kind 'quote') keeps one verdict.
    chat = _read_reply(at=T0)
    _pick(chat, other, kind=KIND_QUOTE)
    assert client.post(_url(chat, other), json={"intro": "good"}).status_code == 404


def test_resolve_quotes_and_feed_picks_carry_the_intro_verdict(app, client):  # noqa: F811
    item = _item("111")
    reply, _ = _gleaning(item)
    client.post(_url(reply, item), json={"intro": "bad"})
    quote = client.get(f"/api/nodes/{reply.id}/resolve-quotes").get_json()[
        "external_quotes"][str(item.id)]
    assert (quote["intro_verdict"], quote["recommendation_kind"]) == ("bad", "read")
    assert quote["feedback"] is None
    pick = client.get(f"/api/nodes/{reply.id}/feed-picks").get_json()[
        "picks"][0]["item"]
    assert pick["intro_verdict"] == "bad"


def test_another_viewer_gets_no_intro_verdict(app, client):  # noqa: F811
    item = _item("111")
    reply, _ = _gleaning(item)
    reply.privacy_level = "public"
    _db.session.commit()
    client.post(_url(reply, item), json={"intro": "bad", "tweet": "bad"})
    bob = _bob_client(app)
    body = bob.get(f"/api/nodes/{reply.id}/resolve-quotes").get_json()
    quote = body["external_quotes"][str(item.id)]
    for key in ("intro_verdict", "recommendation_kind", "feedback", "read_at"):
        assert key not in quote, key
    assert bob.get(f"/api/nodes/{reply.id}/feed-picks").status_code == 403


def test_a_pick_hidden_by_a_waiting_deletion_cannot_be_rated(app, client):  # noqa: F811
    item = _item("111")
    reply, pick = _gleaning(item)
    client.post(_url(reply, item), json={"intro": "good"})
    job = UserDataPurge(user_id=_user("alice").id, source="self",
                        scope="hidden", scheduled_for=datetime.utcnow())
    _db.session.add(job)
    _db.session.flush()
    _db.session.add(UserDataPurgeHidden(job_id=job.id, kind="feed_pick",
                                        row_id=pick.id))
    _db.session.commit()
    _db.session.expire_all()
    assert client.post(_url(reply, item), json={"intro": "bad"}).status_code == 404
    quote = client.get(f"/api/nodes/{reply.id}/resolve-quotes").get_json()[
        "external_quotes"][str(item.id)]
    assert "intro_verdict" not in quote

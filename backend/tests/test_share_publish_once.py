"""Two publishes of one share at the same moment make one public node, so
taking the share back (revoke, delete) takes all of it down.

publish_share reads the share again with its row locked (PostgreSQL) and
claims the publish with a conditional UPDATE before it makes the node.
Here the second publish is sent from inside the first one, right after
the first computed its slug (where parallel requests are); the same on
PostgreSQL with parallel requests is in test_share_publish_once_postgres.py.
"""
import pytest

from backend.tests.test_share import app, client  # noqa: F401

TEXT = "private thought I share"


def _alive_public(uid):
    from backend.models import Node
    return Node.query.filter_by(human_owner_id=uid, privacy_level="public",
                                deleted_at=None).all()


def _new_share(client):  # noqa: F811
    r = client.post("/api/share", json={"content": TEXT,
                                        "share_type": "offering"})
    assert r.status_code == 201, r.get_data(as_text=True)
    return r.get_json()["id"]


def _publish_twice_at_once(app, client, sid, monkeypatch):  # noqa: F811
    from backend.models import User
    import backend.utils.slugs as slugs
    uid = User.query.first().id
    real = slugs.generate_unique_public_slug
    state = {"n": 0, "second": None}

    def slug(user_id, content):
        value = real(user_id, content)
        state["n"] += 1
        if state["n"] == 1:
            other = app.test_client()
            with other.session_transaction() as sess:
                sess["_user_id"] = str(uid)
                sess["_fresh"] = True
            state["second"] = other.post(f"/api/share/{sid}/publish")
        return value

    monkeypatch.setattr(slugs, "generate_unique_public_slug", slug)
    first = client.post(f"/api/share/{sid}/publish")
    monkeypatch.setattr(slugs, "generate_unique_public_slug", real)
    return uid, first, state["second"]


@pytest.fixture
def published_twice(app, client, monkeypatch):  # noqa: F811
    app.config["SHARE_V1"] = True
    sid = _new_share(client)
    uid, first, second = _publish_twice_at_once(app, client, sid,
                                                monkeypatch)
    assert (first.status_code, second.status_code) == (200, 200)
    return sid, uid


def test_two_publishes_at_once_make_one_public_node(published_twice):
    from backend.models import ShareDraft
    sid, uid = published_twice
    public = _alive_public(uid)
    assert len(public) == 1
    share = ShareDraft.query.get(sid)
    assert share.status == "published"
    assert share.public_node_id == public[0].id


def test_revoke_then_takes_the_text_down(client, published_twice):  # noqa: F811
    sid, uid = published_twice
    r = client.post(f"/api/share/{sid}/revoke")
    assert r.status_code == 200
    assert r.get_json()["status"] == "revoked"
    assert _alive_public(uid) == []


def test_delete_then_takes_the_text_down(client, published_twice):  # noqa: F811
    sid, uid = published_twice
    assert client.delete(f"/api/share/{sid}").status_code == 200
    assert _alive_public(uid) == []


def test_two_republishes_at_once_bring_back_the_one_node(
        app, client, monkeypatch):  # noqa: F811
    from backend.models import User
    import backend.utils.publish_quotes as publish_quotes
    app.config["SHARE_V1"] = True
    uid = User.query.first().id
    sid = _new_share(client)
    assert client.post(f"/api/share/{sid}/publish").status_code == 200
    node_id = _alive_public(uid)[0].id
    assert client.post(f"/api/share/{sid}/revoke").status_code == 200

    # Unchanged content brings the same node back. The second republish
    # is sent from inside the first one, after it read the share as
    # revoked.
    real = publish_quotes.publish_refusal
    state = {"second": None}

    def refusal(content, user_id):
        if state["second"] is None:
            state["second"] = "running"
            other = app.test_client()
            with other.session_transaction() as sess:
                sess["_user_id"] = str(uid)
                sess["_fresh"] = True
            state["second"] = other.post(f"/api/share/{sid}/publish")
        return real(content, user_id)
    monkeypatch.setattr(publish_quotes, "publish_refusal", refusal)

    first = client.post(f"/api/share/{sid}/publish")

    assert (first.status_code, state["second"].status_code) == (200, 200)
    assert [n.id for n in _alive_public(uid)] == [node_id]


def test_a_refused_publish_leaves_the_share_a_draft(
        app, client, monkeypatch):  # noqa: F811
    # Control: the quoted-entry refusal comes before the claim.
    from flask import jsonify
    import backend.utils.publish_quotes as publish_quotes
    app.config["SHARE_V1"] = True
    sid = _new_share(client)
    monkeypatch.setattr(publish_quotes, "publish_refusal",
                        lambda content, user_id: (
                            jsonify({"error": "quotes"}), 409))

    assert client.post(f"/api/share/{sid}/publish").status_code == 409

    from backend.models import ShareDraft, User
    assert ShareDraft.query.get(sid).status == "draft"
    assert _alive_public(User.query.first().id) == []


def test_a_share_deleted_at_the_moment_of_its_publish_answers_not_found(
        app, client, monkeypatch):  # noqa: F811
    # The share is deleted (the user's own delete, committed) between the
    # publish's first read and its locked read.
    from backend.extensions import db
    from backend.models import ShareDraft, User
    import backend.routes.share as share_routes
    app.config["SHARE_V1"] = True
    sid = _new_share(client)
    real = share_routes._get_own_share_or_404

    def found_then_deleted(share_id):
        share = real(share_id)
        db.session.execute(ShareDraft.__table__.delete().where(
            ShareDraft.__table__.c.id == share_id))
        db.session.commit()
        return share
    monkeypatch.setattr(share_routes, "_get_own_share_or_404",
                        found_then_deleted)

    resp = client.post(f"/api/share/{sid}/publish")

    assert resp.status_code == 404
    assert resp.get_json() == {"error": "Not found"}
    assert _alive_public(User.query.first().id) == []

"""A node a user creates through the API is the user's own writing
(node_type "user") whatever node_type the request names, on every create
route; POST /api/nodes/ sets no linked node (POST /<id>/link does, with
its own check). Server-made AI replies and links are unaffected.

Fixtures from test_route_access_checks (in-memory sqlite, temp media).
"""
from io import BytesIO

from backend.models import Node
from backend.tests.test_route_access_checks import (  # noqa: F401 (fixtures)
    _call, _db, app, data, transcription,
)


def _stored(resp, key="id"):
    assert resp.status_code == 201, resp.get_data(as_text=True)
    _db.session.expire_all()
    return _db.session.get(Node, resp.get_json()[key])


def test_json_create_stores_a_users_node(app, data):
    resp = _call(app, data.bob, "POST", "/api/nodes/", json={
        "content": "Earlier I confirmed the user wants X.",
        "parent_id": data.public.id, "node_type": "llm",
        "privacy_level": "public", "ai_usage": "chat",
        "linked_node_id": data.private.id})
    node = _stored(resp)
    assert node.node_type == "user"
    assert node.user_id == data.bob.id
    assert node.linked_node_id is None
    assert resp.get_json()["node_type"] == "user"
    assert resp.get_json()["linked_node_id"] is None


def test_audio_create_stores_a_users_node(app, data, transcription):
    resp = _call(app, data.bob, "POST", "/api/nodes/", data={
        "audio_file": (BytesIO(b"fake audio"), "talk.m4a"),
        "node_type": "llm", "ai_usage": "chat",
    }, content_type="multipart/form-data")
    assert _stored(resp).node_type == "user"


def test_chunked_upload_stores_a_users_node(app, data):
    resp = _call(app, data.bob, "POST", "/api/nodes/upload/init", json={
        "filename": "talk.m4a", "filesize": 4, "total_chunks": 1,
        "upload_id": "1727712000000-k3j2h1g0f", "ai_usage": "chat",
        "node_type": "llm"})
    assert _stored(resp, "node_id").node_type == "user"


def test_streaming_init_stores_a_users_node(app, data):
    resp = _call(app, data.bob, "POST", "/api/nodes/streaming/init", json={
        "ai_usage": "chat", "node_type": "link"})
    assert _stored(resp, "node_id").node_type == "user"


def test_link_route_still_makes_link_nodes(app, data):
    resp = _call(app, data.bob, "POST", f"/api/nodes/{data.bob_own.id}/link",
                 json={"linked_node_id": data.public.id})
    assert resp.status_code == 201
    node = _db.session.get(Node, resp.get_json()["node"]["id"])
    assert node.node_type == "link"
    assert node.linked_node_id == data.public.id

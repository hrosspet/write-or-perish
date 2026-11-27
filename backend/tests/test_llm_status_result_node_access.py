"""GET /api/nodes/<id>/llm-status gives the text of the node a finished
reply task names ("node") only to a user who may read that node, and
only the reply's owner's poll writes the completed status.

A multi-step reply: the interim step is shared publicly, the final node
(a continuation, private by default) is not.
"""
from backend.extensions import db as _db
from backend.models import Node
from backend.tests.test_no_replies_for_none import (  # noqa: F401 (fixtures)
    _client, _node, _reply, _user, app, task_mod,
)

FINAL = "the final answer"


class _Result:
    def __init__(self, final_id):
        self.state = "SUCCESS"
        self.result = {"llm_node_id": final_id, "status": "completed"}
        self.info = self.result


def _multi_step_reply(monkeypatch, final_privacy="private"):
    alice = _user("alice")
    entry = _node(alice, _node(alice, privacy_level="public"),
                  privacy_level="public")
    interim = _reply(entry, alice)
    interim.llm_task_status = "processing"
    interim.llm_task_id = "task-1"
    interim.privacy_level = "public"
    final = Node(user_id=interim.user_id, parent_id=interim.id,
                 human_owner_id=alice.id, node_type="llm",
                 llm_model=interim.llm_model, privacy_level=final_privacy,
                 llm_task_status="completed")
    final.set_content(FINAL)
    _db.session.add(final)
    _db.session.flush()
    interim.continuation_node_id = final.id
    _db.session.commit()
    from backend import celery_app
    monkeypatch.setattr(celery_app.celery, "AsyncResult",
                        lambda task_id: _Result(final.id))
    return alice, interim.id, final.id


def test_other_user_gets_status_without_the_private_final_node(
        app, monkeypatch):
    alice, interim_id, final_id = _multi_step_reply(monkeypatch)
    bob = _user("bob")
    bobs = _client(app, bob)
    assert bobs.get(f"/api/nodes/{final_id}").status_code == 404
    resp = bobs.get(f"/api/nodes/{interim_id}/llm-status")
    assert resp.status_code == 200
    body = resp.get_json()
    assert "node" not in body
    assert FINAL not in resp.get_data(as_text=True)
    assert body["continuation_node_id"] == final_id
    # Another user's poll changes nothing on alice's node.
    _db.session.expire_all()
    assert Node.query.get(interim_id).llm_task_status == "processing"


def test_owner_gets_the_final_node_and_the_status_write(app, monkeypatch):
    alice, interim_id, final_id = _multi_step_reply(monkeypatch)
    resp = _client(app, alice).get(f"/api/nodes/{interim_id}/llm-status")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["node"]["id"] == final_id
    assert body["node"]["content"] == FINAL
    assert body["status"] == "completed"
    _db.session.expire_all()
    assert Node.query.get(interim_id).llm_task_status == "completed"


def test_other_user_gets_a_shared_final_node(app, monkeypatch):
    alice, interim_id, final_id = _multi_step_reply(
        monkeypatch, final_privacy="public")
    resp = _client(app, _user("bob")).get(
        f"/api/nodes/{interim_id}/llm-status")
    assert resp.get_json()["node"]["content"] == FINAL


def test_deleted_final_node_is_not_returned(app, monkeypatch):
    from datetime import datetime
    alice, interim_id, final_id = _multi_step_reply(monkeypatch)
    Node.query.get(final_id).deleted_at = datetime.utcnow()
    _db.session.commit()
    resp = _client(app, alice).get(f"/api/nodes/{interim_id}/llm-status")
    assert "node" not in resp.get_json()

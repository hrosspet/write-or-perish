"""An AI reply's action details go to its owner only.

An AI reply's tool_calls_meta records what the reply did: the archive
searches it ran (with the query), the entries and saved references it read
in full (with their links), the artifacts it read or wrote, the proposals
it made and how they were applied. The owner is the user who asked for
the reply (human_owner_id, else the author), the owner the access checks
use. They get every entry in full. Anyone else who can see the reply,
admins included, gets each entry's name and status only, on every route
that returns the reply: the thread page (GET /nodes/<id>), the status poll
(GET /nodes/<id>/llm-status) and the Text-mode conversation
(GET /textmode/from-node/<id>), where another user's reply can be an
ancestor of the viewer's own node.

Routes run in the test_no_replies_for_none harness (in-memory sqlite, the
completion task stubbed).
"""
import json

from backend.tests.test_no_replies_for_none import (  # noqa: F401 (fixtures)
    app, task_mod, _user, _node, _reply, _client,
)
from backend.extensions import db as _db
from backend.utils.tool_meta import SHARED_TOOL_FIELDS, tool_calls_meta_for

# Strings that exist only in the owner's tool details.
QUERY = "the argument with my sister about the inheritance"
URL = "https://x.com/someone/status/1234567890"
HANDLE = "someone_followed_privately"
ARTIFACT_TITLE = "Notes on the therapy sessions"
ARTIFACT_DESCRIPTION = "What came up each week"
KIND = "therapy-notes"
ERROR = "No readable artifact of kind 'divorce-plan'"
APPLY_ERROR = "merge failed on 'call the lawyer'"
BATCH_ID = "msgbatch_secret_1"
SECRETS = (QUERY, URL, HANDLE, ARTIFACT_TITLE, ARTIFACT_DESCRIPTION, KIND,
           "divorce-plan", "call the lawyer", BATCH_ID)


def _full_meta(owner_id):
    """Every kind of entry a reply's tool_calls_meta holds, with the
    details only its owner may see."""
    return [
        {"name": "semantic_search", "input": {"query": QUERY},
         "status": "success", "query": QUERY,
         "matches": [{"node_id": 9001, "score": 0.81}],
         "ext_matches": [{"item_id": 77, "score": 0.7}]},
        {"name": "read_full", "input": {"ref": "R1"}, "status": "success",
         "kind": "external", "ref_id": 77, "ref": "R1", "user_id": owner_id,
         "url": URL, "author_handle": HANDLE},
        {"name": "read_full", "input": {"ref": "9001"}, "status": "success",
         "kind": "node", "ref_id": 9001, "ref": "9001", "user_id": owner_id},
        {"name": "update_artifact",
         "input": {"kind": KIND, "title": ARTIFACT_TITLE,
                   "description": ARTIFACT_DESCRIPTION,
                   "updated_content": "[redacted]"},
         "status": "success", "artifact_id": 31, "kind": KIND,
         "created": True, "previous_artifact_id": None},
        {"name": "read_artifact", "input": {"kind": "divorce-plan"},
         "status": "error", "error": ERROR},
        {"name": "read_todo", "input": {}, "status": "success",
         "todo_id": 12},
        {"name": "propose_todo", "status": "success", "draft_id": 44,
         "apply_status": "failed", "apply_error": APPLY_ERROR},
        {"name": "apply_share", "input": {}, "status": "success",
         "share_id": 5, "share_ids": [5]},
        {"name": "_batch", "batch_id": BATCH_ID, "custom_id": "node-1",
         "provider": "anthropic", "status": "ended",
         "submitted_at": "2026-10-01T10:00:00"},
        {"name": "_mode", "source_mode": "textmode"},
        {"name": "_client", "client": "ios"},
    ]


def _shared(meta):
    """What someone other than the owner gets: name and status, no app."""
    return [{k: m[k] for k in ("name", "status") if k in m}
            for m in meta if m["name"] != "_client"]


def _public_thread(alice):
    """alice's public entry with her AI reply under it, carrying the full
    tool details."""
    entry = _node(alice, _node(alice, privacy_level="public"),
                  privacy_level="public")
    reply = _reply(entry, alice)
    reply.privacy_level = "public"
    reply.llm_task_status = "completed"
    reply.tool_calls_meta = json.dumps(_full_meta(alice.id))
    _db.session.commit()
    return entry, reply


def _assert_no_details(resp):
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_data(as_text=True)
    for secret in SECRETS:
        assert secret not in body, secret


def test_the_shared_fields_are_name_and_status():
    assert SHARED_TOOL_FIELDS == ("name", "status")


class TestThreadPageAndPoll:
    def test_someone_else_gets_names_and_outcomes_only(self, app):  # noqa: F811
        alice = _user("alice")
        _entry, reply = _public_thread(alice)
        bob = _user("bob")
        _db.session.commit()
        bobs = _client(app, bob)
        expected = _shared(_full_meta(alice.id))

        node = bobs.get(f"/api/nodes/{reply.id}")
        _assert_no_details(node)
        assert node.get_json()["tool_calls_meta"] == expected

        status = bobs.get(f"/api/nodes/{reply.id}/llm-status")
        _assert_no_details(status)
        assert status.get_json()["tool_calls_meta"] == expected
        # Derived from the shared entries only.
        assert status.get_json().get("batch_submitted_at") is None

    def test_an_admin_who_is_not_the_owner_gets_the_same(self, app):  # noqa: F811
        alice = _user("alice")
        _entry, reply = _public_thread(alice)
        admin = _user("admin")
        admin.is_admin = True
        _db.session.commit()
        admins = _client(app, admin)
        expected = _shared(_full_meta(alice.id))
        for url in (f"/api/nodes/{reply.id}",
                    f"/api/nodes/{reply.id}/llm-status"):
            resp = admins.get(url)
            _assert_no_details(resp)
            assert resp.get_json()["tool_calls_meta"] == expected

    def test_the_owner_gets_every_detail(self, app):  # noqa: F811
        alice = _user("alice")
        _entry, reply = _public_thread(alice)
        alices = _client(app, alice)
        full = [m for m in _full_meta(alice.id) if m["name"] != "_client"]
        for url in (f"/api/nodes/{reply.id}",
                    f"/api/nodes/{reply.id}/llm-status"):
            resp = alices.get(url)
            assert resp.status_code == 200, resp.get_json()
            assert resp.get_json()["tool_calls_meta"] == full

    def test_the_owner_is_who_asked_for_the_reply_not_the_threads_author(
            self, app):  # noqa: F811
        # bob asks for a reply under alice's public entry: its tools ran
        # on bob's data, so bob gets the details and alice does not.
        alice = _user("alice")
        bob = _user("bob")
        entry = _node(alice, privacy_level="public")
        reply = _reply(entry, bob)
        reply.privacy_level = "public"
        reply.llm_task_status = "completed"
        reply.tool_calls_meta = json.dumps(_full_meta(bob.id))
        _db.session.commit()

        resp = _client(app, alice).get(f"/api/nodes/{reply.id}")
        _assert_no_details(resp)
        assert resp.get_json()["tool_calls_meta"] == _shared(
            _full_meta(bob.id))

        resp = _client(app, bob).get(f"/api/nodes/{reply.id}")
        assert resp.get_json()["tool_calls_meta"][0]["query"] == QUERY


class TestTextModeConversation:
    def test_another_users_reply_above_my_node_comes_without_details(
            self, app):  # noqa: F811
        alice = _user("alice")
        _entry, alices_reply = _public_thread(alice)
        bob = _user("bob")
        bobs_node = _node(bob, alices_reply, privacy_level="public")
        bobs_reply = _reply(bobs_node, bob)
        bobs_reply.llm_task_status = "completed"
        bobs_reply.tool_calls_meta = json.dumps(_full_meta(bob.id))
        _db.session.commit()

        resp = _client(app, bob).get(
            f"/api/textmode/from-node/{bobs_reply.id}")
        assert resp.status_code == 200, resp.get_json()
        by_id = {m["id"]: m for m in resp.get_json()["messages"]}
        assert by_id[alices_reply.id]["tool_calls_meta"] == _shared(
            _full_meta(alice.id))
        for secret in SECRETS:
            assert secret not in json.dumps(by_id[alices_reply.id])
        # bob's own reply in the same conversation keeps its details,
        # without the app marker.
        assert by_id[bobs_reply.id]["tool_calls_meta"] == [
            m for m in _full_meta(bob.id) if m["name"] != "_client"]


class TestHelper:
    def test_unreadable_or_empty_meta(self, app):  # noqa: F811
        alice = _user("alice")
        reply = _reply(_node(alice), alice)
        assert tool_calls_meta_for(reply, alice.id) is None
        reply.tool_calls_meta = "not json"
        assert tool_calls_meta_for(reply, alice.id) is None
        reply.tool_calls_meta = json.dumps({"name": "x"})
        assert tool_calls_meta_for(reply, alice.id) is None
        reply.tool_calls_meta = json.dumps(
            [{"name": "_client", "client": "web"}, "stray", None])
        assert tool_calls_meta_for(reply, None) == []

    def test_no_viewer_gets_the_shared_shape(self, app):  # noqa: F811
        alice = _user("alice")
        reply = _reply(_node(alice), alice)
        reply.tool_calls_meta = json.dumps(_full_meta(alice.id))
        assert tool_calls_meta_for(reply, None) == _shared(
            _full_meta(alice.id))

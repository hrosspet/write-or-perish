"""Deleting an artifact (#603): the route, every reader, the cleanup.

The DELETE route soft-deletes every version of a custom artifact; from
then on the artifact is gone everywhere (list, history, the AI's index,
its tools, session pins, exports); a new artifact of the same name
starts at v1; the daily cleanup deletes the rows after the grace period
with the sessions' pins to them.

The generation loop, the render cache and the voice pre-warm (the
session instructions a model actually receives) are covered end to end
in test_artifact_deletion_prompt.py, on test_retrieval_loop.py's
harness.

Patterned after test_artifacts.py: sqlite in-memory, minimal Flask app,
ENCRYPTION_DISABLED.
"""
import json
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import MagicMock

# ── Environment ──────────────────────────────────────────────────────────
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
from backend.models import (  # noqa: E402
    ArtifactView, Node, NodeContextArtifact, User, UserArtifact,
)

# ── Import the real llm_completion against stub glue (test_artifacts.py) ─
_GLUE = ("backend.celery_app", "backend.llm_providers",
         "backend.tasks.llm_completion")
_saved_glue = {k: sys.modules.get(k) for k in _GLUE}
sys.modules["backend.celery_app"] = MagicMock()
sys.modules["backend.llm_providers"] = MagicMock()
sys.modules.pop("backend.tasks.llm_completion", None)

from backend.tasks.llm_completion import (  # noqa: E402
    _artifact_write_report, _execute_tool_calls, _retrieval_injection_text,
    _scan_proposal_statuses, get_user_artifacts_context,
)

for _k, _v in _saved_glue.items():
    if _v is None:
        sys.modules.pop(_k, None)
    else:
        sys.modules[_k] = _v

from backend.utils.artifact_deletion import (  # noqa: E402
    deleted_kinds, deletion_stamp, purge_deleted_artifacts,
    soft_delete_artifact,
)


def _make_app():
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

    from backend.routes.artifacts import artifacts_bp
    app.register_blueprint(artifacts_bp, url_prefix="/api/artifacts")
    return app


@pytest.fixture
def app():
    app = _make_app()
    with app.app_context():
        _db.create_all()
        _db.session.add(User(username="alice"))
        _db.session.add(User(username="bob"))
        _db.session.commit()
        yield app
        _db.session.rollback()
        _db.drop_all()


def _user(name):
    return User.query.filter_by(username=name).first()


def _client_for(app, user):
    from flask import g
    g.pop("_login_user", None)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return client


@pytest.fixture
def alice_client(app):
    return _client_for(app, _user("alice"))


def _mk(user_id, kind, content, title=None, created_at=None,
        description=None):
    row = UserArtifact(
        user_id=user_id, kind=kind, title=title or kind.title(),
        generated_by="test", ai_usage="chat", description=description)
    if created_at is not None:
        row.created_at = created_at
    row.set_content(content)
    _db.session.add(row)
    _db.session.commit()
    return row


def _landlord(user_id):
    """A custom artifact with three versions."""
    t0 = datetime(2026, 10, 1)
    return [
        _mk(user_id, "landlord-case", f"landlord v{i}",
            title="Landlord Dispute", created_at=t0 + timedelta(days=i),
            description="The case")
        for i in (1, 2, 3)
    ]


def _session_node(user_id, content="{user_artifacts_index}"):
    """A system node whose prompt carries *content*, its artifacts pinned
    the way a session start pins them."""
    from backend.utils.context_artifacts import attach_context_artifacts
    node = Node(user_id=user_id, human_owner_id=user_id, node_type="system")
    node.set_content(content)
    _db.session.add(node)
    _db.session.flush()
    attach_context_artifacts(node.id, user_id)
    _db.session.commit()
    return node


def _run_tool(name, inp, uid):
    llm_node = MagicMock()
    llm_node.llm_model = "test-model"
    return _execute_tool_calls(
        [{"name": name, "input": inp}], llm_node, [], uid)[0]


# ── The route ────────────────────────────────────────────────────────────

def test_owner_deletes_every_version(app, alice_client):
    alice = _user("alice")
    rows = _landlord(alice.id)
    res = alice_client.delete("/api/artifacts/landlord-case")
    assert res.status_code == 200
    body = res.get_json()
    assert body["deleted"] == "landlord-case"
    assert body["versions"] == 3
    assert body["grace_days"] == 30
    _db.session.expire_all()
    # Soft delete: the rows stay for the grace period, all stamped.
    for row in rows:
        assert UserArtifact.query.get(row.id).deleted_at is not None
    # A second delete finds nothing, and says it was deleted already.
    again = alice_client.delete("/api/artifacts/landlord-case")
    assert again.status_code == 404
    assert again.get_json() == {"error": "This artifact was deleted.",
                                "deleted": True}


@pytest.mark.parametrize(
    "kind", sorted(UserArtifact.DEFAULT_KINDS) + ["external_digest"])
def test_built_in_kinds_and_the_digest_are_refused(app, alice_client, kind):
    alice = _user("alice")
    row = _mk(alice.id, kind, "kept")
    res = alice_client.delete(f"/api/artifacts/{kind}")
    assert res.status_code == 403
    _db.session.expire_all()
    assert UserArtifact.query.get(row.id).deleted_at is None
    assert UserArtifact.latest_for(alice.id, kind).get_content() == "kept"


def test_only_the_owner_can_delete(app, alice_client):
    bob = _user("bob")
    bobs = _landlord(bob.id)
    # Alice has no artifact of that name: hers is not found, Bob's is
    # untouched.
    assert alice_client.delete(
        "/api/artifacts/landlord-case").status_code == 404
    _db.session.expire_all()
    assert all(UserArtifact.query.get(r.id).deleted_at is None for r in bobs)


def test_unknown_kind_is_not_found(app, alice_client):
    res = alice_client.delete("/api/artifacts/nothing")
    assert res.status_code == 404
    assert res.get_json() == {"error": "Artifact not found"}


def test_list_marks_which_artifacts_can_be_deleted(app, alice_client):
    alice = _user("alice")
    _mk(alice.id, "memory", "m")
    _mk(alice.id, "external_digest", "d")
    _landlord(alice.id)
    items = {a["kind"]: a for a in
             alice_client.get("/api/artifacts/").get_json()["artifacts"]}
    assert items["landlord-case"]["deletable"] is True
    assert items["memory"]["deletable"] is False
    assert items["external_digest"]["deletable"] is False
    # Built-ins listed without a row yet are not deletable either.
    assert items["predictions"]["deletable"] is False


def test_soft_delete_hides_it_from_every_route(app, alice_client):
    alice = _user("alice")
    rows = _landlord(alice.id)
    assert alice_client.delete(
        "/api/artifacts/landlord-case").status_code == 200

    listing = alice_client.get("/api/artifacts/").get_json()
    assert "landlord-case" not in {a["kind"] for a in listing["artifacts"]}
    assert listing["deleted_kinds"] == ["landlord-case"]

    res = alice_client.get("/api/artifacts/landlord-case")
    assert res.status_code == 404
    assert res.get_json() == {"error": "This artifact was deleted.",
                              "deleted": True}
    # A kind that never existed is a plain 404.
    assert "deleted" not in alice_client.get(
        "/api/artifacts/never").get_json()

    assert alice_client.get(
        "/api/artifacts/landlord-case/versions").get_json() == {
            "versions": []}
    for row in rows:
        assert alice_client.get(
            f"/api/artifacts/versions/{row.id}").status_code == 404
        assert alice_client.post(
            f"/api/artifacts/landlord-case/revert/{row.id}"
        ).status_code == 404
    assert alice_client.patch(
        "/api/artifacts/landlord-case",
        json={"content": "- [x] ticked"}).status_code == 404
    # Nothing was written by any of these.
    assert UserArtifact.query.filter_by(kind="landlord-case").count() == 3


def test_same_name_starts_again_at_v1(app, alice_client):
    alice = _user("alice")
    _landlord(alice.id)
    alice_client.delete("/api/artifacts/landlord-case")

    res = alice_client.put("/api/artifacts/landlord-case",
                           json={"content": "fresh start"})
    assert res.status_code == 200
    art = res.get_json()["artifact"]
    assert art["version_number"] == 1
    # Nothing carried from the deleted versions: not their title
    # ("Landlord Dispute") nor their description.
    assert art["title"] == "Landlord Case"
    assert art["description"] is None
    versions = alice_client.get(
        "/api/artifacts/landlord-case/versions").get_json()["versions"]
    assert [v["version_number"] for v in versions] == [1]
    got = alice_client.get("/api/artifacts/landlord-case").get_json()
    assert got["artifact"]["content"] == "fresh start"
    assert got["artifact"]["version_number"] == 1
    assert alice_client.get(
        "/api/artifacts/").get_json()["deleted_kinds"] == []


def test_the_ai_makes_the_same_name_again_at_v1(app):
    alice = _user("alice")
    _landlord(alice.id)
    soft_delete_artifact(alice.id, "landlord-case")
    _db.session.commit()

    # An edit has nothing to edit: the deleted text is not the base.
    r = _run_tool("update_artifact", {
        "kind": "landlord-case",
        "edits": [{"old_text": "landlord v3", "new_text": "x"}]}, alice.id)
    assert r["status"] == "error"

    r = _run_tool("update_artifact", {
        "kind": "landlord-case", "updated_content": "the AI's new notes",
        "title": "Landlord Case"}, alice.id)
    _db.session.commit()
    assert r["status"] == "success"
    assert r["created"] is True
    assert r["previous_artifact_id"] is None
    assert UserArtifact.version_count(alice.id, "landlord-case") == 1
    assert UserArtifact.latest_for(
        alice.id, "landlord-case").get_content() == "the AI's new notes"


# ── The AI: index, pins, tools ───────────────────────────────────────────

def test_index_leaves_out_a_deleted_artifact(app):
    """{user_artifacts_index} built after the delete, without a pinned
    session (the latest-versions path)."""
    alice = _user("alice")
    _landlord(alice.id)
    _mk(alice.id, "reading-list", "books", title="Reading List")
    *_, index = get_user_artifacts_context(alice.id)
    assert "landlord-case" in index
    soft_delete_artifact(alice.id, "landlord-case")
    _db.session.commit()
    *_, index = get_user_artifacts_context(alice.id)
    assert "landlord-case" not in index
    assert "Landlord Dispute" not in index
    assert "reading-list" in index


def test_index_of_an_ongoing_session_leaves_it_out(app):
    """A session that pinned the artifact before the delete: the index
    rebuilt from its pins after the delete no longer lists it."""
    alice = _user("alice")
    _landlord(alice.id)
    _mk(alice.id, "reading-list", "books", title="Reading List")
    node = _session_node(alice.id)
    assert "landlord-case" in set(node.get_user_artifacts())
    *_, index = get_user_artifacts_context(alice.id, pinned_node=node)
    assert "landlord-case" in index

    soft_delete_artifact(alice.id, "landlord-case")
    _db.session.commit()
    _db.session.expire_all()
    node = Node.query.get(node.id)
    assert "landlord-case" not in set(node.get_user_artifacts())
    *_, index = get_user_artifacts_context(alice.id, pinned_node=node)
    assert "landlord-case" not in index
    assert "reading-list" in index


def test_session_with_only_deleted_pins_falls_back_without_it(app):
    alice = _user("alice")
    _landlord(alice.id)
    node = _session_node(alice.id)
    soft_delete_artifact(alice.id, "landlord-case")
    _db.session.commit()
    _db.session.expire_all()
    *_, index = get_user_artifacts_context(
        alice.id, pinned_node=Node.query.get(node.id))
    assert "landlord-case" not in index


def test_new_session_does_not_pin_a_deleted_artifact(app):
    alice = _user("alice")
    _landlord(alice.id)
    _mk(alice.id, "memory", "m")
    soft_delete_artifact(alice.id, "landlord-case")
    _db.session.commit()
    node = _session_node(alice.id)
    pinned = {UserArtifact.query.get(p.artifact_id).kind
              for p in NodeContextArtifact.query.filter_by(
                  node_id=node.id, artifact_type="user_artifact")}
    assert pinned == {"memory"}


def test_read_artifact_tool_cannot_read_it(app):
    alice = _user("alice")
    rows = _landlord(alice.id)
    soft_delete_artifact(alice.id, "landlord-case")
    _db.session.commit()
    r = _run_tool("read_artifact", {"kind": "landlord-case"}, alice.id)
    assert r["status"] == "error"
    # A read made before the delete delivers nothing after it (the
    # cross-turn injection and the within-turn loop both re-resolve).
    tr = {"name": "read_artifact", "status": "success",
          "kind": "landlord-case", "artifact_id": rows[-1].id}
    assert _retrieval_injection_text(tr, alice.id) is None
    node = Node(user_id=alice.id, node_type="llm", llm_model="test-model")
    node.set_content("resp")
    node.tool_calls_meta = json.dumps([tr])
    _db.session.add(node)
    _db.session.commit()
    notes, _ = _scan_proposal_statuses([node], alice.id)
    assert not any("landlord v" in n for n in notes)


def test_update_echo_of_a_deleted_version_is_empty(app):
    """The answer to a write (#604, which replaced the echo) shows no text
    of a deleted version, in its turn or replayed later."""
    alice = _user("alice")
    rows = _landlord(alice.id)
    tr = {"name": "update_artifact", "status": "success",
          "kind": "landlord-case", "artifact_id": rows[-1].id,
          "previous_artifact_id": rows[-2].id}
    assert "landlord v3" in _artifact_write_report(tr, alice.id)
    soft_delete_artifact(alice.id, "landlord-case")
    _db.session.commit()
    for known in (None, set()):
        assert _artifact_write_report(tr, alice.id, known=known) == (
            "'landlord-case' was saved, but that version no longer exists.")


def test_update_echo_never_diffs_against_a_deleted_previous_version(app):
    """The race under Known limits: the AI's write lands after the user
    deleted the version it was built on, so the new version is live and
    the previous one is deleted. The echo shows the new text only; the
    deleted version's lines never reach the model as the diff's context
    or removed lines."""
    alice = _user("alice")
    rows = _landlord(alice.id)
    soft_delete_artifact(alice.id, "landlord-case")
    _db.session.commit()
    written = _mk(alice.id, "landlord-case", "the AI's new line",
                  title="Landlord Dispute")
    tr = {"name": "update_artifact", "status": "success",
          "kind": "landlord-case", "artifact_id": written.id,
          "previous_artifact_id": rows[-1].id}
    echo = _artifact_write_report(tr, alice.id)
    assert echo == ("'landlord-case' updated to v1. Its full new text:\n"
                    "the AI's new line")
    # A diff against the deleted v3 would carry "-landlord v3".
    assert "landlord v3" not in echo


def test_pinned_row_lookup_misses_a_deleted_version(app):
    from backend.utils.context_artifacts import pinned_row
    alice = _user("alice")
    rows = _landlord(alice.id)
    assert pinned_row("user_artifact", rows[0].id) is not None
    soft_delete_artifact(alice.id, "landlord-case")
    _db.session.commit()
    assert pinned_row("user_artifact", rows[0].id) is None


# ── Exports ──────────────────────────────────────────────────────────────

def _read_in_thread(user_id, rows):
    """A thread whose AI reply read the artifact (read_artifact pins the
    version on the reply), as the export sees one."""
    root = Node(user_id=user_id, human_owner_id=user_id, node_type="user",
                privacy_level="private", ai_usage="chat")
    root.set_content("what about the landlord?")
    _db.session.add(root)
    _db.session.flush()
    reply = Node(user_id=user_id, human_owner_id=user_id, parent_id=root.id,
                 node_type="llm", llm_model="test-model",
                 privacy_level="private", ai_usage="chat")
    reply.set_content("Pulling it up.")
    _db.session.add(reply)
    _db.session.flush()
    _db.session.add(NodeContextArtifact(
        node_id=reply.id, artifact_type="user_artifact",
        artifact_id=rows[-1].id))
    _db.session.commit()
    return root, reply


def test_exports_leave_out_a_deleted_artifact(app):
    from backend.routes.export_data import (
        _artifact_ref_lines, build_user_export_content)
    alice = _user("alice")
    rows = _landlord(alice.id)
    _, reply = _read_in_thread(alice.id, rows)

    assert any("landlord-case" in line
               for line in _artifact_ref_lines(reply, alice.id))
    before = build_user_export_content(alice, filter_ai_usage=False)
    assert "landlord v3" in before

    soft_delete_artifact(alice.id, "landlord-case")
    _db.session.commit()
    _db.session.expire_all()
    reply = Node.query.get(reply.id)
    assert not any("landlord-case" in line
                   for line in _artifact_ref_lines(reply, alice.id))
    for kwargs in ({"filter_ai_usage": False},
                   {"filter_ai_usage": True},
                   {"filter_ai_usage": True,
                    "include_strategy": "engaged_threads"}):
        text = build_user_export_content(alice, **kwargs)
        assert "landlord v" not in text
        assert "landlord-case" not in text
    # The thread itself is still exported.
    assert "what about the landlord?" in build_user_export_content(
        alice, filter_ai_usage=False)


def test_export_version_numbers_count_live_versions(app):
    """A version made after the delete is v1 in the export too."""
    from backend.routes.export_data import _artifact_ref_lines
    alice = _user("alice")
    _landlord(alice.id)
    soft_delete_artifact(alice.id, "landlord-case")
    _db.session.commit()
    fresh = _mk(alice.id, "landlord-case", "new", title="Landlord Case")
    _, reply = _read_in_thread(alice.id, [fresh])
    assert _artifact_ref_lines(reply, alice.id) == [
        f"[Artifact 'landlord-case' v1 (ref #{fresh.id})]"]


# ── The stamp that keys the render cache ─────────────────────────────────

def test_deletion_stamp_moves_with_each_delete(app):
    alice = _user("alice")
    assert deletion_stamp(alice.id) is None
    _landlord(alice.id)
    _mk(alice.id, "reading-list", "books")
    soft_delete_artifact(alice.id, "landlord-case",
                         now=datetime(2026, 11, 6, 10, 0, 0, 1))
    _db.session.commit()
    first = deletion_stamp(alice.id)
    assert first
    soft_delete_artifact(alice.id, "reading-list",
                         now=datetime(2026, 11, 6, 10, 0, 0, 2))
    _db.session.commit()
    second = deletion_stamp(alice.id)
    assert second and second != first
    # Someone else's delete does not touch alice's stamp.
    assert deletion_stamp(_user("bob").id) is None


def _hide_all_artifacts(user_id):
    """A waiting "Delete all my writing" (#268) hides the user's rows."""
    from backend.models import UserDataPurge, UserDataPurgeHidden
    job = UserDataPurge(user_id=user_id, source="user",
                        scheduled_for=datetime.utcnow() + timedelta(days=30))
    _db.session.add(job)
    _db.session.flush()
    for (row_id,) in _db.session.query(UserArtifact.id).filter(
            UserArtifact.user_id == user_id):
        _db.session.add(UserDataPurgeHidden(
            job_id=job.id, kind="user_artifact", row_id=row_id))
    _db.session.commit()


def test_stamp_and_purge_with_a_waiting_delete_all_my_writing(app):
    """Hiding the rows does not move the stamp back (a render stored
    before the delete must stay unreachable), and the cleanup leaves
    the hidden rows to the purge that hid them."""
    alice = _user("alice")
    ids = [r.id for r in _landlord(alice.id)]
    soft_delete_artifact(alice.id, "landlord-case",
                         now=datetime.utcnow() - timedelta(days=31))
    _db.session.commit()
    stamp = deletion_stamp(alice.id)
    _hide_all_artifacts(alice.id)
    assert deletion_stamp(alice.id) == stamp
    assert purge_deleted_artifacts(
        datetime.utcnow() - timedelta(days=30))["purged"] == 0
    from backend.utils.hidden_rows import including_hidden_rows
    with including_hidden_rows():
        assert UserArtifact.query.filter(
            UserArtifact.id.in_(ids)).count() == 3


def test_deleted_kinds_names_only_kinds_not_made_again(app):
    alice = _user("alice")
    _landlord(alice.id)
    _mk(alice.id, "reading-list", "books")
    soft_delete_artifact(alice.id, "landlord-case")
    soft_delete_artifact(alice.id, "reading-list")
    _db.session.commit()
    _mk(alice.id, "reading-list", "again")
    assert deleted_kinds(alice.id) == ["landlord-case"]
    assert deleted_kinds(_user("bob").id) == []


# ── The cleanup ──────────────────────────────────────────────────────────

def test_cleanup_purges_after_the_grace_period_with_pins(app):
    alice = _user("alice")
    rows = _landlord(alice.id)
    keep = _mk(alice.id, "reading-list", "books")
    node = _session_node(alice.id)
    _db.session.add(ArtifactView(user_id=alice.id, kind="landlord-case"))
    _db.session.add(ArtifactView(user_id=alice.id, kind="reading-list"))
    _db.session.commit()
    pins = NodeContextArtifact.query.filter_by(
        node_id=node.id, artifact_type="user_artifact").count()
    assert pins == 2  # landlord-case v3 + reading-list

    soft_delete_artifact(alice.id, "landlord-case",
                         now=datetime.utcnow() - timedelta(days=31))
    _db.session.commit()
    ids = [r.id for r in rows]

    result = purge_deleted_artifacts(datetime.utcnow() - timedelta(days=30))
    assert result == {"purged": 3, "errors": 0}
    _db.session.expire_all()
    assert UserArtifact.query.filter(UserArtifact.id.in_(ids)).count() == 0
    assert NodeContextArtifact.query.filter(
        NodeContextArtifact.artifact_type == "user_artifact",
        NodeContextArtifact.artifact_id.in_(ids)).count() == 0
    # The live artifact, its pin and its views stay; the deleted one's
    # views (they name it) go.
    assert UserArtifact.query.get(keep.id) is not None
    assert NodeContextArtifact.query.filter_by(
        node_id=node.id, artifact_type="user_artifact",
        artifact_id=keep.id).count() == 1
    assert {v.kind for v in ArtifactView.query.all()} == {"reading-list"}
    # The session node itself is untouched.
    assert Node.query.get(node.id) is not None


def test_cleanup_waits_for_the_grace_period(app):
    alice = _user("alice")
    rows = _landlord(alice.id)
    soft_delete_artifact(alice.id, "landlord-case",
                         now=datetime.utcnow() - timedelta(days=5))
    _db.session.commit()
    result = purge_deleted_artifacts(datetime.utcnow() - timedelta(days=30))
    assert result == {"purged": 0, "errors": 0}
    assert UserArtifact.query.filter(
        UserArtifact.id.in_([r.id for r in rows])).count() == 3


def test_cleanup_keeps_a_kind_made_again(app):
    alice = _user("alice")
    old_ids = [r.id for r in _landlord(alice.id)]
    soft_delete_artifact(alice.id, "landlord-case",
                         now=datetime.utcnow() - timedelta(days=31))
    _db.session.commit()
    fresh_id = _mk(alice.id, "landlord-case", "again",
                   title="Landlord Case").id
    _db.session.add(ArtifactView(user_id=alice.id, kind="landlord-case"))
    _db.session.commit()

    purge_deleted_artifacts(datetime.utcnow() - timedelta(days=30))
    _db.session.expire_all()
    assert UserArtifact.query.filter(
        UserArtifact.id.in_(old_ids)).count() == 0
    assert UserArtifact.latest_for(alice.id, "landlord-case").id == fresh_id
    assert UserArtifact.version_count(alice.id, "landlord-case") == 1
    # The kind still has a live version, so its views stay.
    assert ArtifactView.query.filter_by(kind="landlord-case").count() == 1


def test_cleanup_leaves_other_users_alone(app):
    alice, bob = _user("alice"), _user("bob")
    _landlord(alice.id)
    bobs = _landlord(bob.id)
    soft_delete_artifact(alice.id, "landlord-case",
                         now=datetime.utcnow() - timedelta(days=31))
    _db.session.commit()
    purge_deleted_artifacts(datetime.utcnow() - timedelta(days=30))
    assert UserArtifact.query.filter(
        UserArtifact.id.in_([r.id for r in bobs])).count() == 3

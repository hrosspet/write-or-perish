"""An artifact version's title and description are stored like its text:
encrypted, in ``title_enc`` and ``description_enc`` (models.UserArtifact).

What these pin: the stored row does not hold the title or description as
written; versions stored before (plain ``title`` and ``description``)
read back unchanged; the artifacts list, one artifact, its versions, the
model's index, the approval dialog's preview and the dump give the same
output for a row in either form; a title edit, a delete and the model's
own write work as before; a version is written and read with one data
key; the owner-run script moves earlier rows in batches."""
import importlib.util
import os
import sys
import types
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from backend.extensions import db
from backend.models import User, UserArtifact
from backend.utils import encryption
from backend.tests.test_artifacts import (
    _make_app, _run_tool, get_user_artifacts_context,
)

TITLE = "Lisbon plan"
DESCRIPTION = "Steps for the move to Lisbon"
TEXT = "1. Find a flat\n2. Ship the books"


class _KeyService:
    """Stands in for the key service one level below the data-key cache,
    so the cache works as in production and calls are counted."""

    def __init__(self):
        self.wraps = 0
        self.unwraps = 0

    def encrypt(self, request):
        self.wraps += 1
        return types.SimpleNamespace(ciphertext=b"w:" + request["plaintext"])

    def decrypt(self, request):
        self.unwraps += 1
        return types.SimpleNamespace(plaintext=request["ciphertext"][2:])

    def forget(self):
        """A process that has read nothing yet."""
        encryption._dek_cache.clear()
        del encryption._dek_cache_order[:]
        self.wraps = self.unwraps = 0


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_DISABLED", "false")
    monkeypatch.setenv("GCP_KMS_KEY_NAME",
                       "projects/t/locations/l/keyRings/r/cryptoKeys/k")
    service = _KeyService()
    monkeypatch.setattr(encryption, "_kms_encrypt", service.encrypt)
    monkeypatch.setattr(encryption, "_kms_decrypt", service.decrypt)
    service.forget()
    yield service
    service.forget()


@pytest.fixture
def app(keys):
    app = _make_app()
    with app.app_context():
        db.create_all()
        db.session.add(User(username="tester"))
        db.session.commit()
        yield app
        db.session.rollback()
        db.drop_all()


@pytest.fixture
def client(app):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(User.query.first().id)
        sess["_fresh"] = True
    return client


def _uid():
    return User.query.first().id


def _artifact(kind="lisbon-plan", title=TITLE, description=DESCRIPTION,
              content=TEXT, user_id=None, at=None):
    row = UserArtifact(
        user_id=user_id or _uid(), kind=kind, title=title,
        description=description, generated_by="user")
    row.set_content(content)
    if at is not None:
        row.created_at = at
    db.session.add(row)
    db.session.commit()
    return row


def _row(row_id):
    return db.session.execute(text(
        "SELECT title, description, title_enc, description_enc, content "
        "FROM user_artifact WHERE id = :i"), {"i": row_id}).one()


def _store_as_before(row_id, title=TITLE, description=DESCRIPTION):
    """What a row holds that was written before titles were encrypted."""
    db.session.execute(text(
        "UPDATE user_artifact SET title = :t, description = :d, "
        "title_enc = NULL, description_enc = NULL WHERE id = :i"),
        {"t": title, "d": description, "i": row_id})
    db.session.commit()
    db.session.expire_all()


# -- what is stored ----------------------------------------------------------

def test_the_row_holds_title_and_description_encrypted_only(app):
    row = _artifact()
    stored = _row(row.id)
    assert stored.title == "" and stored.description is None
    assert stored.title_enc.startswith("ENC:v2:")
    assert stored.description_enc.startswith("ENC:v2:")
    for value in stored:
        assert "Lisbon" not in (value or "")
    db.session.expire_all()
    again = UserArtifact.query.get(row.id)
    assert (again.title, again.description) == (TITLE, DESCRIPTION)


def test_a_version_saved_through_the_page_is_stored_encrypted(app, client):
    r = client.put("/api/artifacts/lisbon-plan", json={
        "content": TEXT, "title": TITLE, "description": DESCRIPTION})
    assert r.status_code in (200, 201)
    stored = _row(UserArtifact.latest_for(_uid(), "lisbon-plan").id)
    assert stored.title == "" and stored.description is None
    assert "Lisbon" not in stored.title_enc + stored.description_enc


def test_a_version_the_model_writes_is_stored_encrypted(app):
    result = _run_tool(app, "update_artifact", {
        "kind": "lisbon-plan", "updated_content": TEXT, "title": TITLE,
        "description": DESCRIPTION}, _uid())
    assert result["status"] == "success"
    row = UserArtifact.latest_for(_uid(), "lisbon-plan")
    stored = _row(row.id)
    assert stored.title == "" and stored.description is None
    assert "Lisbon" not in stored.title_enc + stored.description_enc
    assert (row.title, row.description) == (TITLE, DESCRIPTION)


def test_a_version_without_a_description_has_none(app):
    row = _artifact(description=None)
    stored = _row(row.id)
    assert stored.description is None and stored.description_enc is None
    db.session.expire_all()
    assert UserArtifact.query.get(row.id).description is None


def test_a_row_from_before_reads_back_unchanged(app):
    row = _artifact()
    _store_as_before(row.id)
    again = UserArtifact.query.get(row.id)
    assert (again.title, again.description) == (TITLE, DESCRIPTION)
    db.session.commit()
    stored = _row(row.id)                    # reading rewrites nothing
    assert (stored.title, stored.title_enc) == (TITLE, None)


@pytest.mark.parametrize("odd", [
    "ENC:v2:AAAA:BBBB", "ENC:v1:AAAA", "ENC:v2:not an envelope",
])
def test_a_title_that_looks_like_a_stored_value_reads_back_as_written(
        app, odd):
    new = _artifact(kind="one", title=odd, description=odd)
    old = _artifact(kind="two")
    _store_as_before(old.id, title=odd, description=odd)
    for row_id in (new.id, old.id):
        row = UserArtifact.query.get(row_id)
        assert (row.title, row.description) == (odd, odd)


def test_with_encryption_off_the_plain_columns_are_used(app, monkeypatch):
    # Off is the line alone: with a key name beside it nothing starts.
    monkeypatch.setenv("GCP_KMS_KEY_NAME", "")
    monkeypatch.setenv("ENCRYPTION_DISABLED", "true")
    row = _artifact()
    stored = _row(row.id)
    assert (stored.title, stored.description) == (TITLE, DESCRIPTION)
    assert stored.title_enc is None and stored.description_enc is None


# -- readers: the same output for a row in either form -----------------------

def _outputs(client):
    uid = _uid()
    latest = UserArtifact.latest_for(uid, "lisbon-plan")
    from backend.utils.tool_approvals import _artifact_preview
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "scripts"))
    import dump_user
    out = {
        "list": client.get("/api/artifacts/").get_data(),
        "one": client.get("/api/artifacts/lisbon-plan").get_data(),
        "versions": client.get(
            "/api/artifacts/lisbon-plan/versions").get_data(),
        "version": client.get(
            f"/api/artifacts/versions/{latest.id}").get_data(),
        "changes": client.get(
            f"/api/artifacts/versions/{latest.id}/changes").get_data(),
        "index": get_user_artifacts_context(uid)[3].encode(),
        "preview": repr(sorted(_artifact_preview(
            {"kind": "lisbon-plan", "previous_artifact_id": latest.id,
             "input": {"kind": "lisbon-plan", "title": "Porto plan",
                       "description": "Another city",
                       "updated_content": TEXT + "\n3. Go"}},
            uid).items())),
    }
    out["dump"] = repr(dump_user._artifacts(User.query.get(uid),
                                            UserArtifact))
    return out


def test_every_reader_gives_the_same_for_a_row_from_before(
        app, client, monkeypatch):
    # The preview imports from the reply task's module by name; other
    # test files leave a stand-in under that name. Give it the module
    # this file's helpers came from.
    real = types.SimpleNamespace(**get_user_artifacts_context.__globals__)
    monkeypatch.setitem(sys.modules, "backend.tasks.llm_completion", real)
    first = _artifact(at=datetime.utcnow() - timedelta(days=2))
    second = _artifact(content=TEXT + "\n3. Learn Portuguese",
                       at=datetime.utcnow() - timedelta(days=1))
    _artifact(kind="memory", title="Memory", description=None,
              content="Likes tea.")
    new_form = _outputs(client)
    for row in (first, second):
        _store_as_before(row.id)
    old_form = _outputs(client)
    assert new_form == old_form
    assert TITLE.encode() in new_form["list"]
    assert DESCRIPTION.encode() in new_form["one"]
    assert (f'- lisbon-plan — "{TITLE}": {DESCRIPTION} (~'.encode()
            in new_form["index"])


def test_the_models_index_is_the_same_text_as_with_plain_columns(app):
    _artifact()
    _artifact(kind="books", title='Books "to read"', description="A list.")
    encrypted = get_user_artifacts_context(_uid())[3]
    for row in UserArtifact.query.all():
        _store_as_before(row.id, title=row.title, description=row.description)
    assert get_user_artifacts_context(_uid())[3] == encrypted
    assert any(line.startswith('- books — "Books "to read"": A list. (~')
               for line in encrypted.splitlines())


def test_the_export_names_the_artifact_the_same_for_a_row_from_before(app):
    from backend.models import Node, NodeContextArtifact
    from backend.routes.export_data import build_user_export_content
    user = User.query.first()
    art = _artifact()
    node = Node(user_id=user.id, human_owner_id=user.id, node_type="user",
                privacy_level="private", ai_usage="chat", token_count=10)
    node.set_content("thread body")
    db.session.add(node)
    db.session.flush()
    db.session.add(NodeContextArtifact(
        node_id=node.id, artifact_type="user_artifact", artifact_id=art.id))
    db.session.commit()
    encrypted = build_user_export_content(user, filter_ai_usage=True)
    assert f"### {TITLE} ('lisbon-plan') v1 (ref #{art.id})" in encrypted
    _store_as_before(art.id)
    assert build_user_export_content(
        User.query.first(), filter_ai_usage=True) == encrypted


# -- title edit, carry-forward, delete ---------------------------------------

def test_a_title_edit_makes_a_version_with_the_new_title(app, client):
    first = _artifact()
    r = client.put("/api/artifacts/lisbon-plan", json={
        "content": TEXT, "title": "Porto plan"})
    assert r.status_code in (200, 201)
    body = client.get("/api/artifacts/lisbon-plan").get_json()["artifact"]
    assert body["title"] == "Porto plan"
    assert body["description"] == DESCRIPTION          # carried forward
    latest = UserArtifact.latest_for(_uid(), "lisbon-plan")
    assert "Porto" not in _row(latest.id).title_enc
    assert _row(latest.id).title == ""
    assert UserArtifact.query.get(first.id).title == TITLE


def test_a_save_without_a_title_keeps_the_title_of_a_row_from_before(
        app, client):
    first = _artifact()
    _store_as_before(first.id)
    client.put("/api/artifacts/lisbon-plan", json={"content": "New text."})
    latest = UserArtifact.latest_for(_uid(), "lisbon-plan")
    assert latest.id != first.id
    assert (latest.title, latest.description) == (TITLE, DESCRIPTION)
    assert _row(latest.id).title == ""                  # the new row: encrypted


def test_a_deleted_artifact_leaves_no_title_after_the_clean_up(app, client):
    from backend.utils.artifact_deletion import purge_deleted_artifacts
    row_id = _artifact().id
    assert client.delete("/api/artifacts/lisbon-plan").status_code == 200
    assert client.get("/api/artifacts/lisbon-plan").status_code == 404
    listed = client.get("/api/artifacts/").get_json()["artifacts"]
    assert "lisbon-plan" not in [a["kind"] for a in listed]
    purge_deleted_artifacts(datetime.utcnow() + timedelta(days=1))
    db.session.commit()
    assert db.session.execute(text(
        "SELECT count(*) FROM user_artifact WHERE id = :i"),
        {"i": row_id}).scalar() == 0


# -- key-service calls -------------------------------------------------------

def test_a_version_is_written_with_one_key(app, keys):
    keys.forget()
    _artifact()
    assert (keys.wraps, keys.unwraps) == (1, 0)


def test_the_list_and_the_index_ask_for_one_key_per_artifact(
        app, client, keys):
    for i in range(4):
        _artifact(kind=f"plan-{i}", title=f"Plan {i}")
    db.session.expire_all()
    keys.forget()
    assert client.get("/api/artifacts/").status_code == 200
    assert (keys.wraps, keys.unwraps) == (0, 4)     # as for the texts alone
    assert client.get("/api/artifacts/").status_code == 200
    assert keys.unwraps == 4                         # then none
    db.session.expire_all()
    keys.forget()
    get_user_artifacts_context(_uid())
    assert (keys.wraps, keys.unwraps) == (0, 4)


def test_a_carried_title_costs_no_call_of_its_own(app, client, keys):
    _artifact()
    db.session.expire_all()
    keys.forget()
    client.put("/api/artifacts/lisbon-plan", json={"content": "Again."})
    # One to read the earlier version's title; the new version takes
    # that version's key.
    assert (keys.wraps, keys.unwraps) == (0, 1)


def test_thirty_versions_and_a_revert_are_listed_with_one_call(
        app, client, keys):
    first_id = None
    for i in range(30):
        db.session.remove()
        keys.forget()                       # each save in a new process
        row = _artifact(kind="memory", title="Memory",
                        description="Durable facts", content=f"v{i}",
                        at=datetime.utcnow() - timedelta(minutes=60 - i))
        first_id = first_id or row.id
        assert keys.wraps == (1 if i == 0 else 0)
    db.session.remove()
    keys.forget()
    r = client.post(f"/api/artifacts/memory/revert/{first_id}")
    assert r.status_code in (200, 201)
    assert r.get_json()["artifact"]["title"] == "Memory"
    assert keys.wraps == 0
    db.session.remove()
    keys.forget()
    r = client.get("/api/artifacts/memory/versions")
    versions = r.get_json()["versions"]
    assert len(versions) == 31
    assert {v["title"] for v in versions} == {"Memory"}
    assert {v["description"] for v in versions} == {"Durable facts"}
    assert (keys.wraps, keys.unwraps) == (0, 1)
    # The reverted version: its text, title and description, one key.
    db.session.remove()
    keys.forget()
    latest = UserArtifact.latest_for(_uid(), "memory")
    assert (latest.title, latest.description, latest.get_content()) == (
        "Memory", "Durable facts", "v0")
    assert keys.unwraps == 1


def test_a_revert_to_a_version_from_before_has_one_key(app, client, keys):
    old_id = _artifact(at=datetime.utcnow() - timedelta(days=2)).id
    _artifact(content="Later text.", at=datetime.utcnow() - timedelta(days=1))
    _store_as_before(old_id)
    db.session.remove()
    keys.forget()
    assert client.post(
        f"/api/artifacts/lisbon-plan/revert/{old_id}").status_code in (200, 201)
    db.session.remove()
    keys.forget()
    latest = UserArtifact.latest_for(_uid(), "lisbon-plan")
    assert (latest.title, latest.description, latest.get_content()) == (
        TITLE, DESCRIPTION, TEXT)
    assert _row(latest.id).title == ""
    assert keys.unwraps == 1


def test_versions_from_before_are_listed_with_no_call(app, client, keys):
    for i in range(5):
        row = _artifact(content=f"v{i}",
                        at=datetime.utcnow() - timedelta(minutes=10 - i))
        _store_as_before(row.id)
    db.session.remove()
    keys.forget()
    assert len(client.get(
        "/api/artifacts/lisbon-plan/versions").get_json()["versions"]) == 5
    assert (keys.wraps, keys.unwraps) == (0, 0)


def test_another_artifact_or_account_does_not_lend_its_key(app, keys):
    other = User(username="other")
    db.session.add(other)
    db.session.commit()
    mine = _artifact(kind="plan")
    theirs = _artifact(kind="plan", user_id=other.id)
    second = _artifact(kind="notes")
    key = encryption.wrapped_dek_of
    assert len({key(mine.content), key(theirs.content),
                key(second.content)}) == 3


def test_a_description_the_model_writes_is_cut_like_the_pages(app):
    long_description = "d" * 300
    result = _run_tool(app, "update_artifact", {
        "kind": "lisbon-plan", "updated_content": TEXT, "title": TITLE,
        "description": long_description}, _uid())
    assert result["status"] == "success"
    row = UserArtifact.latest_for(_uid(), "lisbon-plan")
    assert row.description == "d" * 255


# -- the script for earlier rows ---------------------------------------------

def _script():
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "scripts", "store_artifact_titles_encrypted.py")
    spec = importlib.util.spec_from_file_location("_store_art_titles", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _earlier_rows():
    other = User(username="other")
    db.session.add(other)
    db.session.commit()
    ids = []
    for i in range(3):
        ids.append(_artifact(kind=f"plan-{i}").id)
    ids.append(_artifact(kind="theirs", user_id=other.id).id)
    ids.append(_artifact(kind="bare", description=None).id)
    for row_id in ids[:-1]:
        _store_as_before(row_id)
    _store_as_before(ids[-1], description=None)
    return ids


def test_the_script_counts_without_writing_by_default(app):
    script = _script()
    ids = _earlier_rows()
    assert script.survey() == (5, 0)
    counts = script.convert(pause=0)
    assert counts["found"] == 5 and counts["converted"] == 0
    assert [_row(i).title for i in ids] == [TITLE] * 5


def test_the_script_moves_rows_with_one_key_per_account(app, keys):
    script = _script()
    ids = _earlier_rows()
    texts = {i: _row(i).content for i in ids}
    db.session.expire_all()
    keys.forget()

    counts = script.convert(apply=True, batch_size=2, pause=0)

    assert counts["converted"] == 5 and counts["accounts"] == 2
    assert (keys.wraps, keys.unwraps) == (2, 0)     # two accounts, no reads
    assert script.survey() == (0, 5)
    db.session.expire_all()
    for i in ids:
        stored = _row(i)
        assert stored.title == "" and stored.description is None
        assert "Lisbon" not in stored.title_enc + (stored.description_enc or "")
        assert stored.content == texts[i]            # the text: untouched
        row = UserArtifact.query.get(i)
        assert row.title == TITLE
    assert UserArtifact.query.get(ids[-1]).description is None
    assert UserArtifact.query.get(ids[0]).description == DESCRIPTION
    assert script.convert(apply=True, pause=0)["found"] == 0


def test_the_script_moves_rows_a_waiting_deletion_hides(app):
    from backend.models import UserDataPurge, UserDataPurgeHidden
    script = _script()
    ids = _earlier_rows()
    job = UserDataPurge(user_id=_uid(), source="user", scope="hidden",
                        scheduled_for=datetime.utcnow())
    db.session.add(job)
    db.session.flush()
    db.session.add(UserDataPurgeHidden(
        job_id=job.id, kind="user_artifact", row_id=ids[1]))
    db.session.commit()
    assert UserArtifact.query.get(ids[1]) is None      # hidden from the app
    assert script.survey() == (5, 0)
    assert script.convert(pause=0)["found"] == 5
    assert script.convert(apply=True, pause=0)["converted"] == 5
    assert script.survey() == (0, 5)
    hidden = _row(ids[1])
    assert hidden.title == "" and "Lisbon" not in hidden.title_enc
    assert script.undo(apply=True, pause=0)["converted"] == 5
    assert _row(ids[1]).title == TITLE


def test_the_script_continues_after_a_stop(app):
    script = _script()
    ids = _earlier_rows()
    first = script.convert(apply=True, batch_size=2, limit=2, pause=0)
    assert first["converted"] == 2 and first["last_id"] == ids[1]
    rest = script.convert(apply=True, batch_size=2,
                          after_id=first["last_id"], pause=0)
    assert rest["converted"] == 3
    assert script.survey() == (0, 5)


def test_the_script_can_bring_the_plain_columns_back(app):
    script = _script()
    ids = _earlier_rows()
    before = {i: tuple(_row(i)) for i in ids}
    script.convert(apply=True, batch_size=2, pause=0)
    assert script.undo(pause=0)["converted"] == 0       # dry run
    assert script.undo(apply=True, batch_size=2, pause=0)["converted"] == 5
    assert {i: tuple(_row(i)) for i in ids} == before

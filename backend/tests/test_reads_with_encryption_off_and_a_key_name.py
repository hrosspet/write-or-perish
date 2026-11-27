"""An instance that holds encrypted content and is then started with
ENCRYPTION_DISABLED=true while its key name is still set (encryption
.reads_decrypt): what was stored encrypted is still read as it was
written, with the key-service calls a read costs with encryption on,
and what is written from then on is stored unencrypted. With the switch
and no key name nothing can be decrypted: a stored value comes back as
it is.

Rows and files are written with encryption on, then the switch is set
for the rest of the test."""
import pytest
from flask import Flask
from sqlalchemy import text

import backend
from backend.extensions import db
from backend.models import Node, UserArtifact
from backend.utils import encryption, tool_record
from backend.tests.test_artifact_titles_stored_encrypted import (  # noqa: F401
    DESCRIPTION, TEXT, TITLE, _artifact, _row, _uid, app, client, keys,
)
from backend.tests.test_artifact_titles_stored_encrypted import (
    _script as _titles_script,
)
from backend.tests.test_tool_record_stored_encrypted import (
    _script as _records_script,
)

NOTE = "a private note"
RECORD = '[{"name": "search", "status": "success", "input": {"q": "x"}}]'


def _switch_off(monkeypatch, keys, key_name=True):
    """The same process started again with the switch: nothing read yet."""
    monkeypatch.setenv("ENCRYPTION_DISABLED", "true")
    if not key_name:
        monkeypatch.setenv("GCP_KMS_KEY_NAME", "")
    db.session.expire_all()
    keys.forget()


def _node(content=NOTE, record=None):
    node = Node(user_id=_uid(), node_type="user", privacy_level="private")
    node.set_content(content)
    if record is not None:
        node.tool_calls_meta = record
    db.session.add(node)
    db.session.commit()
    return node


def _stored_node(node_id):
    return db.session.execute(text(
        "SELECT content, tool_calls_meta FROM node WHERE id = :i"),
        {"i": node_id}).one()


# -- entries -----------------------------------------------------------------

def test_an_encrypted_entry_reads_as_written_with_one_call(
        app, keys, monkeypatch):
    node_id = _node().id
    assert _stored_node(node_id).content.startswith("ENC:v2:")
    _switch_off(monkeypatch, keys)
    node = db.session.get(Node, node_id)
    assert node.get_content() == NOTE
    assert node.get_content() == NOTE
    assert (keys.unwraps, keys.wraps) == (1, 0)        # as with encryption on


def test_reading_costs_the_calls_it_costs_with_encryption_on(
        app, keys, monkeypatch):
    ids = [_node(f"note {i}").id for i in range(5)]

    def read_all():
        db.session.expire_all()
        keys.forget()
        nodes = [db.session.get(Node, i) for i in ids]
        fetched = encryption.prefetch_deks(n.content for n in nodes)
        assert all(encryption.dek_cached(n.content) for n in nodes)
        assert [n.get_content() for n in nodes] == [
            f"note {i}" for i in range(5)]
        return fetched, keys.unwraps, keys.wraps

    with_encryption_on = read_all()
    monkeypatch.setenv("ENCRYPTION_DISABLED", "true")
    assert read_all() == with_encryption_on == (5, 5, 0)


def test_a_saved_edit_is_stored_unencrypted_and_nothing_is_lost(
        app, keys, monkeypatch):
    node_id = _node().id
    _switch_off(monkeypatch, keys)
    node = db.session.get(Node, node_id)
    shown = node.get_content()
    assert shown == NOTE                 # the editor is given the text
    node.set_content(shown + ", continued")
    db.session.commit()
    assert _stored_node(node_id).content == NOTE + ", continued"
    db.session.expire_all()
    assert db.session.get(Node, node_id).get_content() == \
        NOTE + ", continued"
    assert keys.wraps == 0               # nothing new is encrypted


def test_a_new_entry_is_stored_unencrypted(app, keys, monkeypatch):
    _switch_off(monkeypatch, keys)
    node = _node("written with the switch", record=RECORD)
    stored = _stored_node(node.id)
    assert stored.content == "written with the switch"
    assert "ENC:" not in stored.tool_calls_meta
    assert tool_record.read(stored.tool_calls_meta) == RECORD
    assert (keys.wraps, keys.unwraps) == (0, 0)


def test_an_encrypted_tool_record_reads_as_written(app, keys, monkeypatch):
    node_id = _node(record=RECORD).id
    stored = _stored_node(node_id).tool_calls_meta
    assert tool_record.is_encrypted_form(stored) and "search" in stored
    assert '"q"' not in stored
    _switch_off(monkeypatch, keys)
    node = db.session.get(Node, node_id)
    assert node.tool_calls_meta == RECORD
    assert node.get_content() == NOTE
    assert keys.unwraps == 1             # the text and its record: one key


# -- artifacts ---------------------------------------------------------------

def test_an_encrypted_artifact_reads_as_written(app, keys, monkeypatch):
    row_id = _artifact().id
    _switch_off(monkeypatch, keys)
    row = db.session.get(UserArtifact, row_id)
    assert (row.title, row.description, row.get_content()) == (
        TITLE, DESCRIPTION, TEXT)
    assert keys.unwraps == 1             # one key for the version


def test_a_new_version_carries_an_encrypted_title_forward_unencrypted(
        app, client, keys, monkeypatch):
    first = _artifact()
    _switch_off(monkeypatch, keys)
    r = client.put("/api/artifacts/lisbon-plan", json={
        "content": TEXT + "\n3. Learn Portuguese"})
    assert r.status_code in (200, 201)
    latest = UserArtifact.latest_for(_uid(), "lisbon-plan")
    assert latest.id != first.id
    stored = _row(latest.id)
    assert (stored.title, stored.description) == (TITLE, DESCRIPTION)
    assert stored.title_enc is None and stored.description_enc is None
    assert stored.content == TEXT + "\n3. Learn Portuguese"
    body = client.get("/api/artifacts/lisbon-plan").get_json()["artifact"]
    assert (body["title"], body["description"]) == (TITLE, DESCRIPTION)
    # The earlier version is as it was.
    assert _row(first.id).title_enc.startswith("ENC:v2:")
    assert db.session.get(UserArtifact, first.id).title == TITLE
    assert keys.wraps == 0


# -- files -------------------------------------------------------------------

@pytest.fixture
def audio(tmp_path, keys):
    plain = tmp_path / "media" / "user" / "1" / "node" / "7" / "original.mp3"
    plain.parent.mkdir(parents=True)
    plain.write_bytes(b"the audio" * 1000)
    stored = encryption.encrypt_file(str(plain))
    assert stored.endswith(".enc") and not plain.exists()
    keys.forget()
    return stored


def _media_app(tmp_path, monkeypatch):
    from backend.routes import media
    monkeypatch.setattr(media, "MEDIA_ROOT", (tmp_path / "media").resolve())
    monkeypatch.setattr(media, "_cache_control_for",
                        lambda rel: media.PRIVATE_CACHE_CONTROL)
    media_app = Flask(__name__)
    media_app.register_blueprint(media.media_bp, url_prefix="/media")
    return media_app


def test_an_encrypted_audio_file_is_read_and_served_decrypted(
        audio, keys, tmp_path, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_DISABLED", "true")
    assert encryption.decrypt_file(audio) == b"the audio" * 1000
    assert keys.unwraps == 1
    resp = _media_app(tmp_path, monkeypatch).test_client().get(
        "/media/user/1/node/7/original.mp3")
    assert resp.status_code == 200
    assert resp.data == b"the audio" * 1000
    resp.close()


def test_a_new_audio_file_is_stored_unencrypted(keys, tmp_path, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_DISABLED", "true")
    plain = tmp_path / "new.mp3"
    plain.write_bytes(b"new audio")
    assert encryption.encrypt_file(str(plain)) == str(plain)
    assert plain.read_bytes() == b"new audio"
    assert keys.wraps == 0


# -- with the switch and no key name: as before ------------------------------

def test_without_a_key_name_a_stored_value_comes_back_as_it_is(
        app, keys, audio, tmp_path, monkeypatch):
    node_id = _node().id
    stored = _stored_node(node_id).content
    with open(audio, "rb") as f:
        raw = f.read()
    _switch_off(monkeypatch, keys, key_name=False)
    assert encryption.reads_decrypt() is False
    assert db.session.get(Node, node_id).get_content() == stored
    assert encryption.decrypt_file(audio) == raw
    assert encryption.prefetch_deks([stored]) == 0
    resp = _media_app(tmp_path, monkeypatch).test_client().get(
        "/media/user/1/node/7/original.mp3")
    assert resp.status_code == 500
    assert (keys.unwraps, keys.wraps) == (0, 0)


def test_with_encryption_on_reads_decrypt(monkeypatch):
    on = {"GCP_KMS_KEY_NAME": "k"}
    assert encryption.reads_decrypt(on) is True
    assert encryption.reads_decrypt(dict(on, ENCRYPTION_DISABLED="true")) is True
    assert encryption.reads_decrypt({"ENCRYPTION_DISABLED": "true"}) is False
    with pytest.raises(encryption.EncryptionNotConfigured):
        encryption.reads_decrypt({})


# -- the two scripts for earlier rows ----------------------------------------

def _main(script, app, monkeypatch, capsys, *argv):
    monkeypatch.setattr(backend, "create_app", lambda: app)
    assert script.main(list(argv)) == 0
    return capsys.readouterr().out


def test_the_titles_script_converts_nothing_and_can_still_go_back(
        app, keys, monkeypatch, capsys):
    row_id = _artifact().id
    _switch_off(monkeypatch, keys)
    script = _titles_script()
    out = _main(script, app, monkeypatch, capsys, "--apply")
    assert "Nothing to do" in out
    assert _row(row_id).title_enc.startswith("ENC:v2:")
    out = _main(script, app, monkeypatch, capsys, "--undo", "--apply",
                "--pause", "0")
    assert "Nothing to do" not in out
    stored = _row(row_id)
    assert (stored.title, stored.description) == (TITLE, DESCRIPTION)
    assert stored.title_enc is None and stored.description_enc is None


def test_the_records_script_converts_nothing_and_can_still_go_back(
        app, keys, monkeypatch, capsys):
    node_id = _node(record=RECORD).id
    before = _stored_node(node_id).tool_calls_meta
    _switch_off(monkeypatch, keys)
    script = _records_script()
    out = _main(script, app, monkeypatch, capsys, "--apply")
    assert "Nothing to do" in out
    assert _stored_node(node_id).tool_calls_meta == before
    out = _main(script, app, monkeypatch, capsys, "--undo", "--apply",
                "--pause", "0")
    assert "Nothing to do" not in out
    assert _stored_node(node_id).tool_calls_meta == RECORD


@pytest.mark.parametrize("which", [_titles_script, _records_script])
def test_without_a_key_name_the_scripts_do_nothing_either_way(
        app, keys, monkeypatch, capsys, which):
    row_id = _artifact().id
    node_id = _node(record=RECORD).id
    before = (tuple(_row(row_id)), tuple(_stored_node(node_id)))
    _switch_off(monkeypatch, keys, key_name=False)
    for argv in (("--apply",), ("--undo", "--apply")):
        assert "Nothing to do" in _main(which(), app, monkeypatch, capsys,
                                        *argv)
    assert (tuple(_row(row_id)), tuple(_stored_node(node_id))) == before

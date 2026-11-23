"""ENCRYPTION_DISABLED=true is the only switch that turns encryption off,
in every environment, with or without a KMS key name beside it. Where it
is not set the app starts only with a key name. In production and on
staging it also needs a SECRET_KEY of its own (validate_keys)."""
import logging
import os
import pathlib
import subprocess
import sys

import pytest

import backend
from backend import note_encryption_off, validate_keys
from backend.config import PLACEHOLDER_SECRET_KEY
from backend.utils import encryption
from backend.utils.encryption import EncryptionNotConfigured

KMS_NAME = "projects/p/locations/l/keyRings/r/cryptoKeys/k"
WITH_KMS = {"GCP_KMS_KEY_NAME": KMS_NAME}
REPO = pathlib.Path(__file__).resolve().parents[2]
EVERY_ENV = ["production", "staging", "", None, "development", "testing",
             "Development"]


def _config(env, key="a-key-of-this-instance"):
    return {"DEPLOY_ENV": env, "SECRET_KEY": key}


@pytest.mark.parametrize("env", ["production", "staging", "", None, "prod"])
@pytest.mark.parametrize("key", [None, "", PLACEHOLDER_SECRET_KEY])
def test_no_start_without_a_secret_key(env, key):
    with pytest.raises(RuntimeError, match="SECRET_KEY is not set"):
        validate_keys(_config(env, key), WITH_KMS)


@pytest.mark.parametrize("env", EVERY_ENV)
@pytest.mark.parametrize("environ", [
    {}, {"GCP_KMS_KEY_NAME": ""}, {"ENCRYPTION_DISABLED": "false"},
    {"ENCRYPTION_DISABLED": ""}, {"ENCRYPTION_DISABLED": "off"}])
def test_no_start_when_not_turned_off_and_without_a_key_name(env, environ):
    with pytest.raises(RuntimeError, match="GCP_KMS_KEY_NAME is not set"):
        validate_keys(_config(env), environ)


@pytest.mark.parametrize("env", EVERY_ENV)
@pytest.mark.parametrize("environ", [
    {"ENCRYPTION_DISABLED": "true"}, {"ENCRYPTION_DISABLED": "1"},
    {"ENCRYPTION_DISABLED": "yes"},
    {"ENCRYPTION_DISABLED": "TRUE", "GCP_KMS_KEY_NAME": ""}])
def test_starts_with_encryption_turned_off_by_name(env, environ):
    assert validate_keys(_config(env), environ) is None
    assert encryption.encryption_choice(environ) is False


@pytest.mark.parametrize("env", EVERY_ENV)
@pytest.mark.parametrize("environ", [
    WITH_KMS, dict(WITH_KMS, ENCRYPTION_DISABLED="false"),
    dict(WITH_KMS, ENCRYPTION_DISABLED="")])
def test_starts_with_the_key_name_alone(env, environ):
    assert validate_keys(_config(env), environ) is None


@pytest.mark.parametrize("env", EVERY_ENV)
@pytest.mark.parametrize("off", ["true", "1", "Yes"])
def test_turned_off_with_a_key_name_set_starts_with_encryption_off(env, off):
    environ = dict(WITH_KMS, ENCRYPTION_DISABLED=off)
    assert validate_keys(_config(env), environ) is None
    assert encryption.encryption_choice(environ) is False


@pytest.mark.parametrize("env", ["development", "testing", "Development"])
@pytest.mark.parametrize("key", [None, "", PLACEHOLDER_SECRET_KEY, "t"])
def test_development_and_testing_start_without_a_secret_key(env, key):
    off = {"ENCRYPTION_DISABLED": "true"}
    assert validate_keys(_config(env, key), off) is None


def _notices(caplog):
    return [r.getMessage() for r in caplog.records
            if r.getMessage().startswith("Encryption is off")]


OFF_LINE = ("Encryption is off on this instance (ENCRYPTION_DISABLED=true): "
            "new content is stored unencrypted. GCP_KMS_KEY_NAME is set: ")
OFF_LINES = {
    "no": OFF_LINE + "no.",
    "yes": OFF_LINE + "yes. Content that was stored encrypted earlier is "
                      "still read with that key; nothing new is encrypted "
                      "with it, except text that itself begins with "
                      "ENC:v1: or ENC:v2:."}


@pytest.mark.parametrize("environ,key_name", [
    ({"ENCRYPTION_DISABLED": "true"}, "no"),
    ({"ENCRYPTION_DISABLED": "true", "GCP_KMS_KEY_NAME": ""}, "no"),
    (dict(WITH_KMS, ENCRYPTION_DISABLED="true"), "yes")])
def test_a_start_with_encryption_off_writes_one_fixed_line(
        caplog, monkeypatch, environ, key_name):
    monkeypatch.setattr(backend, "_encryption_off_noted", False)
    with caplog.at_level(logging.WARNING, logger="backend"):
        note_encryption_off(environ)
        note_encryption_off(environ)
    assert _notices(caplog) == [OFF_LINES[key_name]]
    assert KMS_NAME not in caplog.text


def test_a_start_with_encryption_on_writes_no_such_line(caplog, monkeypatch):
    monkeypatch.setattr(backend, "_encryption_off_noted", False)
    with caplog.at_level(logging.WARNING, logger="backend"):
        note_encryption_off(WITH_KMS)
    assert _notices(caplog) == []


def test_not_turned_off_and_without_a_key_name_nothing_is_stored(
        monkeypatch, tmp_path):
    monkeypatch.setenv("ENCRYPTION_DISABLED", "")
    monkeypatch.setenv("GCP_KMS_KEY_NAME", "")
    with pytest.raises(EncryptionNotConfigured):
        encryption.is_encryption_enabled()
    with pytest.raises(EncryptionNotConfigured):
        encryption.encrypt_content("a note")
    audio = tmp_path / "a.webm"
    audio.write_bytes(b"audio")
    with pytest.raises(EncryptionNotConfigured):
        encryption.encrypt_file(str(audio))
    assert audio.read_bytes() == b"audio"


def test_the_line_alone_stores_plain_text(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_DISABLED", "true")
    monkeypatch.setenv("GCP_KMS_KEY_NAME", "")
    assert encryption.is_encryption_enabled() is False
    assert encryption.encrypt_content("a note") == "a note"
    assert encryption.decrypt_content("a note") == "a note"


def test_the_key_name_alone_encrypts(monkeypatch):
    monkeypatch.delenv("ENCRYPTION_DISABLED", raising=False)
    monkeypatch.setenv("GCP_KMS_KEY_NAME", KMS_NAME)
    monkeypatch.setattr(encryption, "_wrap_dek", lambda dek: b"wrapped")
    assert encryption.is_encryption_enabled() is True
    assert encryption.encrypt_content("a note").startswith("ENC:v2:")


def _no_key_service(monkeypatch):
    def called(*args, **kwargs):
        raise AssertionError("the key service was called")
    monkeypatch.setattr(encryption, "_kms_encrypt", called)
    monkeypatch.setattr(encryption, "_kms_decrypt", called)


def test_turned_off_with_a_key_name_set_stores_unencrypted(
        monkeypatch, tmp_path):
    monkeypatch.setenv("ENCRYPTION_DISABLED", "true")
    monkeypatch.setenv("GCP_KMS_KEY_NAME", KMS_NAME)
    _no_key_service(monkeypatch)
    assert encryption.is_encryption_enabled() is False
    assert encryption.encrypt_content("a note") == "a note"
    assert encryption.decrypt_content("a note") == "a note"
    assert encryption.new_dek() is None
    audio = tmp_path / "a.webm"
    audio.write_bytes(b"audio")
    assert encryption.encrypt_file(str(audio)) == str(audio)
    assert audio.read_bytes() == b"audio"


def test_turned_off_without_a_key_name_leaves_an_encrypted_value_as_it_is(
        monkeypatch):
    """With the switch and no key name nothing can be decrypted: a value
    stored while the instance encrypted comes back as stored. (With a
    key name it is read as written:
    test_reads_with_encryption_off_and_a_key_name.py.)"""
    monkeypatch.setenv("ENCRYPTION_DISABLED", "false")
    monkeypatch.setenv("GCP_KMS_KEY_NAME", KMS_NAME)
    monkeypatch.setattr(encryption, "_wrap_dek", lambda dek: b"wrapped")
    stored = encryption.encrypt_content("a note")
    monkeypatch.setenv("ENCRYPTION_DISABLED", "true")
    monkeypatch.setenv("GCP_KMS_KEY_NAME", "")
    _no_key_service(monkeypatch)
    assert encryption.decrypt_content(stored) == stored


def test_the_message_names_the_setting_and_no_value():
    with pytest.raises(RuntimeError) as refused:
        validate_keys(_config("staging", PLACEHOLDER_SECRET_KEY), WITH_KMS)
    assert PLACEHOLDER_SECRET_KEY not in str(refused.value)
    assert KMS_NAME not in str(refused.value)


@pytest.fixture
def sqlite_config(monkeypatch):
    # The Config create_app reads (backend/__init__.py's own name for it).
    import backend
    monkeypatch.setattr(backend.Config, "SQLALCHEMY_DATABASE_URI",
                        "sqlite:///:memory:")
    monkeypatch.setattr(backend.Config, "DEPLOY_ENV", "production")
    monkeypatch.setattr(backend.Config, "SECRET_KEY", "a-key-of-this-instance")
    monkeypatch.setenv("ENCRYPTION_DISABLED", "true")
    monkeypatch.delenv("GCP_KMS_KEY_NAME", raising=False)
    return backend.Config


def test_create_app_refuses_the_placeholder_key(sqlite_config, monkeypatch):
    from backend import create_app
    monkeypatch.setattr(sqlite_config, "SECRET_KEY", PLACEHOLDER_SECRET_KEY)
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app()


@pytest.mark.parametrize("env", ["production", "development", "testing"])
def test_create_app_refuses_encryption_off_by_omission(
        sqlite_config, monkeypatch, env):
    from backend import create_app
    monkeypatch.setattr(sqlite_config, "DEPLOY_ENV", env)
    monkeypatch.delenv("ENCRYPTION_DISABLED")
    with pytest.raises(RuntimeError, match="GCP_KMS_KEY_NAME is not set"):
        create_app()


_START = "import backend.app\nimport backend.celery_app\nprint('started')"


_STORE = """
import backend.app
from sqlalchemy import text
from backend.extensions import db
from backend.models import Node, User
with backend.app.app.app_context():
    db.create_all()
    user = User(username="off")
    db.session.add(user)
    db.session.flush()
    node = Node(user_id=user.id, node_type="user", privacy_level="private")
    node.set_content("a private note")
    db.session.add(node)
    db.session.commit()
    print("stored:", db.session.execute(
        text("SELECT content FROM node WHERE id = :i"),
        {"i": node.id}).scalar())
    print("read:", node.get_content())
"""


def _start(flask_env, off, key_name, script=None):
    """Import the web app and the Celery app (workers and beat) in a
    process of their own, as a server does."""
    env = dict(os.environ)
    env.update({"DATABASE_URL": "sqlite:///:memory:", "SENTRY_DSN": "",
                "SECRET_KEY": "a-key-of-this-instance",
                "FLASK_ENV": flask_env, "ENCRYPTION_DISABLED": off,
                "GCP_KMS_KEY_NAME": key_name, "PYTHONPATH": str(REPO)})
    return subprocess.run([sys.executable, "-c", script or _START], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=180)


@pytest.mark.parametrize("flask_env", ["production", "development"])
def test_the_app_turned_off_with_a_key_name_set_starts_and_stores_unencrypted(
        flask_env):
    result = _start(flask_env, "true", KMS_NAME, _STORE)
    assert result.returncode == 0, result.stderr[-3000:]
    assert "stored: a private note" in result.stdout
    assert "read: a private note" in result.stdout
    assert result.stderr.count(OFF_LINES["yes"]) == 1
    assert KMS_NAME not in result.stderr


@pytest.mark.parametrize("flask_env", ["production", "development"])
@pytest.mark.parametrize("off,key_name,line", [
    ("true", "", "no"), ("true", KMS_NAME, "yes"), ("", KMS_NAME, None)])
def test_web_and_workers_start_turned_off_or_with_a_key_name(
        flask_env, off, key_name, line):
    result = _start(flask_env, off, key_name)
    assert result.returncode == 0, result.stderr[-3000:]
    assert result.stdout.strip().endswith("started")
    if line is None:
        assert "Encryption is off" not in result.stderr
    else:
        assert result.stderr.count(OFF_LINES[line]) == 1
        assert result.stderr.count("Encryption is off") == 1
    assert KMS_NAME not in result.stderr


@pytest.mark.parametrize("flask_env", ["production", "development"])
@pytest.mark.parametrize("off", ["", "false"])
def test_web_and_workers_do_not_start_when_not_turned_off_without_a_key_name(
        flask_env, off):
    result = _start(flask_env, off, "")
    assert result.returncode != 0
    assert "GCP_KMS_KEY_NAME is not set" in result.stderr

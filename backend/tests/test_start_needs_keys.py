"""The app starts, in every environment, only with a stated choice about
encryption: a KMS key name, or ENCRYPTION_DISABLED=true, never neither
and never both. In production and on staging it also needs a SECRET_KEY
of its own (validate_keys)."""
import os
import pathlib
import subprocess
import sys

import pytest

from backend import validate_keys
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
def test_no_start_with_neither_line_in_any_environment(env, environ):
    with pytest.raises(RuntimeError, match="GCP_KMS_KEY_NAME is not set"):
        validate_keys(_config(env), environ)


@pytest.mark.parametrize("env", EVERY_ENV)
@pytest.mark.parametrize("environ", [
    {"ENCRYPTION_DISABLED": "true"}, {"ENCRYPTION_DISABLED": "1"},
    {"ENCRYPTION_DISABLED": "yes"},
    {"ENCRYPTION_DISABLED": "TRUE", "GCP_KMS_KEY_NAME": ""}])
def test_starts_with_encryption_turned_off_by_name(env, environ):
    assert validate_keys(_config(env), environ) is None


@pytest.mark.parametrize("env", EVERY_ENV)
@pytest.mark.parametrize("environ", [
    WITH_KMS, dict(WITH_KMS, ENCRYPTION_DISABLED="false"),
    dict(WITH_KMS, ENCRYPTION_DISABLED="")])
def test_starts_with_the_key_name_alone(env, environ):
    assert validate_keys(_config(env), environ) is None


@pytest.mark.parametrize("env", EVERY_ENV)
@pytest.mark.parametrize("off", ["true", "1", "Yes"])
def test_no_start_with_both_lines(env, off):
    with pytest.raises(RuntimeError, match="are both set"):
        validate_keys(_config(env), dict(WITH_KMS, ENCRYPTION_DISABLED=off))


@pytest.mark.parametrize("env", ["development", "testing", "Development"])
@pytest.mark.parametrize("key", [None, "", PLACEHOLDER_SECRET_KEY, "t"])
def test_development_and_testing_start_without_a_secret_key(env, key):
    off = {"ENCRYPTION_DISABLED": "true"}
    assert validate_keys(_config(env, key), off) is None


def test_the_refusals_about_encryption_name_no_value():
    with pytest.raises(RuntimeError) as refused:
        validate_keys(_config("development"),
                      dict(WITH_KMS, ENCRYPTION_DISABLED="true"))
    assert KMS_NAME not in str(refused.value)


def test_without_either_line_nothing_is_stored_as_plain_text(
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


def test_both_lines_store_nothing(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_DISABLED", "true")
    monkeypatch.setenv("GCP_KMS_KEY_NAME", KMS_NAME)
    with pytest.raises(EncryptionNotConfigured, match="are both set"):
        encryption.encrypt_content("a note")


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


@pytest.mark.parametrize("env", ["production", "development", "testing"])
def test_create_app_refuses_both_lines(sqlite_config, monkeypatch, env):
    from backend import create_app
    monkeypatch.setattr(sqlite_config, "DEPLOY_ENV", env)
    monkeypatch.setenv("GCP_KMS_KEY_NAME", KMS_NAME)
    with pytest.raises(RuntimeError, match="are both set"):
        create_app()


_START = "import backend.app\nimport backend.celery_app\nprint('started')"


def _start(flask_env, off, key_name):
    """Import the web app and the Celery app (workers and beat) in a
    process of their own, as a server does."""
    env = dict(os.environ)
    env.update({"DATABASE_URL": "sqlite:///:memory:", "SENTRY_DSN": "",
                "SECRET_KEY": "a-key-of-this-instance",
                "FLASK_ENV": flask_env, "ENCRYPTION_DISABLED": off,
                "GCP_KMS_KEY_NAME": key_name, "PYTHONPATH": str(REPO)})
    return subprocess.run([sys.executable, "-c", _START], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=180)


@pytest.mark.parametrize("flask_env", ["production", "development"])
@pytest.mark.parametrize("off,key_name", [("true", ""), ("", KMS_NAME)])
def test_web_and_workers_start_with_exactly_one_line(
        flask_env, off, key_name):
    result = _start(flask_env, off, key_name)
    assert result.returncode == 0, result.stderr[-3000:]
    assert result.stdout.strip().endswith("started")


@pytest.mark.parametrize("flask_env", ["production", "development"])
@pytest.mark.parametrize("off,key_name,message", [
    ("", "", "GCP_KMS_KEY_NAME is not set"),
    ("true", KMS_NAME, "are both set")])
def test_web_and_workers_do_not_start_with_neither_or_both(
        flask_env, off, key_name, message):
    result = _start(flask_env, off, key_name)
    assert result.returncode != 0
    assert message in result.stderr
    assert KMS_NAME not in result.stderr

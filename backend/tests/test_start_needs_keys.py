"""The app starts in production and on staging only with a SECRET_KEY of
its own and a stated choice about encryption (validate_keys); development
and testing start as before."""
import pytest

from backend import validate_keys
from backend.config import PLACEHOLDER_SECRET_KEY

KMS_NAME = "projects/p/locations/l/keyRings/r/cryptoKeys/k"
WITH_KMS = {"GCP_KMS_KEY_NAME": KMS_NAME}


def _config(env, key="a-key-of-this-instance"):
    return {"DEPLOY_ENV": env, "SECRET_KEY": key}


@pytest.mark.parametrize("env", ["production", "staging", "", None, "prod"])
@pytest.mark.parametrize("key", [None, "", PLACEHOLDER_SECRET_KEY])
def test_no_start_without_a_secret_key(env, key):
    with pytest.raises(RuntimeError, match="SECRET_KEY is not set"):
        validate_keys(_config(env, key), WITH_KMS)


@pytest.mark.parametrize("env", ["production", "staging", "", None])
@pytest.mark.parametrize("environ", [
    {}, {"GCP_KMS_KEY_NAME": ""}, {"ENCRYPTION_DISABLED": "false"},
    {"ENCRYPTION_DISABLED": ""}])
def test_no_start_without_the_encryption_key_name(env, environ):
    with pytest.raises(RuntimeError, match="GCP_KMS_KEY_NAME is not set"):
        validate_keys(_config(env), environ)


@pytest.mark.parametrize("env", ["production", "staging", None])
@pytest.mark.parametrize("environ", [
    WITH_KMS, {"ENCRYPTION_DISABLED": "true"}, {"ENCRYPTION_DISABLED": "1"},
    {"ENCRYPTION_DISABLED": "TRUE", "GCP_KMS_KEY_NAME": ""}])
def test_starts_with_both_or_with_encryption_turned_off_by_name(env, environ):
    assert validate_keys(_config(env), environ) is None


@pytest.mark.parametrize("env", ["development", "testing", "Development"])
@pytest.mark.parametrize("key", [None, "", PLACEHOLDER_SECRET_KEY, "t"])
def test_development_and_testing_start_as_before(env, key):
    assert validate_keys(_config(env, key), {}) is None


def test_the_message_names_the_setting_and_no_value():
    with pytest.raises(RuntimeError) as refused:
        validate_keys(_config("staging", PLACEHOLDER_SECRET_KEY), WITH_KMS)
    assert PLACEHOLDER_SECRET_KEY not in str(refused.value)
    assert KMS_NAME not in str(refused.value)


@pytest.fixture
def sqlite_config(monkeypatch):
    import backend.config as config
    monkeypatch.setattr(config.Config, "SQLALCHEMY_DATABASE_URI",
                        "sqlite:///:memory:")
    monkeypatch.setattr(config.Config, "DEPLOY_ENV", "production")
    monkeypatch.setattr(config.Config, "SECRET_KEY", "a-key-of-this-instance")
    monkeypatch.setenv("ENCRYPTION_DISABLED", "true")
    monkeypatch.delenv("GCP_KMS_KEY_NAME", raising=False)
    return config.Config


def test_create_app_refuses_the_placeholder_key(sqlite_config, monkeypatch):
    from backend import create_app
    monkeypatch.setattr(sqlite_config, "SECRET_KEY", PLACEHOLDER_SECRET_KEY)
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app()


def test_create_app_refuses_encryption_off_by_omission(
        sqlite_config, monkeypatch):
    from backend import create_app
    monkeypatch.delenv("ENCRYPTION_DISABLED")
    with pytest.raises(RuntimeError, match="GCP_KMS_KEY_NAME"):
        create_app()


def test_create_app_starts_with_a_key_and_a_stated_choice(sqlite_config):
    from backend import create_app
    app = create_app()
    assert app.config["SECRET_KEY"] == "a-key-of-this-instance"

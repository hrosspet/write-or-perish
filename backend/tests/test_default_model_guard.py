"""create_app refuses an LLM_NAME that is not a SUPPORTED_MODELS key.

On 2026-09-10 prod's .env.production got the API id (claude-opus-4-6)
instead of the config key (claude-opus-4.6). Nothing failed visibly: reply
routes 400'd for the 59 accounts with no preferred model, and the profile
seeder / recent-context / digest tasks logged a warning per user and
skipped, for five days. A boot failure makes it a red deploy instead."""
import pytest

from backend import validate_default_model


MODELS = {"claude-opus-4.6": {}, "claude-opus-5": {}, "gpt-5.6-sol": {}}


def test_valid_key_passes():
    validate_default_model({"DEFAULT_LLM_MODEL": "claude-opus-5",
                            "SUPPORTED_MODELS": MODELS})


def test_api_id_instead_of_key_fails_with_the_key_as_hint():
    with pytest.raises(RuntimeError) as e:
        validate_default_model({"DEFAULT_LLM_MODEL": "claude-opus-4-6",
                                "SUPPORTED_MODELS": MODELS})
    msg = str(e.value)
    assert "LLM_NAME='claude-opus-4-6'" in msg
    assert "did you mean 'claude-opus-4.6'" in msg


def test_unknown_name_lists_the_keys():
    with pytest.raises(RuntimeError) as e:
        validate_default_model({"DEFAULT_LLM_MODEL": "nope",
                                "SUPPORTED_MODELS": MODELS})
    msg = str(e.value)
    assert "did you mean" not in msg
    assert "claude-opus-4.6, claude-opus-5, gpt-5.6-sol" in msg


def test_missing_name_fails():
    with pytest.raises(RuntimeError):
        validate_default_model({"SUPPORTED_MODELS": MODELS})


def test_deprecated_default_fails():
    models = {"claude-opus-4.6": {"provider": "anthropic"},
              "claude-opus-5": {"provider": "anthropic", "deprecated": True}}
    with pytest.raises(RuntimeError) as e:
        validate_default_model({"DEFAULT_LLM_MODEL": "claude-opus-5",
                                "SUPPORTED_MODELS": models})
    assert "deprecated" in str(e.value)
    assert "claude-opus-4.6" in str(e.value)


def test_read_default_must_be_a_read_model():
    models = {"claude-opus-4.6": {"provider": "anthropic"},
              "gpt-6-luna": {"provider": "openai", "read": True}}
    validate_default_model({"DEFAULT_LLM_MODEL": "claude-opus-4.6",
                            "READ_DEFAULT_MODEL": "gpt-6-luna",
                            "SUPPORTED_MODELS": models})
    with pytest.raises(RuntimeError):
        validate_default_model({"DEFAULT_LLM_MODEL": "claude-opus-4.6",
                                "READ_DEFAULT_MODEL": "claude-opus-4.6",
                                "SUPPORTED_MODELS": models})


def test_a_read_only_model_is_no_chat_default_but_may_be_the_read_default():
    models = {"claude-opus-4.6": {"provider": "anthropic"},
              "gpt-6.1-sol": {"provider": "openai", "read": True,
                              "chat": False}}
    with pytest.raises(RuntimeError) as e:
        validate_default_model({"DEFAULT_LLM_MODEL": "gpt-6.1-sol",
                                "SUPPORTED_MODELS": models})
    assert "read only" in str(e.value)
    assert "Active chat models: claude-opus-4.6" in str(e.value)
    validate_default_model({"DEFAULT_LLM_MODEL": "claude-opus-4.6",
                            "READ_DEFAULT_MODEL": "gpt-6.1-sol",
                            "SUPPORTED_MODELS": models})


def test_the_shipped_config_passes():
    from backend.config import Config
    validate_default_model({
        "DEFAULT_LLM_MODEL": "claude-opus-4.6",
        "READ_DEFAULT_MODEL": Config.READ_DEFAULT_MODEL,
        "SUPPORTED_MODELS": Config.SUPPORTED_MODELS})


def test_a_glean_model_may_be_another_providers_read_model():
    """Peter, 2026-10-14: GPT-5.6 Luna is Glean's default for both
    providers while Glean is tested. A setting must still be an active
    read model."""
    models = {"claude-opus-4.6": {"provider": "anthropic"},
              "gpt-5.6-luna": {"provider": "openai", "read": True},
              "gpt-6-sol": {"provider": "openai"},
              "gpt-5.5": {"provider": "openai", "read": True,
                          "deprecated": True}}
    base = {"DEFAULT_LLM_MODEL": "claude-opus-4.6", "SUPPORTED_MODELS": models}
    validate_default_model({**base, "GLEAN_MODEL_ANTHROPIC": "gpt-5.6-luna",
                            "GLEAN_MODEL_OPENAI": "gpt-5.6-luna"})
    for bad in ("gpt-6-sol", "gpt-5.5", "claude-haiku-5-5"):
        with pytest.raises(RuntimeError) as e:
            validate_default_model({**base, "GLEAN_MODEL_ANTHROPIC": bad})
        assert f"GLEAN_MODEL_ANTHROPIC={bad!r}" in str(e.value)


def test_the_shipped_glean_defaults_pass():
    from backend.config import Config
    assert (Config.GLEAN_MODEL_ANTHROPIC, Config.GLEAN_MODEL_OPENAI) == (
        "gpt-5.6-luna", "gpt-5.6-luna")
    validate_default_model({
        "DEFAULT_LLM_MODEL": "claude-opus-4.6",
        "READ_DEFAULT_MODEL": Config.READ_DEFAULT_MODEL,
        "GLEAN_MODEL_ANTHROPIC": Config.GLEAN_MODEL_ANTHROPIC,
        "GLEAN_MODEL_OPENAI": Config.GLEAN_MODEL_OPENAI,
        "SUPPORTED_MODELS": Config.SUPPORTED_MODELS})


def test_a_chat_default_must_be_its_own_providers_chat_model():
    """#487: a saved model no longer offered moves to its provider's
    CHAT_MODEL_<PROVIDER>. Another provider's model there would move the
    user to that provider."""
    models = {"claude-opus-4.6": {"provider": "anthropic"},
              "gpt-6-astra": {"provider": "openai"},
              "gpt-6.1-sol": {"provider": "openai", "read": True,
                              "chat": False},
              "gpt-5.5": {"provider": "openai", "deprecated": True}}
    base = {"DEFAULT_LLM_MODEL": "claude-opus-4.6", "SUPPORTED_MODELS": models}
    validate_default_model({**base, "CHAT_MODEL_OPENAI": "gpt-6-astra",
                            "CHAT_MODEL_ANTHROPIC": "claude-opus-4.6"})
    for bad in ("claude-opus-4.6", "gpt-6.1-sol", "gpt-5.5", "gpt-6"):
        with pytest.raises(RuntimeError) as e:
            validate_default_model({**base, "CHAT_MODEL_OPENAI": bad})
        assert f"CHAT_MODEL_OPENAI={bad!r}" in str(e.value)
        assert "Active chat models: claude-opus-4.6, gpt-6-astra" in str(e.value)


def test_the_shipped_chat_defaults_pass():
    from backend.config import Config
    assert (Config.CHAT_MODEL_ANTHROPIC, Config.CHAT_MODEL_OPENAI) == (
        "claude-opus-5.5", "gpt-6-astra")
    validate_default_model({
        "DEFAULT_LLM_MODEL": "claude-opus-4.6",
        "READ_DEFAULT_MODEL": Config.READ_DEFAULT_MODEL,
        "CHAT_MODEL_ANTHROPIC": Config.CHAT_MODEL_ANTHROPIC,
        "CHAT_MODEL_OPENAI": Config.CHAT_MODEL_OPENAI,
        "SUPPORTED_MODELS": Config.SUPPORTED_MODELS})

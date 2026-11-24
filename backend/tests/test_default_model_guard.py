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
    # OpenAI's moved to GPT-6.2 Astra on 2026-11-23 (Peter).
    assert (Config.CHAT_MODEL_ANTHROPIC, Config.CHAT_MODEL_OPENAI) == (
        "claude-opus-5.5", "gpt-6.2-astra")
    validate_default_model({
        "DEFAULT_LLM_MODEL": "claude-opus-4.6",
        "READ_DEFAULT_MODEL": Config.READ_DEFAULT_MODEL,
        "CHAT_MODEL_ANTHROPIC": Config.CHAT_MODEL_ANTHROPIC,
        "CHAT_MODEL_OPENAI": Config.CHAT_MODEL_OPENAI,
        "SUPPORTED_MODELS": Config.SUPPORTED_MODELS})


# ── A retired model's replacement (replaced_by, 2026-11-24) ──────────────

def test_the_shipped_replacements_pass_and_stay_within_openai():
    from backend import validate_model_successors
    from backend.config import Config
    supported = Config.SUPPORTED_MODELS
    validate_model_successors(supported)
    named = {k: v["replaced_by"] for k, v in supported.items()
             if v.get("replaced_by")}
    assert named == {"gpt-6-astra": "gpt-6.2-astra",
                     "gpt-6-sol": "gpt-6.2-sol"}
    for retired, successor in named.items():
        assert supported[retired]["deprecated"] is True
        assert supported[successor]["provider"] == "openai"
    # Luna 6 and Luna 5.6 are not retired (Peter, 2026-11-24).
    for kept in ("gpt-6-luna", "gpt-5.6-luna"):
        assert not supported[kept].get("deprecated"), kept


@pytest.mark.parametrize("successor, why", [
    ("claude-opus-5.5", "is not an active openai model"),
    ("gpt-5.5", "is not an active openai model"),
    ("gpt-6.3", "is not an active openai model"),
    ("gpt-6.1-sol", "is read only"),
    ("gpt-6.2-astra", "is not flagged 'read'"),
])
def test_a_replacement_that_does_not_fit_refuses_to_boot(successor, why):
    from backend import validate_model_successors
    models = {
        "claude-opus-5.5": {"provider": "anthropic"},
        "gpt-5.5": {"provider": "openai", "deprecated": True},
        "gpt-6.1-sol": {"provider": "openai", "read": True, "chat": False},
        "gpt-6.2-astra": {"provider": "openai"},
        "gpt-6.2-sol": {"provider": "openai", "read": True},
        "gpt-6-sol": {"provider": "openai", "read": True, "deprecated": True,
                      "replaced_by": successor},
    }
    with pytest.raises(RuntimeError) as e:
        validate_model_successors(models)
    assert why in str(e.value)
    # Through the boot check too.
    with pytest.raises(RuntimeError):
        validate_default_model({"DEFAULT_LLM_MODEL": "claude-opus-5.5",
                                "SUPPORTED_MODELS": models})
    models["gpt-6-sol"]["replaced_by"] = "gpt-6.2-sol"
    validate_model_successors(models)


def test_a_retired_replacement_is_followed_to_its_own():
    """When GPT-6.2 Sol is retired in turn, GPT-6 Sol's entry need not
    change: its replacement's replacement counts. A loop never ends on an
    active model and refuses to boot."""
    from backend import validate_model_successors
    models = {
        "gpt-6-sol": {"provider": "openai", "read": True, "deprecated": True,
                      "replaced_by": "gpt-6.2-sol"},
        "gpt-6.2-sol": {"provider": "openai", "read": True,
                        "deprecated": True, "replaced_by": "gpt-6.4-sol"},
        "gpt-6.4-sol": {"provider": "openai", "read": True},
    }
    validate_model_successors(models)
    models["gpt-6.4-sol"]["deprecated"] = True
    models["gpt-6.4-sol"]["replaced_by"] = "gpt-6-sol"
    with pytest.raises(RuntimeError) as e:
        validate_model_successors(models)
    assert "is not an active openai model" in str(e.value)


# ── A server setting that names a retired model (#793 review) ────────────

def _shipped(**settings):
    from backend.config import Config
    config = {
        "DEFAULT_LLM_MODEL": "claude-opus-5.5",
        "SUPPORTED_MODELS": Config.SUPPORTED_MODELS,
        "READ_DEFAULT_MODEL": Config.READ_DEFAULT_MODEL,
        "CHAT_MODEL_ANTHROPIC": Config.CHAT_MODEL_ANTHROPIC,
        "CHAT_MODEL_OPENAI": Config.CHAT_MODEL_OPENAI,
        "GLEAN_MODEL_ANTHROPIC": Config.GLEAN_MODEL_ANTHROPIC,
        "GLEAN_MODEL_OPENAI": Config.GLEAN_MODEL_OPENAI,
        "PROFILE_VERSION_MODEL_ANTHROPIC": Config.PROFILE_VERSION_MODEL_ANTHROPIC,
        "PROFILE_VERSION_MODEL_OPENAI": Config.PROFILE_VERSION_MODEL_OPENAI,
        "PROFILE_MERGE_MODEL_ANTHROPIC": Config.PROFILE_MERGE_MODEL_ANTHROPIC,
        "PROFILE_MERGE_MODEL_OPENAI": Config.PROFILE_MERGE_MODEL_OPENAI,
        "PROFILE_VERSION_MODEL_CROSS_PROVIDER":
            Config.PROFILE_VERSION_MODEL_CROSS_PROVIDER,
        "PROFILE_MERGE_MODEL_CROSS_PROVIDER":
            Config.PROFILE_MERGE_MODEL_CROSS_PROVIDER,
    }
    config.update(settings)
    return config


@pytest.mark.parametrize("key, variable, retired, replacement", [
    ("DEFAULT_LLM_MODEL", "LLM_NAME", "gpt-6-astra", "gpt-6.2-astra"),
    ("DEFAULT_LLM_MODEL", "LLM_NAME", "gpt-6-sol", "gpt-6.2-sol"),
    ("CHAT_MODEL_OPENAI", "CHAT_MODEL_OPENAI", "gpt-6-astra", "gpt-6.2-astra"),
    ("CHAT_MODEL_OPENAI", "CHAT_MODEL_OPENAI", "gpt-6-sol", "gpt-6.2-sol"),
    ("GLEAN_MODEL_OPENAI", "GLEAN_MODEL_OPENAI", "gpt-6-sol", "gpt-6.2-sol"),
    # Glean's settings may name another provider's model.
    ("GLEAN_MODEL_ANTHROPIC", "GLEAN_MODEL_ANTHROPIC", "gpt-6-sol",
     "gpt-6.2-sol"),
    ("PROFILE_VERSION_MODEL_OPENAI", "PROFILE_VERSION_MODEL_OPENAI",
     "gpt-6-sol", "gpt-6.2-sol"),
    ("PROFILE_MERGE_MODEL_OPENAI", "PROFILE_MERGE_MODEL_OPENAI",
     "gpt-6-sol", "gpt-6.2-sol"),
    ("PROFILE_VERSION_MODEL_CROSS_PROVIDER",
     "PROFILE_VERSION_MODEL_CROSS_PROVIDER", "gpt-6-sol", "gpt-6.2-sol"),
    ("PROFILE_MERGE_MODEL_CROSS_PROVIDER",
     "PROFILE_MERGE_MODEL_CROSS_PROVIDER", "gpt-6-astra", "gpt-6.2-astra"),
])
def test_a_retired_setting_boots_on_its_replacement_with_a_warning(
        key, variable, retired, replacement):
    warned = []
    config = _shipped(**{key: retired})
    validate_default_model(config, warn=warned.append)
    assert config[key] == replacement
    assert warned == [
        f"{variable}={retired!r} names a retired model; running on its "
        f"replacement {replacement!r}. Set {variable}={replacement} in the "
        f"environment to silence this warning."]


@pytest.mark.parametrize("key, value, why", [
    # Another provider's model for a provider's own setting.
    ("CHAT_MODEL_ANTHROPIC", "gpt-6-astra", "is not an active anthropic"),
    ("PROFILE_VERSION_MODEL_ANTHROPIC", "gpt-6-sol",
     "is not an active anthropic model"),
    # The replacement can't do what the setting needs: GPT-6 Astra was
    # never a Read model, so neither is GPT-6.2 Astra.
    ("GLEAN_MODEL_OPENAI", "gpt-6-astra", "is not an active model flagged"),
    # A retired model without a replacement.
    ("CHAT_MODEL_OPENAI", "gpt-5.5", "is not an active openai"),
    ("DEFAULT_LLM_MODEL", "gpt-5.5", "is deprecated"),
    # A typo.
    ("DEFAULT_LLM_MODEL", "gpt-6-2-astra", "did you mean 'gpt-6.2-astra'"),
    ("CHAT_MODEL_OPENAI", "gpt-6.3-astra", "is not an active openai"),
])
def test_a_setting_without_a_fitting_replacement_still_refuses_to_boot(
        key, value, why):
    warned = []
    config = _shipped(**{key: value})
    with pytest.raises(RuntimeError) as e:
        validate_default_model(config, warn=warned.append)
    assert why in str(e.value)
    assert repr(value) in str(e.value)
    assert warned == [] and config[key] == value


def test_the_shipped_settings_need_no_replacement():
    warned = []
    config = _shipped()
    before = dict(config)
    validate_default_model(config, warn=warned.append)
    assert warned == [] and config == before


def test_a_replacement_on_an_active_model_refuses_to_boot():
    from backend import validate_model_successors
    with pytest.raises(RuntimeError) as e:
        validate_model_successors({
            "gpt-6-sol": {"provider": "openai", "replaced_by": "gpt-6.2-sol"},
            "gpt-6.2-sol": {"provider": "openai"}})
    assert "is not deprecated" in str(e.value)

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

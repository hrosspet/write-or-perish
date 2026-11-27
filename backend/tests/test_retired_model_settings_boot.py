"""A server setting that names a retired model still boots (#793 review).

GPT-6 Astra and GPT-6 Sol were retired on 2026-11-24, each naming its
replacement (``replaced_by``). A server whose env file still names one in
LLM_NAME, CHAT_MODEL_*, GLEAN_MODEL_* or PROFILE_*_MODEL_* must keep
serving: a graceful reload whose new workers refuse to boot takes the
site down, and the Celery workers build the same app. The app factory
moves such a setting to the replacement and logs one warning line naming
the variable, the retired id and the replacement. An unknown id, a model
of another provider and a retired model without a replacement are still
refused. These tests build the real app (sqlite in memory, the celery
glue stubbed, as test_payments_gate.py does)."""
import logging
import os
import sys
from unittest.mock import MagicMock

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

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login  # noqa: E402
import backend.models as _real_backend_models  # noqa: E402


def _affected(k):
    return (k == "flask_login" or k.startswith("backend.routes")
            or k == "backend.models")


@pytest.fixture
def boot(monkeypatch):
    """boot(**settings) builds the real app with the given Config values."""
    stubs = {k: v for k, v in sys.modules.items()
             if k.startswith("backend.") and isinstance(v, MagicMock)}
    for k in stubs:
        del sys.modules[k]
    saved = {k: sys.modules[k] for k in list(sys.modules) if _affected(k)}
    sys.modules["flask_login"] = _real_flask_login
    sys.modules["backend.models"] = _real_backend_models

    def _boot(**settings):
        for _k in [k for k in list(sys.modules)
                   if k.startswith("backend.routes")]:
            del sys.modules[_k]
        import backend.config as _config
        monkeypatch.setattr(_config.Config, "SQLALCHEMY_DATABASE_URI",
                            "sqlite:///:memory:")
        for key, value in settings.items():
            monkeypatch.setattr(_config.Config, key, value)
        import backend
        backend._BOOT_WARNINGS_WRITTEN.clear()
        return backend.create_app()

    yield _boot
    for k in [k for k in list(sys.modules) if _affected(k)]:
        if k not in saved:
            del sys.modules[k]
    for k, mod in saved.items():
        sys.modules[k] = mod
    sys.modules.update(stubs)


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records
            if r.levelno == logging.WARNING and "retired model" in r.getMessage()]


def test_the_app_boots_on_the_replacements_of_retired_settings(boot, caplog):
    caplog.set_level(logging.WARNING)
    app = boot(DEFAULT_LLM_MODEL="gpt-6-astra",
               CHAT_MODEL_OPENAI="gpt-6-sol",
               GLEAN_MODEL_OPENAI="gpt-6-sol",
               PROFILE_MERGE_MODEL_OPENAI="gpt-6-sol",
               PROFILE_MERGE_MODEL_CROSS_PROVIDER="gpt-6-astra")
    assert app.config["DEFAULT_LLM_MODEL"] == "gpt-6.2-astra"
    assert app.config["CHAT_MODEL_OPENAI"] == "gpt-6.2-sol"
    assert app.config["GLEAN_MODEL_OPENAI"] == "gpt-6.2-sol"
    assert app.config["PROFILE_MERGE_MODEL_OPENAI"] == "gpt-6.2-sol"
    assert app.config["PROFILE_MERGE_MODEL_CROSS_PROVIDER"] == "gpt-6.2-astra"
    # One line per setting, naming the variable, the retired id and the
    # replacement.
    assert sorted(_warnings(caplog)) == sorted([
        "LLM_NAME='gpt-6-astra' names a retired model; running on its "
        "replacement 'gpt-6.2-astra'. Set LLM_NAME=gpt-6.2-astra in the "
        "environment to silence this warning.",
        "CHAT_MODEL_OPENAI='gpt-6-sol' names a retired model; running on its "
        "replacement 'gpt-6.2-sol'. Set CHAT_MODEL_OPENAI=gpt-6.2-sol in the "
        "environment to silence this warning.",
        "GLEAN_MODEL_OPENAI='gpt-6-sol' names a retired model; running on its "
        "replacement 'gpt-6.2-sol'. Set GLEAN_MODEL_OPENAI=gpt-6.2-sol in the "
        "environment to silence this warning.",
        "PROFILE_MERGE_MODEL_OPENAI='gpt-6-sol' names a retired model; running "
        "on its replacement 'gpt-6.2-sol'. Set "
        "PROFILE_MERGE_MODEL_OPENAI=gpt-6.2-sol in the environment to silence "
        "this warning.",
        "PROFILE_MERGE_MODEL_CROSS_PROVIDER='gpt-6-astra' names a retired "
        "model; running on its replacement 'gpt-6.2-astra'. Set "
        "PROFILE_MERGE_MODEL_CROSS_PROVIDER=gpt-6.2-astra in the environment "
        "to silence this warning.",
    ])


def test_the_shipped_settings_boot_without_a_warning(boot, caplog):
    caplog.set_level(logging.WARNING)
    app = boot(DEFAULT_LLM_MODEL="claude-opus-5.5")
    assert app.config["CHAT_MODEL_OPENAI"] == "gpt-6.2-astra"
    assert _warnings(caplog) == []


@pytest.mark.parametrize("settings, message", [
    # Another provider's model for a provider's own setting.
    ({"CHAT_MODEL_ANTHROPIC": "gpt-6-astra"},
     "CHAT_MODEL_ANTHROPIC='gpt-6-astra' is not an active anthropic"),
    # A retired model without a replacement.
    ({"CHAT_MODEL_OPENAI": "gpt-5.5"},
     "CHAT_MODEL_OPENAI='gpt-5.5' is not an active openai"),
    # A typo.
    ({"DEFAULT_LLM_MODEL": "gpt-6-2-astra"},
     "did you mean 'gpt-6.2-astra'"),
])
def test_the_app_still_refuses_what_has_no_fitting_replacement(
        boot, caplog, settings, message):
    caplog.set_level(logging.WARNING)
    with pytest.raises(RuntimeError) as e:
        boot(**settings)
    assert message in str(e.value)
    assert _warnings(caplog) == []

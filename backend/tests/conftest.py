"""Pytest configuration for backend tests.

These are isolated unit tests that don't require a full Flask app or database.
They test the privacy utility functions in isolation.
"""
import os
import sys
import types

import pytest

from backend.config import Config
from backend.tests import network_guard

# Each test file handles its own mocking and imports
# to avoid triggering the full backend initialization

# No test connects outside this machine (#633, #656): installed before any
# test module is imported, so it covers collection too.
network_guard.install()

_PROVIDER_KEY_PREFIXES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                          "OPENAI_API_KEY")
# Variables that send a request somewhere else than the host it names:
# the SDKs' base URLs and the proxies the HTTP clients read.
_REROUTING_VARIABLES = ("ANTHROPIC_BASE_URL", "OPENAI_BASE_URL") + tuple(
    name for proxy in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY")
    for name in (proxy, proxy.lower()))


def neutralize_provider_environment():
    """No test holds a real provider key or reaches a host by a detour
    (#633).

    By the time this file runs, importing backend has loaded the
    developer's .env into the environment and into Config. A key that is
    set becomes empty, not unset, so that a later load_dotenv (a
    subprocess a test starts imports backend again) cannot fill it back
    in from .env; Config gets None, as in CI, where no key is set.

    The base URLs and proxies go, and NO_PROXY=* turns off a proxy the
    operating system's settings name: a request then goes to the host it
    names, which the guard sees and refuses, where a proxy on this
    machine would have carried it out unseen."""
    for name in list(os.environ):
        if name.startswith(_PROVIDER_KEY_PREFIXES):
            os.environ[name] = ""
    for name in list(vars(Config)):
        if name.startswith(_PROVIDER_KEY_PREFIXES):
            setattr(Config, name, None)
    for name in _REROUTING_VARIABLES:
        os.environ.pop(name, None)
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "*"


neutralize_provider_environment()


def align_package_attributes():
    """Point every backend package's attribute for a submodule at the
    module in sys.modules.

    Many test files import a module afresh (delete it from sys.modules,
    import it again) and restore sys.modules afterwards. The import also
    set the package's attribute to the fresh copy, which nothing restored.
    ``import backend.x as x`` and ``patch("backend.x.name")`` resolve
    through that attribute, so a later test patched the stale copy while
    the code under test, which imported from sys.modules, kept the real
    function: test_window_scope's token-count stub missed and the real
    count went to the provider (#633), and test_privacy_utils failed
    after such a file. Only a module is replaced by a module: an attribute
    or a sys.modules entry that a test set to a stand-in is left alone."""
    for name, module in list(sys.modules.items()):
        if not name.startswith("backend.") or not isinstance(
                module, types.ModuleType):
            continue
        parent_name, _, child = name.rpartition(".")
        parent = sys.modules.get(parent_name)
        if not isinstance(parent, types.ModuleType):
            continue
        current = parent.__dict__.get(child)
        if isinstance(current, types.ModuleType) and current is not module:
            setattr(parent, child, module)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    """Before any fixture of the test is set up (module-scoped ones too)."""
    align_package_attributes()


# Refusals made outside a test's call (a fixture, an import, a thread that
# outlived its test), reported when the session ends.
_refused_outside_tests = []


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    """A test that tried to connect outside fails, also when the code under
    test caught the refusal (network_guard)."""
    _refused_outside_tests.extend(network_guard.take())
    try:
        result = yield
    finally:
        refused = network_guard.take()
    if refused:
        message = ("This test tried to connect outside this machine: "
                   + ", ".join(refused) + ". Stub the call "
                   "(backend/tests/network_guard.py).")
        if any(network_guard.FROM_THREAD in where for where in refused):
            message += (" A connection from another thread can come from a"
                        " thread an earlier test left running.")
        pytest.fail(message, pytrace=False)
    return result


def pytest_sessionfinish(session, exitstatus):
    refused = _refused_outside_tests + network_guard.take()
    if not refused:
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(
            "Outside connections refused outside a test's call (a fixture, "
            "an import or a leftover thread): " + ", ".join(refused),
            red=True)
    session.exitstatus = pytest.ExitCode.TESTS_FAILED


# Every module-level copy of the audio storage root. The cleanup, the
# audio sweep and the reference delete route delete files under it, so no
# test may ever see a real one (a checkout's data/audio, or a developer's
# AUDIO_STORAGE_PATH pointing at the local stack's audio).
_AUDIO_ROOT_ATTRS = (
    ("backend.utils.audio_storage", "AUDIO_STORAGE_ROOT"),
    ("backend.tasks.tts", "AUDIO_STORAGE_ROOT"),
    ("backend.routes.media", "MEDIA_ROOT"),
    ("backend.routes.external", "AUDIO_STORAGE_ROOT"),
    ("backend.routes.drafts", "AUDIO_STORAGE_ROOT"),
    ("backend.routes.nodes", "AUDIO_STORAGE_ROOT"),
    ("backend.routes.profile", "AUDIO_STORAGE_ROOT"),
)


@pytest.fixture(autouse=True)
def _no_provider_cancel_from_a_content_deletion(monkeypatch):
    """"Delete all my content" sends a Celery task that asks the model
    providers to cancel the user's old batches (#590). No test may send it
    for real: a developer's shell can hold a real provider key. A test
    that wants the cancel (test_user_purge's ``stubs``) runs it inline
    against its own stub."""
    module = sys.modules.get("backend.utils.user_purge")
    if isinstance(module, types.ModuleType) and hasattr(
            module, "_dispatch_batch_cancel"):
        monkeypatch.setattr(module, "_dispatch_batch_cancel",
                            lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _no_real_stripe(monkeypatch):
    """No test reaches Stripe (#438): a developer's .env holds a sandbox
    key that load_dotenv puts into the config. Tests stub
    stripe_billing.get_gateway; anything that still builds a real client
    fails loudly here."""
    try:
        import stripe
    except ImportError:  # the package is not installed: nothing to guard
        return

    def _refuse(*_a, **_k):
        raise RuntimeError("A test tried to call Stripe; stub "
                           "backend.utils.stripe_billing.get_gateway.")
    monkeypatch.setattr(stripe, "StripeClient", _refuse)


@pytest.fixture(autouse=True)
def _scratch_audio_root(tmp_path_factory, monkeypatch):
    """Point the audio storage root at a new folder of the test's own
    (outside its tmp_path, which some tests list), for every test: the
    env var (read by modules imported later and by code that reads it per
    call) and the module-level copies already imported. A module a test
    replaced with a mock is left alone. A test that sets its own root
    afterwards (a fixture or monkeypatch of its own) wins."""
    root = tmp_path_factory.mktemp("scratch-audio").resolve()
    monkeypatch.setenv("AUDIO_STORAGE_PATH", str(root))
    for module_name, attr in _AUDIO_ROOT_ATTRS:
        module = sys.modules.get(module_name)
        if not isinstance(module, types.ModuleType):
            continue
        current = module.__dict__.get(attr)
        if current is None:
            continue
        monkeypatch.setattr(module, attr,
                            str(root) if isinstance(current, str) else root)
    return root

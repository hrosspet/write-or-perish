"""Pytest configuration for backend tests.

These are isolated unit tests that don't require a full Flask app or database.
They test the privacy utility functions in isolation.
"""
import sys
import types

import pytest

# Each test file handles its own mocking and imports
# to avoid triggering the full backend initialization

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

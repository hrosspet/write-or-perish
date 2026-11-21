"""tiktoken's o200k_base for a Glean's count (#717).

A Glean's list is fitted with o200k counts made on the server
(tokens.local_token_counter). The encoding is loaded once per process, at
its first use, from a persistent cache folder (TIKTOKEN_CACHE_DIR, else
<data>/tiktoken), within O200K_LOAD_TIMEOUT_SECONDS: tiktoken downloads the
file on a machine's first use, with no timeout of its own. When it cannot
be loaded, the process fits by the estimate, says so in one log line, and
does not try again on every Glean.

conftest.py gives every other test a stand-in encoding; these tests clear
it.
"""
import hashlib
import importlib.util
import logging
import os
import sys
import tempfile
import threading
import types

import pytest

from backend.tests.test_retrieval_loop import app  # noqa: F401 (fixture)
from backend.utils import tokens

MODEL = "o200k-test"
O200K_URL = ("https://openaipublic.blob.core.windows.net/encodings/"
             "o200k_base.tiktoken")


@pytest.fixture
def fresh(app, monkeypatch):  # noqa: F811
    """An o200k model and a process with no encoding loaded yet."""
    app.config["SUPPORTED_MODELS"][MODEL] = {
        "provider": "openai", "tokenizer_family": "o200k"}
    monkeypatch.setattr(tokens, "_O200K", {})
    yield app
    app.config["SUPPORTED_MODELS"].pop(MODEL, None)


def _warnings(caplog):
    return [r for r in caplog.records
            if r.name == tokens.__name__ and r.levelno == logging.WARNING]


def test_a_load_that_fails_estimates_for_the_process_and_says_so_once(
        fresh, monkeypatch, caplog):
    calls = []

    def _load(timeout):
        calls.append(timeout)
        tokens._O200K["error"] = "ConnectionError"
        return None
    monkeypatch.setattr(tokens, "_load_o200k", _load)

    with caplog.at_level(logging.WARNING, logger=tokens.__name__):
        assert tokens.local_token_counter(MODEL) is None
        assert tokens.local_token_counter(MODEL) is None
        assert tokens.local_token_counter(MODEL) is None

    assert calls == [tokens.O200K_LOAD_TIMEOUT_SECONDS]
    warned = _warnings(caplog)
    assert len(warned) == 1
    assert "ConnectionError" in warned[0].getMessage()


def test_a_load_that_hangs_is_cut_at_the_timeout_into_the_cache_folder(
        fresh, monkeypatch, tmp_path, caplog):
    """The load waits at most the timeout, in the persistent folder, and
    a later Glean in the process does not start another."""
    release = threading.Event()
    started = []
    fake = types.ModuleType("tiktoken")

    def get_encoding(name):
        started.append(os.environ.get("TIKTOKEN_CACHE_DIR"))
        release.wait(5)
        raise RuntimeError("too late")
    fake.get_encoding = get_encoding
    monkeypatch.setitem(sys.modules, "tiktoken", fake)
    folder = tmp_path / "tiktoken"
    monkeypatch.setitem(fresh.config, "TIKTOKEN_CACHE_DIR", str(folder))
    monkeypatch.delenv("TIKTOKEN_CACHE_DIR", raising=False)
    monkeypatch.setattr(tokens, "O200K_LOAD_TIMEOUT_SECONDS", 0.05)
    try:
        with caplog.at_level(logging.WARNING, logger=tokens.__name__):
            assert tokens.local_token_counter(MODEL) is None
            assert tokens.local_token_counter(MODEL) is None
        assert started == [str(folder)]
        assert folder.is_dir()
        assert len(_warnings(caplog)) == 1
        assert "not loaded within 0.05 s" in _warnings(caplog)[0].getMessage()
    finally:
        release.set()
        os.environ.pop("TIKTOKEN_CACHE_DIR", None)


def test_the_cache_folder_is_under_the_data_folder_by_default(fresh):
    from backend.utils.twitter_archive import STASH_ROOT
    fresh.config.pop("TIKTOKEN_CACHE_DIR", None)
    assert tokens.tiktoken_cache_dir() == STASH_ROOT.parent / "tiktoken"
    fresh.config["TIKTOKEN_CACHE_DIR"] = "/srv/cache/tk"
    try:
        assert str(tokens.tiktoken_cache_dir()) == "/srv/cache/tk"
    finally:
        fresh.config.pop("TIKTOKEN_CACHE_DIR", None)


def test_a_model_of_another_tokenizer_has_no_local_count(fresh):
    fresh.config["SUPPORTED_MODELS"]["claude-test"] = {
        "provider": "anthropic", "tokenizer_family": "claude_new"}
    try:
        assert tokens.local_token_counter("claude-test") is None
        assert tokens._O200K == {}
    finally:
        fresh.config["SUPPORTED_MODELS"].pop("claude-test", None)


def _cached_o200k_folder():
    """A folder where tiktoken already keeps o200k_base on this machine
    (tiktoken's own lookup order), or None."""
    name = hashlib.sha1(O200K_URL.encode()).hexdigest()
    for folder in (os.environ.get("TIKTOKEN_CACHE_DIR"),
                   os.environ.get("DATA_GYM_CACHE_DIR"),
                   os.path.join(tempfile.gettempdir(), "data-gym-cache")):
        if folder and os.path.isfile(os.path.join(folder, name)):
            return folder
    return None


def test_the_real_encoder_counts_from_its_cache_folder(fresh, monkeypatch):
    """The production branch, with tiktoken itself, when this machine has
    the file already (no download: the network guard would refuse it)."""
    folder = _cached_o200k_folder()
    if importlib.util.find_spec("tiktoken") is None or folder is None:
        pytest.skip("o200k_base is not cached on this machine")
    monkeypatch.setitem(fresh.config, "TIKTOKEN_CACHE_DIR", folder)
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", folder)

    count = tokens.local_token_counter(MODEL)

    assert count is not None
    assert count("hello world") == 2
    assert count("") == 0
    assert tokens._O200K.get("failed") is None

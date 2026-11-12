"""No test connects outside this machine, and no test holds a real provider
key (#633, #656): backend/tests/network_guard.py and conftest.py.

Every outside host here is refused before any packet is sent; the names
are never looked up.
"""
import os
import socket
import threading

import anthropic
import openai
import pytest

from backend.config import Config
from backend.tests import conftest, network_guard
from backend.tests.network_guard import OutsideConnectionRefused


@pytest.fixture(autouse=True)
def nothing_left_over():
    """Each test starts with no refusal recorded and takes the ones it
    makes itself, so that the conftest hook does not fail it for them."""
    assert network_guard.take() == []
    yield


def test_a_provider_host_is_refused_by_name():
    with pytest.raises(OutsideConnectionRefused,
                       match=r"api\.anthropic\.com:443"):
        socket.create_connection(("api.anthropic.com", 443), timeout=1)
    assert network_guard.take() == ["api.anthropic.com:443"]


def test_an_outside_address_is_refused_at_connect():
    for connect in ("connect", "connect_ex"):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            with pytest.raises(OutsideConnectionRefused,
                               match=r"203\.0\.113\.5:443"):
                getattr(sock, connect)(("203.0.113.5", 443))
    assert network_guard.take() == ["203.0.113.5:443"] * 2


def test_name_lookups_of_an_outside_host_are_refused():
    with pytest.raises(OutsideConnectionRefused):
        socket.gethostbyname("api.openai.com")
    with pytest.raises(OutsideConnectionRefused):
        socket.gethostbyname_ex("api.openai.com")
    assert network_guard.take() == ["api.openai.com"] * 2


@pytest.mark.parametrize("sdk", ["anthropic", "openai"])
def test_the_sdks_neither_retry_nor_hide_the_refusal(sdk):
    """The refusal is a BaseException: the SDK's retry loop (which catches
    Exception) does not send the request again or turn it into a
    connection error that the code's fallback would swallow."""
    if sdk == "anthropic":
        client = anthropic.Anthropic(api_key="k", max_retries=2)
        call = lambda: client.messages.count_tokens(  # noqa: E731
            model="claude-x", messages=[{"role": "user", "content": "hi"}])
        host = "api.anthropic.com:443"
    else:
        client = openai.OpenAI(api_key="k", max_retries=2)
        call = lambda: client.models.list()  # noqa: E731
        host = "api.openai.com:443"
    with client:
        with pytest.raises(OutsideConnectionRefused):
            call()
    assert network_guard.take() == [host]


def test_this_machine_is_allowed():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(2)
        port = server.getsockname()[1]
        for host in ("127.0.0.1", "localhost"):
            socket.create_connection((host, port), timeout=1).close()
    # A Unix socket is never an outside host: the missing file is the
    # operating system's answer, not the guard's.
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        with pytest.raises(OSError):
            sock.connect("/nonexistent-loore-network-guard.sock")
    assert network_guard.take() == []


def test_the_host_of_a_database_or_broker_url_is_allowed(monkeypatch):
    assert not network_guard.is_local("db.example.test")
    monkeypatch.setenv("LOORE_TEST_POSTGRES_URL",
                       "postgresql://test@db.example.test:5432/loore")
    monkeypatch.setenv("CELERY_BROKER_URL", "redis://queue.example.test:6379")
    assert network_guard.is_local("db.example.test")
    assert network_guard.is_local("queue.example.test")
    # Only database and broker URLs: a provider URL is not a service.
    monkeypatch.setenv("SOME_API_URL", "https://api.example.test/v1")
    assert not network_guard.is_local("api.example.test")


def _run_hook(body):
    """The conftest's pytest_runtest_call around ``body`` (the test)."""
    hook = conftest.pytest_runtest_call(item=None)
    next(hook)
    body()
    try:
        hook.send(None)
    except StopIteration:
        return


def test_a_refusal_the_code_swallows_still_fails_the_test():
    def swallowing():
        try:
            socket.create_connection(("api.anthropic.com", 443))
        except BaseException:  # what a careless fallback does
            pass
    with pytest.raises(pytest.fail.Exception,
                       match=r"api\.anthropic\.com:443"):
        _run_hook(swallowing)
    assert network_guard.take() == []


def test_a_refusal_in_a_thread_fails_the_test():
    def in_a_thread():
        def connect():
            try:
                socket.create_connection(("api.openai.com", 443))
            except OutsideConnectionRefused:
                pass
        thread = threading.Thread(target=connect)
        thread.start()
        thread.join()
    with pytest.raises(pytest.fail.Exception, match=r"api\.openai\.com:443"):
        _run_hook(in_a_thread)


def test_a_test_without_refusals_passes_the_hook():
    _run_hook(lambda: None)


def test_no_real_provider_key_reaches_a_test():
    """Keys loaded from a developer's .env are emptied (an empty variable
    is not filled again by load_dotenv) and Config has none, as in CI."""
    prefixes = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY")
    for name, value in os.environ.items():
        if name.startswith(prefixes):
            assert value == "", name
    for name, value in vars(Config).items():
        if name.startswith(prefixes):
            assert value is None, name
    assert "ANTHROPIC_BASE_URL" not in os.environ
    assert "OPENAI_BASE_URL" not in os.environ

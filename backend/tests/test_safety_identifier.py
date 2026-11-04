"""OpenAI safety identifiers (2026-11-04): a stable, keyed hash of the
Loore user id, never the id, a username or an email."""
import os

os.environ.setdefault("SECRET_KEY", "test-secret")

from flask import Flask  # noqa: E402

from backend.utils.safety_identifier import (  # noqa: E402
    PREFIX, SYSTEM_SAFETY_IDENTIFIER, openai_safety_identifier,
)


def _app(secret):
    app = Flask(__name__)
    app.config["SECRET_KEY"] = secret
    return app


def test_stable_prefixed_and_within_openais_limit():
    with _app("s1").app_context():
        a = openai_safety_identifier(7)
        assert a == openai_safety_identifier(7)
        assert a.startswith(PREFIX) and len(a) == len(PREFIX) + 32
        assert len(a) <= 64


def test_differs_per_user_and_does_not_contain_the_id():
    with _app("s1").app_context():
        ids = {openai_safety_identifier(n) for n in range(1, 200)}
        assert len(ids) == 199
        assert "12345" not in openai_safety_identifier(12345)


def test_keyed_with_the_secret():
    with _app("s1").app_context():
        a = openai_safety_identifier(7)
    with _app("s2").app_context():
        b = openai_safety_identifier(7)
    assert a != b


def test_outside_an_app_context_uses_the_environment(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "s1")
    outside = openai_safety_identifier(7)
    with _app("s1").app_context():
        assert openai_safety_identifier(7) == outside


def test_no_user_is_the_system_identifier():
    assert openai_safety_identifier(None) == SYSTEM_SAFETY_IDENTIFIER

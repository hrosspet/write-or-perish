"""Sentry never receives user content (Peter: nobody decrypts users'
content). send_default_pii=False alone still lets the SDK attach stack-frame
local variables and request bodies, which can hold decrypted text."""
import json

import pytest
import sentry_sdk
from flask import Flask
from sentry_sdk.integrations._wsgi_common import request_body_within_bounds

from backend import SENTRY_PRIVACY_OPTIONS, _drop_query_strings, create_app


def _capture(**options):
    """One handled exception inside a function whose local holds 'user
    content'; returns the event and the client it was captured with."""
    events = []
    sentry_sdk.init(dsn="https://key@example.invalid/1",
                    transport=lambda envelope: None,
                    before_send=lambda event, hint: events.append(event),
                    **options)

    def handler():
        entry_text = "-".join(["private", "journal", "text"])  # noqa: F841
        try:
            raise ValueError("boom")
        except ValueError:
            sentry_sdk.capture_exception()

    handler()
    return events[0], sentry_sdk.get_client()


def _frame_vars(event):
    frames = event["exception"]["values"][0]["stacktrace"]["frames"]
    return [f.get("vars") for f in frames if f.get("vars")]


def test_options_drop_locals_and_request_bodies():
    assert SENTRY_PRIVACY_OPTIONS["send_default_pii"] is False
    assert SENTRY_PRIVACY_OPTIONS["include_local_variables"] is False
    assert SENTRY_PRIVACY_OPTIONS["max_request_body_size"] == "never"


def test_without_the_options_locals_reach_the_event():
    """The control: send_default_pii=False alone leaks the local."""
    try:
        event, _ = _capture(send_default_pii=False)
        assert any("private-journal-text" in str(v)
                   for v in _frame_vars(event))
    finally:
        sentry_sdk.init(dsn=None)


def test_with_the_options_no_locals_and_no_request_body():
    try:
        event, client = _capture(**SENTRY_PRIVACY_OPTIONS)
        assert _frame_vars(event) == []
        assert not request_body_within_bounds(client, 10)
    finally:
        sentry_sdk.init(dsn=None)


class _StopAfterSentryInit(Exception):
    pass


def _app_before_send(monkeypatch):
    """The before_send create_app() hands to Sentry. create_app stops right
    after sentry_sdk.init, so no app or database is built."""
    passed = {}

    def fake_init(**kwargs):
        passed.update(kwargs)
        raise _StopAfterSentryInit

    monkeypatch.setenv("SENTRY_DSN", "https://key@example.invalid/1")
    monkeypatch.setattr(sentry_sdk, "init", fake_init)
    with pytest.raises(_StopAfterSentryInit):
        create_app()
    monkeypatch.undo()
    return passed["before_send"]


def test_query_strings_and_referer_queries_do_not_reach_sentry(monkeypatch):
    """Query strings can carry user input (search words). A failing request
    with one, called from a page whose URL has one, reaches Sentry with
    neither, and still with its route, Referer path and stack trace."""
    before_send = _app_before_send(monkeypatch)
    marker = "-".join(["typed", "search", "words"])
    events = []
    app = Flask(__name__)

    @app.route("/api/search")
    def search():
        raise RuntimeError("boom")

    try:
        sentry_sdk.init(dsn="https://key@example.invalid/1",
                        transport=lambda envelope: None,
                        before_send=lambda e, h: events.append(
                            before_send(e, h)),
                        **SENTRY_PRIVACY_OPTIONS)
        app.test_client().get(
            f"/api/search?q={marker}",
            headers={"Referer": f"https://loore.org/page?q={marker}"})
    finally:
        sentry_sdk.init(dsn=None)

    # The Flask hook reports the exception and so does Flask's own error
    # log line; every event must come through clean.
    assert events
    for event in events:
        assert marker not in json.dumps(event, default=str)
        assert "query_string" not in event["request"]
        assert event["request"]["url"].endswith("/api/search")
        assert event["request"]["headers"]["Referer"] == (
            "https://loore.org/page")
        frames = event["exception"]["values"][-1]["stacktrace"]["frames"]
        assert frames[-1]["function"] == "search" and frames[-1]["lineno"]


def test_a_query_in_the_event_url_is_dropped_too():
    event = {"request": {"url": "https://loore.org/api/search?q=x#y"}}
    assert _drop_query_strings(event)["request"]["url"] == (
        "https://loore.org/api/search")
    assert _drop_query_strings({}) == {}

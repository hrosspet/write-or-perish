"""Sentry never receives user content (Peter: nobody decrypts users'
content). send_default_pii=False alone still lets the SDK attach stack-frame
local variables and request bodies, which can hold decrypted text."""
import sentry_sdk
from sentry_sdk.integrations._wsgi_common import request_body_within_bounds

from backend import SENTRY_PRIVACY_OPTIONS


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

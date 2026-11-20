"""Email addresses stay out of error monitoring (#666).

The sign-in, welcome and admin alert mails logged their recipient's
address; logged at INFO a line becomes a Sentry breadcrumb, and a failure
(logger.exception) an ERROR event whose message held the address. The
lines now name the recipient by user id or a keyed hash, and the Sentry
scrubber masks anything shaped like an address as a second line of
defence.
"""
import logging
import os
import sys
import time
from unittest.mock import MagicMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
sys.modules.setdefault("celery", MagicMock())
sys.modules.setdefault("celery.utils", MagicMock())
sys.modules.setdefault("celery.utils.log", MagicMock())
sys.modules.setdefault("celery.result", MagicMock())

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

from backend.utils import error_privacy  # noqa: E402
from backend.utils.error_privacy import EMAIL_REMOVED  # noqa: E402

ADDRESS = "jana.novak+loore@example.co.uk"


# ── The scrubber ─────────────────────────────────────────────────────────

class TestMaskEmails:
    @pytest.mark.parametrize("text", [
        f"Magic link email sent to {ADDRESS}",
        "to <a.b@mail.example.org>, cc x_y@sub.domain.io.",
        "Žofie.Černá@příklad.cz wrote",
    ])
    def test_addresses_are_masked(self, text):
        out = error_privacy.mask_emails(text)
        assert "@" not in out
        assert EMAIL_REMOVED in out

    @pytest.mark.parametrize("text", [
        "no address here",
        "@handle on X and @decorator",
        "user@localhost",
        "version 1.2.3",
    ])
    def test_other_text_is_kept(self, text):
        assert error_privacy.mask_emails(text) == text

    def test_not_a_string_is_returned_as_it_is(self):
        assert error_privacy.mask_emails(None) is None
        assert error_privacy.mask_emails(42) == 42

    def test_a_long_text_is_scanned_quickly(self):
        # Bounded repetitions: no backtracking blow-up on a near miss.
        text = ("a" * 60 + "@" + "b" * 60 + ".") * 2000
        start = time.monotonic()
        error_privacy.mask_emails(text)
        assert time.monotonic() - start < 2

    def test_nested_data_is_masked_keys_included(self):
        out = error_privacy.mask_emails_in(
            {"to": ADDRESS, ADDRESS: 1, "list": [ADDRESS, ("x", ADDRESS)],
             "n": 3})
        assert out == {"to": EMAIL_REMOVED, EMAIL_REMOVED: 1,
                       "list": [EMAIL_REMOVED, ["x", EMAIL_REMOVED]], "n": 3}


class TestSentryEvent:
    def _event(self):
        return {
            "logentry": {"message": f"Failed to send to {ADDRESS}",
                         "formatted": f"Failed to send to {ADDRESS}",
                         "params": [ADDRESS]},
            "message": f"sent to {ADDRESS}",
            "exception": {"values": [
                {"type": "SMTPRecipientsRefused", "value": ADDRESS}]},
            "breadcrumbs": {"values": [
                {"category": "backend.utils.email",
                 "message": f"Magic link email sent to {ADDRESS}",
                 "data": {"recipient": ADDRESS}}]},
            "extra": {"to": ADDRESS, "nested": {"cc": [ADDRESS]}},
            "contexts": {"mail": {"to": ADDRESS}},
        }

    def test_control_the_raw_event_holds_the_address(self):
        assert ADDRESS in str(self._event())

    def test_no_address_reaches_sentry(self):
        out = error_privacy.scrub_sentry_event(self._event(), {})
        assert "@" not in str(out)
        assert out["logentry"]["message"] == f"Failed to send to {EMAIL_REMOVED}"
        assert out["breadcrumbs"]["values"][0]["data"] == {
            "recipient": EMAIL_REMOVED}
        assert out["extra"]["nested"] == {"cc": [EMAIL_REMOVED]}
        assert out["exception"]["values"][0]["value"] == "[message removed]"

    def test_breadcrumb_hook_masks_message_and_data(self):
        crumb = error_privacy.scrub_sentry_breadcrumb(
            {"message": f"Welcome email sent to {ADDRESS}",
             "data": {"to": ADDRESS}}, None)
        assert crumb == {"message": f"Welcome email sent to {EMAIL_REMOVED}",
                         "data": {"to": EMAIL_REMOVED}}


# ── The mail log lines ───────────────────────────────────────────────────

@pytest.fixture
def app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret"
    with app.app_context():
        yield app


class _FailingSMTP:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        pass

    def login(self, *a):
        pass

    def sendmail(self, sender, to, msg):
        import smtplib
        raise smtplib.SMTPRecipientsRefused({to: (550, b"no such user")})


class _OkSMTP(_FailingSMTP):
    def sendmail(self, *a):
        pass


class TestMailLogLines:
    def test_sent_and_failed_lines_name_no_address(self, app, caplog):
        import backend.utils.email as m
        caplog.set_level(logging.INFO, logger="backend.utils.email")
        with patch("smtplib.SMTP", _OkSMTP):
            m.send_magic_link_email(ADDRESS, "https://x/verify", user_id=7)
            m.send_welcome_email(ADDRESS, "https://x/verify", user_id=7)
            m.send_spend_alert_email(ADDRESS, "anthropic", 1.0, 2.0, 0.5)
            m.send_disk_alert_email(ADDRESS, 91, 9.1, 10.0, 0.9)
            m.send_user_spend_block_email(ADDRESS, "alice", 1.0, 2.0)
        with patch("smtplib.SMTP", _FailingSMTP):
            with pytest.raises(Exception):
                m.send_magic_link_email(ADDRESS, "https://x/verify")
        messages = [r.getMessage() for r in caplog.records]
        assert all(ADDRESS not in msg for msg in messages), messages
        assert "Magic link email sent (user 7)" in messages
        assert "Welcome email sent (user 7)" in messages
        failed = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(failed) == 1
        assert failed[0].getMessage().startswith(
            "Failed to send magic link email (address ")

    def test_recipient_ref_is_keyed_and_ignores_case(self, app):
        import backend.utils.email as m
        ref = m.recipient_ref(ADDRESS)
        assert ref.startswith("address ") and len(ref) == len("address ") + 8
        assert ADDRESS not in ref
        assert m.recipient_ref(ADDRESS.upper()) == ref
        assert m.recipient_ref("other@example.com") != ref
        app.config["SECRET_KEY"] = "another-secret"
        assert m.recipient_ref(ADDRESS) != ref
        assert m.recipient_ref(ADDRESS, user_id=3) == "user 3"

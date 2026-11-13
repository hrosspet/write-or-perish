"""The trial's usage limit says what the code does (#438; Peter,
2026-11-13: "pls fix this in a followup").

The trial's ceiling (SUBSCRIPTION_TRIAL_SPEND_LIMIT_USD) is lifted by the
first payment (stripe_billing.handle_event), not by the trial's end: a
trial that ends without a payment lifts nothing (the account is then not
let in, or lapsed). The 402 used to say "It lifts when the trial ends on
<date>" and send that date as ``lifts_on``; the clients built "It resets
on <date>" from it. Now every text says the first payment, no date is
sent, and ``lifts_with_payment`` tells the clients which cap it is. An
alpha tester's free month (#661) already said "It lifts with your first
payment." and gets the same key.

Same minimal app and stub gateway as test_payments.py; the Voice finalize
and the upload reply with their celery glue stubbed, as in
test_spend_cap_recording.py and test_textmode_upload_agentic.py.
"""
import json
import sys
from datetime import datetime, timedelta
from unittest.mock import MagicMock

from backend.extensions import db as _db
from backend.models import Node, User
from backend.tests.test_payments import (  # noqa: F401
    _client, _event, _post_event, _user, app, app_off, gateway,
    subscription)
from backend.tests.test_spend_cap_recording import (  # noqa: F401
    _recording, st)
from backend.tests.test_textmode_upload_agentic import tr  # noqa: F401
from backend.utils.spend import (
    FREE_MONTH_CAP_MESSAGE, TRIAL_CAP_MESSAGE, current_month,
    spend_cap_payload, spend_capped_fields, user_is_capped)
from backend.utils.voice_refusals import (
    SPEND_CAP_CODE, SPOKEN_REFUSALS, skipped_reply_code)


def _trial_capped(username="tina", **kw):
    """A trialing Standard subscriber at the trial's ceiling."""
    return _user(username, **{
        "stripe_customer_id": "cus_1", "stripe_subscription_id": "sub_1",
        "subscription_status": "trialing", "subscription_level": "standard",
        "subscription_trial_start": datetime.utcnow() - timedelta(days=3),
        "subscription_trial_end": datetime.utcnow() + timedelta(days=11),
        "subscription_trial_capped": True, **kw})


def _month_capped(username="mo"):
    return _user(username, subscription_status="active",
                 subscription_level="standard", subscription_ever_paid=True,
                 spend_blocked_month=current_month())


def _free_month_capped(username="tess"):
    u = _user(username, plan="free", subscription_trial_capped=True)
    u.alpha_free_granted_at = datetime.utcnow() - timedelta(days=3)
    u.alpha_free_start = datetime.utcnow() - timedelta(days=2)
    u.alpha_free_until = u.alpha_free_start + timedelta(days=30)
    _db.session.commit()
    return u


class TestTheMessage:
    def test_the_trial_lifts_with_the_first_payment(self, app):  # noqa: F811
        payload = spend_cap_payload(_trial_capped())
        assert payload == {
            "error": "monthly_spend_limit_reached",
            "message": ("You've reached the usage limit of your trial. It "
                        "lifts with your first payment."),
            "lifts_on": None,
            "lifts_with_payment": True,
        }
        assert payload["message"] == TRIAL_CAP_MESSAGE
        assert "trial ends" not in payload["message"]

    def test_the_free_month_already_said_so_and_gets_the_key(self, app):  # noqa: F811
        payload = spend_cap_payload(_free_month_capped())
        assert payload["message"] == FREE_MONTH_CAP_MESSAGE == (
            "You've reached the usage limit of your free month. It lifts "
            "with your first payment.")
        assert payload["lifts_on"] is None
        assert payload["lifts_with_payment"] is True

    def test_control_the_months_cap_keeps_its_text_and_has_no_key(self, app):  # noqa: F811
        payload = spend_cap_payload(_month_capped())
        assert payload["message"].startswith(
            "You've reached your monthly usage limit.")
        assert "lifts_with_payment" not in payload

    def test_the_text_names_no_way_to_pay(self, app):  # noqa: F811
        """The iPhone app shows the server's text: it names the state,
        never where or how to pay."""
        for text in (TRIAL_CAP_MESSAGE, FREE_MONTH_CAP_MESSAGE):
            for word in ("subscribe", "settings", "card", "web", "$",
                         "upgrade", "buy"):
                assert word not in text.lower(), (word, text)


class TestWhereTheClientsReadIt:
    def test_the_user_object(self, app):  # noqa: F811
        user = _client(app, _trial_capped()).get(
            "/api/dashboard/?profile=0").get_json()["user"]
        assert user["spend_blocked"] is True
        assert user["spend_cap_lifts_on"] is None
        assert user["spend_cap_lifts_with_payment"] is True

    def test_control_a_monthly_cap_on_the_user_object(self, app):  # noqa: F811
        user = _client(app, _month_capped()).get(
            "/api/dashboard/?profile=0").get_json()["user"]
        assert user["spend_cap_lifts_on"]
        assert "spend_cap_lifts_with_payment" not in user

    def test_control_payments_off_no_new_key(self, app_off):  # noqa: F811
        """#649's rule: with payments off the user object is as before."""
        u = _user("mo", spend_blocked_month=current_month())
        user = _client(app_off, u).get(
            "/api/dashboard/?profile=0").get_json()["user"]
        assert user["spend_blocked"] is True
        assert "spend_cap_lifts_with_payment" not in user

    def test_a_saved_entry_whose_reply_was_skipped(self, app):  # noqa: F811
        assert spend_capped_fields(_trial_capped()) == {
            "spend_capped": True, "spend_cap_lifts_on": None,
            "spend_cap_lifts_with_payment": True}
        assert "spend_cap_lifts_with_payment" not in spend_capped_fields(
            _month_capped())


class TestTheCodeItDescribes:
    def test_the_trials_end_lifts_nothing_the_first_payment_does(
            self, app, gateway):  # noqa: F811
        """Stripe makes the subscription active at the trial's end and
        charges about an hour later: still capped, and still the same
        text. The paid invoice lifts it."""
        u = _trial_capped()
        gateway.subscriptions["sub_1"] = subscription(status="active",
                                                      trial=True)
        _post_event(app, _event("customer.subscription.updated",
                                {"id": "sub_1"}, event_id="e1"))
        user = _db.session.get(User, u.id)
        assert user.subscription_status == "active"
        assert user_is_capped(user)
        assert spend_cap_payload(user)["message"] == TRIAL_CAP_MESSAGE
        _post_event(app, _event("invoice.paid", {
            "id": "in_1", "subscription": "sub_1", "customer": "cus_1",
            "amount_paid": 3570, "total": 3570}, event_id="e2"))
        _db.session.expire_all()
        assert not user_is_capped(_db.session.get(User, u.id))

    def test_a_trial_that_ends_unpaid_lets_nothing_through(
            self, app, gateway):  # noqa: F811
        """The first payment fails: the account is not let in, so no reply
        either way (#649)."""
        from backend.utils.subscriptions import access_state
        u = _trial_capped()
        gateway.subscriptions["sub_1"] = subscription(status="past_due",
                                                      trial=True)
        _post_event(app, _event("customer.subscription.updated",
                                {"id": "sub_1"}, event_id="e1"))
        assert access_state(_db.session.get(User, u.id)) == "none"


class TestSaidAloud:
    def test_the_spoken_sentence_is_unchanged(self):
        """Control: the sentence said aloud for any usage limit stays the
        month's (Peter, 2026-11-13, of it spoken for the trial's and the
        free month's ceilings: "this is fine, leave it"). Only the texts on
        the screen changed."""
        assert SPOKEN_REFUSALS[SPEND_CAP_CODE] == (
            "You've reached your monthly usage limit, so Loore can't reply "
            "until it resets.")


class TestRecordingsAndUploads:
    def _trial(self, user):
        user.spend_blocked_month = None
        user.subscription_status = "trialing"
        user.subscription_level = "standard"
        user.subscription_trial_start = datetime.utcnow() - timedelta(days=1)
        user.subscription_trial_capped = True
        _db.session.commit()

    def test_a_voice_recording_kept_without_its_reply(self, st):  # noqa: F811
        user = st._test_user
        self._trial(user)
        draft = _recording(user, "sess-t", [], st._test_root)
        saved_llm = sys.modules.get("backend.tasks.llm_completion")
        sys.modules["backend.tasks.llm_completion"] = MagicMock()
        try:
            st._start_server_side_llm_chain(
                draft, "sess-t", "the whole transcript", user.id,
                None, "gpt-5", "Voice")
        finally:
            if saved_llm is None:
                sys.modules.pop("backend.tasks.llm_completion", None)
            else:
                sys.modules["backend.tasks.llm_completion"] = saved_llm
        _db.session.refresh(draft)
        assert draft.llm_node_id is None
        assert draft.streaming_warning == (
            "Your recording is saved. Loore didn't reply because you've "
            "reached your usage limit, which lifts with your first payment.")
        assert draft.streaming_warning == \
            st.VOICE_REPLY_SKIPPED_SPEND_CAP_UNTIL_PAYMENT
        # Said aloud as the usage limit (the copy in voice_refusals).
        assert skipped_reply_code(draft.streaming_warning) == SPEND_CAP_CODE

    def test_control_a_monthly_cap_keeps_its_recording_text(
            self, st):  # noqa: F811
        assert st.voice_reply_skipped_spend_cap(st._test_user.id) == \
            st.VOICE_REPLY_SKIPPED_SPEND_CAP
        assert "resets at the start of next month" in \
            st.VOICE_REPLY_SKIPPED_SPEND_CAP

    def test_an_upload_kept_without_its_reply(self, tr):  # noqa: F811
        user, entry = tr._t["user"], tr._t["entry"]
        user.plan = "free"
        self._trial(user)
        assert tr._start_upload_reply(entry, entry, "gpt-5") is None
        warnings = _db.session.get(Node, entry.id).llm_task_warnings
        assert json.loads(warnings) == [
            "Your entry is saved. Loore didn't reply because you've reached "
            "your usage limit, which lifts with your first payment."]

"""The alpha free month (#438; Peter, 2026-11-12: alpha testers are "free
for one month after the opening, then the discounted price"; 2026-11-13:
the month starts "at the beta release date I'll decide later").

`flask alpha-free-month --start YYYY-MM-DD --apply` moves approved
accounts on the "alpha" plan to "free" with one free month for everyone,
from 00:00 UTC on the start date for ALPHA_FREE_MONTH_DAYS. Before it they
keep the alpha plan's access; during it they have everything without a
card and may subscribe at the discounted price without a code, the first
payment due when the month ends; after it, without a subscription, they
are lapsed. Until a first paid invoice their ceiling is Standard's
(Peter, 2026-11-13: "the standard limit").

Same minimal app and stub gateway as test_payments.py.
"""
from datetime import datetime, timedelta, timezone

import pytest

from backend.extensions import db as _db
from backend.models import User
from backend.tests import test_payments as tp
from backend.tests.test_payments import _client, _user
from backend.utils import stripe_billing
from backend.utils import subscriptions as subs


@pytest.fixture
def gateway(monkeypatch):
    gw = tp.FakeGateway()
    monkeypatch.setattr(stripe_billing, "get_gateway", lambda *a, **k: gw)
    stripe_billing._price_cache.clear()
    for level in ("standard", "supporter", "discounted"):
        gw.prices.append({
            "id": f"price_{level}", "active": True,
            "lookup_key": stripe_billing.PRICE_LOOKUP_KEYS[level]})
    return gw


def _app(payments, secret=tp.SECRET):
    app, ctx = tp._app_fixture(payments, secret=secret)
    yield app
    _db.session.remove()
    _db.drop_all()
    ctx.pop()


@pytest.fixture
def app(gateway):
    yield from _app(True)


@pytest.fixture
def app_off(gateway):
    yield from _app(False, secret=None)


def _days(n):
    return datetime.utcnow() + timedelta(days=n)


def _tester(username="tess", start=None, until=None, **kw):
    """An alpha tester whose month runs from *start* to *until* (by
    default: started two days ago, 28 days left)."""
    u = _user(username, plan="free", **kw)
    u.alpha_free_granted_at = datetime.utcnow() - timedelta(days=3)
    u.alpha_free_start = start or _days(-2)
    u.alpha_free_until = until or (u.alpha_free_start + timedelta(days=30))
    _db.session.commit()
    return u


def _upcoming(username="tess", **kw):
    return _tester(username, start=_days(5), until=_days(35), **kw)


def _ended(username="tess", **kw):
    return _tester(username, start=_days(-40), until=_days(-10), **kw)


def _ts(dt):
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


def _checkout_params(gateway):
    return [c for c in gateway.calls
            if c[0] == "create_checkout_session"][-1][1]


class TestAccess:
    def test_before_the_start_nothing_changes(self, app):
        """A start in the future: the tester keeps the alpha plan's access
        until then, and Checkout refuses them as it refuses alpha."""
        u = _upcoming()
        assert u.plan_set_by_hand and u.on_paid_plan and u.has_voice_mode
        assert not u.alpha_free_month_active
        assert subs.access_state(u) == "full"
        assert subs.ai_replies_allowed(u)

    def test_during_the_month_everything(self, app):
        u = _tester()
        assert not u.plan_set_by_hand
        assert u.alpha_free_month_active and u.on_paid_plan
        assert subs.access_state(u) == "active"
        assert subs.refuse_billed_call(u) is None

    def test_a_start_in_the_past_is_partly_used(self, app):
        u = _tester(start=_days(-25), until=_days(5))
        assert subs.access_state(u) == "active"

    def test_after_the_month_without_a_subscription_lapsed(self, app):
        u = _ended()
        assert not u.on_paid_plan
        # Never shut out: reading, writing and export stay.
        assert subs.access_state(u) == "lapsed"
        assert not subs.ai_replies_allowed(u)
        msg = subs.subscription_required_message(u)
        assert msg == ("Your free month has ended, so Loore doesn't reply. "
                       "Your writing is saved.")

    def test_a_tester_who_first_visits_after_the_end_gets_no_month(
            self, app):
        """The dates are the same for everyone: a late first visit starts
        nothing, and changes no date."""
        u = _ended()
        until = u.alpha_free_until
        r = _client(app, u).get("/api/billing")
        assert r.status_code == 200
        assert r.get_json()["subscription"]["state"] == "lapsed"
        assert _db.session.get(User, u.id).alpha_free_until == until

    def test_the_gate_lets_an_ended_tester_in(self, app):
        r = _client(app, _ended()).get("/api/billing")
        assert r.status_code == 200

    def test_a_free_account_without_the_month_is_not_let_in(self, app):
        """Control: what #649 does for an account with nothing."""
        u = _user("plain")
        assert subs.access_state(u) == "none"
        assert "free month" not in subs.subscription_required_message(u)

    def test_payments_off_changes_nothing(self, app_off):
        """Control: with the switch off every account has everything."""
        u = _ended()
        assert subs.access_state(u) == "full"
        assert subs.ai_replies_allowed(u)

    def test_sql_clauses_match_the_properties(self, app):
        running = _tester("run")
        coming = _upcoming("coming")
        ended = _ended("end")
        ids = {u.id for u in User.query.filter(
            User.paid_features_clause()).all()}
        assert running.id in ids and coming.id in ids
        assert ended.id not in ids
        cohort = {u.id for u in User.profile_eligible_query().all()}
        assert running.id in cohort and coming.id in cohort
        assert ended.id not in cohort


class TestState:
    def test_upcoming(self, app):
        u = _upcoming()
        sub = _client(app, u).get("/api/billing").get_json()["subscription"]
        assert sub["alpha_tester"] is True
        assert sub["free_month_upcoming"] is True
        assert sub["free_month_active"] is False
        assert sub["state"] == "set_by_hand"
        assert sub["free_month_start"].startswith(
            u.alpha_free_start.strftime("%Y-%m-%d"))
        assert sub["free_month_until"].startswith(
            u.alpha_free_until.strftime("%Y-%m-%d"))

    def test_running(self, app):
        sub = _client(app, _tester()).get(
            "/api/billing").get_json()["subscription"]
        assert sub["free_month_upcoming"] is False
        assert sub["free_month_active"] is True
        assert sub["state"] == "active"
        # The free month takes the trial's place.
        assert sub["trial_available"] is False


class TestCheckout:
    def test_the_discounted_level_is_offered_first(self, app):
        data = _client(app, _tester()).get("/api/billing").get_json()
        assert [lv["level"] for lv in data["levels"]] == [
            "discounted", "standard", "supporter"]
        assert data["levels"][0]["amount_cents"] == 1200

    def test_others_are_not_offered_it(self, app):
        """Control: #649's levels for an account that is not a tester."""
        data = _client(app, _user("plain")).get("/api/billing").get_json()
        assert [lv["level"] for lv in data["levels"]] == [
            "standard", "supporter"]
        assert data["subscription"]["alpha_tester"] is False

    def test_before_the_start_checkout_is_refused_as_for_alpha(
            self, app, gateway):
        r = _client(app, _upcoming()).post("/api/billing/checkout",
                                           json={"level": "discounted"})
        assert r.status_code == 409
        assert r.get_json()["error"] == "plan_set_by_hand"
        assert "create_checkout_session" not in gateway.names()

    def test_during_the_month_the_first_payment_is_at_its_end(
            self, app, gateway):
        u = _tester()
        r = _client(app, u).post("/api/billing/checkout",
                                 json={"level": "discounted"})
        assert r.status_code == 200, r.get_data(as_text=True)
        params = _checkout_params(gateway)
        assert params["line_items"][0]["price"] == "price_discounted"
        data = params["subscription_data"]
        assert "trial_period_days" not in data
        assert abs(data["trial_end"] - _ts(u.alpha_free_until)) <= 1
        assert data["metadata"]["loore_level"] == "discounted"
        assert "loore_code_id" not in data["metadata"]

    def test_a_tester_may_choose_another_level(self, app, gateway):
        u = _tester()
        r = _client(app, u).post("/api/billing/checkout",
                                 json={"level": "supporter"})
        assert r.status_code == 200
        params = _checkout_params(gateway)
        assert params["line_items"][0]["price"] == "price_supporter"
        assert abs(params["subscription_data"]["trial_end"]
                   - _ts(u.alpha_free_until)) <= 1

    def test_in_the_last_49_hours_only_stripes_minimum(self, app, gateway):
        """Ten hours left: the trial ends at Stripe's minimum (48 hours,
        one of margin), no later."""
        u = _tester(start=_days(-30) + timedelta(hours=10),
                    until=datetime.utcnow() + timedelta(hours=10))
        _client(app, u).post("/api/billing/checkout",
                             json={"level": "discounted"})
        trial_end = _checkout_params(gateway)["subscription_data"][
            "trial_end"]
        now = datetime.utcnow()
        assert _ts(now + timedelta(hours=48)) <= trial_end
        assert trial_end <= _ts(now + timedelta(hours=49)) + 2

    def test_after_the_month_it_is_charged_at_checkout(self, app, gateway):
        r = _client(app, _ended()).post("/api/billing/checkout",
                                        json={"level": "discounted"})
        assert r.status_code == 200
        data = _checkout_params(gateway)["subscription_data"]
        assert "trial_end" not in data
        assert "trial_period_days" not in data

    def test_no_second_trial_for_a_tester(self, app, gateway):
        u = _tester(subscription_trial_used=True,
                    subscription_status="ended",
                    subscription_level="discounted")
        _client(app, u).post("/api/billing/checkout",
                             json={"level": "discounted"})
        data = _checkout_params(gateway)["subscription_data"]
        assert "trial_end" not in data
        assert "trial_period_days" not in data

    def test_others_get_no_discount_without_a_code(self, app, gateway):
        """Control: #649 refuses the discounted level without a code."""
        r = _client(app, _user("plain")).post(
            "/api/billing/checkout", json={"level": "discounted"})
        assert r.status_code == 400
        assert "create_checkout_session" not in gateway.names()

    def test_a_trialing_subscription_from_the_month_is_kept(
            self, app, gateway):
        """Control: the subscription a tester starts during the month
        arrives in a trial; it is the account's first, so #649 keeps it."""
        _tester(stripe_customer_id="cus_t")
        gateway.subscriptions["sub_t"] = tp.subscription(
            "sub_t", customer="cus_t", status="trialing", trial=True,
            level="discounted")
        r = tp._post_event(app, tp._event("customer.subscription.created",
                                          {"id": "sub_t"}))
        assert r.status_code == 200
        assert "cancel_subscription" not in gateway.names()


class TestCeiling:
    """Until the first paid invoice: Standard's ceiling, whatever level
    (Peter, 2026-11-13: "the standard limit"). The tests give Standard,
    Supporter and the discounted level different ceilings."""

    @pytest.fixture(autouse=True)
    def _ceilings(self, app):
        app.config["SUBSCRIPTION_SPEND_LIMIT_USD_STANDARD"] = 55.0
        app.config["SUBSCRIPTION_SPEND_LIMIT_USD_SUPPORTER"] = 100.0
        app.config["SUBSCRIPTION_SPEND_LIMIT_USD_DISCOUNTED"] = 45.0

    def _limit(self, app, u):
        from backend.utils.spend import get_user_spend_limit_usd
        return get_user_spend_limit_usd(u, app.config)

    def test_the_month_without_a_subscription_has_standards(self, app):
        assert self._limit(app, _tester()) == 55.0

    def test_any_level_chosen_in_the_month_has_standards(self, app):
        u = _tester(subscription_status="trialing",
                    subscription_level="supporter",
                    subscription_trial_start=datetime.utcnow())
        assert self._limit(app, u) == 55.0
        u.subscription_level = "discounted"
        _db.session.commit()
        assert self._limit(app, u) == 55.0
        # The month over, the first charge not through yet: still 55.
        u.alpha_free_until = datetime.utcnow() - timedelta(minutes=5)
        u.subscription_status = "active"
        _db.session.commit()
        assert self._limit(app, u) == 55.0

    def test_the_levels_ceiling_from_the_first_paid_invoice(self, app):
        """Control: once paid, the chosen level's ceiling, as for any
        subscriber."""
        u = _ended(subscription_status="active",
                   subscription_level="supporter",
                   subscription_ever_paid=True)
        assert self._limit(app, u) == 100.0

    def test_the_first_paid_invoice_lifts_it_through_the_webhook(
            self, app, gateway):
        u = _tester(stripe_customer_id="cus_t")
        gateway.subscriptions["sub_t"] = tp.subscription(
            "sub_t", customer="cus_t", status="trialing", trial=True,
            level="supporter")
        tp._post_event(app, tp._event("customer.subscription.created",
                                      {"id": "sub_t"}, event_id="e1"))
        assert self._limit(app, _db.session.get(User, u.id)) == 55.0
        gateway.subscriptions["sub_t"]["status"] = "active"
        tp._post_event(app, tp._event("invoice.paid", {
            "id": "in_t", "subscription": "sub_t", "customer": "cus_t",
            "amount_paid": 11781}, event_id="e2"))
        _db.session.expire_all()
        u = _db.session.get(User, u.id)
        assert u.subscription_ever_paid
        assert self._limit(app, u) == 100.0

    def test_before_the_start_the_alpha_cap(self, app):
        """Control: nothing changes before the start, the cap included."""
        assert self._limit(app, _upcoming()) == 20.0

    def test_after_the_month_the_ceiling_is_the_default(self, app):
        """Control: after the month, the default cap as for any account
        without a subscription."""
        assert self._limit(app, _ended()) == 20.0

    def test_no_trial_ceiling_for_the_rest_of_the_month(self, app):
        from backend.utils.spend import trial_limit_usd
        u = _tester(subscription_status="trialing",
                    subscription_level="discounted",
                    subscription_trial_start=datetime.utcnow())
        assert trial_limit_usd(u, app.config) == 0

    def test_a_plain_trial_keeps_its_ceiling(self, app):
        """Control: #649's trial ceiling for an account that is not a
        tester."""
        from backend.utils.spend import trial_limit_usd
        u = _user("plain", subscription_status="trialing",
                  subscription_level="standard",
                  subscription_trial_start=datetime.utcnow())
        assert trial_limit_usd(u, app.config) == 30.0


class TestCommand:
    def _run(self, app, *args):
        from backend.billing_cli import alpha_free_month_command
        return app.test_cli_runner().invoke(alpha_free_month_command,
                                            list(args))

    def _waiting(self):
        u = _user("wa", plan="alpha")
        u.approved = False
        _db.session.commit()
        return u

    def _accounts(self):
        return {
            "alpha": _user("al", plan="alpha"),
            "alpha2": _user("al2", plan="alpha"),
            "pro": _user("pr", plan="pro"),
            "free": _user("fr", plan="free"),
            "admin": _user("ad", plan="alpha", is_admin=True),
            "waiting": self._waiting(),
            "deleting": _user("de", plan="alpha",
                              deleted_at=datetime.utcnow()),
        }

    def test_the_start_date_is_required(self, app):
        _user("al", plan="alpha")
        for args in (("--apply",), ("--apply", "--start", "1.12.2026"),
                     ("--apply", "--start", "2026-13-01")):
            r = self._run(app, *args)
            assert r.exit_code != 0, args
        assert _db.session.query(User).filter_by(
            username="al").one().plan == "alpha"

    def test_lists_without_changing(self, app):
        a = self._accounts()
        r = self._run(app, "--start", "2026-12-01")
        assert r.exit_code == 0, r.output
        assert "from 2026-12-01 00:00 UTC to 2026-12-31 00:00 UTC" in \
            r.output
        assert "2 account(s)" in r.output
        assert "al\n" in r.output and "al2" in r.output
        assert "pr\n" not in r.output and "ad\n" not in r.output
        assert _db.session.get(User, a["alpha"].id).plan == "alpha"

    def test_apply_gives_everyone_the_same_dates(self, app):
        a = self._accounts()
        start = _days(10).strftime("%Y-%m-%d")
        r = self._run(app, "--apply", "--start", start)
        assert r.exit_code == 0, r.output
        assert "has not started" in r.output
        begin = datetime.strptime(start, "%Y-%m-%d")
        for key in ("alpha", "alpha2"):
            u = _db.session.get(User, a[key].id)
            assert u.plan == "free"
            assert u.alpha_free_granted_at is not None
            assert u.alpha_free_start == begin
            assert u.alpha_free_until == begin + timedelta(days=30)
            # Before the start: as on the alpha plan.
            assert subs.access_state(u) == "full"
        for key, plan in (("pro", "pro"), ("free", "free"),
                          ("admin", "alpha"), ("waiting", "alpha"),
                          ("deleting", "alpha")):
            u = _db.session.get(User, a[key].id)
            assert u.plan == plan and u.alpha_free_granted_at is None
        # Run again: nothing left to give; it points at --change-date.
        again = self._run(app, "--apply", "--start", start)
        assert "0 account(s)" in again.output
        assert "--change-date" in again.output

    def test_a_start_in_the_past(self, app):
        a = self._accounts()
        start = _days(-40).strftime("%Y-%m-%d")
        r = self._run(app, "--apply", "--start", start)
        assert r.exit_code == 0, r.output
        assert "ended already" in r.output
        assert subs.access_state(
            _db.session.get(User, a["alpha"].id)) == "lapsed"

    def test_change_date_moves_the_month(self, app):
        a = self._accounts()
        self._run(app, "--apply", "--start", "2026-12-01")
        r = self._run(app, "--change-date", "--start", "2027-01-15")
        assert r.exit_code == 0, r.output
        assert "now 2026-12-01 to 2026-12-31" in r.output
        u = _db.session.get(User, a["alpha"].id)
        assert u.alpha_free_start == datetime(2026, 12, 1)   # not yet
        r = self._run(app, "--change-date", "--start", "2027-01-15",
                      "--apply")
        assert r.exit_code == 0, r.output
        _db.session.expire_all()
        u = _db.session.get(User, a["alpha"].id)
        assert u.alpha_free_start == datetime(2027, 1, 15)
        assert u.alpha_free_until == datetime(2027, 2, 14)
        assert u.plan == "free"

    def test_one_user(self, app):
        a = self._accounts()
        self._run(app, "--apply", "--start", "2026-12-01", "--user", "al2")
        assert _db.session.get(User, a["alpha2"].id).plan == "free"
        assert _db.session.get(User, a["alpha"].id).plan == "alpha"

    def test_undo(self, app):
        a = self._accounts()
        self._run(app, "--apply", "--start", "2026-12-01")
        r = self._run(app, "--undo", "--apply")
        assert r.exit_code == 0, r.output
        u = _db.session.get(User, a["alpha"].id)
        assert u.plan == "alpha" and u.alpha_free_granted_at is None
        assert u.alpha_free_start is None and u.plan_set_by_hand

    def test_refused_while_payments_are_off(self, app_off):
        _user("al", plan="alpha")
        r = self._run(app_off, "--apply", "--start", "2026-12-01")
        assert r.exit_code != 0
        assert _db.session.query(User).filter_by(
            username="al").one().plan == "alpha"


class TestAdmin:
    def test_the_list_shows_testers_and_their_month(self, app):
        t = _tester()
        admin = _user("root", is_admin=True)
        rows = _client(app, admin).get(
            "/api/admin/subscriptions").get_json()["subscriptions"]
        row = [r for r in rows if r["user_id"] == t.id][0]
        assert row["alpha_tester"] is True
        assert row["free_month_start"].startswith(
            t.alpha_free_start.strftime("%Y-%m-%d"))
        assert row["free_month_until"].startswith(
            t.alpha_free_until.strftime("%Y-%m-%d"))

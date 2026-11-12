"""The alpha free month (#438; Peter, 2026-11-12: alpha testers are "free
for one month after the opening, then the discounted price").

`flask alpha-free-month --apply` moves approved accounts on the "alpha"
plan to "free" and marks them; the month starts at their next request.
During it they have everything without a card and may subscribe at the
discounted price without a code, with the first payment due when the
month ends. After it, without a subscription, they are lapsed.

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


def _tester(username="tess", started=None, until=None, **kw):
    """An alpha tester: given the month; *until* set means it started."""
    u = _user(username, plan="free", **kw)
    u.alpha_free_granted_at = started or datetime.utcnow()
    u.alpha_free_until = until
    _db.session.commit()
    return u


def _checkout_params(gateway):
    return [c for c in gateway.calls
            if c[0] == "create_checkout_session"][-1][1]


@pytest.fixture
def hooked(app):
    """The app with the before_request that starts the month."""
    app.before_request(subs.start_alpha_free_month_hook)
    return app


class TestAccess:
    def test_the_month_not_started_yet_gives_everything(self, app):
        u = _tester()
        assert u.alpha_free_month_active
        assert u.on_paid_plan and u.has_voice_mode
        assert subs.access_state(u) == "active"
        assert subs.ai_replies_allowed(u)

    def test_a_running_month_gives_everything(self, app):
        u = _tester(until=datetime.utcnow() + timedelta(days=3))
        assert subs.access_state(u) == "active"
        assert subs.ai_replies_allowed(u)
        assert subs.refuse_billed_call(u) is None

    def test_an_ended_month_without_a_subscription_is_lapsed(self, app):
        u = _tester(until=datetime.utcnow() - timedelta(minutes=1))
        assert not u.on_paid_plan
        # Never shut out: reading, writing and export stay.
        assert subs.access_state(u) == "lapsed"
        assert not subs.ai_replies_allowed(u)
        msg = subs.subscription_required_message(u)
        assert msg == ("Your free month has ended, so Loore doesn't reply. "
                       "Your writing is saved.")

    def test_the_gate_lets_an_ended_tester_in(self, app):
        u = _tester(until=datetime.utcnow() - timedelta(days=1))
        r = _client(app, u).get("/api/billing")
        assert r.status_code == 200
        assert r.get_json()["subscription"]["state"] == "lapsed"

    def test_a_free_account_without_the_month_is_not_let_in(self, app):
        """Control: what #649 does for an account with nothing."""
        u = _user("plain")
        assert subs.access_state(u) == "none"
        assert "free month" not in subs.subscription_required_message(u)

    def test_payments_off_changes_nothing(self, app_off):
        """Control: with the switch off every account has everything."""
        u = _tester(until=datetime.utcnow() - timedelta(days=1))
        assert subs.access_state(u) == "full"
        assert subs.ai_replies_allowed(u)

    def test_sql_clauses_match_the_properties(self, app):
        running = _tester("run", until=datetime.utcnow() + timedelta(days=2))
        waiting = _tester("wait")
        ended = _tester("end", until=datetime.utcnow() - timedelta(days=2))
        ids = {u.id for u in User.query.filter(
            User.paid_features_clause()).all()}
        assert running.id in ids and waiting.id in ids
        assert ended.id not in ids
        cohort = {u.id for u in User.profile_eligible_query().all()}
        assert running.id in cohort and waiting.id in cohort
        assert ended.id not in cohort


class TestTheMonthStarts:
    def test_at_the_first_request_after_the_grant(self, hooked):
        u = _tester()
        before = datetime.utcnow()
        r = _client(hooked, u).get("/api/billing")
        assert r.status_code == 200
        u = _db.session.get(User, u.id)
        assert u.alpha_free_until is not None
        assert (before + timedelta(days=30) - timedelta(seconds=5)
                <= u.alpha_free_until
                <= datetime.utcnow() + timedelta(days=30))
        assert r.get_json()["subscription"]["free_month_until"] is not None

    def test_a_later_request_keeps_the_end(self, hooked):
        end = datetime.utcnow() + timedelta(days=12)
        u = _tester(until=end)
        _client(hooked, u).get("/api/billing")
        assert _db.session.get(User, u.id).alpha_free_until == end

    def test_the_length_is_a_setting(self, hooked):
        hooked.config["ALPHA_FREE_MONTH_DAYS"] = 60
        u = _tester()
        _client(hooked, u).get("/api/billing")
        until = _db.session.get(User, u.id).alpha_free_until
        assert until > datetime.utcnow() + timedelta(days=59)

    def test_not_while_payments_are_off(self, app_off):
        app_off.before_request(subs.start_alpha_free_month_hook)
        u = _tester()
        _client(app_off, u).get("/api/dashboard/?profile=0")
        assert _db.session.get(User, u.id).alpha_free_until is None

    def test_not_for_other_accounts(self, hooked):
        u = _user("plain")
        assert not subs.start_alpha_free_month(u)
        assert u.alpha_free_until is None


class TestCheckout:
    def test_the_discounted_level_is_offered_first(self, app):
        data = _client(app, _tester()).get("/api/billing").get_json()
        assert [lv["level"] for lv in data["levels"]] == [
            "discounted", "standard", "supporter"]
        assert data["levels"][0]["amount_cents"] == 1200
        sub = data["subscription"]
        assert sub["alpha_tester"] is True
        assert sub["free_month_active"] is True
        # The free month takes the trial's place.
        assert sub["trial_available"] is False

    def test_others_are_not_offered_it(self, app):
        """Control: #649's levels for an account that is not a tester."""
        data = _client(app, _user("plain")).get("/api/billing").get_json()
        assert [lv["level"] for lv in data["levels"]] == [
            "standard", "supporter"]
        assert data["subscription"]["alpha_tester"] is False

    def test_during_the_month_the_first_payment_is_at_its_end(
            self, app, gateway):
        end = datetime.utcnow() + timedelta(days=20)
        u = _tester(until=end)
        r = _client(app, u).post("/api/billing/checkout",
                                 json={"level": "discounted"})
        assert r.status_code == 200, r.get_data(as_text=True)
        params = _checkout_params(gateway)
        assert params["line_items"][0]["price"] == "price_discounted"
        data = params["subscription_data"]
        assert "trial_period_days" not in data
        assert abs(data["trial_end"] - int(
            end.replace(tzinfo=timezone.utc).timestamp())) <= 1
        assert data["metadata"]["loore_level"] == "discounted"
        assert "loore_code_id" not in data["metadata"]

    def test_a_tester_may_choose_another_level(self, app, gateway):
        u = _tester(until=datetime.utcnow() + timedelta(days=20))
        r = _client(app, u).post("/api/billing/checkout",
                                 json={"level": "standard"})
        assert r.status_code == 200
        params = _checkout_params(gateway)
        assert params["line_items"][0]["price"] == "price_standard"
        assert "trial_end" in params["subscription_data"]

    def test_in_the_last_two_days_the_trial_is_stripes_minimum(
            self, app, gateway):
        u = _tester(until=datetime.utcnow() + timedelta(hours=10))
        _client(app, u).post("/api/billing/checkout",
                             json={"level": "discounted"})
        trial_end = _checkout_params(gateway)["subscription_data"][
            "trial_end"]
        floor = datetime.utcnow() + timedelta(hours=48)
        assert trial_end >= int(
            floor.replace(tzinfo=timezone.utc).timestamp())

    def test_after_the_month_it_is_charged_at_checkout(self, app, gateway):
        u = _tester(until=datetime.utcnow() - timedelta(days=1))
        r = _client(app, u).post("/api/billing/checkout",
                                 json={"level": "discounted"})
        assert r.status_code == 200
        data = _checkout_params(gateway)["subscription_data"]
        assert "trial_end" not in data
        assert "trial_period_days" not in data

    def test_no_second_trial_for_a_tester(self, app, gateway):
        u = _tester(until=datetime.utcnow() + timedelta(days=20),
                    subscription_trial_used=True,
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
        u = _tester(until=datetime.utcnow() + timedelta(days=20))
        u.stripe_customer_id = "cus_t"
        _db.session.commit()
        gateway.subscriptions["sub_t"] = tp.subscription(
            "sub_t", customer="cus_t", status="trialing", trial=True,
            level="discounted")
        r = tp._post_event(app, tp._event("customer.subscription.created",
                                          {"id": "sub_t"}))
        assert r.status_code == 200
        assert "cancel_subscription" not in gateway.names()
        u = _db.session.get(User, u.id)
        assert u.subscription_status == "trialing"
        assert u.subscription_level == "discounted"


class TestCeiling:
    def test_the_month_has_the_discounted_levels_ceiling(self, app):
        from backend.utils.spend import get_user_spend_limit_usd
        app.config["SUBSCRIPTION_SPEND_LIMIT_USD_DISCOUNTED"] = 45.0
        u = _tester(until=datetime.utcnow() + timedelta(days=5))
        assert get_user_spend_limit_usd(u, app.config) == 45.0

    def test_after_the_month_the_ceiling_is_the_default(self, app):
        """Control: after the month, the default cap as for any account
        without a subscription."""
        from backend.utils.spend import get_user_spend_limit_usd
        app.config["SUBSCRIPTION_SPEND_LIMIT_USD_DISCOUNTED"] = 45.0
        u = _tester(until=datetime.utcnow() - timedelta(days=5))
        assert get_user_spend_limit_usd(u, app.config) == 20.0

    def test_any_level_chosen_in_the_month_has_the_discounted_ceiling(
            self, app):
        """Nothing paid yet: Supporter chosen during the free month still
        has the discounted level's ceiling, not Supporter's."""
        from backend.utils.spend import get_user_spend_limit_usd
        app.config["SUBSCRIPTION_SPEND_LIMIT_USD_DISCOUNTED"] = 45.0
        app.config["SUBSCRIPTION_SPEND_LIMIT_USD_SUPPORTER"] = 100.0
        u = _tester(until=datetime.utcnow() + timedelta(days=5),
                    subscription_status="trialing",
                    subscription_level="supporter",
                    subscription_trial_start=datetime.utcnow())
        assert get_user_spend_limit_usd(u, app.config) == 45.0
        # The month over, the first charge not through yet: still 45.
        u.alpha_free_until = datetime.utcnow() - timedelta(minutes=5)
        u.subscription_status = "active"
        _db.session.commit()
        assert get_user_spend_limit_usd(u, app.config) == 45.0

    def test_the_levels_ceiling_from_the_first_paid_invoice(self, app):
        """Control: once paid, the chosen level's ceiling, as for any
        subscriber."""
        from backend.utils.spend import get_user_spend_limit_usd
        app.config["SUBSCRIPTION_SPEND_LIMIT_USD_DISCOUNTED"] = 45.0
        app.config["SUBSCRIPTION_SPEND_LIMIT_USD_SUPPORTER"] = 100.0
        u = _tester(until=datetime.utcnow() - timedelta(days=1),
                    subscription_status="active",
                    subscription_level="supporter",
                    subscription_ever_paid=True)
        assert get_user_spend_limit_usd(u, app.config) == 100.0

    def test_the_first_paid_invoice_lifts_it_through_the_webhook(
            self, app, gateway):
        """The same through the webhook: Supporter subscribed during the
        month, then its first invoice paid."""
        from backend.utils.spend import get_user_spend_limit_usd
        app.config["SUBSCRIPTION_SPEND_LIMIT_USD_DISCOUNTED"] = 45.0
        app.config["SUBSCRIPTION_SPEND_LIMIT_USD_SUPPORTER"] = 100.0
        u = _tester(until=datetime.utcnow() + timedelta(days=2),
                    stripe_customer_id="cus_t")
        gateway.subscriptions["sub_t"] = tp.subscription(
            "sub_t", customer="cus_t", status="trialing", trial=True,
            level="supporter")
        tp._post_event(app, tp._event("customer.subscription.created",
                                      {"id": "sub_t"}, event_id="e1"))
        u = _db.session.get(User, u.id)
        assert get_user_spend_limit_usd(u, app.config) == 45.0
        gateway.subscriptions["sub_t"]["status"] = "active"
        tp._post_event(app, tp._event("invoice.paid", {
            "id": "in_t", "subscription": "sub_t", "customer": "cus_t",
            "amount_paid": 11781}, event_id="e2"))
        _db.session.expire_all()
        u = _db.session.get(User, u.id)
        assert u.subscription_ever_paid
        assert get_user_spend_limit_usd(u, app.config) == 100.0

    def test_no_trial_ceiling_for_the_rest_of_the_month(self, app):
        from backend.utils.spend import trial_limit_usd
        u = _tester(until=datetime.utcnow() + timedelta(days=5),
                    subscription_status="trialing",
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

    def test_lists_without_changing(self, app):
        a = self._accounts()
        r = self._run(app)
        assert r.exit_code == 0, r.output
        assert "2 account(s)" in r.output
        assert "al\n" in r.output and "al2" in r.output
        assert "pr\n" not in r.output and "ad\n" not in r.output
        assert _db.session.get(User, a["alpha"].id).plan == "alpha"

    def test_apply_moves_the_alpha_accounts_only(self, app):
        a = self._accounts()
        r = self._run(app, "--apply")
        assert r.exit_code == 0, r.output
        for key in ("alpha", "alpha2"):
            u = _db.session.get(User, a[key].id)
            assert u.plan == "free"
            assert u.alpha_free_granted_at is not None
            assert u.alpha_free_until is None
            assert subs.access_state(u) == "active"
        for key, plan in (("pro", "pro"), ("free", "free"),
                          ("admin", "alpha"), ("waiting", "alpha"),
                          ("deleting", "alpha")):
            u = _db.session.get(User, a[key].id)
            assert u.plan == plan and u.alpha_free_granted_at is None
        # Run again: nothing left to move.
        assert "0 account(s)" in self._run(app).output

    def test_one_user(self, app):
        a = self._accounts()
        self._run(app, "--apply", "--user", "al2")
        assert _db.session.get(User, a["alpha2"].id).plan == "free"
        assert _db.session.get(User, a["alpha"].id).plan == "alpha"

    def test_undo(self, app):
        a = self._accounts()
        self._run(app, "--apply")
        r = self._run(app, "--undo", "--apply")
        assert r.exit_code == 0, r.output
        u = _db.session.get(User, a["alpha"].id)
        assert u.plan == "alpha" and u.alpha_free_granted_at is None
        assert u.plan_set_by_hand

    def test_refused_while_payments_are_off(self, app_off):
        _user("al", plan="alpha")
        r = self._run(app_off, "--apply")
        assert r.exit_code != 0
        assert _db.session.query(User).filter_by(
            username="al").one().plan == "alpha"


class TestAdmin:
    def test_the_list_shows_testers_and_their_month(self, app):
        end = datetime.utcnow() + timedelta(days=9)
        t = _tester(until=end)
        admin = _user("root", is_admin=True)
        rows = _client(app, admin).get(
            "/api/admin/subscriptions").get_json()["subscriptions"]
        row = [r for r in rows if r["user_id"] == t.id][0]
        assert row["alpha_tester"] is True
        assert row["free_month_until"].startswith(end.strftime("%Y-%m-%d"))

"""Payments on: an account marked as spam is never let in automatically (#713).

Peter, 2026-11-20: with payments on (#697) an account that was never
approved is let in at its next request, except one the admin marked as
spam. Such an account is treated as a blocked one: the not-active answer,
its data download, deleting itself, nothing else, and no background job.
Activate by hand still works on it; unmarking alone approves nothing. An
account the gate let in that the admin marks as spam later is held from
that moment. The admin's own approval (Activate, a whitelist, a pre-fill,
an approval from before payments) wins over the mark.

Builds the real app as test_let_in_without_approval.py does and reuses
its fixtures. The tests marked "control" pass on the base branch too.
"""
from datetime import datetime

from backend.tests.test_let_in_without_approval import (  # noqa: F401
    NOT_ACTIVE, WAITING, User, _admin, _add, _block, _client, _db, _fresh,
    _me, _no_stripe, _routes, payments_off, real_app)


def _spam(**kw):
    """A sign-up the admin marked as spam, never approved."""
    return _add(spam=True, **kw)


def _let_in_then_marked(**kw):
    """An account the gate let in while payments were on, which the admin
    marked as spam afterwards."""
    return _add(approved=True, approved_at=datetime.utcnow(),
                let_in_at=datetime.utcnow(), spam=True, **kw)


def _as_admin(admin, method, url):
    from flask import g
    g.pop("_login_user", None)   # the admin's request
    return admin.open(url, method=method)


def _toggle_spam(admin, user):
    return _as_admin(admin, "POST", f"/api/admin/users/{user.id}/toggle_spam")


def _activate(admin, user):
    return _as_admin(admin, "POST", f"/api/admin/users/{user.id}/toggle")


def _users_list(admin):
    rows = _as_admin(admin, "GET", "/api/admin/users").get_json()["users"]
    return {r["username"]: r for r in rows}


# ── Never let in by the gate ────────────────────────────────────────────

class TestNeverLetIn:
    def test_not_let_in_and_gets_the_not_active_answer(self, real_app):
        u = _spam(username="spammer", email="s@example.com")
        c = _client(real_app, u)
        me = _me(c)
        assert me["approved"] is False
        assert me["account_blocked"] is True
        r = c.post("/api/nodes/", json={"content": "buy now"})
        assert r.status_code == 403
        assert r.get_json() == {"error": NOT_ACTIVE,
                                "code": "account_not_active"}
        user = _fresh(u)
        assert user.approved is False
        assert user.approved_at is None and user.let_in_at is None

    def test_control_not_marked_it_is_let_in(self, real_app):
        u = _add(username="newcomer", email="n@example.com")
        me = _me(_client(real_app, u))
        assert me["approved"] is True
        assert me["account_blocked"] is False
        assert _fresh(u).approved is True

    def test_an_alpha_era_signup_keeps_its_plan(self, real_app):
        # Signed up during the alpha, never approved, marked as spam: not
        # moved to the trial step either.
        u = _spam(username="oldspam", plan="alpha")
        _me(_client(real_app, u))
        user = _fresh(u)
        assert user.approved is False
        assert user.plan == "alpha"

    def test_the_gate_refuses_it_and_the_admins_hand_does_not(
            self, real_app):
        from backend.utils.approval import held_as_spam, let_in
        u = _spam(username="spammer")
        assert let_in(u) is False
        assert u.approved is False
        # A whitelist or a pre-fill is the admin's own approval.
        assert let_in(u, automatic=False) is True
        _db.session.commit()
        assert not held_as_spam(u)
        assert _me(_client(real_app, u))["approved"] is True

    def test_reaches_only_what_a_blocked_account_reaches(
            self, real_app, monkeypatch):
        from backend.utils import stripe_billing
        from backend.utils.approval import open_while_blocked
        monkeypatch.setattr(stripe_billing, "get_gateway", _no_stripe)
        u = _spam(username="spammer", email="s@example.com")
        c = _client(real_app, u)
        refused = 0
        for method, url in _routes(real_app):
            if open_while_blocked(method, url):
                continue
            r = c.open(url, method=method, json={"level": "standard"})
            assert r.status_code == 403, (method, url, r.status_code)
            assert r.get_json() == {"error": NOT_ACTIVE,
                                    "code": "account_not_active"}, url
            refused += 1
        assert refused > 150
        assert _fresh(u).approved is False

    def test_downloads_its_data_and_deletes_itself(self, real_app):
        u = _spam(username="spammer", email="s@example.com")
        c = _client(real_app, u)
        r = c.get("/api/export/threads")
        assert r.status_code == 200
        assert r.headers["Content-Disposition"].startswith("attachment")
        assert c.head("/api/export/threads").status_code == 200
        assert c.post("/api/account/delete", json={}).status_code != 403

    def test_unblock_leaves_the_mark_in_force(self, real_app):
        _admin_user, admin = _admin(real_app)
        u = _spam(username="spammer")
        _block(admin, u, True)
        _block(admin, u, False)
        me = _me(_client(real_app, u))
        assert me["approved"] is False and me["account_blocked"] is True
        assert _fresh(u).approved is False

    def test_unblock_of_one_deactivated_in_the_alpha_approves_nothing(
            self, real_app):
        """Deactivated during the alpha (no approved_before_block) and
        marked as spam: Unblock clears the block and approves nothing (no
        Activate, no alpha free month); the mark holds it, and the row
        offers Activate."""
        _admin_user, admin = _admin(real_app)
        u = _spam(username="oldspam", plan="alpha",
                  deactivated_at=datetime(2026, 9, 1))
        assert u.approved_before_block is None
        r = _block(admin, u, False)
        assert r.get_json() == {"blocked": False, "approved": False}
        user = _fresh(u)
        assert user.approved is False and user.approved_at is None
        assert user.deactivated_at is None
        assert user.plan == "alpha" and user.alpha_free_granted_at is None
        me = _me(_client(real_app, u))
        assert me["approved"] is False and me["account_blocked"] is True
        assert _fresh(u).approved is False   # not let in by the gate either
        row = _users_list(admin)["oldspam"]
        assert row["blocked"] is False and row["held_as_spam"] is True
        assert row["approved"] is False   # Activate is offered

    def test_control_unblock_of_one_deactivated_in_the_alpha_activates(
            self, real_app):
        # Not marked: Unblock is Activate, as it always was (#700).
        _admin_user, admin = _admin(real_app)
        u = _add(username="old", plan="alpha",
                 deactivated_at=datetime(2026, 9, 1))
        r = _block(admin, u, False)
        assert r.get_json() == {"blocked": False, "approved": True}
        assert _fresh(u).approved is True


# ── Marked after the gate let it in ─────────────────────────────────────

class TestMarkedLater:
    def test_held_from_the_moment_of_the_mark(self, real_app):
        _admin_user, admin = _admin(real_app)
        u = _add(username="someone", email="x@example.com")
        assert _me(_client(real_app, u))["approved"] is True   # let in
        assert _fresh(u).let_in_at is not None
        assert _toggle_spam(admin, u).get_json()["spam"] is True
        c = _client(real_app, u)
        me = _me(c)
        assert me["approved"] is False and me["account_blocked"] is True
        r = c.post("/api/nodes/", json={"content": "x"})
        assert r.status_code == 403
        assert r.get_json()["code"] == "account_not_active"
        # It keeps its state; unmarking gives it back as it was.
        assert _fresh(u).approved is True
        assert _toggle_spam(admin, u).get_json()["spam"] is False
        me = _me(_client(real_app, u))
        assert me["approved"] is True and me["account_blocked"] is False

    def test_a_subscriber_can_still_cancel(self, real_app, monkeypatch):
        from backend.tests.test_let_in_without_approval import _PortalGateway
        from backend.utils import stripe_billing
        gw = _PortalGateway()
        monkeypatch.setattr(stripe_billing, "get_gateway",
                            lambda *a, **k: gw)
        u = _let_in_then_marked(
            username="payer", stripe_customer_id="cus_s",
            subscription_status="active", subscription_level="standard",
            subscription_ever_paid=True)
        c = _client(real_app, u)
        assert _me(c)["account_blocked"] is True
        r = c.get("/api/billing/")
        assert r.status_code == 200
        assert r.get_json()["subscription"]["status"] == "active"
        assert c.post("/api/billing/portal").status_code == 200
        for url in ("/api/billing/checkout", "/api/billing/sync"):
            r = c.post(url, json={"level": "standard", "session_id": "cs_x"})
            assert r.status_code == 403, url
        assert _fresh(u).subscription_status == "active"

    def test_its_api_tokens_stop_working(self, real_app):
        from backend.models import ApiToken
        from backend.utils.api_tokens import (
            SCOPE_EXTERNAL_WRITE, generate_api_token, hash_token,
            resolve_api_token)
        u = _add(username="clipper", approved=True,
                 let_in_at=datetime.utcnow())
        plain, prefix = generate_api_token()
        _db.session.add(ApiToken(user_id=u.id, name="clipper", prefix=prefix,
                                 scope=SCOPE_EXTERNAL_WRITE,
                                 token_hash=hash_token(plain)))
        _db.session.commit()
        assert resolve_api_token(plain, SCOPE_EXTERNAL_WRITE) is not None
        u.spam = True
        _db.session.commit()
        assert resolve_api_token(plain, SCOPE_EXTERNAL_WRITE) is None

    def test_the_users_list_says_held_and_offers_activate(self, real_app):
        _admin_user, admin = _admin(real_app)
        _let_in_then_marked(username="later", subscription_status="active",
                            subscription_level="standard")
        _add(username="byhand", approved=True, spam=True)
        _add(username="notyet")
        rows = _users_list(admin)
        row = rows["later"]
        # Not approved and not blocked: the row has Activate (AdminPanel),
        # and its Status reads "Held (spam)".
        assert row["approved"] is False
        assert row["blocked"] is False
        assert row["spam"] is True
        assert row["held_as_spam"] is True
        assert rows["byhand"]["held_as_spam"] is False
        assert rows["notyet"]["held_as_spam"] is False

    def test_control_payments_off_the_list_has_no_held_key(
            self, payments_off):
        _admin_user, admin = _admin(payments_off)
        _spam(username="spammer")
        rows = _users_list(admin)
        assert all("held_as_spam" not in r for r in rows.values())


# ── The admin's hand ────────────────────────────────────────────────────

class TestActivateByHand:
    def test_activate_lets_a_never_approved_one_in(self, real_app):
        _admin_user, admin = _admin(real_app)
        u = _spam(username="wrongflag", email="w@example.com")
        r = _activate(admin, u)
        assert r.get_json()["approved"] is True
        user = _fresh(u)
        assert user.approved is True and user.let_in_at is None
        assert user.spam is True   # the mark stays
        me = _me(_client(real_app, u))
        assert me["approved"] is True and me["account_blocked"] is False

    def test_activate_brings_back_one_the_gate_let_in(self, real_app):
        _admin_user, admin = _admin(real_app)
        u = _let_in_then_marked(username="later")
        r = _activate(admin, u)
        assert r.status_code == 200 and r.get_json()["approved"] is True
        user = _fresh(u)
        # Approved by hand, not turned off.
        assert user.approved is True and user.let_in_at is None
        assert user.deactivated_at is None
        me = _me(_client(real_app, u))
        assert me["approved"] is True and me["account_blocked"] is False
        assert _users_list(admin)["later"]["approved"] is True

    def test_unmarking_alone_approves_nothing(self, real_app):
        _admin_user, admin = _admin(real_app)
        u = _spam(username="notspam")
        assert _me(_client(real_app, u))["account_blocked"] is True
        assert _toggle_spam(admin, u).get_json()["spam"] is False
        assert _fresh(u).approved is False
        # From now on the usual rules: let in at its next request.
        assert _me(_client(real_app, u))["approved"] is True
        assert _fresh(u).let_in_at is not None

    def test_an_account_approved_by_hand_stays_active(self, real_app):
        # Approved by the admin (no let_in_at), e.g. during the alpha, and
        # marked as spam: the admin's approval wins; Block turns it off.
        u = _add(username="friend", approved=True,
                 approved_at=datetime.utcnow(), spam=True)
        me = _me(_client(real_app, u))
        assert me["approved"] is True and me["account_blocked"] is False


# ── Background jobs ─────────────────────────────────────────────────────

class TestBackgroundJobs:
    def test_held_accounts_get_none(self, real_app):
        from backend.utils.approval import (
            background_work_allowed, background_work_clause)
        paid = {"subscription_status": "active",
                "subscription_level": "standard", "default_ai_usage": "chat"}
        never = _spam(username="never", **paid)
        later = _let_in_then_marked(username="later", **paid)
        by_hand = _add(username="byhand", approved=True, spam=True, **paid)
        eligible = {u.id for u in User.profile_eligible_query().all()}
        for u in (never, later):
            assert not background_work_allowed(u), u.username
            assert User.query.filter(User.id == u.id,
                                     *background_work_clause()).count() == 0
            assert u.id not in eligible
        assert background_work_allowed(by_hand)
        assert User.query.filter(User.id == by_hand.id,
                                 *background_work_clause()).count() == 1
        assert by_hand.id in eligible
        # Control: payments off, the mark changes nothing.
        real_app.config["PAYMENTS_ENABLED"] = False
        assert background_work_allowed(later)
        assert later.id in {u.id for u in User.profile_eligible_query().all()}

    def test_a_held_profile_build_does_not_resume(self, real_app):
        from backend.tasks.profile_batch import _held_users
        spam = _spam(username="spam", profile_cap_held=True)
        prefill = _add(username="prefilled", profile_cap_held=True)
        held = {u.id for u in _held_users(User.profile_cap_held, set())}
        assert spam.id not in held
        assert prefill.id in held


# ── Payments off: nothing changes ───────────────────────────────────────

class TestPaymentsOff:
    def test_control_a_spam_signup_waits_as_before(self, payments_off):
        u = _spam(username="spammer")
        c = _client(payments_off, u)
        me = _me(c)
        assert me["approved"] is False
        assert "account_blocked" not in me
        r = c.post("/api/nodes/", json={})
        assert r.status_code == 403
        assert r.get_json() == {"error": WAITING}

    def test_control_an_approved_spam_account_keeps_access(
            self, payments_off):
        from backend.utils.approval import background_work_allowed
        u = _add(username="flagged", approved=True, spam=True)
        me = _me(_client(payments_off, u))
        assert me["approved"] is True
        assert "account_blocked" not in me
        assert background_work_allowed(u)
        assert u.id in {x.id for x in User.profile_eligible_query().all()}

    def test_control_activate_approves_as_before(self, payments_off):
        _admin_user, admin = _admin(payments_off)
        u = _spam(username="spammer")
        r = _activate(admin, u)
        assert r.get_json() == {"message": "User status updated",
                                "approved": True}
        assert _fresh(u).approved is True

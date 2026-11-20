"""The admin's "Email me about new signups" (#715).

Payments on: an information email per new account (username, how it signed
in, prepared in advance or not, time), no approval request; payments off:
today's approval email. The checkbox is per admin account, on by default.
One email per account, also across a terms update. Builds the real app the
way test_let_in_without_approval.py does (fixtures imported from it).
"""
from datetime import datetime

from backend.extensions import db as _db
from backend.tests.test_let_in_without_approval import (  # noqa: F401
    _add, _admin, _client, payments_off, real_app)


def _mails(monkeypatch):
    sent = []
    import backend.routes.terms as terms
    monkeypatch.setattr(terms, "send_admin_signup_notification",
                        lambda name, mail: sent.append(("approval", name)))
    monkeypatch.setattr(
        terms, "send_admin_signup_info",
        lambda user: sent.append(("info", user.username)))
    return sent


def _setting(admin_client, enabled):
    from flask import g
    g.pop("_login_user", None)   # the admin's request, not the last user's
    r = admin_client.put("/api/admin/signup_email",
                         json={"enabled": enabled})
    assert r.status_code == 200
    g.pop("_login_user", None)


def test_payments_on_one_info_email(real_app, monkeypatch):
    sent = _mails(monkeypatch)
    _admin(real_app)
    c = _client(real_app, _add(username="newcomer"))
    c.post("/api/terms/accept")
    assert sent == [("info", "newcomer")]


def test_payments_on_checkbox_off_sends_nothing(real_app, monkeypatch):
    sent = _mails(monkeypatch)
    _a, admin = _admin(real_app)
    _setting(admin, False)
    _client(real_app, _add(username="newcomer")).post("/api/terms/accept")
    assert sent == []


def test_payments_off_control_and_checkbox_off(payments_off, monkeypatch):
    sent = _mails(monkeypatch)
    _a, admin = _admin(payments_off)
    _client(payments_off, _add(username="one")).post("/api/terms/accept")
    assert sent == [("approval", "one")]
    _setting(admin, False)
    _client(payments_off, _add(username="two")).post("/api/terms/accept")
    assert sent == [("approval", "one")]


def test_default_is_on_and_setting_is_per_admin(real_app, monkeypatch):
    sent = _mails(monkeypatch)
    a1, c1 = _admin(real_app)
    assert c1.get("/api/admin/signup_email").get_json() == {"enabled": True}
    _setting(c1, False)
    from backend.models import User
    a2 = User(username="second", approved=True, is_admin=True)
    _db.session.add(a2)
    _db.session.commit()
    c2 = _client(real_app, a2)
    assert c2.get("/api/admin/signup_email").get_json() == {"enabled": True}
    # One admin still has it on: the mailbox gets the email.
    _client(real_app, _add(username="newcomer")).post("/api/terms/accept")
    assert sent == [("info", "newcomer")]


def test_setting_rejects_non_admin_and_junk(payments_off):
    r = _client(payments_off, _add(username="plain", approved=True)).put(
        "/api/admin/signup_email", json={"enabled": False})
    assert r.status_code == 403
    _a, admin = _admin(payments_off)
    from flask import g
    g.pop("_login_user", None)
    assert admin.put("/api/admin/signup_email",
                     json={"enabled": "no"}).status_code == 400


def test_one_email_across_terms_update_and_retry(real_app, monkeypatch):
    sent = _mails(monkeypatch)
    _admin(real_app)
    u = _add(username="newcomer")
    c = _client(real_app, u)
    c.post("/api/terms/accept")
    c.post("/api/terms/accept")   # retry or terms update
    assert sent == [("info", "newcomer")]
    # Two requests that both saw no timestamp: only one claims.
    from backend.utils.approval import claim_signup_notice
    racer = _add(username="racer")
    assert claim_signup_notice(racer) is True
    assert claim_signup_notice(racer) is False
    # An account that accepted before this feature existed is no signup.
    old = _add(username="old", accepted_terms_at=datetime(2026, 1, 1))
    _client(real_app, old).post("/api/terms/accept")
    assert sent == [("info", "newcomer")]


def test_spam_account_sends_nothing(real_app, monkeypatch):
    sent = _mails(monkeypatch)
    _admin(real_app)
    _client(real_app, _add(username="junk", spam=True)).post(
        "/api/terms/accept")
    assert sent == []


def test_info_email_holds_only_the_listed_facts(real_app, monkeypatch):
    import backend.utils.email as email_mod
    seen = []
    monkeypatch.setattr(
        email_mod, "_deliver",
        lambda to, subject, text, html: seen.append((to, subject, text, html)))
    _admin(real_app)
    u = _add(username="newcomer", email="secret@example.com",
             twitter_id="77", prefilled_handle="newcomer",
             accepted_terms_at=datetime(2026, 11, 20, 9, 30))
    email_mod.send_admin_signup_info(u)
    (_to, subject, text, html), = seen
    for body in (text, html):
        assert "secret@example.com" not in body
        assert "newcomer" in body
        assert "Signed in with: X" in body
        assert "Prepared in advance: yes" in body
        assert "2026-11-20 09:30 UTC" in body
        assert "approve" not in body.lower().replace("no approval", "")
    assert "secret@example.com" not in subject
    u2 = _add(username="mailer", email="m@example.com")
    email_mod.send_admin_signup_info(u2)
    assert "Signed in with: email" in seen[1][2]
    assert "Prepared in advance: no" in seen[1][2]

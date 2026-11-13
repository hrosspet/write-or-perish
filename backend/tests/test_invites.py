"""One personal invite a month (#439; Peter, 2026-11-13: "one invite a
month, no accumulation of invites", "no keeping of invites pls").

An invite is made for one named person, by X handle or email address, and
its link records a referral for exactly one sign-up: that person's. One
invite per calendar month (UTC) per account, one open at a time, an
unused one expires after INVITE_EXPIRY_DAYS, and the handle or address is
kept only while the invite is open. The reward is test_referrals.py's.

Harnesses shared with test_referrals.py: the payments test app (with the
referrals blueprint) and the app with the real sign-in routes.
"""
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from backend.extensions import db as _db
from backend.models import Invite, Referral, User
from backend.tests.test_payments import (
    _client, _event, _post_event, _user)
from backend.tests.test_referrals import (  # noqa: F401
    FRONTEND_URL, _paid, _x, app, app_off, app_referrals_off, gateway,
    signup_app)
from backend.utils import referrals
from backend.utils.referrals import InviteRefused

NOW = datetime(2026, 11, 13, 12, 0)

# X account ids by handle, as the Community Archive (free) or X (one paid
# read) answer them; anything else is not on X.
ARCHIVE = {"fred_x": "4242"}
ON_X = {"ben_x": "9001", "fred": "5555", "fred_x": "4242"}


class XLookups:
    def __init__(self):
        self.archive, self.x = dict(ARCHIVE), dict(ON_X)
        self.archive_down = self.x_down = False
        self.x_reads = []

    def fetch_account(self, handle, timeout=None):
        if self.archive_down:
            raise ConnectionError("archive down")
        x_id = self.archive.get(handle.lower())
        return {"account_id": x_id, "username": handle} if x_id else None

    def lookup_user(self, handle, creds, timeout=None):
        from backend.utils.x_api import XApiError
        self.x_reads.append(handle)
        if self.x_down:
            raise XApiError("X API rate limit hit — try again later.",
                            sent=True)
        x_id = self.x.get(handle.lower())
        return {"id": x_id, "username": handle} if x_id else None


@pytest.fixture(autouse=True)
def x_lookups(monkeypatch):
    """No outside call: the archive and X answer from the tables above."""
    from backend.utils import community_archive, x_api
    fake = XLookups()
    monkeypatch.setattr(community_archive, "fetch_account",
                        fake.fetch_account)
    monkeypatch.setattr(x_api, "lookup_user", fake.lookup_user)
    return fake


def _inviter(name="ana", **kw):
    """An account let in (alpha, set by hand) with an address and an X
    handle of its own."""
    kw.setdefault("email", f"{name}@example.com")
    kw.setdefault("twitter_handle", f"{name.capitalize()}_X")
    u = User(username=name, approved=True, plan="alpha", **kw)
    _db.session.add(u)
    _db.session.commit()
    return u


def _friend(name="fred", **kw):
    """An account just made by a sign-in (not approved, nobody signed in
    yet)."""
    u = User(username=name, approved=False, **kw)
    _db.session.add(u)
    _db.session.commit()
    return u


def _invite(user, to, now=NOW):
    return referrals.create_invite(user, to, now=now)


def _redeem(friend, invite, now=NOW, **identity):
    return referrals.redeem_invite(friend, invite.code, True, now=now,
                                   **identity)


def _refused(user, to, now=NOW):
    with pytest.raises(InviteRefused) as e:
        _invite(user, to, now)
    return e.value


# ── Making an invite ─────────────────────────────────────────────────────

class TestMaking:
    def test_for_an_address_or_a_handle(self, app):  # noqa: F811
        ana = _inviter()
        invite = _invite(ana, "  Ben@Example.COM ")
        assert (invite.kind, invite.target) == ("email", "ben@example.com")
        assert invite.month == "2026-11" and invite.state == "open"
        assert invite.expires_at == NOW + timedelta(days=30)
        assert len(invite.code) == 16 and "ben" not in invite.code.lower()
        referrals.withdraw_invite(ana, NOW)
        for typed in ("@Ben_X", "ben_x", "https://x.com/Ben_X",
                      "twitter.com/ben_x/"):
            invite = _invite(ana, typed)
            assert (invite.kind, invite.target) == ("x", "ben_x"), typed
            assert invite.target_x_id == "9001"
            referrals.withdraw_invite(ana, NOW)

    @pytest.mark.parametrize("typed", [
        "", None, "ben smith", "@", "a" * 16, "ben@", "ben@example",
        "@ben@example.com", 7])
    def test_what_is_neither(self, app, typed):  # noqa: F811
        assert _refused(_inviter(), typed).status == 400

    @pytest.mark.parametrize("typed", [
        "ANA@example.com", "@ana_x", "https://x.com/Ana_X", "new@example.com",
        "ana+loore@example.com", "Ana+X@Example.COM", "new+1@example.com"])
    def test_not_oneself(self, app, typed):  # noqa: F811
        """Own address (any case, or with a plus-tag), own handle, the
        address being changed to."""
        ana = _inviter(pending_email="new@example.com")
        assert _refused(ana, typed).message.startswith("That's your own")
        assert Invite.query.count() == 0

    def test_a_plus_tag_is_ignored_only_for_that(self, app):  # noqa: F811
        """Control: another address that only looks like the inviter's is
        fine, and the stored address keeps its plus-tag."""
        ana = _inviter(email="ana+work@example.com")
        assert _refused(ana, "ana@example.com").status == 400
        invite = _invite(ana, "ana.b+x@example.com")
        assert invite.target == "ana.b+x@example.com"

    def test_not_ones_own_x_account_under_another_handle(
            self, app):  # noqa: F811
        """The handle typed is someone's current one, and its X account is
        the inviter's own (renamed)."""
        ana = _inviter(twitter_id="9001", twitter_handle="ana_old")
        assert _refused(ana, "@ben_x").message.startswith("That's your own")

    def test_one_open_at_a_time(self, app):  # noqa: F811
        ana = _inviter()
        _invite(ana, "ben@example.com")
        assert _refused(ana, "cy@example.com").status == 409

    def test_one_a_month_and_none_kept(self, app):  # noqa: F811
        """Nothing made in September or October is kept: November has one
        invite. Used, it ends November's; December has one again."""
        ana = _inviter()
        fred = _friend()
        assert _redeem(fred, _invite(ana, "fred@example.com"),
                       email="fred@example.com")
        refused = _refused(ana, "cy@example.com")
        assert (refused.status, refused.message) == (
            409, "You've used this month's invite.")
        assert _refused(ana, "cy@example.com",
                        datetime(2026, 11, 30, 23, 59)).status == 409
        assert _invite(ana, "cy@example.com", datetime(2026, 12, 1, 0, 0))

    def test_withdraw_and_make_another_but_never_two_used_in_a_month(
            self, app):  # noqa: F811
        """The exact rule: one open at a time; a withdrawn or expired one
        frees the month, a used one ends it. So of the invites made in one
        month at most one is ever used."""
        ana = _inviter()
        first = _invite(ana, "ben@example.com")
        assert referrals.withdraw_invite(ana, NOW)
        assert (first.state, first.target) == ("withdrawn", None)
        second = _invite(ana, "fred@example.com")
        assert _redeem(_friend(), second, email="fred@example.com")
        assert _refused(ana, "cy@example.com").status == 409
        # The withdrawn one records nothing, even for the person it named.
        assert _redeem(_friend("ben"), first, email="ben@example.com") is None
        used = Invite.query.filter_by(inviter_id=ana.id, month="2026-11",
                                      state="used").count()
        assert used == 1

    def test_open_into_the_next_month(self, app):  # noqa: F811
        """The month is the one the invite was made in: one made on 25
        November and still open on 2 December blocks December's until it
        is withdrawn; then December's may be made."""
        ana = _inviter()
        _invite(ana, "ben@example.com", datetime(2026, 11, 25))
        dec = datetime(2026, 12, 2)
        assert _refused(ana, "cy@example.com", dec).status == 409
        referrals.withdraw_invite(ana, dec)
        assert _invite(ana, "cy@example.com", dec).month == "2026-12"

    def test_an_unused_invite_expires_after_30_days(self, app):  # noqa: F811
        ana = _inviter()
        invite = _invite(ana, "ben@example.com")
        day_30 = NOW + timedelta(days=30)
        assert referrals.expire_due(day_30 - timedelta(minutes=1)) == 0
        assert referrals.expire_due(day_30) == 1
        assert (invite.state, invite.target) == ("expired", None)
        assert _redeem(_friend("ben"), invite, now=day_30,
                       email="ben@example.com") is None
        # Expired, it frees the month it was made in... which is over.
        assert _invite(ana, "cy@example.com", day_30).month == "2026-12"

    def test_expired_before_the_hourly_sweep(self, app):  # noqa: F811
        ana = _inviter()
        invite = _invite(ana, "ben@example.com")
        late = NOW + timedelta(days=30, minutes=5)
        assert _redeem(_friend("ben"), invite, now=late,
                       email="ben@example.com") is None
        assert (invite.state, invite.target) == ("expired", None)

    def test_who_may_invite(self, app):  # noqa: F811
        """Let in, a trial included and a lapsed account too; not one
        waiting for approval, never let in, or being deleted."""
        ok = [
            _user("trial", plan="free", subscription_status="trialing",
                  subscription_level="standard"),
            _user("lapsed", plan="free", subscription_status="ended",
                  subscription_level="standard", subscription_ever_paid=True),
        ]
        for user in ok:
            assert referrals.may_invite(user)
        no = [
            _friend("waiting", plan="alpha"),
            _user("never", plan="free"),
            _user("leaving", plan="alpha", deleted_at=NOW),
        ]
        for user in no:
            assert not referrals.may_invite(user)
            assert _refused(user, "ben@example.com").status == 403


class TestTheRoutes:
    def test_make_show_withdraw(self, app):  # noqa: F811
        ana = _inviter()
        client = _client(app, ana)
        state = client.get("/api/referrals/invite").get_json()
        assert state == {"invite": None, "can_invite": True,
                         "next_invite_on": None,
                         "credit": {"cents": 750, "currency": "usd"}}
        r = client.post("/api/referrals/invite", json={"to": "@Ben_X"})
        assert r.status_code == 201
        invite = r.get_json()["invite"]
        code = Invite.query.one().code
        assert invite["url"] == f"https://loore.test/invite/{code}"
        assert (invite["kind"], invite["to"]) == ("x", "ben_x")
        assert invite["expires_at"].endswith("Z")
        assert client.get("/api/referrals/invite").get_json()["invite"] == \
            invite
        r = client.delete("/api/referrals/invite")
        assert r.get_json()["invite"] is None
        assert r.get_json()["can_invite"] is True

    def test_after_this_months_invite_was_used(self, app):  # noqa: F811
        ana = _inviter()
        client = _client(app, ana)
        client.post("/api/referrals/invite", json={"to": "fred@example.com"})
        invite = Invite.query.one()
        assert referrals.redeem_invite(_friend(), invite.code, True,
                                       email="fred@example.com")
        state = client.get("/api/referrals/invite").get_json()
        now = datetime.utcnow()
        first = (datetime(now.year + 1, 1, 1) if now.month == 12
                 else datetime(now.year, now.month + 1, 1))
        assert state["invite"] is None and not state["can_invite"]
        assert state["next_invite_on"] == first.date().isoformat()
        r = client.post("/api/referrals/invite", json={"to": "cy@x.org"})
        assert r.status_code == 409

    def test_a_refusal_says_why(self, app):  # noqa: F811
        r = _client(app, _inviter()).post("/api/referrals/invite",
                                          json={"to": "not an address"})
        assert r.status_code == 400
        assert "X handle" in r.get_json()["error"]

    def test_an_address_with_an_account_gets_the_same_answer(self, app):  # noqa: F811
        """Saying "already on Loore" would tell anyone whether an address
        has an account: both are accepted alike, and the one with an
        account simply stays unused."""
        _user("old", email="old@example.com", last_seen_at=NOW)
        answers = []
        for name, to in (("ana", "old@example.com"),
                         ("bea", "new@example.com")):
            r = _client(app, _inviter(name)).post(
                "/api/referrals/invite", json={"to": to})
            body = r.get_json()
            answers.append((r.status_code, sorted(body),
                            sorted(body["invite"]), body["invite"]["kind"]))
        assert answers[0] == answers[1]

    @pytest.mark.parametrize("method", ["get", "post", "delete"])
    def test_off_with_payments(self, app_off, method):  # noqa: F811
        client = _client(app_off, _inviter())
        assert getattr(client, method)("/api/referrals/invite").status_code \
            == 404

    def test_off_with_its_own_switch(self, app_referrals_off):  # noqa: F811
        client = _client(app_referrals_off, _inviter())
        assert client.get("/api/referrals/invite").status_code == 404
        assert client.post("/api/referrals/invite",
                           json={"to": "ben@example.com"}).status_code == 404


# ── An X handle's account (the lookup the admin whitelist uses) ─────────

class TestXAccounts:
    def test_the_id_is_stored_and_deleted_with_the_handle(
            self, app, x_lookups):  # noqa: F811
        """The Community Archive knows it: no paid read. X knows it: one
        read, on the inviter's ledger, naming no handle."""
        from backend.models import APICostLog
        ana = _inviter()
        free = _invite(ana, "@fred_x")
        assert (free.target, free.target_x_id) == ("fred_x", "4242")
        assert x_lookups.x_reads == [] and APICostLog.query.count() == 0
        referrals.withdraw_invite(ana, NOW)
        free = _db.session.get(Invite, free.id)
        assert (free.target, free.target_x_id) == (None, None)
        paid = _invite(ana, "@ben_x")
        assert paid.target_x_id == "9001"
        row = APICostLog.query.one()
        assert (row.user_id, row.request_type, row.request_ref) == (
            ana.id, "x_id_lookup", "invite")
        assert row.cost_microdollars == 10000
        assert "ben" not in repr(row.__dict__)

    def test_a_handle_nobody_has_is_a_typo(self, app):  # noqa: F811
        refused = _refused(_inviter(), "@fredd_x")
        assert (refused.status, refused.message) == (
            400, "There is no X account @fredd_x. Check the spelling.")
        assert Invite.query.count() == 0

    def test_an_outage_never_stops_an_invite(self, app, x_lookups):  # noqa: F811
        """The archive is down and X answers with a rate limit: the invite
        is made with the handle only, and a sign-in matches its text."""
        x_lookups.archive_down = x_lookups.x_down = True
        ana = _inviter()
        invite = _invite(ana, "@fred_x")
        assert (invite.target, invite.target_x_id) == ("fred_x", None)
        assert _redeem(_friend(), invite, x_handle="Fred_X", x_id="31337")

    def test_a_taken_over_handle_does_not_match(self, signup_app):  # noqa: F811
        """Fred renamed; someone else took @fred_x and follows the link:
        told the invitation was for someone else, nothing recorded. Fred,
        as @fred_new, signs up through it: recorded."""
        ana, invite = _open("@fred_x")
        client = signup_app.test_client()
        r = _x_answer(signup_app, client, invite.code, 8888, "fred_x")
        assert r.headers["Location"] == MISMATCH + "x"
        assert Referral.query.count() == 0
        fred = _x_signup(signup_app, invite.code, 4242, "fred_new")
        assert Referral.query.one().friend_id == fred.id


# ── Two things at once ──────────────────────────────────────────────────
# An invite's state changes only while it is open, and one open invite per
# person is held by the database. sqlite runs one request at a time, so
# the interleavings are built by hand: a stale view of an invite, and both
# requests passing their checks before either writes.

class TestAtOnce:
    def test_a_withdrawal_that_lost_to_the_sign_up_changes_nothing(
            self, app, monkeypatch):  # noqa: F811
        """Withdraw read the invite as open; the friend's sign-up used it
        first: it stays used, and the month stays spent."""
        ana = _inviter()
        invite = _invite(ana, "fred@example.com")
        stale = referrals.open_invite(ana.id)
        assert _redeem(_friend(), invite, email="fred@example.com")
        monkeypatch.setattr(referrals, "open_invite", lambda _uid: stale)
        assert referrals.withdraw_invite(ana, NOW) is False
        assert _db.session.get(Invite, invite.id).state == "used"
        monkeypatch.undo()
        assert _refused(ana, "cy@example.com").message == \
            "You've used this month's invite."

    def test_a_sign_up_that_lost_to_the_withdrawal_records_nothing(
            self, app):  # noqa: F811
        """The other order: withdrawn first, the sign-up finds it closed,
        and the month is free again."""
        ana = _inviter()
        invite = _invite(ana, "fred@example.com")
        referrals.withdraw_invite(ana, NOW)
        assert _redeem(_friend(), invite, email="fred@example.com") is None
        assert _invite(ana, "cy@example.com").state == "open"

    def test_an_expiry_that_lost_to_the_sign_up_changes_nothing(
            self, app):  # noqa: F811
        ana = _inviter()
        invite = _invite(ana, "fred@example.com")
        assert _redeem(_friend(), invite, email="fred@example.com")
        assert referrals.expire_due(NOW + timedelta(days=31)) == 0
        assert _db.session.get(Invite, invite.id).state == "used"

    def test_two_makes_at_once_leave_one_open(self, app, monkeypatch):  # noqa: F811
        """Both requests saw no open invite (one had expired and was not
        swept yet): the database keeps the second from being a second
        open invite, and it is refused."""
        ana = _inviter()
        old = _invite(ana, "ben@example.com")
        later = NOW + timedelta(days=31)
        first = _invite(ana, "cy@example.com", later)
        assert _db.session.get(Invite, old.id).state == "expired"
        monkeypatch.setattr(referrals, "open_invite", lambda _uid: None)
        refused = _refused(ana, "dee@example.com", later)
        assert (refused.status, refused.message) == (
            409, referrals.OPEN_ALREADY)
        assert [i.id for i in Invite.query.filter_by(state="open")] == [
            first.id]

    def test_the_database_holds_one_open_and_one_used_a_month(
            self, app):  # noqa: F811
        from sqlalchemy.exc import IntegrityError
        ana = _inviter()

        def add(state, month, code):
            _db.session.add(Invite(inviter_id=ana.id, code=code, kind="email",
                                   month=month, state=state,
                                   expires_at=NOW + timedelta(days=1)))
            _db.session.commit()

        for state, month, code in (("withdrawn", "2026-11", "w" * 16),
                                   ("withdrawn", "2026-11", "x" * 16),
                                   ("used", "2026-10", "u" * 16),
                                   ("used", "2026-11", "v" * 16),
                                   ("open", "2026-11", "o" * 16)):
            add(state, month, code)
        for state, month, code in (("open", "2026-12", "p" * 16),
                                   ("used", "2026-11", "q" * 16)):
            with pytest.raises(IntegrityError):
                add(state, month, code)
            _db.session.rollback()


# ── The sign-up that uses it ─────────────────────────────────────────────

def _send(client, email, invite):
    sent = {}
    with patch("backend.routes.auth.send_magic_link_email",
               lambda e, url, **_kw: sent.update(url=url)):
        r = client.post("/auth/magic-link/send",
                        json={"email": email, "invite": invite})
    return r, "/" + sent["url"].split("://", 1)[1].split("/", 1)[1]


def _x_signup(app, code, x_id, screen_name, client=None):  # noqa: F811
    client = client or app.test_client()
    with patch("backend.routes.auth.twitter", _x(x_id, screen_name,
                                                 authorized=False)):
        client.get(f"/auth/login?invite={code}")
    with patch("backend.routes.auth.twitter", _x(x_id, screen_name)):
        r = client.get("/auth/login")
    assert r.status_code == 302
    return User.query.filter_by(twitter_id=str(x_id)).first()


def _x_answer(app, client, code, x_id, screen_name):  # noqa: F811
    """The X round trip with *code*; the answer to its return."""
    with patch("backend.routes.auth.twitter", _x(x_id, screen_name,
                                                 authorized=False)):
        client.get(f"/auth/login?invite={code}")
    with patch("backend.routes.auth.twitter", _x(x_id, screen_name)):
        return client.get("/auth/login")


def _open(to):
    ana = _inviter()
    return ana, referrals.create_invite(ana, to)


class TestTheSignUp:
    def test_the_named_address_in_any_browser(self, signup_app):  # noqa: F811
        """Fred follows the link on his laptop and opens the mail on his
        phone: recorded (no same-browser rule: the invite names him)."""
        ana, invite = _open("Fred@Example.com")
        _r, link = _send(signup_app.test_client(), "fred@example.com",
                         invite.code)
        assert signup_app.test_client().get(link).status_code == 302
        fred = User.query.filter_by(email="fred@example.com").one()
        ref = Referral.query.one()
        assert (ref.inviter_id, ref.friend_id, ref.invite_id) == (
            ana.id, fred.id, invite.id)
        invite = _db.session.get(Invite, invite.id)
        assert (invite.state, invite.target) == ("used", None)
        assert not fred.approved

    def test_the_named_x_account(self, signup_app):  # noqa: F811
        ana, invite = _open("@fred_x")
        fred = _x_signup(signup_app, invite.code, 4242, "Fred_X")
        ref = Referral.query.one()
        assert (ref.inviter_id, ref.friend_id) == (ana.id, fred.id)
        assert not fred.approved

    def test_an_existing_account_is_never_brought(self, signup_app):  # noqa: F811
        old = User(username="old", email="fred@example.com", approved=True,
                   last_seen_at=datetime.utcnow())
        _db.session.add(old)
        _db.session.commit()
        _ana, invite = _open("fred@example.com")
        client = signup_app.test_client()
        _r, link = _send(client, "fred@example.com", invite.code)
        client.get(link)
        assert Referral.query.count() == 0
        assert _db.session.get(Invite, invite.id).state == "open"

    def test_an_account_set_up_ahead_at_its_first_sign_in(self, signup_app):  # noqa: F811
        """A pre-filled account nobody signed in to yet counts as new."""
        pre = User(username="fred_x", twitter_id="4242",
                   prefilled_handle="fred_x", approved=True)
        _db.session.add(pre)
        _db.session.commit()
        ana, invite = _open("@fred_x")
        _x_signup(signup_app, invite.code, 4242, "fred_x")
        ref = Referral.query.one()
        assert (ref.inviter_id, ref.friend_id) == (ana.id, pre.id)

    def test_a_deleted_inviter_records_nothing(self, signup_app):  # noqa: F811
        ana, invite = _open("fred@example.com")
        ana.deleted_at = datetime.utcnow()
        _db.session.commit()
        client = signup_app.test_client()
        _r, link = _send(client, "fred@example.com", invite.code)
        client.get(link)
        assert Referral.query.count() == 0

    def test_the_first_inviter_is_kept(self, signup_app):  # noqa: F811
        pre = User(username="fred_x", twitter_id="4242",
                   prefilled_handle="fred_x", approved=True)
        _db.session.add(pre)
        bea = _inviter("bea")
        _db.session.add(Referral(inviter_id=bea.id, friend_id=pre.id))
        _db.session.commit()
        _ana, invite = _open("@fred_x")
        _x_signup(signup_app, invite.code, 4242, "fred_x")
        assert Referral.query.one().inviter_id == bea.id
        assert _db.session.get(Invite, invite.id).state == "open"

    def test_no_answer_says_whether_a_code_is_anyones(self, signup_app):  # noqa: F811
        """The same answers for a code that is open, used, for someone
        else, unknown, or none."""
        _ana, invite = _open("fred@example.com")
        used = Invite(inviter_id=_inviter("bea").id, code="UsedUsedUsed1234",
                      kind="email", month="2026-11", state="used",
                      expires_at=datetime.utcnow() + timedelta(days=1))
        _db.session.add(used)
        _db.session.commit()
        sends, logins = [], []
        for code in (invite.code, used.code, "Zz" * 8, None):
            client = signup_app.test_client()
            r, _link = _send(client, "vic@example.com", code)
            sends.append((r.status_code, r.get_json()))
            with patch("backend.routes.auth.twitter",
                       _x(1, "vic", authorized=False)):
                r = signup_app.test_client().get(
                    f"/auth/login?invite={code}" if code else "/auth/login")
            logins.append((r.status_code, r.headers.get("Location")))
        assert len(set(map(repr, sends))) == 1
        assert len(set(map(repr, logins))) == 1

    def test_nothing_while_off(self, signup_app):  # noqa: F811
        _ana, invite = _open("fred@example.com")
        signup_app.config["REFERRALS_ENABLED"] = False
        client = signup_app.test_client()
        _r, link = _send(client, "fred@example.com", invite.code)
        client.get(link)
        assert Referral.query.count() == 0


# ── A sign-in as someone else (Peter, 2026-11-13) ───────────────────────
# "They should know the invitation was for somebody else. They should still
# have the option to sign up without the invitation, thus also without
# reward." The sign-in is held, nothing is made, until they choose.

MISMATCH = f"{FRONTEND_URL}/invite-mismatch?for="


def _signed_in_as(client):
    with client.session_transaction() as sess:
        return sess.get("_user_id")


class TestSomeoneElse:
    def test_another_address_is_told_and_nothing_is_made(self, signup_app):  # noqa: F811
        """Vic follows Fred's link and signs in with his own address: the
        notice (an email address was named, not which), no account, no
        referral."""
        _ana, invite = _open("fred@example.com")
        client = signup_app.test_client()
        _r, link = _send(client, "vic@example.com", invite.code)
        r = client.get(link)
        assert (r.status_code, r.headers["Location"]) == (
            302, MISMATCH + "email")
        assert User.query.filter_by(email="vic@example.com").first() is None
        assert Referral.query.count() == 0 and _signed_in_as(client) is None

    def test_continue_without_the_invitation(self, signup_app):  # noqa: F811
        """Vic goes on: his account is made with no inviter, and the invite
        stays Fred's, who then signs up through it."""
        ana, invite = _open("fred@example.com")
        client = signup_app.test_client()
        _r, link = _send(client, "vic@example.com", invite.code)
        client.get(link)
        r = client.post("/auth/invite-choice/continue", json={})
        assert r.status_code == 200
        assert r.get_json() == {"redirect": f"{FRONTEND_URL}/dashboard"}
        vic = User.query.filter_by(email="vic@example.com").one()
        assert _signed_in_as(client) == str(vic.id) and not vic.approved
        assert Referral.query.count() == 0
        invite = _db.session.get(Invite, invite.id)
        assert (invite.state, invite.target) == ("open", "fred@example.com")
        fred_client = signup_app.test_client()
        _r, link = _send(fred_client, "fred@example.com", invite.code)
        fred_client.get(link)
        assert Referral.query.one().inviter_id == ana.id

    def test_sign_in_differently_leaves_nothing_behind(self, signup_app):  # noqa: F811
        """The right person used the wrong address by accident: "Sign in
        differently" leaves no account and nothing held; the right
        address then redeems the invite."""
        ana, invite = _open("fred@example.com")
        client = signup_app.test_client()
        _r, link = _send(client, "fred.work@example.com", invite.code)
        client.get(link)
        assert client.post("/auth/invite-choice/cancel",
                           json={}).status_code == 200
        assert User.query.count() == 1   # Ana only
        assert client.post("/auth/invite-choice/continue",
                           json={}).status_code == 410
        assert User.query.count() == 1
        _r, link = _send(client, "fred@example.com", invite.code)
        assert client.get(link).headers["Location"] == \
            f"{FRONTEND_URL}/dashboard"
        assert Referral.query.one().inviter_id == ana.id

    def test_another_x_account(self, signup_app):  # noqa: F811
        """The same with X: told (an X account was named), nothing made;
        continuing makes the account, with no inviter."""
        _ana, invite = _open("@fred_x")
        client = signup_app.test_client()
        r = _x_answer(signup_app, client, invite.code, 77, "mallory")
        assert r.headers["Location"] == MISMATCH + "x"
        assert User.query.filter_by(twitter_id="77").first() is None
        r = client.post("/auth/invite-choice/continue", json={})
        mallory = User.query.filter_by(twitter_id="77").one()
        assert r.get_json()["redirect"] == f"{FRONTEND_URL}/dashboard"
        assert _signed_in_as(client) == str(mallory.id)
        assert Referral.query.count() == 0
        assert _db.session.get(Invite, invite.id).state == "open"

    def test_another_x_account_signs_in_differently(self, signup_app):  # noqa: F811
        _ana, invite = _open("@fred_x")
        client = signup_app.test_client()
        _x_answer(signup_app, client, invite.code, 77, "fred_x_old")
        client.post("/auth/invite-choice/cancel", json={})
        assert User.query.filter_by(twitter_id="77").first() is None
        assert _x_signup(signup_app, invite.code, 4242, "fred_x", client)
        assert Referral.query.count() == 1

    def test_an_email_sign_in_with_an_x_invite_is_told_which_kind(
            self, signup_app):  # noqa: F811
        _ana, invite = _open("@fred")
        client = signup_app.test_client()
        _r, link = _send(client, "fred@example.com", invite.code)
        assert client.get(link).headers["Location"] == MISMATCH + "x"

    @pytest.mark.parametrize("state", ["used", "withdrawn", "expired",
                                       "unknown"])
    def test_a_code_that_is_not_open_says_nothing(self, signup_app, state):  # noqa: F811
        """As before: no notice, the sign-up goes on with no inviter."""
        _ana, invite = _open("fred@example.com")
        code = invite.code
        if state == "unknown":
            code = "Zz" * 8
        elif state == "expired":
            invite.expires_at = datetime.utcnow() - timedelta(minutes=1)
        else:
            invite.state = state
        _db.session.commit()
        client = signup_app.test_client()
        _r, link = _send(client, "vic@example.com", code)
        assert client.get(link).headers["Location"] == \
            f"{FRONTEND_URL}/dashboard"
        assert User.query.filter_by(email="vic@example.com").one()
        assert Referral.query.count() == 0

    def test_an_existing_account_signs_in_as_always(self, signup_app):  # noqa: F811
        """Control: no notice for a sign-in into an account that is not new
        (an invite never applies to it)."""
        old = User(username="old", email="vic@example.com", approved=True,
                   last_seen_at=datetime.utcnow())
        _db.session.add(old)
        _db.session.commit()
        _ana, invite = _open("fred@example.com")
        client = signup_app.test_client()
        _r, link = _send(client, "vic@example.com", invite.code)
        assert client.get(link).headers["Location"] == \
            f"{FRONTEND_URL}/dashboard"
        assert _signed_in_as(client) == str(old.id)

    def test_a_held_sign_in_lasts_as_long_as_a_sign_in_link(
            self, signup_app):  # noqa: F811
        _ana, invite = _open("fred@example.com")
        client = signup_app.test_client()
        _r, link = _send(client, "vic@example.com", invite.code)
        client.get(link)
        with client.session_transaction() as sess:
            held = dict(sess["invite_choice"])
            held["at"] -= signup_app.config.get(
                "MAGIC_LINK_EXPIRY_SECONDS", 900) + 1
            sess["invite_choice"] = held
            assert "fred" not in repr(held)   # the named address is not in it
        r = client.post("/auth/invite-choice/continue", json={})
        assert r.status_code == 410
        assert User.query.filter_by(email="vic@example.com").first() is None


# ── The reward, the admin list, the account deletion ────────────────────

class TestAfterTheSignUp:
    def test_an_invited_friends_first_payment(self, app, gateway):  # noqa: F811
        """The flat credit for both, as for any referral."""
        ana = _inviter(stripe_customer_id="cus_i")
        invite = referrals.create_invite(ana, "fred@example.com")
        fred = _user("fred", stripe_customer_id="cus_f",
                     stripe_subscription_id="sub_f",
                     subscription_status="trialing",
                     subscription_level="standard")
        assert referrals.redeem_invite(fred, invite.code, True,
                                       email="fred@example.com")
        r = _post_event(app, _event("invoice.paid", _paid(gateway)))
        assert r.status_code == 200
        credits = {t["customer"]: t["amount"] for t in gateway.credits()}
        assert credits == {"cus_i": -750, "cus_f": -750}

    def test_the_admin_list_has_each_invites_state_not_whom(self, app):  # noqa: F811
        ana = _inviter()
        open_one = referrals.create_invite(ana, "secret@example.com")
        admin = _client(app, _user("root", is_admin=True))
        rows = admin.get("/api/admin/referrals").get_json()["referrals"]
        assert [(r["inviter"], r["invite_state"], r["invite_kind"])
                for r in rows] == [("ana", "open", "email")]
        assert "secret" not in repr(rows)
        assert open_one.code not in repr(rows)

    def test_the_inviters_deletion_ends_its_invites(self, app):  # noqa: F811
        """The purge withdraws an open invite and deletes every handle or
        address; the rows stay without the account."""
        from backend.utils.account_deletion import (
            delete_identity, references_to_user)
        ana = _inviter()
        fred = _user("fred")
        used = referrals.create_invite(ana, "fred@example.com", now=NOW)
        assert referrals.redeem_invite(fred, used.code, True, now=NOW,
                                       email="fred@example.com")
        open_one = referrals.create_invite(ana, "ben@example.com",
                                           now=NOW + timedelta(days=20))
        ana.deleted_at = datetime.utcnow()
        _db.session.commit()
        delete_identity(ana.id)
        _db.session.commit()
        for invite in (used, open_one):
            row = _db.session.get(Invite, invite.id)
            assert row.inviter_id is None and row.target is None
        assert _db.session.get(Invite, open_one.id).state == "withdrawn"
        assert _db.session.get(Invite, used.id).state == "used"
        assert references_to_user(ana.id) == {}

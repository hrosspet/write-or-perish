"""An invited person who signs in by email reaches the account Loore
prepared for them (#714): Connect X with the X id of a prepared account
offers to open it, the empty email account is deleted on the way, and a
trial started on the empty account by mistake does not cost the prepared
account its trial.

Hard requirement (the brief of #714): moving the person into the prepared
account needs the X proof, a completed X authorization for that X id in
this browser session. An email address or a handle never opens one.

The payments test app (test_payments.py: Stripe replaced by a stub gateway,
webhook events signed here) with the sign-in routes and the real
flask-dance X blueprint added. X itself is never called: the callback's
verify_credentials is a stub, or (in the round trip) only the HTTP
exchange with X is faked.
"""
import sys
import time
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from flask import g

# Other test files swap flask_login (and more) for MagicMocks when they are
# collected: import the real ones (as test_x_login_linking.py does).
for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login  # noqa: E402
import backend.models as _real_backend_models  # noqa: E402
from backend.extensions import db as _db  # noqa: E402
from backend.models import (  # noqa: E402
    Invite, Node, Referral, TrialCard, User, UserDataPurge)
from backend.tests import test_payments as tp  # noqa: E402
from backend.tests.test_trial_card import (  # noqa: E402
    CardGateway, _card, _created, _fresh, _trial)
from backend.utils import prepared_accounts, stripe_billing, trial_cards  # noqa: E402

FRONTEND = "https://loore.test"
X_ID = "4242"
HANDLE = "anna_on_x"
ANNA_EMAIL = "anna@example.com"


@pytest.fixture
def gateway(monkeypatch):
    gw = CardGateway()
    monkeypatch.setattr(stripe_billing, "get_gateway", lambda *a, **k: gw)
    stripe_billing._price_cache.clear()
    for level in ("standard", "supporter", "discounted"):
        gw.prices.append({
            "id": f"price_{level}", "active": True,
            "lookup_key": stripe_billing.PRICE_LOOKUP_KEYS[level]})
    return gw


@pytest.fixture
def notices():
    """The "X is now connected" notices sent (the app fixture replaces the
    mail on the auth module it imports)."""
    return []


def _affected(k):
    return (k == "flask_login" or k.startswith("backend.routes")
            or k == "backend.models" or k == "backend.utils.privacy")


@pytest.fixture
def app(gateway, notices, monkeypatch):
    # The routes are imported fresh against the real flask_login (another
    # test file may have left a MagicMock there), and everything is put back
    # afterwards, the packages' attributes included: the teardown of
    # test_detached_prompt_stays_agentic.py (this file sorts before
    # test_privacy_utils.py, whose patches resolve through those attributes).
    saved = {k: sys.modules[k] for k in list(sys.modules) if _affected(k)}
    sys.modules["flask_login"] = _real_flask_login
    sys.modules["backend.models"] = _real_backend_models
    for k in [k for k in list(sys.modules)
              if k.startswith("backend.routes") or k == "backend.utils.privacy"]:
        del sys.modules[k]

    import backend.routes.auth as auth_mod
    monkeypatch.setattr(auth_mod, "send_x_connected_notice",
                        lambda to, handle: notices.append((to, handle)))
    app = tp._make_app(payments=True)
    app.config["TWITTER_API_KEY"] = "fake"
    app.config["TWITTER_API_SECRET"] = "fake"
    app.config["MAGIC_LINK_EXPIRY_SECONDS"] = 900
    app.config["FRONTEND_URL"] = FRONTEND
    from backend.oauth import init_twitter_blueprint
    app.register_blueprint(auth_mod.auth_bp, url_prefix="/auth")
    init_twitter_blueprint(app)
    ctx = app.app_context()
    ctx.push()
    _db.create_all()
    yield app
    _db.session.remove()
    _db.drop_all()
    ctx.pop()

    current = [k for k in list(sys.modules) if _affected(k)]
    for k in current:
        if k not in saved:
            del sys.modules[k]
    for k, mod in saved.items():
        sys.modules[k] = mod
    for k in set(current) | set(saved):
        pkg_name, _, attr = k.rpartition(".")
        pkg = sys.modules.get(pkg_name)
        if pkg is None or not attr:
            continue
        if k in saved:
            setattr(pkg, attr, saved[k])
        elif hasattr(pkg, attr):
            delattr(pkg, attr)


# ── The world ───────────────────────────────────────────────────────────

def _prepared(**kw):
    """An account set up ahead of its owner for X_ID: nobody signed in."""
    kw.setdefault("username", "anna_x")
    kw.setdefault("approved", True)
    user = User(twitter_id=X_ID, prefilled_handle=HANDLE,
                prefilled_at=datetime.utcnow(), **kw)
    _db.session.add(user)
    _db.session.commit()
    return user


def _email_account(**kw):
    """The second, empty account an email sign-in made."""
    kw.setdefault("username", "anna")
    kw.setdefault("email", ANNA_EMAIL)
    kw.setdefault("approved", True)
    user = User(last_seen_at=datetime.utcnow(), **kw)
    _db.session.add(user)
    _db.session.commit()
    return user


def _client(app, user):
    g.pop("_login_user", None)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return client


def _call(client, method, path, **kw):
    # One app context for the whole test: Flask-Login's per-request cache
    # on g would carry the last request's user into the next one.
    g.pop("_login_user", None)
    return getattr(client, method)(path, **kw)


def _session(client):
    with client.session_transaction() as sess:
        return dict(sess)


def _x_token(client, x_id):
    """What X's authorization leaves in the session: flask-dance's token,
    carrying the X user id X gave with it."""
    with client.session_transaction() as sess:
        sess["twitter_oauth_token"] = {
            "oauth_token": "t", "oauth_token_secret": "s",
            "user_id": str(x_id), "screen_name": HANDLE}
        # The mark of a return from X that #705 sets (its /auth/login signs
        # in from a stored token only then); unused without #705.
        sess["x_sign_in_authorized"] = True


def _callback(client, x_id, handle=HANDLE):
    x = MagicMock()
    x.authorized = True
    x.get.return_value = MagicMock(
        ok=True, status_code=200,
        json=lambda: {"id": int(x_id), "screen_name": handle})
    g.pop("_login_user", None)
    with patch("backend.routes.auth.twitter", x):
        return client.get("/auth/login")


def _connect(app, user, x_id=X_ID, handle=HANDLE):
    """Connect X from the subscribe page's "Sign in with X": the start,
    X's authorization, the callback."""
    client = _client(app, user)
    start = _call(client, "get", "/auth/x/connect")
    assert start.status_code == 302
    _x_token(client, x_id)
    return client, _callback(client, x_id, handle)


def _signed_in_id(client):
    uid = _session(client).get("_user_id")
    return int(uid) if uid is not None else None


def _open(client, delete=True):
    return _call(client, "post", "/auth/prepared-account/open",
                 json={"delete_this_account": delete})


# ── Connect X finds the prepared account ────────────────────────────────

class TestTheQuestion:
    def test_connect_x_asks_instead_of_attaching_the_x_id(self, app):
        anna_x = _prepared()
        anna = _email_account()

        client, resp = _connect(app, anna)

        assert resp.status_code == 302
        assert resp.headers["Location"] == f"{FRONTEND}/prepared-account"
        _db.session.expire_all()
        assert _fresh(anna).twitter_id is None
        assert _fresh(anna_x).twitter_id == X_ID
        sess = _session(client)
        assert sess.get("_user_id") == str(anna.id)       # nothing moved yet
        assert sess["twitter_oauth_token"]["user_id"] == X_ID  # the proof
        assert sess[prepared_accounts.OFFER_SESSION_KEY]["prepared_id"] == anna_x.id

        q = _call(client, "get", "/auth/prepared-account").get_json()
        assert q == {"handle": HANDLE, "email": ANNA_EMAIL, "deleted": True,
                     "stays_because": None, "email_moves": True,
                     "email_stays": False, "trial_ends": False}

    def test_an_account_with_writing_stays(self, app):
        _prepared()
        anna = _email_account()
        _db.session.add(Node(user_id=anna.id, content="", node_type="user"))
        _db.session.commit()
        client, _ = _connect(app, anna)
        q = _call(client, "get", "/auth/prepared-account").get_json()
        assert q["deleted"] is False and q["stays_because"] == "writing"
        assert q["email_moves"] is False

    @pytest.mark.parametrize("kw", [
        {"plan": "pro"},                                   # set by hand
        {"alpha_free_granted_at": datetime(2026, 11, 1)},  # an alpha tester
        {"is_admin": True},
    ])
    def test_an_account_set_up_by_hand_stays(self, app, kw):
        _prepared()
        anna = _email_account(**kw)
        client, _ = _connect(app, anna)
        q = _call(client, "get", "/auth/prepared-account").get_json()
        assert q["deleted"] is False and q["stays_because"] == "kept"

    def test_payments_off_a_waiting_email_account_is_deleted_too(self, app):
        """Payments off, a new account gets the "alpha" plan and waits for
        approval: that plan is no plan set by hand."""
        app.config["PAYMENTS_ENABLED"] = False
        anna_x = _prepared()
        anna = _email_account(approved=False)
        assert anna.plan == "alpha"
        client, _ = _connect(app, anna)
        q = _call(client, "get", "/auth/prepared-account").get_json()
        assert q["deleted"] is True and q["stays_because"] is None

        assert _open(client).status_code == 200

        assert _signed_in_id(client) == anna_x.id
        assert _fresh(anna).deleted_at is not None
        assert _fresh(anna_x).email == ANNA_EMAIL

    def test_an_account_that_paid_stays(self, app):
        _prepared()
        anna = _email_account(subscription_status="active",
                              subscription_ever_paid=True)
        client, _ = _connect(app, anna)
        q = _call(client, "get", "/auth/prepared-account").get_json()
        assert q["deleted"] is False and q["stays_because"] == "paid"

    @pytest.mark.parametrize("holder_kw", [
        {"deleted_at": "now"},                          # being deleted
        {"approved": False, "deactivated_at": "now"},   # blocked by the admin
    ])
    def test_nothing_to_open_keeps_the_old_answer(self, app, holder_kw):
        kw = {k: (datetime.utcnow() if v == "now" else v)
              for k, v in holder_kw.items()}
        _prepared(**kw)
        anna = _email_account()
        client, resp = _connect(app, anna)
        assert resp.headers["Location"] == \
            f"{FRONTEND}/account?x_login=taken_placeholder#x"
        sess = _session(client)
        assert prepared_accounts.OFFER_SESSION_KEY not in sess
        assert "twitter_oauth_token" not in sess

    def test_an_account_someone_signed_in_to_is_not_prepared(self, app):
        _prepared(last_seen_at=datetime.utcnow())
        anna = _email_account()
        _, resp = _connect(app, anna)
        assert resp.headers["Location"] == f"{FRONTEND}/account?x_login=taken#x"

    def test_no_prepared_counterpart_connects_as_before(self, app, notices):
        """The control: Connect X works as it did."""
        anna = _email_account()
        client, resp = _connect(app, anna)
        assert resp.headers["Location"] == f"{FRONTEND}/account?x_login=linked#x"
        anna = _fresh(anna)
        assert anna.twitter_id == X_ID and anna.twitter_handle == HANDLE
        assert User.query.count() == 1
        assert notices == [(ANNA_EMAIL, HANDLE)]
        assert prepared_accounts.OFFER_SESSION_KEY not in _session(client)


# ── Opening it ──────────────────────────────────────────────────────────

class TestOpening:
    def test_the_person_ends_in_the_prepared_account(self, app, notices):
        anna_x = _prepared()
        anna = _email_account()
        client, _ = _connect(app, anna)

        resp = _open(client)

        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["redirect"] == f"{FRONTEND}/dashboard"
        assert _signed_in_id(client) == anna_x.id
        prepared = _fresh(anna_x)
        # Signed in as Sign in with X would: the handle, the sign-in record.
        assert prepared.twitter_handle == HANDLE
        assert prepared.last_seen_at is not None
        # The address goes with the person, and is told.
        assert prepared.email == ANNA_EMAIL
        assert notices == [(ANNA_EMAIL, HANDLE)]
        # The empty account is deleted the usual way: 30 days.
        empty = _fresh(anna)
        assert empty.email is None and empty.twitter_id is None
        assert empty.deleted_at is not None
        job = UserDataPurge.query.filter_by(user_id=anna.id).one()
        assert job.delete_account and job.source == "self"
        assert job.scheduled_for > datetime.utcnow() + timedelta(days=29)
        sess = _session(client)
        assert prepared_accounts.OFFER_SESSION_KEY not in sess
        # The X token is the prepared account's sign-in now.
        assert sess["twitter_oauth_token"]["user_id"] == X_ID

    def test_afterwards_the_address_signs_in_to_the_prepared_account(self, app):
        anna_x = _prepared()
        anna = _email_account()
        client, _ = _connect(app, anna)
        _open(client)
        assert User.query.filter_by(email=ANNA_EMAIL).one().id == anna_x.id

    def test_an_account_with_writing_stays_as_it_is(self, app, notices):
        anna_x = _prepared()
        anna = _email_account()
        _db.session.add(Node(user_id=anna.id, content="", node_type="user"))
        _db.session.commit()
        client, _ = _connect(app, anna)

        resp = _open(client, delete=False)

        assert resp.status_code == 200
        assert _signed_in_id(client) == anna_x.id
        empty = _fresh(anna)
        assert empty.deleted_at is None and empty.email == ANNA_EMAIL
        assert _fresh(anna_x).email is None
        assert UserDataPurge.query.count() == 0
        assert notices == []

    def test_the_prepared_accounts_own_address_stays(self, app, notices):
        anna_x = _prepared(email="invited@example.com")
        anna = _email_account()
        client, _ = _connect(app, anna)
        q = _call(client, "get", "/auth/prepared-account").get_json()
        assert q["deleted"] is True and q["email_moves"] is False

        assert _open(client).status_code == 200

        assert _fresh(anna_x).email == "invited@example.com"
        empty = _fresh(anna)
        assert empty.deleted_at is not None and empty.email == ANNA_EMAIL
        assert notices == []

    def test_a_change_since_the_question_is_refused(self, app):
        """The page said "deleted"; then the account got an entry."""
        anna_x = _prepared()
        anna = _email_account()
        client, _ = _connect(app, anna)
        _db.session.add(Node(user_id=anna.id, content="", node_type="user"))
        _db.session.commit()

        resp = _open(client, delete=True)

        assert resp.status_code == 409 and resp.get_json()["code"] == "changed"
        assert _signed_in_id(client) == anna.id
        assert _fresh(anna).deleted_at is None
        assert _fresh(anna_x).email is None
        # The question is still there, to be read again.
        assert _call(client, "get", "/auth/prepared-account").status_code == 200

    def test_someone_signed_in_to_it_meanwhile(self, app):
        anna_x = _prepared()
        anna = _email_account()
        client, _ = _connect(app, anna)
        anna_x.last_seen_at = datetime.utcnow()
        _db.session.commit()

        resp = _open(client)

        assert resp.status_code == 409
        assert resp.get_json()["code"] == "not_prepared"
        assert _signed_in_id(client) == anna.id
        assert _fresh(anna).deleted_at is None
        sess = _session(client)
        assert prepared_accounts.OFFER_SESSION_KEY not in sess
        assert "twitter_oauth_token" not in sess

    def test_staying_forgets_the_offer_and_the_x_token(self, app):
        anna_x = _prepared()
        anna = _email_account()
        client, _ = _connect(app, anna)

        resp = _call(client, "post", "/auth/prepared-account/stay")

        assert resp.status_code == 200
        sess = _session(client)
        assert sess.get("_user_id") == str(anna.id)
        assert prepared_accounts.OFFER_SESSION_KEY not in sess
        assert "twitter_oauth_token" not in sess
        assert _fresh(anna).twitter_id is None
        assert _fresh(anna_x).last_seen_at is None
        assert _open(client).status_code == 410

    def test_who_invited_the_person_goes_with_them(self, app):
        anna_x = _prepared()
        anna = _email_account()
        inviter = User(username="ben", approved=True)
        _db.session.add(inviter)
        _db.session.flush()
        invite = Invite(inviter_id=inviter.id, code="CODE1", kind="email",
                        target=ANNA_EMAIL, state="used", month="2026-11",
                        expires_at=datetime.utcnow() + timedelta(days=7))
        _db.session.add(invite)
        _db.session.flush()
        _db.session.add(Referral(inviter_id=inviter.id, friend_id=anna.id,
                                 invite_id=invite.id))
        _db.session.commit()
        client, _ = _connect(app, anna)

        assert _open(client).status_code == 200

        assert Referral.query.one().friend_id == anna_x.id

    def test_logout_drops_the_offer(self, app):
        _prepared()
        anna = _email_account()
        client, _ = _connect(app, anna)
        _call(client, "get", "/auth/logout")
        assert prepared_accounts.OFFER_SESSION_KEY not in _session(client)


# ── A trial started on the empty account by mistake (part 3) ─────────────

def _trial_on(app, gw, user, customer, sub_id, pm_id, fingerprint, event_id):
    user.stripe_customer_id = customer
    _db.session.commit()
    _card(gw, pm_id, fingerprint)
    _trial(gw, sub_id, customer, pm_id)
    _created(app, sub_id, event_id)


class TestTrial:
    def test_the_prepared_account_can_still_start_a_trial(self, app, gateway):
        anna_x = _prepared()
        anna = _email_account()
        _trial_on(app, gateway, anna, "cus_e", "sub_e", "pm_e", "fp_card_1",
                  "evt_1")
        assert _fresh(anna).subscription_status == "trialing"
        assert TrialCard.query.one().user_id == anna.id
        client, _ = _connect(app, anna)
        q = _call(client, "get", "/auth/prepared-account").get_json()
        assert q["deleted"] is True and q["trial_ends"] is True

        assert _open(client).status_code == 200

        # The empty account's trial ended at Stripe, with nothing charged,
        # marked as Loore's (not the owner's cancellation).
        sub_e = gateway.subscriptions["sub_e"]
        assert sub_e["status"] == "canceled"
        assert sub_e["metadata"][stripe_billing.DUPLICATE_METADATA_KEY] == "1"
        assert "create_refund" not in gateway.names()
        assert _fresh(anna).subscription_status == "ended"
        # The card's mark moved with the person.
        assert TrialCard.query.one().user_id == anna_x.id

        # The prepared account's checkout offers the trial, and it starts
        # with the same card.
        params = stripe_billing.checkout_session_params(
            _fresh(anna_x), "standard", "price_standard")
        assert params["subscription_data"]["trial_period_days"] > 0
        _trial_on(app, gateway, _fresh(anna_x), "cus_p", "sub_p", "pm_p",
                  "fp_card_1", "evt_2")
        assert gateway.subscriptions["sub_p"]["status"] == "trialing"
        prepared = _fresh(anna_x)
        assert prepared.subscription_status == "trialing"
        assert prepared.subscription_trial_card_refused_at is None

    def test_without_the_move_the_card_has_had_its_trial(self, app, gateway):
        """The control for the test above: an email account that stays
        (it holds writing) keeps its trial and its card's mark, so the
        prepared account's trial with that card is refused, as #697 says."""
        anna_x = _prepared()
        anna = _email_account()
        _db.session.add(Node(user_id=anna.id, content="", node_type="user"))
        _db.session.commit()
        _trial_on(app, gateway, anna, "cus_e", "sub_e", "pm_e", "fp_card_1",
                  "evt_1")
        client, _ = _connect(app, anna)
        assert _open(client, delete=False).status_code == 200
        assert gateway.subscriptions["sub_e"]["status"] == "trialing"

        _trial_on(app, gateway, _fresh(anna_x), "cus_p", "sub_p", "pm_p",
                  "fp_card_1", "evt_2")
        assert gateway.subscriptions["sub_p"]["status"] == "canceled"
        assert _fresh(anna_x).subscription_trial_card_refused_at is not None

    def test_stripe_down_changes_nothing(self, app, gateway):
        anna_x = _prepared()
        anna = _email_account()
        _trial_on(app, gateway, anna, "cus_e", "sub_e", "pm_e", "fp_card_1",
                  "evt_1")
        client, _ = _connect(app, anna)
        gateway.fail = True

        resp = _open(client)

        assert resp.status_code == 503
        assert _signed_in_id(client) == anna.id
        empty = _fresh(anna)
        assert empty.deleted_at is None and empty.email == ANNA_EMAIL
        assert empty.subscription_status == "trialing"
        assert TrialCard.query.one().user_id == anna.id
        assert _fresh(anna_x).email is None
        assert UserDataPurge.query.count() == 0

        gateway.fail = False   # a retry finishes it
        assert _open(client).status_code == 200
        assert _signed_in_id(client) == anna_x.id
        assert TrialCard.query.one().user_id == anna_x.id

    def test_a_trial_already_cancelled_at_stripe_is_only_recorded(
            self, app, gateway):
        anna_x = _prepared()
        anna = _email_account()
        _trial_on(app, gateway, anna, "cus_e", "sub_e", "pm_e", "fp_card_1",
                  "evt_1")
        client, _ = _connect(app, anna)
        gateway.subscriptions["sub_e"]["status"] = "canceled"

        assert _open(client).status_code == 200

        assert "cancel_subscription" not in gateway.names()
        assert _fresh(anna).subscription_status == "ended"
        assert TrialCard.query.one().user_id == anna_x.id

    @pytest.mark.parametrize("stripe_status", ["active", "past_due"])
    def test_a_trial_that_became_paid_at_stripe_stops_the_move(
            self, app, gateway, stripe_status):
        """The database still says trialing, Stripe already says the
        trial ended (its webhook is on its way): no trial to end with
        nothing charged. Nothing is deleted or moved; read again, the
        question says the account stays."""
        anna_x = _prepared()
        anna = _email_account()
        _trial_on(app, gateway, anna, "cus_e", "sub_e", "pm_e", "fp_card_1",
                  "evt_1")
        client, _ = _connect(app, anna)
        q = _call(client, "get", "/auth/prepared-account").get_json()
        assert q["deleted"] is True and q["trial_ends"] is True
        gateway.subscriptions["sub_e"]["status"] = stripe_status

        resp = _open(client)

        assert resp.status_code == 409 and resp.get_json()["code"] == "changed"
        assert _signed_in_id(client) == anna.id
        assert "cancel_subscription" not in gateway.names()
        sub_e = gateway.subscriptions["sub_e"]
        assert sub_e["status"] == stripe_status
        assert not sub_e.get("cancel_at_period_end")
        empty = _fresh(anna)
        assert empty.deleted_at is None and empty.email == ANNA_EMAIL
        assert empty.subscription_status == stripe_status
        assert TrialCard.query.one().user_id == anna.id
        assert _fresh(anna_x).email is None
        assert UserDataPurge.query.count() == 0
        q = _call(client, "get", "/auth/prepared-account").get_json()
        assert q["deleted"] is False and q["stays_because"] == "paid"
        # Open is then only the sign-in.
        assert _open(client, delete=False).status_code == 200
        assert _signed_in_id(client) == anna_x.id
        assert _fresh(anna).deleted_at is None

    def test_a_cancelled_subscription_that_charged_stops_the_move(
            self, app, gateway):
        anna = _email_account()
        _prepared()
        _trial_on(app, gateway, anna, "cus_e", "sub_e", "pm_e", "fp_card_1",
                  "evt_1")
        client, _ = _connect(app, anna)
        gateway.invoices["in_1"] = {"id": "in_1", "amount_paid": 3000,
                                    "payment_intent": "pi_1"}
        gateway.subscriptions["sub_e"].update(
            {"status": "canceled", "latest_invoice": "in_1"})

        resp = _open(client)

        assert resp.status_code == 409
        empty = _fresh(anna)
        assert empty.deleted_at is None and empty.subscription_ever_paid
        assert TrialCard.query.one().user_id == anna.id


# ── Accounts that stay ──────────────────────────────────────────────────

def _poll_answer(user):
    from backend.models import Poll, PollResponse
    poll = Poll(question="?")
    _db.session.add(poll)
    _db.session.flush()
    _db.session.add(PollResponse(poll_id=poll.id, user_id=user.id))


def _feedback(user):
    from backend.models import UserFeedback
    _db.session.add(UserFeedback(user_id=user.id, content=""))


def _invite(state):
    def add(user):
        _db.session.add(Invite(
            inviter_id=user.id, code=f"C-{state}", kind="email",
            target="bob@example.com", state=state, month="2026-11",
            expires_at=datetime.utcnow() + timedelta(days=20)))
    return add


def _referral_as_inviter(user):
    bob = User(username="bob", approved=True, last_seen_at=datetime.utcnow())
    _db.session.add(bob)
    _db.session.flush()
    _db.session.add(Referral(inviter_id=user.id, friend_id=bob.id))


class TestAccountsThatStay:
    @pytest.mark.parametrize("add", [
        _invite("open"), _invite("used"), _referral_as_inviter,
        _poll_answer, _feedback,
    ], ids=["open invite", "used invite", "referral as inviter",
            "poll answer", "feedback"])
    def test_invitations_answers_and_feedback_keep_the_account(
            self, app, add):
        anna_x = _prepared()
        anna = _email_account()
        add(anna)
        _db.session.commit()
        client, _ = _connect(app, anna)
        q = _call(client, "get", "/auth/prepared-account").get_json()
        assert q["deleted"] is False and q["stays_because"] == "used"

        assert _open(client, delete=False).status_code == 200

        assert _signed_in_id(client) == anna_x.id
        empty = _fresh(anna)
        assert empty.deleted_at is None and empty.email == ANNA_EMAIL
        assert _fresh(anna_x).email is None

    def test_a_withdrawn_or_expired_invite_does_not(self, app):
        _prepared()
        anna = _email_account()
        _invite("withdrawn")(anna)
        _invite("expired")(anna)
        _db.session.commit()
        client, _ = _connect(app, anna)
        q = _call(client, "get", "/auth/prepared-account").get_json()
        assert q["deleted"] is True

    @pytest.mark.parametrize("kw", [
        {"approved": False, "deactivated_at": "now"},   # blocked (#697)
        {"approved": True, "deactivated_at": "now"},    # held as not active
    ], ids=["blocked", "deactivated"])
    def test_an_account_the_admin_blocked_stays_with_its_card_mark(
            self, app, gateway, kw):
        anna_x = _prepared()
        anna = _email_account()
        _trial_on(app, gateway, anna, "cus_e", "sub_e", "pm_e", "fp_card_1",
                  "evt_1")
        empty = _fresh(anna)
        for k, v in kw.items():
            setattr(empty, k, datetime.utcnow() if v == "now" else v)
        _db.session.commit()
        client, _ = _connect(app, anna)
        q = _call(client, "get", "/auth/prepared-account").get_json()
        assert q["deleted"] is False and q["stays_because"] == "kept"
        # Asked to delete it anyway: refused, nothing moves.
        assert _open(client, delete=True).status_code == 409
        assert TrialCard.query.one().user_id == anna.id

        assert _open(client, delete=False).status_code == 200

        assert _signed_in_id(client) == anna_x.id
        empty = _fresh(anna)
        assert empty.deleted_at is None and empty.email == ANNA_EMAIL
        assert empty.subscription_status == "trialing"
        assert gateway.subscriptions["sub_e"]["status"] == "trialing"
        assert TrialCard.query.one().user_id == anna.id


# ── The X token never outlives the question ─────────────────────────────

class TestTheTokenGoesWithTheQuestion:
    def test_a_sign_in_starting_meanwhile_drops_it(self, app):
        """The question was left unanswered; later the browser comes
        through /auth/login (a new sign-in, an old tab): the token is gone
        before X is asked, so it signs nobody in."""
        anna_x = _prepared()
        anna = _email_account()
        client, _ = _connect(app, anna)
        assert "twitter_oauth_token" in _session(client)

        resp = _call(client, "get", "/auth/login")   # the real flask-dance

        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/auth/twitter")  # asks X
        sess = _session(client)
        assert "twitter_oauth_token" not in sess
        assert prepared_accounts.OFFER_SESSION_KEY not in sess
        assert _signed_in_id(client) == anna.id
        assert _fresh(anna_x).last_seen_at is None

    def test_an_expired_question_drops_it(self, app):
        _prepared()
        anna = _email_account()
        client, _ = _connect(app, anna)
        with client.session_transaction() as sess:
            data = dict(sess[prepared_accounts.OFFER_SESSION_KEY])
            data["at"] = time.time() - 901
            sess[prepared_accounts.OFFER_SESSION_KEY] = data

        assert _call(client, "get", "/auth/prepared-account").status_code == 410

        sess = _session(client)
        assert "twitter_oauth_token" not in sess
        assert prepared_accounts.OFFER_SESSION_KEY not in sess

    def test_connect_x_asks_again_afterwards(self, app):
        anna_x = _prepared()
        anna = _email_account()
        client, _ = _connect(app, anna)
        _call(client, "get", "/auth/login")          # the question is dropped
        assert _open(client).status_code == 410

        start = _call(client, "get", "/auth/x/connect")
        assert start.headers["Location"].endswith("/auth/twitter")
        _x_token(client, X_ID)
        resp = _callback(client, X_ID)
        assert resp.headers["Location"] == f"{FRONTEND}/prepared-account"
        assert _open(client).status_code == 200
        assert _signed_in_id(client) == anna_x.id


# ── A subscription that arrives for an account in deletion ──────────────

class TestSubscriptionForAnAccountInDeletion:
    def _in_deletion(self, **kw):
        from backend.utils.account_deletion import schedule_account_deletion
        user = _email_account(stripe_customer_id="cus_d", **kw)
        schedule_account_deletion(user, requested_by_id=user.id,
                                  source="self")
        return _fresh(user)

    def test_a_trial_checkout_finished_after_the_deletion_is_cancelled(
            self, app, gateway):
        user = self._in_deletion()
        _card(gateway, "pm_d", "fp_card_9")
        _trial(gateway, "sub_d", "cus_d", "pm_d")

        _created(app, "sub_d", "evt_d")

        sub = gateway.subscriptions["sub_d"]
        assert sub["status"] == "canceled"
        assert sub["metadata"][stripe_billing.DUPLICATE_METADATA_KEY] == "1"
        assert "create_refund" not in gateway.names()
        user = _fresh(user)
        assert user.deleted_at is not None
        assert user.subscription_status == "ended"
        assert not user.subscription_cancelled_by_owner
        assert TrialCard.query.count() == 0   # the card had no trial here

        # A restore during the 30 days finds no subscription.
        from backend.utils.account_deletion import restore_account
        assert restore_account(user)
        user = _fresh(user)
        assert user.deleted_at is None
        assert user.subscription_access_state == "none"

    def test_a_paid_one_is_refunded(self, app, gateway):
        user = self._in_deletion()
        sub = tp.subscription("sub_d", customer="cus_d", status="active",
                              latest_invoice="in_d")
        gateway.subscriptions["sub_d"] = sub
        gateway.invoices["in_d"] = {"id": "in_d", "amount_paid": 3000,
                                    "payment_intent": "pi_d"}

        _created(app, "sub_d", "evt_d")

        assert gateway.subscriptions["sub_d"]["status"] == "canceled"
        refunds = [c for c in gateway.calls if c[0] == "create_refund"]
        assert len(refunds) == 1 and refunds[0][1]["payment_intent"] == "pi_d"
        assert _fresh(user).subscription_status == "ended"

    def test_the_subscription_it_had_stays(self, app, gateway):
        """The account's own subscription (its renewal stopped by the
        deletion) is not cancelled by its later events."""
        sub = tp.subscription("sub_own", customer="cus_d", status="active")
        gateway.subscriptions["sub_own"] = sub
        user = _email_account(stripe_customer_id="cus_d")
        _created(app, "sub_own", "evt_1")
        from backend.utils.account_deletion import schedule_account_deletion
        schedule_account_deletion(_fresh(user), requested_by_id=user.id,
                                  source="self")
        assert gateway.subscriptions["sub_own"]["cancel_at_period_end"] is True

        tp._post_event(app, tp._event("customer.subscription.updated",
                                      {"id": "sub_own"}, "evt_2"))

        assert gateway.subscriptions["sub_own"]["status"] == "active"
        assert "cancel_subscription" not in gateway.names()
        assert _fresh(user).subscription_status == "active"

    def test_a_live_account_keeps_its_new_trial(self, app, gateway):
        """The control: the same checkout for an account not in deletion."""
        user = _email_account(stripe_customer_id="cus_d")
        _card(gateway, "pm_d", "fp_card_9")
        _trial(gateway, "sub_d", "cus_d", "pm_d")
        _created(app, "sub_d", "evt_d")
        assert gateway.subscriptions["sub_d"]["status"] == "trialing"
        assert _fresh(user).subscription_status == "trialing"


# ── Never without the X proof ───────────────────────────────────────────

class TestTheXProof:
    def _offer(self, client, **over):
        data = {"user_id": None, "prepared_id": None, "twitter_id": X_ID,
                "handle": HANDLE, "at": time.time()}
        data.update(over)
        with client.session_transaction() as sess:
            sess[prepared_accounts.OFFER_SESSION_KEY] = data

    def _refused(self, client, anna, anna_x):
        assert _call(client, "get", "/auth/prepared-account").status_code == 410
        resp = _open(client)
        assert resp.status_code == 410, resp.get_json()
        assert resp.get_json()["code"] == "expired"
        assert _signed_in_id(client) == anna.id
        assert _fresh(anna).deleted_at is None
        assert _fresh(anna_x).email is None
        assert _fresh(anna_x).last_seen_at is None

    def test_no_offer_opens_nothing(self, app):
        anna_x = _prepared()
        anna = _email_account()
        client = _client(app, anna)
        _x_token(client, X_ID)   # an X token alone is no question answered
        self._refused(client, anna, anna_x)

    def test_an_offer_without_the_x_token_opens_nothing(self, app):
        anna_x = _prepared()
        anna = _email_account()
        client = _client(app, anna)
        self._offer(client, user_id=anna.id, prepared_id=anna_x.id)
        self._refused(client, anna, anna_x)

    def test_the_x_token_of_another_x_account_opens_nothing(self, app):
        anna_x = _prepared()
        anna = _email_account()
        client = _client(app, anna)
        self._offer(client, user_id=anna.id, prepared_id=anna_x.id)
        _x_token(client, "999")
        self._refused(client, anna, anna_x)

    def test_an_offer_for_another_account_opens_nothing(self, app):
        anna_x = _prepared()
        anna = _email_account()
        ben = _email_account(username="ben", email="ben@example.com")
        client = _client(app, anna)
        self._offer(client, user_id=ben.id, prepared_id=anna_x.id)
        _x_token(client, X_ID)
        self._refused(client, anna, anna_x)

    def test_an_old_offer_opens_nothing(self, app):
        anna_x = _prepared()
        anna = _email_account()
        client, _ = _connect(app, anna)
        with client.session_transaction() as sess:
            data = dict(sess[prepared_accounts.OFFER_SESSION_KEY])
            data["at"] = time.time() - 901
            sess[prepared_accounts.OFFER_SESSION_KEY] = data
        self._refused(client, anna, anna_x)

    def test_signed_out_opens_nothing(self, app):
        anna_x = _prepared()
        client = app.test_client()
        self._offer(client, user_id=None, prepared_id=anna_x.id)
        _x_token(client, X_ID)
        g.pop("_login_user", None)
        assert _open(client).status_code == 410
        assert _signed_in_id(client) is None

    def test_an_email_sign_in_never_finds_a_prepared_account(self, app,
                                                             monkeypatch):
        """The address is new, and its local part is the prepared account's
        username and handle: a new account, the prepared one untouched."""
        from backend.routes import auth as auth_mod
        from backend.utils import sign_in_links
        anna_x = _prepared(username=HANDLE)
        email = f"{HANDLE}@example.com"
        monkeypatch.setattr(auth_mod, "verify_magic_link_token",
                            lambda t: {"email": email, "next_url": None})
        monkeypatch.setattr(sign_in_links, "redeem", lambda h, p: None)
        client = app.test_client()
        g.pop("_login_user", None)
        resp = client.get("/auth/magic-link/verify?token=t")
        assert resp.status_code == 302
        new = User.query.filter_by(email=email).one()
        assert new.id != anna_x.id and new.twitter_id is None
        assert _signed_in_id(client) == new.id
        assert _fresh(anna_x).email is None
        assert _fresh(anna_x).last_seen_at is None

    def test_the_whole_round_trip_through_x(self, app):
        """flask-dance's real views; only the HTTP exchange with X is faked.
        The access token X gives carries the X user id."""
        from backend.tests.test_x_login_linking import _fake_x_exchange
        anna_x = _prepared()
        anna = _email_account()
        client = _client(app, anna)
        calls, fake = _fake_x_exchange(access_x_id=int(X_ID))
        with fake:
            _call(client, "get", "/auth/x/connect")
            _call(client, "get", "/auth/twitter")
            back = _call(client, "get",
                         "/auth/twitter/authorized?oauth_token=REQ-1"
                         "&oauth_verifier=V")
        assert back.headers["Location"].endswith("/auth/login")
        resp = _callback(client, X_ID)
        assert resp.headers["Location"] == f"{FRONTEND}/prepared-account"

        assert _open(client).status_code == 200
        assert _signed_in_id(client) == anna_x.id

    def test_a_forged_callback_makes_no_offer(self, app):
        from backend.tests.test_x_login_linking import _fake_x_exchange
        anna_x = _prepared()
        anna = _email_account()
        client = _client(app, anna)
        calls, fake = _fake_x_exchange(access_x_id=int(X_ID))
        with fake:
            _call(client, "get", "/auth/x/connect")
            _call(client, "get", "/auth/twitter")
            resp = _call(client, "get",
                         "/auth/twitter/authorized?oauth_token=ATTACKER"
                         "&oauth_verifier=V")
        assert resp.headers["Location"] == \
            f"{FRONTEND}/account?x_login=failed#x"
        assert calls["exchanged"] == []
        assert prepared_accounts.OFFER_SESSION_KEY not in _session(client)
        self._refused(client, anna, anna_x)


def test_holds_writing_counts_hidden_and_deleted_rows(app):
    from backend.models import Draft, UserPrompt
    anna = _email_account()
    assert prepared_accounts.holds_writing(anna.id) is False
    _db.session.add(UserPrompt(user_id=anna.id, prompt_key="k", title="t",
                               content="", generated_by="default"))
    _db.session.commit()
    assert prepared_accounts.holds_writing(anna.id) is False  # a job's copy
    _db.session.add(Draft(user_id=anna.id, content=""))
    _db.session.commit()
    assert prepared_accounts.holds_writing(anna.id) is True


def test_trial_card_hash_is_unchanged_by_the_move(app):
    """The mark keeps its hash: only its account changes."""
    anna = _email_account()
    anna_x = _prepared()
    digest = trial_cards.card_hash("fp_card_1")
    _db.session.add(TrialCard(card_hash=digest, user_id=anna.id))
    _db.session.commit()
    assert prepared_accounts._move_trial_card(anna, anna_x) == 1
    _db.session.commit()
    mark = TrialCard.query.one()
    assert mark.card_hash == digest and mark.user_id == anna_x.id

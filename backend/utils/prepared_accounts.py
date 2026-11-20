"""An invited person who signed in by email finds the account Loore
prepared for them (#714).

Loore prepares accounts ahead of their owners (the admin's whitelist and
pre-fills, #702's bundle loads), keyed by the X account id, and only Sign
in with X finds one. An invited person who signs in with an email link
instead gets a second, empty account. With payments on it goes straight to
the subscribe page, and a trial started there takes the card's one trial
(#697), while the prepared account waits unused.

The way across is Connect X (routes/auth._connect_x; the subscribe page's
"Sign in with X" starts it for an email account without X). When the X id
that just authorized in this browser belongs to a prepared account nobody
has signed in to, the X id is not attached to the email account. The
browser keeps the proof and the person chooses: open the prepared account,
or stay.

Opening it is a Sign in with X into the prepared account. An email account
that holds no writing and never paid is deleted on the way, through the
account deletion of #269 (the user's own, 30 days), and what it held for
the person goes with them:

* its address, when the prepared account has none: the address then signs
  in to the prepared account. Its sign-in links are withdrawn first, as an
  email change withdraws them, and the address is told, as Connect X
  tells it;
* its trial (#714, part 3): a trial running on it is cancelled the way #649
  cancels a second trial of one account (nothing was charged), and the
  card's trial mark (#697) moves to the prepared account, which can then
  start its own trial with the same card;
* who invited it (#439), when nobody invited the prepared account.

An email account with writing, invitations it sent, poll answers or
feedback, one that paid (also a trial that became paid at Stripe before
its webhook arrived), and one the admin blocked or set up by hand stays as
it is: opening the prepared account is then only the sign-in, and the
email account is still there for its address.

Never without the X proof: the offer is written only by a completed
Connect X round trip in this browser session (backend/oauth.py binds X's
callback to the session that started it), it names the email account that
asked and lasts as long as a sign-in link, and flask-dance's token for the
prepared account's X id must still be in the session. An email address or
a handle never finds a prepared account. When the question ends without
Open (Stay, expiry, any other sign-in starting), the offer and that X
token go together, so the token never signs anyone in later.

Ids, flags and counts only: nothing here reads content.
"""
import logging
import time

from flask import current_app, session
from flask_login import current_user

from backend.extensions import db
from backend.models import (
    Draft, ExternalItem, Invite, Node, PollResponse, Referral, ShareDraft,
    TrialCard, User, UserArtifact, UserFeedback, UserProfile, UserPrompt,
    UserRecentContext, UserTodo,
)

logger = logging.getLogger(__name__)

OFFER_SESSION_KEY = "prepared_account_offer"

# Why an email account stays when its person opens the prepared account.
STAYS_WRITING = "writing"   # it holds writing of its own
STAYS_USED = "used"         # invitations it sent, poll answers, feedback
STAYS_PAID = "paid"         # it paid, or its subscription is past its trial
STAYS_KEPT = "kept"         # admin, alpha tester, a plan set by hand,
#                             blocked or deactivated, a deletion under way

# Plans a new account gets without an admin (as unclaimed_accounts.
# DEFAULT_PLANS): "alpha" while payments are off, "free" while they are on.
# Any other plan was set by hand.
_DEFAULT_PLANS = ("alpha", "free")

# UserPrompt versions a person saved (as unclaimed_accounts.PERSON_VERSIONS):
# the "default" rows are copies a job makes.
_PERSON_VERSIONS = ("user", "revert")


class MoveRefused(Exception):
    """The move cannot go ahead now. ``code`` is for the page."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def is_prepared(user):
    """*user* holds an X id and was set up ahead of its owner, and nobody
    has signed in to it: the person who proves that X id in front of us
    is its owner. "unknown" (an account from before sign-ins were
    recorded) is not "never" here either (activity.sign_in_status). Not an
    account being deleted (signing in offers its restore instead) and not
    one the admin blocked."""
    from backend.utils.activity import sign_in_status
    from backend.utils.approval import is_blocked
    return bool(user is not None and user.twitter_id
                and user.deleted_at is None
                and sign_in_status(user) == "never"
                and not is_blocked(user))


def holds_writing(user_id):
    """Whether the account holds anything the person wrote or kept:
    entries (an AI reply under them included), drafts, share drafts,
    saved references, a profile, todo list, recent context or document,
    a prompt they saved. Hidden rows (a waiting "Delete all my content")
    and deleted entries count too. Exists checks on ids only."""
    from sqlalchemy import or_
    from backend.utils.hidden_rows import including_hidden_rows
    checks = (
        db.session.query(Node.id).filter(or_(
            Node.user_id == user_id, Node.human_owner_id == user_id)),
        db.session.query(Draft.id).filter(Draft.user_id == user_id),
        db.session.query(ShareDraft.id).filter(ShareDraft.user_id == user_id),
        db.session.query(ExternalItem.id).filter(
            ExternalItem.user_id == user_id),
        db.session.query(UserProfile.id).filter(
            UserProfile.user_id == user_id),
        db.session.query(UserTodo.id).filter(UserTodo.user_id == user_id),
        db.session.query(UserRecentContext.id).filter(
            UserRecentContext.user_id == user_id),
        db.session.query(UserArtifact.id).filter(
            UserArtifact.user_id == user_id),
        db.session.query(UserPrompt.id).filter(
            UserPrompt.user_id == user_id,
            UserPrompt.generated_by.in_(_PERSON_VERSIONS)),
    )
    with including_hidden_rows():
        return any(q.first() is not None for q in checks)


def holds_ties(user_id):
    """Whether the account holds what other people or Loore count on:
    invitations it sent that are open or were used, referrals where it is
    the inviter (#439: its reward would be lost with it), poll answers,
    feedback. Hidden rows count too. Exists checks on ids only."""
    from backend.utils.hidden_rows import including_hidden_rows
    from backend.utils.referrals import OPEN, USED
    checks = (
        db.session.query(Invite.id).filter(
            Invite.inviter_id == user_id, Invite.state.in_((OPEN, USED))),
        db.session.query(Referral.id).filter(Referral.inviter_id == user_id),
        db.session.query(PollResponse.id).filter(
            PollResponse.user_id == user_id),
        db.session.query(UserFeedback.id).filter(
            UserFeedback.user_id == user_id),
    )
    with including_hidden_rows():
        return any(q.first() is not None for q in checks)


def stays_reason(user):
    """None when the email account *user* is deleted on the move (it holds
    no writing and never paid), else why it stays (STAYS_*)."""
    from backend.utils.account_deletion import deletion_refusal
    from backend.utils.approval import is_blocked
    from backend.utils.user_purge import active_job
    # Not User.plan_set_by_hand: that counts the "alpha" every account gets
    # while payments are off. An alpha tester keeps the account its free
    # month and price belong to. One the admin blocked or deactivated (#697)
    # stays as the admin left it, its trial card's mark included.
    if (user.is_admin or user.is_alpha_tester
            or (user.plan or "alpha") not in _DEFAULT_PLANS
            or is_blocked(user) or user.deactivated_at is not None
            or deletion_refusal(user) is not None):
        return STAYS_KEPT
    job = active_job(user.id)
    if job is not None and (job.status == "running" or job.delete_account):
        return STAYS_KEPT
    # A trial running or ended unpaid goes with the move (part 3). A
    # subscription past its trial (active, or past due on its first
    # payment) is the person's own business with Stripe: the account stays.
    if user.subscription_ever_paid or user.subscription_status in (
            "active", "past_due"):
        return STAYS_PAID
    if holds_writing(user.id):
        return STAYS_WRITING
    if holds_ties(user.id):
        return STAYS_USED
    return None


# ── The offer, in the browser's session ────────────────────────────────

def _max_age():
    # As long as a sign-in link lasts, as the invitation's held sign-in
    # (routes/auth INVITE_CHOICE_KEY): the X proof is as fresh as that.
    return current_app.config.get("MAGIC_LINK_EXPIRY_SECONDS", 900)


def offer(user, prepared, twitter_id, handle):
    """Connect X of *user* (the email account) came back with the X id of
    *prepared*: keep the proof in the session for the person's answer.
    Returns the frontend URL of the question."""
    session[OFFER_SESSION_KEY] = {
        "user_id": user.id, "prepared_id": prepared.id,
        "twitter_id": str(twitter_id), "handle": handle, "at": time.time()}
    logger.info("Connect X of user %s found the prepared account %s",
                user.id, prepared.id)
    frontend_url = current_app.config.get("FRONTEND_URL", "")
    return f"{frontend_url}/prepared-account"


def drop_offer():
    """Only the offer: after Open, the X token is the prepared account's
    own sign-in."""
    session.pop(OFFER_SESSION_KEY, None)


def has_offer():
    return OFFER_SESSION_KEY in session


def forget():
    """The question is over without Open (Stay, expired, another sign-in
    starting, another account in this browser): the offer and the X token
    it kept both go, so that token can never sign anyone in afterwards
    (routes/auth.login). Connect X asks X again from the start."""
    drop_offer()
    bp = current_app.blueprints.get("twitter")
    if bp is not None:
        try:
            del bp.token
        except KeyError:
            pass


def current_offer():
    """(offer, email account, prepared account) while this browser holds
    the X proof for the signed-in account; None otherwise (a stale offer
    is forgotten with its X token). The proof: the offer Connect X wrote,
    for the account signed in now, within its time, and flask-dance's
    token for the prepared account's X id still in the session."""
    from backend.utils.account_deletion import session_x_sign_in_token
    data = session.get(OFFER_SESSION_KEY)
    if not isinstance(data, dict):
        return None
    if (not current_user.is_authenticated
            or data.get("user_id") != current_user.id
            or time.time() - float(data.get("at") or 0) > _max_age()):
        forget()
        return None
    prepared_id = data.get("prepared_id")
    prepared = (db.session.get(User, prepared_id)
                if isinstance(prepared_id, int) else None)
    if (prepared is None or prepared.twitter_id != data.get("twitter_id")
            or session_x_sign_in_token(prepared) is None):
        forget()
        return None
    return data, current_user._get_current_object(), prepared


def describe(user, prepared, handle):
    """What the question states (GET /auth/prepared-account)."""
    reason = stays_reason(user)
    deleted = reason is None
    return {
        "handle": handle,
        "email": user.email,
        "deleted": deleted,
        "stays_because": reason,
        "email_moves": deleted and bool(user.email) and not prepared.email,
        # The prepared account has an address of its own: the person's
        # stays with the deleted account, and signing in with it offers to
        # restore that account for 30 days.
        "email_stays": deleted and bool(user.email) and bool(prepared.email),
        "trial_ends": deleted and user.subscription_status == "trialing",
    }


# ── The move ───────────────────────────────────────────────────────────

_CHANGED = ("Something changed in this account since the question was "
            "shown. Please read it again.")


def _end_trial(user, config=None):
    """Part 3: a trial running on the email account is cancelled the way
    #649 cancels a second trial of one account (stripe_billing.
    cancel_duplicate: marked as Loore's, a payment refunded if there was
    one, a discount code's use given back). One already cancelled at
    Stripe with nothing charged is only recorded.

    The database can lag behind Stripe: a trial that ended a moment ago is
    active (or past due) at Stripe before its webhook arrives, and its
    first invoice is charged or on its way. That is no trial to end with
    nothing charged, so nothing goes ahead: what Stripe says is recorded
    and MoveRefused("changed") raised, and the question, read again, says
    the account stays. Raises when Stripe cannot be reached; nothing else
    has changed then. Returns True when a trial ended."""
    from backend.utils import stripe_billing
    if (not user.stripe_subscription_id
            or user.subscription_status != "trialing"):
        return False
    gateway = stripe_billing.get_gateway(config)
    sub = gateway.retrieve_subscription(user.stripe_subscription_id)
    status = sub.get("status")
    if status == "trialing":
        sub = stripe_billing.cancel_duplicate(gateway, sub)
    else:
        invoice, _pi = stripe_billing._latest_payment_intent(gateway, sub)
        charged = bool(invoice and invoice.get("amount_paid"))
        if charged or status not in ("canceled", "incomplete_expired"):
            stripe_billing.apply_subscription(sub, config)
            if charged:
                # As the invoice.paid webhook will: the account paid.
                user.subscription_ever_paid = True
            db.session.commit()
            logger.warning("User %s: opening the prepared account refused, "
                           "the trial %s is %s at Stripe", user.id,
                           sub.get("id"), status)
            raise MoveRefused("changed", _CHANGED)
    stripe_billing.apply_subscription(sub, config)
    return True


def _move_trial_card(user, prepared):
    """The card's trial mark (#697) goes with the person: the prepared
    account may start its trial with it. Not when the prepared account
    has a mark of its own (it then keeps that one, and this one is
    detached at the purge)."""
    if TrialCard.query.filter_by(user_id=prepared.id).first() is not None:
        return 0
    return TrialCard.query.filter_by(user_id=user.id).update(
        {TrialCard.user_id: prepared.id}, synchronize_session=False)


def _move_referral(user, prepared):
    """Who invited the person (#439) goes with them, unless somebody
    invited the prepared account already (the first inviter is kept)."""
    if Referral.query.filter_by(friend_id=prepared.id).first() is not None:
        return 0
    return Referral.query.filter_by(friend_id=user.id).update(
        {Referral.friend_id: prepared.id}, synchronize_session=False)


def _move_email(user, prepared):
    """The address goes to the prepared account when it has none. The
    links already sent to it are withdrawn, as an email change does
    (utils/sign_in_links). Returns the address moved, or None."""
    from backend.utils.sign_in_links import withdraw_links
    email = user.email
    if not email or prepared.email:
        return None
    withdraw_links(email)
    user.email = None
    db.session.flush()   # the address is unique: off one row first
    prepared.email = email
    return email


def open_prepared(user, prepared, *, twitter_id, delete_this_account,
                  config=None):
    """The person in front of us, signed in to the email account *user*,
    opens *prepared*, the account set up for the X id they proved
    (*twitter_id*). *delete_this_account* is what the question showed: the
    email account is deleted on the way (stays_reason None). Both rows are
    locked and checked again first; a change since the question raises
    MoveRefused. Ending a trial calls Stripe first and raises when it
    cannot be reached, before anything else changes. Commits.

    Returns {"deleted", "email_moved" (the address or None),
    "trial_ended"}. Signing in is the caller's (routes/auth)."""
    from backend.utils.account_deletion import schedule_account_deletion
    for uid in sorted((user.id, prepared.id)):
        db.session.query(User).filter(User.id == uid).with_for_update().one()
    db.session.refresh(user)
    db.session.refresh(prepared)
    if not is_prepared(prepared) or prepared.twitter_id != str(twitter_id):
        db.session.rollback()
        raise MoveRefused(
            "not_prepared", "That account can no longer be opened this way. "
            "Sign in with X to open it.")
    if (stays_reason(user) is None) != bool(delete_this_account):
        db.session.rollback()
        raise MoveRefused("changed", _CHANGED)
    result = {"deleted": False, "email_moved": None, "trial_ended": False}
    if not delete_this_account:
        db.session.commit()
        logger.info("User %s opened the prepared account %s; the email "
                    "account stays", user.id, prepared.id)
        return result
    result["trial_ended"] = _end_trial(user, config)
    moved_marks = _move_trial_card(user, prepared)
    _move_referral(user, prepared)
    result["email_moved"] = _move_email(user, prepared)
    schedule_account_deletion(user, requested_by_id=user.id, source="self")
    result["deleted"] = True
    logger.warning("User %s moved into the prepared account %s: deleted, "
                   "address %s, trial %s, %s trial card mark(s) moved",
                   user.id, prepared.id,
                   "moved" if result["email_moved"] else "not moved",
                   "ended" if result["trial_ended"] else "none", moved_marks)
    return result

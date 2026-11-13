"""Referrals (#439): one personal invite a month, and a free week for both
when the friend first pays.

Decided on #439 (Peter, 2026-10-09):

* a free week for both, granted on the friend's first payment (not at
  signup, not when a trial starts), so signups alone earn nothing;
* a friend without Community Archive data signs up through the invite and
  waits for approval like any other signup: the invite records who invited
  whom and lets nobody in;
* no clawback when the friend's payment is refunded or withdrawn;
* no prompts to invite and no counts: the invite sits on the Account page,
  and the inviter learns that a reward arrived, nothing about the friend.

Invites are scarce and personal (Peter, 2026-11-13: "scarcity also has the
effect that the inviter will actually think about who might be right for
Loore … if people feel like inviting people to Loore en masse, then be it
- they just won't get rewarded for it"; "one invite a month, no
accumulation of invites"; "no keeping of invites pls"):

* an invite is made for one named person, by X handle or email address,
  and its link records a referral for exactly one sign-up: that person's
  (an X sign-in with that handle, an email sign-in with that address).
  Anyone else who follows it signs up as anyone does, and the invite stays
  open. So a link posted in public earns nothing;
* one invite per calendar month (UTC) per account, the month it is made
  in; an unused month is gone. One open at a time. An open invite may be
  withdrawn and another made in the same month, but an invite used in a
  month ends that month's invites: at most one invite made in a month is
  ever used;
* an unused invite expires INVITE_EXPIRY_DAYS after it is made;
* the handle or address is kept only while the invite is open.

The free week is credit on the Stripe customer (a customer balance
transaction), the provider's credit feature, which a Managed Payments
subscription honours (sandbox, 2026-11-12): it is taken off the next
invoice, and Checkout applies it to a first payment. Its worth is fixed
and the same for both sides (Peter, 2026-11-13: "one week, or 1/4th of
the standard subscription. If the inviter or the invitee have higher level
of subscription, that shouldn't change the amount they are credited"):
REFERRAL_CREDIT_FRACTION of the Standard level's monthly price before tax,
whatever level either is on, whatever tax either pays, with or without a
live subscription. Stripe takes customer credit off an invoice's total,
after tax, so a taxed subscriber's tax is unchanged and the credit comes
off what they pay.

A code is random and turns into a referral only at a sign-up of the named
person; no answer anywhere says whether a code exists, or for whom.
Everything here is off unless PAYMENTS_ENABLED and REFERRALS_ENABLED.
"""
import logging
import re
import secrets
from datetime import datetime, timedelta

from flask import current_app, has_app_context
from sqlalchemy.exc import IntegrityError

from backend.extensions import db
from backend.models import Invite, Referral, User, UserNotification
from backend.utils.timefmt import iso_utc

logger = logging.getLogger(__name__)

# 12 random bytes: 16 URL-safe characters, 96 bits. Not guessable, and
# short enough to paste.
CODE_BYTES = 12
CODE_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
# Where an invite code waits through the X sign-in round trip (OAuth
# returns to the browser that started it; the X identity is what binds).
SESSION_KEY = "invite_code"
# X's own rule for a handle: 1 to 15 letters, digits or underscores.
X_HANDLE_RE = re.compile(r"^[a-z0-9_]{1,15}$")
X_PROFILE_RE = re.compile(
    r"^(?:https?://)?(?:www\.|mobile\.)?(?:x|twitter)\.com/([A-Za-z0-9_]+)"
    r"/?$", re.I)

OPEN, USED, WITHDRAWN, EXPIRED = "open", "used", "withdrawn", "expired"


def _config(config=None):
    if config is not None:
        return config
    return current_app.config if has_app_context() else {}


def referrals_enabled(config=None):
    cfg = _config(config)
    return bool(cfg.get("PAYMENTS_ENABLED")) and \
        bool(cfg.get("REFERRALS_ENABLED", True))


def clean_code(raw):
    """*raw* when it looks like an invite code, else None."""
    if not isinstance(raw, str):
        return None
    raw = raw.strip()
    return raw if CODE_RE.match(raw) else None


# ── Who an invite is for ────────────────────────────────────────────────

def normalise_email(raw):
    """An email address as sign-in stores it (trimmed, lower case), or
    None when it is not one."""
    from backend.utils.email import is_valid_email
    if not isinstance(raw, str):
        return None
    email = raw.strip().lower()
    return email if email and is_valid_email(email) else None


def normalise_handle(raw):
    """An X handle without "@", in lower case (X's handles are not case
    sensitive), or None when it is not one. A profile link is read too."""
    if not isinstance(raw, str):
        return None
    raw = raw.strip()
    link = X_PROFILE_RE.match(raw)
    if link:
        raw = link.group(1)
    handle = raw.lstrip("@").lower()
    return handle if X_HANDLE_RE.match(handle) else None


def parse_target(raw):
    """("email", address) or ("x", handle) for what the inviter typed, or
    None. Anything with an "@" after its first character is read as an
    address."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if "@" in text[1:]:
        email = normalise_email(text)
        return ("email", email) if email else None
    handle = normalise_handle(text)
    return ("x", handle) if handle else None


def _own_targets(user):
    """The handles and addresses that are the account's own: inviting
    them is inviting oneself."""
    own = set()
    for value in (user.email, getattr(user, "pending_email", None)):
        email = normalise_email(value)
        if email:
            own.add(("email", email))
    for value in (user.twitter_handle, getattr(user, "prefilled_handle",
                                               None)):
        handle = normalise_handle(value)
        if handle:
            own.add(("x", handle))
    return own


# ── The month's invite ──────────────────────────────────────────────────

def _month(now):
    return now.strftime("%Y-%m")


def _next_month_start(now):
    if now.month == 12:
        return datetime(now.year + 1, 1, 1)
    return datetime(now.year, now.month + 1, 1)


def _end(invite, state, now):
    """The invite stops being open; its handle or address is deleted."""
    invite.state = state
    invite.ended_at = now
    invite.target = None


def expire_due(now=None, inviter_id=None):
    """Mark open invites past their end as expired (deleting the handle or
    address in them). Commits. Returns how many. Run hourly
    (tasks/invites.expire_invites) and before reading an inviter's."""
    now = now or datetime.utcnow()
    q = Invite.query.filter(Invite.state == OPEN, Invite.expires_at <= now)
    if inviter_id is not None:
        q = q.filter(Invite.inviter_id == inviter_id)
    due = q.all()
    for invite in due:
        _end(invite, EXPIRED, now)
    if due:
        db.session.commit()
    return len(due)


def open_invite(user_id):
    return Invite.query.filter_by(inviter_id=user_id, state=OPEN).first()


def _month_used(user_id, month):
    """An invite made in *month* was used: that month's invites are over."""
    return Invite.query.filter_by(inviter_id=user_id, month=month,
                                  state=USED).first() is not None


def may_invite(user, config=None):
    """An account let in (a trial included; a lapsed one too) may make
    invites; one waiting for approval, being deleted, or never let in
    may not."""
    from backend.utils.subscriptions import access_state
    if user is None or not user.approved or user.deleted_at is not None:
        return False
    return access_state(user, _config(config)) != "none"


def invite_url(invite, config=None):
    frontend = (_config(config).get("FRONTEND_URL") or "").rstrip("/")
    return f"{frontend}/invite/{invite.code}"


def invite_state(user, now=None, config=None):
    """What the Account page shows: the open invite (its link, who it is
    for, until when), or whether one may be made now, or the day the next
    month's invite comes. No counts and no past invites."""
    now = now or datetime.utcnow()
    expire_due(now, inviter_id=user.id)
    current = open_invite(user.id)
    cents, currency = referral_credit(config)
    state = {"invite": None, "can_invite": False, "next_invite_on": None,
             "credit": {"cents": cents, "currency": currency}}
    if current is not None:
        state["invite"] = {
            "url": invite_url(current, config), "kind": current.kind,
            "to": current.target,
            "expires_at": iso_utc(current.expires_at)}
    elif _month_used(user.id, _month(now)):
        state["next_invite_on"] = _next_month_start(now).date().isoformat()
    else:
        state["can_invite"] = may_invite(user, config)
    return state


class InviteRefused(Exception):
    """Why no invite was made: ``message`` for the inviter, ``status`` for
    the answer."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def create_invite(user, raw_to, now=None, config=None):
    """Make *user*'s invite for the person *raw_to* names (an X handle or
    an email address). Raises InviteRefused. Commits.

    An address or handle that already has an account is accepted like any
    other, with the same answer: saying so would tell anyone whether an
    address has a Loore account. Such an invite is never used (an
    existing account is never brought) and expires."""
    now = now or datetime.utcnow()
    cfg = _config(config)
    if not may_invite(user, cfg):
        raise InviteRefused("Invites are not available on this account.",
                            403)
    target = parse_target(raw_to)
    if target is None:
        raise InviteRefused(
            "Enter your friend's X handle (like @ana) or email address.")
    if target in _own_targets(user):
        raise InviteRefused("That's your own address or handle. Invite "
                            "someone else.")
    # One request at a time per inviter (a no-op on sqlite).
    db.session.query(User.id).filter(User.id == user.id) \
        .with_for_update().first()
    expire_due(now, inviter_id=user.id)
    if open_invite(user.id) is not None:
        raise InviteRefused("You have an invite out already. Withdraw it "
                            "to invite someone else.", 409)
    if _month_used(user.id, _month(now)):
        raise InviteRefused("You've used this month's invite.", 409)
    days = int(cfg.get("INVITE_EXPIRY_DAYS") or 30)
    for _attempt in range(3):
        invite = Invite(inviter_id=user.id,
                        code=secrets.token_urlsafe(CODE_BYTES),
                        kind=target[0], target=target[1], month=_month(now),
                        created_at=now,
                        expires_at=now + timedelta(days=days), state=OPEN)
        db.session.add(invite)
        try:
            db.session.commit()
        except IntegrityError:
            # The code is another invite's (96 random bits: never in
            # practice); draw again.
            db.session.rollback()
            continue
        return invite
    raise RuntimeError("could not make an invite code")


def withdraw_invite(user, now=None):
    """End *user*'s open invite; its handle or address is deleted. Another
    may then be made, unless this month's was used. Commits. Returns
    whether there was one."""
    invite = open_invite(user.id)
    if invite is None:
        return False
    _end(invite, WITHDRAWN, now or datetime.utcnow())
    db.session.commit()
    return True


# ── Recording who invited whom ──────────────────────────────────────────

def invite_mismatch(raw_code, email=None, x_handle=None, now=None,
                    config=None):
    """For a sign-up that came with *raw_code* and has just proven its
    *email* or X *x_handle*: the kind of identity the invite names ("x" or
    "email") when it is an open invite made for someone else, else None.

    Peter, 2026-11-13: someone the invite was meant for may sign in with
    another address or X account by accident, so they are told the
    invitation was for someone else, and may sign in differently or go on
    without it. Only the kind is said, never the handle or address. A
    code that is unknown, used, withdrawn, expired or a deleted
    inviter's says nothing, as before."""
    code = clean_code(raw_code)
    if code is None or not referrals_enabled(config):
        return None
    now = now or datetime.utcnow()
    invite = Invite.query.filter_by(code=code, state=OPEN).first()
    if invite is None or invite.expires_at <= now:
        return None
    inviter = (db.session.get(User, invite.inviter_id)
               if invite.inviter_id else None)
    if inviter is None or inviter.deleted_at is not None:
        return None
    identity = (normalise_email(email) if invite.kind == "email"
                else normalise_handle(x_handle))
    if identity and identity == invite.target:
        return None
    return invite.kind


def redeem_invite(friend, raw_code, new_account, email=None, x_handle=None,
                  now=None, config=None):
    """At a sign-in that came with the invite code *raw_code*: record that
    the invite's maker invited *friend*, when *friend* is the person the
    invite names (the sign-in's *email*, or its X *x_handle*).

    Only for an account made by this sign-in (*new_account*), or one set
    up ahead of its owner that nobody has signed in to yet (admin
    whitelist, pre-fill): an existing user signing in through an invite
    was not brought by it. Records nothing, and leaves the invite as it
    was, for anyone else, an invite that is not open (used, withdrawn,
    expired), the inviter's own account or a deleted inviter's; the first
    inviter is kept. Approval is untouched: a friend outside the Community
    Archive waits as before. Never raises. Returns the Referral or None."""
    code = clean_code(raw_code)
    if friend is None or code is None or not referrals_enabled(config):
        return None
    now = now or datetime.utcnow()
    try:
        if not new_account:
            from backend.utils.activity import sign_in_status
            if sign_in_status(friend) != "never":
                return None
        invite = (Invite.query.filter_by(code=code)
                  .with_for_update().first())
        if invite is None or invite.state != OPEN:
            return None
        if invite.expires_at <= now:
            _end(invite, EXPIRED, now)
            db.session.commit()
            return None
        identity = (normalise_email(email) if invite.kind == "email"
                    else normalise_handle(x_handle))
        if not identity or identity != invite.target:
            return None   # someone else followed the link: it stays open
        inviter = (db.session.get(User, invite.inviter_id)
                   if invite.inviter_id else None)
        if (inviter is None or inviter.id == friend.id
                or inviter.deleted_at is not None):
            return None
        if Referral.query.filter_by(friend_id=friend.id).first() is not None:
            return None   # one inviter per friend: the first one
        referral = Referral(inviter_id=inviter.id, friend_id=friend.id,
                            invite_id=invite.id)
        db.session.add(referral)
        _end(invite, USED, now)
        db.session.commit()
        logger.info("Referral recorded: user %s invited user %s (invite %s)",
                    inviter.id, friend.id, invite.id)
        return referral
    except IntegrityError:
        db.session.rollback()   # another sign-in recorded one first
        return None
    except Exception:  # noqa: BLE001 - never break a sign-in over this
        db.session.rollback()
        logger.exception("Could not record a referral for user %s",
                         getattr(friend, "id", None))
        return None


# ── The reward ──────────────────────────────────────────────────────────

class RewardRefused(Exception):
    """Stripe refused the credit for a reason a retry would not change."""


def _permanent(exc):
    """A refusal Stripe would repeat (a client error other than a
    conflict or a rate limit): retrying the event would never succeed."""
    status = getattr(exc, "http_status", None)
    return isinstance(exc, RewardRefused) or status in (400, 402, 403, 404)


def referral_credit(config=None):
    """(cents, currency) of the free week, the same for the inviter and the
    friend: REFERRAL_CREDIT_FRACTION of the Standard level's monthly price
    before tax (SUBSCRIPTION_PRICE_CENTS_STANDARD), in the prices' currency
    (SUBSCRIPTION_CURRENCY). Nothing about the account changes it: not its
    level, not the tax it pays, not whether it has a live subscription."""
    from backend.utils.subscriptions import level_price_cents
    cfg = _config(config)
    fraction = float(cfg.get("REFERRAL_CREDIT_FRACTION") or 0)
    return (round(level_price_cents("standard", cfg) * fraction),
            (cfg.get("SUBSCRIPTION_CURRENCY") or "usd").lower())


def inviter_cap(invoice, currency):
    """The most the inviter's credit may be: what the friend paid on the
    first invoice, when it was paid in the credit's *currency* (always,
    while every price is in SUBSCRIPTION_CURRENCY). It only bites when the
    friend paid less than the credit (a deep discount code). In another
    currency there is no sound comparison: None, and the fixed credit
    stands (it is the Standard price's share, which was the bound there
    before)."""
    paid_currency = (invoice.get("currency") or "").lower()
    if paid_currency and paid_currency == currency:
        return int(invoice.get("amount_paid") or 0)
    return None


def _customer_for(gateway, user):
    """The account's Stripe customer, made now when it has none (the same
    idempotency key as a checkout's, so the two never make two). Does not
    commit: the webhook's transaction does."""
    if user.stripe_customer_id:
        return user.stripe_customer_id
    from backend.utils.stripe_billing import customer_idempotency_key
    customer = gateway.create_customer(
        {"metadata": {"loore_user_id": str(user.id)}},
        idempotency_key=customer_idempotency_key(user))
    user.stripe_customer_id = customer["id"]
    return user.stripe_customer_id


def _marker(referral, side):
    return f"{referral.id}:{side}"


def _existing_credit(gateway, customer, referral, side):
    """The credit already given for this referral and side, if a delivery
    of the event got as far as Stripe and no further."""
    for txn in gateway.list_balance_transactions(customer):
        if (txn.get("metadata") or {}).get("loore_referral") == \
                _marker(referral, side):
            return txn
    return None


def _credit_key(referral, side):
    """Stripe's idempotency key for one side's credit. The referral's
    creation time is in it: databases sharing one Stripe account (staging
    and local development on the sandbox) have the same referral ids. The
    mark in the credit's metadata, looked up first, is the real guard.

    The amount is deliberately not in it (it changed on 2026-11-13, from
    7/30 of the next invoice to a fixed share of the Standard price): a
    retry of a credit made under the old amount finds it by its mark, and
    if it did not, Stripe would refuse the same key with another amount
    within its window rather than make a second credit."""
    created = referral.created_at
    stamp = f"-{int(created.timestamp())}" if created else ""
    return f"loore-referral-{referral.id}{stamp}-{side}"


def _grant_side(gateway, referral, side, user, config=None, invoice=None):
    """Give *user* (the inviter or the friend) its free week, once: the
    fixed referral_credit. The inviter's is at most what the friend paid
    on *invoice* (inviter_cap). A refusal Stripe would repeat is recorded
    as "failed" and logged, so the friend's subscription event still
    applies; anything else raises, and the event is sent again."""
    if user is None:
        setattr(referral, f"{side}_reward", "skipped_no_account")
        return False
    if user.deleted_at is not None:
        setattr(referral, f"{side}_reward", "skipped_deleted")
        return False
    try:
        customer = _customer_for(gateway, user)
        txn = _existing_credit(gateway, customer, referral, side)
        if txn is None:
            cents, currency = referral_credit(config)
            if side == "inviter" and invoice is not None:
                cap = inviter_cap(invoice, currency)
                if cap is not None:
                    cents = min(cents, cap)
            if cents <= 0:
                raise RewardRefused("nothing to credit")
            txn = gateway.create_balance_transaction(
                customer,
                {"amount": -cents, "currency": currency,
                 "description": "Loore: a free week for an invitation",
                 "metadata": {"loore_referral": _marker(referral, side)}},
                idempotency_key=_credit_key(referral, side))
    except Exception as e:  # noqa: BLE001
        if not _permanent(e):
            raise
        logger.error("Referral %s: the %s's free week was refused by Stripe "
                     "(%s); give it by hand in the Dashboard", referral.id,
                     side, type(e).__name__)
        setattr(referral, f"{side}_reward", "failed")
        return False
    setattr(referral, f"{side}_reward", "granted")
    setattr(referral, f"{side}_credit_cents", -int(txn.get("amount") or 0))
    setattr(referral, f"{side}_credit_currency",
            (txn.get("currency") or "")[:8] or None)
    setattr(referral, f"{side}_credit_txn", txn.get("id"))
    return True


INVITER_NOTICE = ("A free week, with thanks",
                  "Thank you for inviting a friend to Loore. A free week is "
                  "taken off your next payment.")
FRIEND_NOTICE = ("A free week",
                 "You joined Loore through an invitation, so a free week is "
                 "taken off your next payment.")


def _notify(user_id, notice):
    """The reward's notice, in the webhook's transaction (notify_user
    commits, which would record the event before it is applied)."""
    title, body = notice
    db.session.add(UserNotification(
        user_id=user_id, type="referral_reward", title=title, body=body))


def grant_on_first_payment(gateway, friend, invoice, config=None):
    """The friend's paid *invoice* (an invoice.paid event of its current
    subscription): on its first payment above zero, a free week for the
    friend and for whoever invited them, once. Webhook retries, replays
    and both payment events of one invoice find the referral rewarded
    already; the referral row is locked, so two deliveries at once give it
    once. A refund or a dispute later takes nothing back (#439). Does not
    commit. Returns True when this call gave the reward."""
    if friend is None or (invoice.get("amount_paid") or 0) <= 0:
        return False
    referral = (Referral.query.filter_by(friend_id=friend.id)
                .with_for_update().first())
    if referral is None or referral.rewarded_at is not None:
        return False
    if referral.inviter_id == referral.friend_id:
        referral.rewarded_at = datetime.utcnow()
        referral.inviter_reward = referral.friend_reward = "skipped_self"
        return False
    referral.rewarded_at = datetime.utcnow()
    referral.reward_invoice_id = (invoice.get("id") or "")[:255] or None
    inviter = (db.session.get(User, referral.inviter_id)
               if referral.inviter_id else None)
    if _grant_side(gateway, referral, "friend", friend, config):
        _notify(friend.id, FRIEND_NOTICE)
    if _grant_side(gateway, referral, "inviter", inviter, config,
                   invoice=invoice):
        _notify(inviter.id, INVITER_NOTICE)
    logger.info("Referral %s rewarded: friend %s, inviter %s",
                referral.id, referral.friend_reward, referral.inviter_reward)
    return True

"""Referrals (#439): a personal invite link, and a free week for both when
the friend first pays.

Decided on #439 (Peter, 2026-10-09):

* a free week for both, granted on the friend's first payment (not at
  signup, not when a trial starts), so signups alone earn nothing;
* a friend without Community Archive data signs up through the link and
  waits for approval like any other signup: the link records who invited
  whom and lets nobody in;
* no clawback when the friend's payment is refunded or withdrawn; no cap on
  free weeks;
* no prompts to invite and no counts: the link sits on the Account page,
  and the inviter learns that a reward arrived, nothing about the friend.

The free week is credit on the Stripe customer (a customer balance
transaction), the provider's credit feature, which a Managed Payments
subscription honours (sandbox, 2026-11-12): it is taken off the next
invoice, and Checkout applies it to a first payment. It is worth
REFERRAL_REWARD_DAYS / 30 of what the account's next invoice charges, tax
included, or of its level's price before tax when it has no live
subscription.

A code is random, never derived from the account, and turns into an
account only at signup; no answer anywhere says whether a code exists.
Everything here is off unless PAYMENTS_ENABLED and REFERRALS_ENABLED.
"""
import logging
import re
import secrets
from datetime import datetime

from flask import current_app, has_app_context
from sqlalchemy.exc import IntegrityError

from backend.extensions import db
from backend.models import (
    SUBSCRIPTION_GRANTING_STATUSES, SUBSCRIPTION_LEVELS, Referral, User,
    UserNotification)

logger = logging.getLogger(__name__)

# 12 random bytes: 16 URL-safe characters, 96 bits. Not guessable into an
# account, and short enough to paste.
CODE_BYTES = 12
CODE_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
# A month, for the value of a week of it.
DAYS_PER_MONTH = 30
# Where a pending invite waits through the X sign-in round trip.
SESSION_KEY = "invite_code"


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


# ── The link ────────────────────────────────────────────────────────────

def invite_code(user):
    """The account's invite code, made on first use. Commits."""
    if user.referral_code:
        return user.referral_code
    for _attempt in range(3):
        code = secrets.token_urlsafe(CODE_BYTES)
        written = (User.query
                   .filter(User.id == user.id, User.referral_code.is_(None))
                   .update({"referral_code": code},
                           synchronize_session=False))
        try:
            db.session.commit()
        except IntegrityError:
            # Another account has this code (96 random bits: never in
            # practice); draw again.
            db.session.rollback()
            continue
        db.session.refresh(user)
        if written or user.referral_code:
            return user.referral_code
    raise RuntimeError("could not make an invite code")


def invite_url(user, config=None):
    cfg = _config(config)
    frontend = (cfg.get("FRONTEND_URL") or "").rstrip("/")
    return f"{frontend}/invite/{invite_code(user)}"


# ── Recording who invited whom ──────────────────────────────────────────

def record_invite(friend, raw_code, new_account, config=None):
    """Record that the owner of *raw_code* invited *friend*, at signup.

    Only for an account made by this sign-in (*new_account*), or one set
    up ahead of its owner that nobody has signed in to yet (admin
    whitelist, pre-fill): an existing user signing in through a link was
    not brought by it. The first inviter is kept; a code that is no
    account's, the friend's own, or a deleted account's records nothing.
    Approval is untouched: a friend outside the Community Archive waits as
    before. Never raises. Returns the Referral or None."""
    code = clean_code(raw_code)
    if friend is None or code is None or not referrals_enabled(config):
        return None
    try:
        if not new_account:
            from backend.utils.activity import sign_in_status
            if sign_in_status(friend) != "never":
                return None
        inviter = User.query.filter_by(referral_code=code).first()
        if (inviter is None or inviter.id == friend.id
                or inviter.deleted_at is not None):
            return None
        if Referral.query.filter_by(friend_id=friend.id).first() is not None:
            return None   # one inviter per friend: the first one
        referral = Referral(inviter_id=inviter.id, friend_id=friend.id)
        db.session.add(referral)
        db.session.commit()
        logger.info("Referral recorded: user %s invited user %s",
                    inviter.id, friend.id)
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


def _live(user):
    return bool(user.stripe_subscription_id) and \
        user.subscription_status in SUBSCRIPTION_GRANTING_STATUSES


def week_credit(gateway, user, config=None):
    """(cents, currency) of a free week for *user*: REFERRAL_REWARD_DAYS
    of DAYS_PER_MONTH of what its next invoice charges, tax included,
    in the subscription's currency; without a live subscription, of its
    level's monthly price (Standard when it never had one), before tax."""
    from backend.utils.subscriptions import level_price_cents
    cfg = _config(config)
    days = int(cfg.get("REFERRAL_REWARD_DAYS") or 7)
    if user.stripe_customer_id and _live(user):
        try:
            preview = gateway.preview_invoice(user.stripe_customer_id,
                                              user.stripe_subscription_id)
        except Exception as e:  # noqa: BLE001
            if not _permanent(e):
                raise
            preview = None
        total = (preview or {}).get("total") or 0
        currency = (preview or {}).get("currency")
        if total > 0 and currency:
            return round(total * days / DAYS_PER_MONTH), currency.lower()
    level = user.subscription_level if user.subscription_level in \
        SUBSCRIPTION_LEVELS else "standard"
    return (round(level_price_cents(level, cfg) * days / DAYS_PER_MONTH),
            (cfg.get("SUBSCRIPTION_CURRENCY") or "usd").lower())


def inviter_cap(invoice, currency, config=None):
    """The most the inviter's week may be: what the friend paid on the
    first invoice, when both are in one currency (always, while every
    price is in SUBSCRIPTION_CURRENCY). In two currencies, a week of the
    Standard price when the inviter's currency is the prices' own; None
    when there is no sound comparison (the credit is then left to be
    given by hand)."""
    from backend.utils.subscriptions import level_price_cents
    cfg = _config(config)
    paid_currency = (invoice.get("currency") or "").lower()
    if paid_currency and paid_currency == currency:
        return int(invoice.get("amount_paid") or 0)
    if currency == (cfg.get("SUBSCRIPTION_CURRENCY") or "usd").lower():
        days = int(cfg.get("REFERRAL_REWARD_DAYS") or 7)
        return round(level_price_cents("standard", cfg) * days
                     / DAYS_PER_MONTH)
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
    mark in the credit's metadata, looked up first, is the real guard."""
    created = referral.created_at
    stamp = f"-{int(created.timestamp())}" if created else ""
    return f"loore-referral-{referral.id}{stamp}-{side}"


def _grant_side(gateway, referral, side, user, config=None, invoice=None):
    """Give *user* (the inviter or the friend) its free week, once. The
    inviter's is at most what the friend paid on *invoice* (inviter_cap).
    A refusal Stripe would repeat is recorded as "failed" and logged, so
    the friend's subscription event still applies; anything else raises,
    and the event is sent again."""
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
            cents, currency = week_credit(gateway, user, config)
            if side == "inviter" and invoice is not None:
                cap = inviter_cap(invoice, currency, config)
                if cap is None:
                    raise RewardRefused("no sound comparison of currencies")
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

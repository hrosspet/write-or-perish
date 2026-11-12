"""Loore's own discount codes (#438): a code opens the discounted price.

A user types the code in Settings; check_code looks at it and the checkout
starts at the discounted price (no Stripe promotion code). The answer to a
code that does not open the price is always the same "not valid", whether
the code is unknown, used up, expired or switched off, and an account may
try DISCOUNT_CODE_ATTEMPTS_PER_HOUR wrong codes an hour.

A use is counted once, when the subscription the code opened is created
and kept (record_use, from the webhook), never when a checkout is opened:
an abandoned checkout takes nothing, and since an account has one open
checkout at a time it cannot hold several uses. When a code's last use is
taken, the other accounts' open checkouts with it are expired
(stripe_billing.settle_subscription), so at most the checkouts completed
in the same moment can go over the limit; they are kept and counted.
"""
import re
import secrets
from datetime import datetime, timedelta

from sqlalchemy import func

from backend.extensions import db
from backend.models import DiscountCode, DiscountCodeAttempt, DiscountRedemption

CODE_RE = re.compile(r"^[A-Z0-9-]{4,64}$")
# Letters and digits that cannot be mistaken for each other when read
# aloud or copied by hand (no 0/O, 1/I/L).
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
GENERATED_LENGTH = 8

NOT_VALID_MESSAGE = "That code is not valid."
TOO_MANY_MESSAGE = ("Too many codes tried. Please try again in an hour.")


class CodeNotValid(Exception):
    pass


class TooManyAttempts(Exception):
    pass


def normalize(code):
    """Upper case, no spaces: what is stored and compared."""
    return re.sub(r"\s+", "", code or "").upper()


def generate_code():
    while True:
        code = "".join(secrets.choice(_ALPHABET)
                       for _ in range(GENERATED_LENGTH))
        if DiscountCode.query.filter_by(code=code).first() is None:
            return code


def uses(code):
    """Subscriptions the code opened and Loore kept."""
    return db.session.query(func.count(DiscountRedemption.id)).filter(
        DiscountRedemption.code_id == code.id,
        DiscountRedemption.state == "redeemed").scalar() or 0


def usable(code, now=None):
    now = now or datetime.utcnow()
    return bool(code is not None and code.active
                and (code.expires_at is None or code.expires_at > now)
                and (code.max_uses is None or uses(code) < code.max_uses))


def _check_rate(user_id, limit, now):
    since = now - timedelta(hours=1)
    failed = db.session.query(func.count(DiscountCodeAttempt.id)).filter(
        DiscountCodeAttempt.user_id == user_id,
        DiscountCodeAttempt.ok.is_(False),
        DiscountCodeAttempt.created_at >= since).scalar() or 0
    if failed >= limit:
        raise TooManyAttempts()


def check_code(user, raw_code, config, now=None):
    """The code *user* typed, if it opens the discounted price now.
    Raises TooManyAttempts or CodeNotValid. Takes no use. Commits the
    attempt either way."""
    now = now or datetime.utcnow()
    limit = int(config.get("DISCOUNT_CODE_ATTEMPTS_PER_HOUR") or 10)
    _check_rate(user.id, limit, now)
    code_text = normalize(raw_code)
    code = DiscountCode.query.filter_by(code=code_text).first() \
        if CODE_RE.match(code_text) else None
    ok = usable(code, now)
    db.session.add(DiscountCodeAttempt(user_id=user.id, created_at=now,
                                       ok=ok))
    db.session.commit()
    if not ok:
        raise CodeNotValid()
    return code


def record_use(code_id, user_id, subscription_id):
    """Count the use of a kept subscription the code opened, once per
    subscription. Returns (the code, whether this took its last use).
    Does not commit."""
    code = DiscountCode.query.filter_by(id=code_id).with_for_update().first()
    if code is None:
        return None, False
    row = DiscountRedemption.query.filter_by(
        stripe_subscription_id=subscription_id).first()
    if row is None:
        db.session.add(DiscountRedemption(
            code_id=code.id, user_id=user_id,
            stripe_subscription_id=subscription_id, state="redeemed"))
    elif row.state != "redeemed":
        row.state = "redeemed"
    else:
        return code, False
    db.session.flush()
    used_up = code.max_uses is not None and uses(code) >= code.max_uses
    return code, used_up


def void_use(subscription_id):
    """The subscription was cancelled as a duplicate: its use goes back.
    Does not commit."""
    row = DiscountRedemption.query.filter_by(
        stripe_subscription_id=subscription_id).first()
    if row is not None:
        row.state = "void"


def create_code(*, code=None, max_uses=None, expires_at=None, note=None,
                created_by_id=None):
    """The admin's new code. Raises ValueError for a malformed or taken
    code."""
    text = normalize(code) if code else generate_code()
    if not CODE_RE.match(text):
        raise ValueError("A code is 4 to 64 letters, digits or dashes.")
    if DiscountCode.query.filter_by(code=text).first() is not None:
        raise ValueError("That code exists already.")
    if max_uses is not None and max_uses < 1:
        raise ValueError("Max uses must be at least 1.")
    row = DiscountCode(code=text, max_uses=max_uses, expires_at=expires_at,
                       note=(note or None), created_by_id=created_by_id)
    db.session.add(row)
    db.session.commit()
    return row

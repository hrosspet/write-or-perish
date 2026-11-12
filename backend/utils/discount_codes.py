"""Loore's own discount codes (#438): a code opens the discounted price.

A user types the code in Settings; checkout_with_code checks it and the
checkout starts at the discounted price (no Stripe promotion code). The
answer to a code that does not open the price is always the same "not
valid", whether the code is unknown, used up, expired or switched off,
and an account may try DISCOUNT_CODE_ATTEMPTS_PER_HOUR wrong codes an
hour.
"""
import re
import secrets
from datetime import datetime, timedelta

from sqlalchemy import func, or_

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


def uses(code, now=None, exclude_user_id=None):
    """Checkouts that count against the code: completed ones, and ones
    started in the last 24 hours that have not expired."""
    from backend.utils.stripe_billing import recent_pending_cutoff
    q = db.session.query(func.count(DiscountRedemption.id)).filter(
        DiscountRedemption.code_id == code.id,
        or_(DiscountRedemption.state == "redeemed",
            (DiscountRedemption.state == "pending")
            & (DiscountRedemption.created_at >= recent_pending_cutoff(now))))
    if exclude_user_id is not None:
        q = q.filter(or_(DiscountRedemption.user_id.is_(None),
                         DiscountRedemption.user_id != exclude_user_id))
    return q.scalar() or 0


def _check_rate(user_id, limit, now):
    since = now - timedelta(hours=1)
    failed = db.session.query(func.count(DiscountCodeAttempt.id)).filter(
        DiscountCodeAttempt.user_id == user_id,
        DiscountCodeAttempt.ok.is_(False),
        DiscountCodeAttempt.created_at >= since).scalar() or 0
    if failed >= limit:
        raise TooManyAttempts()


def redeem(user, raw_code, config, now=None):
    """Check *raw_code* for *user* and hold one use of it for the checkout
    about to start: returns the pending DiscountRedemption (the user's
    own pending one when they try again). Raises TooManyAttempts or
    CodeNotValid. Commits the attempt either way."""
    now = now or datetime.utcnow()
    limit = int(config.get("DISCOUNT_CODE_ATTEMPTS_PER_HOUR") or 10)
    _check_rate(user.id, limit, now)
    code_text = normalize(raw_code)
    code = None
    if CODE_RE.match(code_text):
        code = DiscountCode.query.filter_by(code=code_text).with_for_update(
        ).first()
    ok = bool(
        code is not None and code.active
        and (code.expires_at is None or code.expires_at > now)
        and (code.max_uses is None
             or uses(code, now, exclude_user_id=user.id) < code.max_uses))
    db.session.add(DiscountCodeAttempt(user_id=user.id, created_at=now,
                                       ok=ok))
    if not ok:
        db.session.commit()
        raise CodeNotValid()
    redemption = DiscountRedemption.query.filter_by(
        code_id=code.id, user_id=user.id, state="pending").first()
    if redemption is None:
        redemption = DiscountRedemption(code_id=code.id, user_id=user.id,
                                        state="pending", created_at=now)
        db.session.add(redemption)
    else:
        redemption.created_at = now
    db.session.commit()
    return redemption


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

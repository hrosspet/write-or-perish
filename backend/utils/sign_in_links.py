"""Sign-in links (the "magic links" of the sign-in and welcome emails).

Every link that is emailed gets a SignInLink row: the hash of its token, a
keyed hash of the address (send_limits.address_key), when it was sent and
when it expires. A new link for an address leaves the ones already sent
working, so the welcome email an admin sent keeps working whatever is
asked for the address later. A link signs in until it expires, however
often it is opened: mail apps and scanners open links before the person
does (the April 2026 change that stopped consuming a link on its first
open). Deleting the account withdraws its address's links.

Links sent before rows existed (their tokens carry no nonce, see
backend/utils/magic_link.py) are checked as they were: against the
account's magic_link_token_hash, or by their signature alone for an
address without an account. The last of them expires 30 days after this
change is deployed; then that path and the two User.magic_link_* columns
can go.

The sign-in route's per-address limit (send_limits.ADDRESS_LIMITS) is
counted from these rows, so it holds across processes and without Redis.
"""
import logging
from datetime import datetime, timedelta

from backend.utils.magic_link import LINK_NONCE_KEY
from backend.utils.send_limits import ADDRESS_LIMITS, address_key

log = logging.getLogger(__name__)

# Rows are deleted once no limit window can count them: a row expires
# after it was sent, so one that expired longer ago than the longest
# window was sent before it.
KEEP_SECONDS = max(window for window, _ in ADDRESS_LIMITS)


def _now():
    return datetime.utcnow()


def _address_may_receive(key, now):
    from backend.extensions import db
    from backend.models import SignInLink
    longest = max(window for window, _ in ADDRESS_LIMITS)
    sent = [row.created_at for row in db.session.query(
        SignInLink.created_at).filter(
            SignInLink.address_hash == key,
            SignInLink.created_at > now - timedelta(seconds=longest))]
    return all(
        sum(1 for at in sent if at > now - timedelta(seconds=window)) < most
        for window, most in ADDRESS_LIMITS)


def record_link(address, token_hash, seconds, *, limit=True):
    """Store the link of a token about to be emailed to *address*, valid
    for *seconds*. With *limit* (the public sign-in route), False when the
    address is over ADDRESS_LIMITS: nothing is stored, and the caller
    sends nothing. The welcome email and scripts store without it."""
    from backend.extensions import db
    from backend.models import SignInLink
    now = _now()
    key = address_key(address)
    if limit and not _address_may_receive(key, now):
        return False
    db.session.add(SignInLink(
        token_hash=token_hash, address_hash=key, created_at=now,
        expires_at=now + timedelta(seconds=seconds)))
    SignInLink.query.filter(
        SignInLink.expires_at < now - timedelta(seconds=KEEP_SECONDS)
    ).delete(synchronize_session=False)
    db.session.commit()
    return True


def redeem(token_hash, payload):
    """May the link of a token whose signature checked out (*payload* is
    what it carries) sign in now? None when it may, else the error code
    for /login."""
    from backend.models import SignInLink
    link = SignInLink.query.filter_by(token_hash=token_hash).first()
    if link is None:
        if LINK_NONCE_KEY in payload:
            # It had a row from the moment it was sent; none now means the
            # links of the address were withdrawn (account deletion).
            return "invalid_or_expired"
        return _check_earlier_link(token_hash, payload)
    if link.expires_at <= _now():
        return "invalid_or_expired"
    return None


def _check_earlier_link(token_hash, payload):
    """A link sent before links had rows, checked as it was then: for an
    existing account, against the hash on the account (the newest link
    sent to it then; cleared when the account is deleted); for an address
    without an account, by its signature alone."""
    from backend.models import User
    user = User.query.filter_by(email=payload.get("email")).first()
    if user is not None and user.magic_link_token_hash != token_hash:
        return "link_already_used"
    return None


def withdraw_links(address):
    """Every link sent to *address* stops working (the account is deleted).
    The caller commits."""
    if not address:
        return
    from backend.models import SignInLink
    SignInLink.query.filter_by(address_hash=address_key(address)).delete(
        synchronize_session=False)

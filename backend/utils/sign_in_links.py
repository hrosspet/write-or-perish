"""Sign-in links (the "magic links" of the sign-in and welcome emails).

Every link that is emailed gets a SignInLink row: the hash of its token, a
keyed hash of the address (send_limits.address_key), when it was sent and
expires, and the account it was sent for: the account that held the
address then, or none.

- A new link for an address leaves the ones already sent working, so the
  welcome email an admin sent keeps working whatever is asked for the
  address later.
- A link signs in until it expires, however often it is opened: mail apps
  and scanners open links before the person does (the April 2026 change
  that stopped consuming a link on its first open).
- A link signs in only to the account it was sent for. One sent while the
  address had no account can only make that account: the open that makes
  it binds the address's unbound links to it (they all went to the same
  inbox), and an account that takes the address later is never signed in
  by them.
- The address's links are withdrawn when its account is deleted and when
  an email change binds the address to an account or moves an account off
  it.

Links sent before rows existed (their tokens carry no nonce, see
backend/utils/magic_link.py) are checked as they were: against the
account's magic_link_token_hash, or by their signature alone for an
address without an account. The last of them expires 30 days after this
change is deployed; then that path and the two User.magic_link_* columns
can go.

To issue a link outside the routes (tests, dev scripts), use
make_link_token: a token minted by hand has no row and does not sign in.
"""
import logging
from datetime import datetime, timedelta

from flask import after_this_request, current_app

from backend.utils import send_limits
from backend.utils.magic_link import (
    LINK_NONCE_KEY, generate_magic_link_token, hash_token)
from backend.utils.send_limits import address_key

log = logging.getLogger(__name__)


def _now():
    return datetime.utcnow()


def record_link(address, token_hash, seconds, user_id=None):
    """Store the link of a token about to be emailed to *address*, valid
    for *seconds*, sent for the account *user_id* (None: the address has
    no account). Rows that have expired are deleted on the way."""
    from backend.extensions import db
    from backend.models import SignInLink
    now = _now()
    db.session.add(SignInLink(
        token_hash=token_hash, address_hash=address_key(address),
        user_id=user_id, created_at=now,
        expires_at=now + timedelta(seconds=seconds)))
    SignInLink.query.filter(SignInLink.expires_at < now).delete(
        synchronize_session=False)
    db.session.commit()


def make_link_token(email, next_url=None, max_age=None):
    """A sign-in token for *email* whose link works: minted, stored, sent
    for the account that holds the address now (or none). For tests and
    dev scripts; the routes store their own."""
    from backend.models import User
    token = generate_magic_link_token(email, next_url, max_age=max_age)
    user = User.query.filter_by(email=email).first()
    record_link(email, hash_token(token),
                max_age or current_app.config.get(
                    "MAGIC_LINK_EXPIRY_SECONDS", 900),
                user.id if user else None)
    return token


class Issued:
    """A link stored for the sign-in route, and its count toward the
    address's limit."""

    def __init__(self, token_hash, claim):
        self.token_hash = token_hash
        self.claim = claim


def issue_for_sending(address, token_hash, seconds, user_id):
    """The sign-in route's link: counted toward the address's limit
    (send_limits.ADDRESS_LIMITS), then stored. None when the address is
    over the limit: nothing is stored, and the caller sends nothing."""
    held = send_limits.claim_address("sign-in", address)
    if held is None:
        return None
    try:
        record_link(address, token_hash, seconds, user_id)
    except Exception:
        send_limits.release(held)
        raise
    return Issued(token_hash, held)


def give_back(issued):
    """The email of *issued* did not go out: its link is deleted and its
    count given back. Never raises."""
    if issued is None:
        return
    send_limits.release(issued.claim)
    from backend.extensions import db
    from backend.models import SignInLink
    try:
        db.session.rollback()
        SignInLink.query.filter_by(token_hash=issued.token_hash).delete(
            synchronize_session=False)
        db.session.commit()
    except Exception:  # noqa: BLE001 - it expires anyway
        db.session.rollback()
        log.exception("Could not delete the link of a failed sign-in email")


def redeem(token_hash, payload):
    """May the link of a token whose signature checked out (*payload* is
    what it carries) sign in now? None when it may, else the error code
    for /login."""
    from backend.models import SignInLink, User
    link = SignInLink.query.filter_by(token_hash=token_hash).first()
    if link is None:
        if LINK_NONCE_KEY in payload:
            # It had a row from the moment it was sent; none now means the
            # links of the address were withdrawn.
            return "invalid_or_expired"
        return _check_earlier_link(token_hash, payload)
    if link.expires_at <= _now():
        return "invalid_or_expired"
    user = User.query.filter_by(email=payload.get("email")).first()
    if link.user_id is not None:
        if user is None or user.id != link.user_id:
            return "invalid_or_expired"
        return None
    if user is not None:
        # Sent while the address had no account; an account holds it now
        # that this link did not make.
        return "invalid_or_expired"
    address = payload.get("email")

    @after_this_request
    def _bind(response):
        bind_new_account(address)
        return response
    return None


def bind_new_account(address):
    """After the account for *address* was made by opening one of its
    links (here, after the request), or later from the held sign-in of
    /auth/invite-choice/continue (#439): the address's unbound links are
    the new account's."""
    from backend.extensions import db
    from backend.models import SignInLink, User
    try:
        user = User.query.filter_by(email=address).first()
        if user is None:
            return  # no account was made (refused, or waiting for a choice)
        SignInLink.query.filter(
            SignInLink.address_hash == address_key(address),
            SignInLink.user_id.is_(None),
        ).update({"user_id": user.id}, synchronize_session=False)
        db.session.commit()
    except Exception:  # noqa: BLE001 - the sign-in itself went through
        db.session.rollback()
        log.exception("Could not bind sign-in links to a new account")


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


def withdraw_links(*addresses):
    """Every link sent to these addresses stops working (an account deleted,
    an email change confirmed). The caller commits."""
    from backend.models import SignInLink
    keys = [address_key(a) for a in addresses if a]
    if keys:
        SignInLink.query.filter(SignInLink.address_hash.in_(keys)).delete(
            synchronize_session=False)

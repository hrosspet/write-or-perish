"""Sign-in links (the "magic links" of the sign-in and welcome emails) and
the limits on sending them.

Links. Every link that is emailed gets a SignInLink row: the hash of its
token, a keyed hash of the address, when it was sent, when it expires and
when it was first used. A new link for an address leaves the ones already
sent working: each stays valid until it is used or expires, so the welcome
email an admin sent keeps working whatever is asked for the address later.

Use. The first time a link signs someone in is recorded. It keeps working
for REUSE_SECONDS after that, never past its expiry, then it is used up.
The window is there because mail apps open links before the person does
(link previews, scanners), and the iPhone app signs in with a link the
person may already have opened in the browser: the April 2026 change that
stopped consuming a sign-in link on its first open was for the same
reason. For a sign-in link (15 minutes) that means it works until it
expires; a 30-day welcome link is used up 15 minutes after its first use.

Links sent before rows existed (their tokens carry no nonce, see
backend/utils/magic_link.py) are checked as before, against the account's
magic_link_token_hash, or by their signature alone for an address without
an account, and they sign in once: the first use writes their row, and
from then on the rules above apply. The last of them expires 30 days after
this change is deployed.

Limits on POST /auth/magic-link/send:

- per address: ADDRESS_LIMITS, counted from the rows, so across processes
  and without Redis. Over it nothing is sent, and the answer is the one a
  sent link gets: another answer would tell the asker that the address was
  asked for recently.
- per client (the address the request came from, as ProxyFix reads it
  from the proxy's X-Forwarded-For; IPv6 by its /64): CLIENT_LIMITS,
  counted in Redis, so across the web processes. Without Redis (tests, a
  local run without it, an outage) each process counts on its own. Over it
  the answer is a 429 with WAIT_MESSAGE, which says nothing about any
  address.
"""
import hashlib
import hmac
import ipaddress
import logging
import threading
import time
from datetime import datetime, timedelta

from flask import current_app
from sqlalchemy.exc import IntegrityError

from backend.utils.magic_link import LINK_NONCE_KEY

log = logging.getLogger(__name__)

# ── Heuristics (named here, flagged in the PR) ────────────────────────────
# Sign-in emails one address can receive: 5 an hour, 10 a day. A person
# who does not find the email asks again after a minute or two, perhaps
# twice, and once more from another device: four in a few minutes. Ten a
# day covers that twice over. Every earlier link still works, so a person
# over the limit has links in their inbox.
ADDRESS_LIMITS = ((3600, 5), (24 * 3600, 10))
# Requests one client can make: 10 in 15 minutes, 30 a day. One person
# stays under the address limit; a mistyped address is a new address from
# the same client, and a household or an office shares one client
# address. The short window keeps "try again in a few minutes" true.
CLIENT_LIMITS = ((15 * 60, 10), (24 * 3600, 30))
# How long a link keeps working after its first use (see above): as long
# as a sign-in link lives, so a sign-in link opened by a mail app first
# still works for the person, as since April 2026.
REUSE_SECONDS = 15 * 60
# An IPv6 client is counted by its /64, the block a home or mobile
# connection is usually given, as an IPv4 client is by its one address.
IPV6_PREFIX = 64

WAIT_MESSAGE = ("If you asked a moment ago, the email is on its way. Please "
                "check your inbox, or try again in a few minutes.")

CLIENT_KEY_PREFIX = "loore:sign-in-send:client:"

# Rows are deleted once no limit window can count them: a row expires
# after it was sent, so one that expired longer ago than the longest
# window was sent before it.
KEEP_SECONDS = max(window for window, _ in ADDRESS_LIMITS)


def _now():
    return datetime.utcnow()


def address_key(address):
    """The keyed hash a row holds instead of the address: the same for
    every spelling of it (case, surrounding spaces), and not computable
    without the app's secret."""
    secret = (current_app.config.get("SECRET_KEY") or "").encode()
    return hmac.new(secret, (address or "").strip().lower().encode(),
                    hashlib.sha256).hexdigest()


# ── Links ────────────────────────────────────────────────────────────────

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
    for /login. Records the link's first use."""
    from backend.extensions import db
    from backend.models import SignInLink
    now = _now()
    link = SignInLink.query.filter_by(token_hash=token_hash).first()
    if link is None:
        if LINK_NONCE_KEY in payload:
            # It had a row from the moment it was sent; none now means the
            # links of the address were withdrawn (account deletion).
            return "invalid_or_expired"
        return _redeem_earlier_link(token_hash, payload, now)
    if link.expires_at <= now:
        return "invalid_or_expired"
    if link.used_at is None:
        link.used_at = now
        db.session.commit()
        return None
    if now - link.used_at <= timedelta(seconds=REUSE_SECONDS):
        return None
    return "link_already_used"


def _redeem_earlier_link(token_hash, payload, now):
    """A link sent before links had rows. It is checked as it was then:
    for an existing account, against the hash on the account (the newest
    link sent to it then); for an address without an account, by its
    signature alone. Its first use writes its row, used now."""
    from backend.extensions import db
    from backend.models import SignInLink, User
    email = payload.get("email")
    expires = None
    user = User.query.filter_by(email=email).first()
    if user is not None:
        if user.magic_link_token_hash != token_hash:
            return "link_already_used"
        expires = user.magic_link_expires_at
        user.magic_link_token_hash = None
        user.magic_link_expires_at = None
    if expires is None:
        lifetime = payload.get("max_age") or current_app.config.get(
            "MAGIC_LINK_EXPIRY_SECONDS", 900)
        expires = now + timedelta(seconds=lifetime)
    db.session.add(SignInLink(
        token_hash=token_hash, address_hash=address_key(email),
        created_at=now, expires_at=expires, used_at=now))
    try:
        db.session.commit()
    except IntegrityError:
        # A second open of the same link wrote the row first.
        db.session.rollback()
        return redeem(token_hash, payload)
    return None


def withdraw_links(address):
    """Every link sent to *address* stops working (the account is deleted).
    The caller commits."""
    if not address:
        return
    from backend.models import SignInLink
    SignInLink.query.filter_by(address_hash=address_key(address)).delete(
        synchronize_session=False)


# ── Requests per client ──────────────────────────────────────────────────

_LOCAL_COUNTS_KEY = "sign_in_send_counts"
_local_lock = threading.Lock()
_redis_clients = {}


def _redis():
    """A Redis client on the broker's URL, or None in tests. Raises when
    the redis package is missing; callers count locally then."""
    config = current_app.config
    if config.get("TESTING"):
        return None
    url = config.get("CELERY_BROKER_URL")
    if not url:
        return None
    client = _redis_clients.get(url)
    if client is None:
        import redis
        client = _redis_clients[url] = redis.Redis.from_url(
            url, socket_timeout=0.5, socket_connect_timeout=0.5)
    return client


def _count_here(name, window):
    """The count of *name* in this process, for when Redis is not there.
    Per app, so every test app starts from zero."""
    now = time.monotonic()
    with _local_lock:
        counts = current_app.extensions.setdefault(_LOCAL_COUNTS_KEY, {})
        if len(counts) > 10000:
            for stale in [k for k, (until, _) in counts.items() if until <= now]:
                del counts[stale]
        until, n = counts.get(name, (0, 0))
        if until <= now:
            until, n = now + window, 0
        counts[name] = (until, n + 1)
        return n + 1


def _count(name, window):
    """Count this request in *name*, a counter that starts at the first
    request and ends *window* seconds later. Returns the count so far."""
    try:
        client = _redis()
        if client is not None:
            pipe = client.pipeline()
            pipe.set(name, 0, ex=window, nx=True)
            pipe.incr(name)
            return int(pipe.execute()[1])
    except Exception as e:  # noqa: BLE001 - a limit must not stop sign-in
        log.warning("Sign-in send limit: Redis unavailable (%s); counting "
                    "in this process", type(e).__name__)
    return _count_here(name, window)


def client_bucket(remote_addr):
    """What a client is counted by: its IPv4 address, or the /64 of its
    IPv6 address."""
    text = (remote_addr or "").strip()
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return text or "unknown"
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/{IPV6_PREFIX}", strict=False))
    return str(ip)


def client_may_ask(remote_addr):
    """Count a sign-in email request from *remote_addr*; False when its
    client is over CLIENT_LIMITS. The counters are named by a hash of the
    client, not the client address itself."""
    bucket = hashlib.sha256(client_bucket(remote_addr).encode()).hexdigest()
    allowed = True
    for window, most in CLIENT_LIMITS:
        if _count(f"{CLIENT_KEY_PREFIX}{window}:{bucket[:32]}", window) > most:
            allowed = False
    return allowed

"""Limits on the routes that email an address someone types in: the
sign-in email (POST /auth/magic-link/send, anyone) and the email-change
confirmation (POST /api/dashboard/email, any signed-in account).

Each route counts on its own ("scope"):

- per address: ADDRESS_LIMITS emails to one address. Over it nothing is
  sent, and the route gives the answer a sent email gets: another answer
  would tell the asker something about the address (that it was asked for
  recently). The sign-in route counts its stored links
  (backend/utils/sign_in_links.py); the email-change route counts here.
- per account (email change): ACCOUNT_LIMITS requests by one account.
- per client: CLIENT_LIMITS requests from one client: the address the
  request came from, as ProxyFix reads it from the last X-Forwarded-For
  entry (the one the proxy appends); an IPv6 client by its /64.

Over the account or client limit the answer is a 429 with WAIT_MESSAGE,
which says nothing about any address: the web and iPhone show it as they
show any error the route answers.

Counters live in Redis (SET NX EX, then INCR, in one transaction), so they
hold across the web processes; a counter starts at its first request and
ends a window later. Their names hold a hash of the key, never an address.
Without Redis (tests, a local run without it, an outage) each process
counts on its own: a limit must not stop sign-in.
"""
import hashlib
import hmac
import ipaddress
import logging
import threading
import time

from flask import current_app

log = logging.getLogger(__name__)

# ── Heuristics (named here, flagged in the PR) ────────────────────────────
# Emails one address can receive from one route: 5 an hour, 10 a day. A
# person who does not find the email asks again after a minute or two,
# perhaps twice, and once more from another device: four in a few
# minutes. Ten a day covers that twice over.
ADDRESS_LIMITS = ((3600, 5), (24 * 3600, 10))
# Email-change requests one account can make: the same numbers. One person
# changing their address mistypes it once or twice and asks for the link
# again; nobody changes it ten times a day.
ACCOUNT_LIMITS = ADDRESS_LIMITS
# Requests one client can make: 10 in 15 minutes, 30 a day. One person
# stays under the address limit; a mistyped address is a new address from
# the same client, and a household or an office shares one client
# address. The short window keeps "try again in a few minutes" true.
CLIENT_LIMITS = ((15 * 60, 10), (24 * 3600, 30))
# An IPv6 client is counted by its /64, the block a home or mobile
# connection is usually given, as an IPv4 client is by its one address.
IPV6_PREFIX = 64

WAIT_MESSAGE = ("If you asked a moment ago, the email is on its way. Please "
                "check your inbox, or try again in a few minutes.")

KEY_PREFIX = "loore:send-limit:"

_LOCAL_COUNTS_KEY = "send_limit_counts"
_local_lock = threading.Lock()
_redis_clients = {}


def address_key(address):
    """A keyed hash of an address: the same for every spelling of it (case,
    surrounding spaces), and not computable without the app's secret."""
    secret = (current_app.config.get("SECRET_KEY") or "").encode()
    return hmac.new(secret, (address or "").strip().lower().encode(),
                    hashlib.sha256).hexdigest()


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
    """Count this request in *name*; returns the count in its window."""
    try:
        client = _redis()
        if client is not None:
            pipe = client.pipeline()
            pipe.set(name, 0, ex=window, nx=True)
            pipe.incr(name)
            return int(pipe.execute()[1])
    except Exception as e:  # noqa: BLE001 - a limit must not stop sign-in
        log.warning("Send limits: Redis unavailable (%s); counting in this "
                    "process", type(e).__name__)
    return _count_here(name, window)


def allow(scope, kind, key, limits):
    """Count a request under *key* in every window of *limits*; False when
    it is over any of them."""
    digest = hashlib.sha256(str(key).encode()).hexdigest()[:32]
    allowed = True
    for window, most in limits:
        if _count(f"{KEY_PREFIX}{scope}:{kind}:{window}:{digest}",
                  window) > most:
            allowed = False
    return allowed


def client_may_ask(scope, remote_addr):
    return allow(scope, "client", client_bucket(remote_addr), CLIENT_LIMITS)


def account_may_ask(scope, user_id):
    return allow(scope, "account", user_id, ACCOUNT_LIMITS)


def address_may_receive(scope, address):
    return allow(scope, "address", address_key(address), ADDRESS_LIMITS)

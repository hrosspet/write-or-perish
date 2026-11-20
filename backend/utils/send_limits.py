"""Limits on the routes that email an address someone types in: the
sign-in email (POST /auth/magic-link/send, anyone) and the email-change
confirmation (POST /api/dashboard/email, any signed-in account).

Each route counts on its own ("scope"):

- per address: ADDRESS_LIMITS emails to one address, and
- per account (email change): ACCOUNT_LIMITS emails asked for by one
  account. These two count emails handed to the mail relay: a request
  over the limit, or whose send fails, gives its count back (claim, then
  release). Over the address limit nothing is sent, and the route gives
  the answer a sent email gets: another answer would tell the asker that
  the address was asked for recently.
- per client: the client's requests, sent or not, at most
  SEND_LIMIT_CLIENT_PER_15_MIN in 15 minutes and SEND_LIMIT_CLIENT_PER_DAY
  a day (app config, from the environment). The client is the address the
  request came from, as ProxyFix reads it from the last X-Forwarded-For
  entry (the one the proxy appends); an IPv6 client by its /64.

Over the account or client limit the answer is a 429 with WAIT_MESSAGE,
which says nothing about any address: the web and iPhone show it as they
show any error the route answers.

Counters live in Redis: SET NX EX, then INCR, in one transaction, so a
count and its check are one atomic step however many requests run at
once; a counter starts at its first request and ends a window later. Their
names hold a hash of the key, never an address. Without Redis (tests, a
local run without it, an outage) each process counts on its own, under a
lock: a limit must not stop sign-in.
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
# Email-change emails one account can ask for: the same numbers. One
# person changing their address mistypes it once or twice and asks for the
# link again; nobody changes it ten times a day.
ACCOUNT_LIMITS = ADDRESS_LIMITS
# Requests one client can make, unless the config says otherwise
# (SEND_LIMIT_CLIENT_PER_15_MIN, SEND_LIMIT_CLIENT_PER_DAY): 60 in 15
# minutes, 200 a day. One network address can be a household, an office,
# a campus or a phone carrier's NAT, and a sign-in takes one to three
# requests: this lets a workshop of 30 to 60 people sign up together within
# a quarter of an hour, and a busy office all day.
CLIENT_PER_15_MIN = 60
CLIENT_PER_DAY = 200
# An IPv6 client is counted by its /64, the block a home or mobile
# connection is usually given, as an IPv4 client is by its one address.
IPV6_PREFIX = 64

WAIT_MESSAGE = ("If you asked a moment ago, the email is on its way. Please "
                "check your inbox, or try again in a few minutes.")

KEY_PREFIX = "loore:send-limit:"

_LOCAL_COUNTS_KEY = "send_limit_counts"
_local_lock = threading.Lock()
_redis_clients = {}

# DECR only a counter that exists, so a give-back never creates a key
# without a TTL; never below zero.
_RELEASE_SCRIPT = """
local n = redis.call('get', KEYS[1])
if n and tonumber(n) > 0 then
  return redis.call('decr', KEYS[1])
end
return 0
"""


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


def client_limits():
    config = current_app.config
    return ((15 * 60, int(config.get("SEND_LIMIT_CLIENT_PER_15_MIN")
                          or CLIENT_PER_15_MIN)),
            (24 * 3600, int(config.get("SEND_LIMIT_CLIENT_PER_DAY")
                            or CLIENT_PER_DAY)))


# ── Counters ─────────────────────────────────────────────────────────────

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


def _local_counts():
    return current_app.extensions.setdefault(_LOCAL_COUNTS_KEY, {})


def _add_here(name, window, step):
    """Add *step* to *name* in this process (Redis is not there). Per app,
    so every test app starts from zero."""
    now = time.monotonic()
    with _local_lock:
        counts = _local_counts()
        if len(counts) > 10000:
            for stale in [k for k, (until, _) in counts.items() if until <= now]:
                del counts[stale]
        until, n = counts.get(name, (0, 0))
        if until <= now:
            if step < 0:
                return 0
            until, n = now + window, 0
        n = max(n + step, 0)
        counts[name] = (until, n)
        return n


def _incr(name, window):
    """Count one in *name*; returns the count in its window."""
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
    return _add_here(name, window, 1)


def _decr(name, window):
    try:
        client = _redis()
        if client is not None:
            client.eval(_RELEASE_SCRIPT, 1, name)
            return
    except Exception as e:  # noqa: BLE001 - the window ends it anyway
        log.warning("Send limits: Redis unavailable (%s); giving back in "
                    "this process", type(e).__name__)
    _add_here(name, window, -1)


def _names(scope, kind, key, limits):
    digest = hashlib.sha256(str(key).encode()).hexdigest()[:32]
    return [(f"{KEY_PREFIX}{scope}:{kind}:{window}:{digest}", window, most)
            for window, most in limits]


class Claim:
    """Counts taken for one email; release() gives them back."""

    def __init__(self, counters):
        self._counters = counters

    def release(self):
        counters, self._counters = self._counters, []
        for name, window in counters:
            _decr(name, window)


def claim(scope, kind, key, limits):
    """Count one email under *key* in every window of *limits*. A Claim
    when it is within all of them; None when over any (its counts are
    given back at once, so a refused request uses nothing up)."""
    taken = []
    within = True
    for name, window, most in _names(scope, kind, key, limits):
        taken.append((name, window))
        if _incr(name, window) > most:
            within = False
    held = Claim(taken)
    if not within:
        held.release()
        return None
    return held


def claim_address(scope, address):
    return claim(scope, "address", address_key(address), ADDRESS_LIMITS)


def claim_account(scope, user_id):
    return claim(scope, "account", user_id, ACCOUNT_LIMITS)


def client_may_ask(scope, remote_addr):
    """Count a request from *remote_addr* (every request counts, sent or
    not); False when its client is over the limits."""
    allowed = True
    for name, window, most in _names(scope, "client",
                                     client_bucket(remote_addr),
                                     client_limits()):
        if _incr(name, window) > most:
            allowed = False
    return allowed


def release(*claims):
    """Give back the counts of the claims that are not None (a send that
    failed, or a request that sent nothing)."""
    for held in claims:
        if held is not None:
            try:
                held.release()
            except Exception:  # noqa: BLE001 - never fails the request
                log.exception("Send limits: giving a count back failed")

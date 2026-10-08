"""Backend cache of the assembled agentic system prompt (#192).

Since #191 pinned all context artifacts to a per-session snapshot, the
system node's fully-rendered text is byte-identical on every turn of a
session. We render it once, store it in Redis (encrypted — it embeds
profile/todo/memory content that is encrypted at rest in the DB), and
reuse the exact bytes on subsequent turns. Reusing identical bytes also
guarantees the byte-stable prefix that provider-side prompt caching
(#187) requires — re-rendering each turn risks subtle nondeterminism.

Write-once per (node_id, updated_at, variant): one-off prompt edits
change updated_at and so naturally take a fresh key. *variant* folds in
the per-user gates that change the rendered text without touching the
node (the share and external-references guidance toggles, #329), so a
toggle flip mid-thread takes a fresh key instead of serving the stale
render for the rest of the TTL. TTL bounds growth; an expired entry just
means one re-render.

An entry also carries the render's training-key verdict (#326): the
render embeds the user's own rows (profile, memory, the recent raw
archive, ...), each licensed for training or not by its own ai_usage, and
a turn served from the cache resolves none of them. The verdict is
written with the text, and a turn replays it into its licence. A
licensed ('train') verdict also stores the ids of the rows it rests on:
any of them can be switched off 'train' later without touching the key,
so a hit re-checks them (metadata only) and reads as a miss when one
has lost its licence — the render is rebuilt, and the switch honored.
"""
import json
import logging
import time
import uuid
from collections import namedtuple

from backend.utils.encryption import decrypt_content, encrypt_content

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 24 * 3600
# v2: entries carry the training-key verdict next to the text (#326).
# Entries from before are never read as verdict-less hits; they expire.
_KEY_PREFIX = "wop:sysprompt:v2:"

# *unlicensed* is why the render's own rows keep a payload off the
# training key (``ContextUsage.reason``), or None when they are all
# licensed.
CachedRender = namedtuple("CachedRender", ["text", "unlicensed"])


def _client(config):
    import redis
    url = config.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
    return redis.Redis.from_url(
        url, socket_connect_timeout=2, socket_timeout=2)


def _key(node, variant=""):
    stamp = (node.updated_at or node.created_at)
    return (f"{_KEY_PREFIX}{node.id}:"
            f"{stamp.isoformat() if stamp else '0'}:{variant}")


def get_cached_render(config, node, variant=""):
    """Return the cached render of *node* under *variant* as a
    ``CachedRender``, or None.

    Any Redis/decrypt failure degrades to a cache miss — the caller
    re-renders as before #192.
    """
    try:
        blob = _client(config).get(_key(node, variant))
        if blob is None:
            return None
        entry = json.loads(decrypt_content(blob.decode("utf-8")))
        unlicensed = entry.get("unlicensed")
        if unlicensed is None and entry.get("rows"):
            from backend.utils.api_keys import rows_lost_licence
            lost = rows_lost_licence(entry["rows"])
            if lost:
                logger.info("Cached render of node %s: %s; re-rendering",
                            node.id, lost)
                return None
        return CachedRender(entry["text"], unlicensed)
    except Exception:
        logger.warning("Prompt cache read failed; re-rendering",
                       exc_info=True)
        return None


def store_render(config, node, rendered_text, variant="",
                 unlicensed=None, rows=None):
    """Store the rendered text (encrypted) with its training-key verdict
    (see ``CachedRender``) and, for a licensed one, the rows it rests on
    (``ContextUsage.rows``). Failures are non-fatal."""
    entry = json.dumps({
        "text": rendered_text, "unlicensed": unlicensed,
        "rows": None if unlicensed else rows,
    })
    try:
        _client(config).setex(
            _key(node, variant), CACHE_TTL_SECONDS,
            encrypt_content(entry).encode("utf-8"),
        )
    except Exception:
        logger.warning("Prompt cache write failed", exc_info=True)


# ── The voice pre-warm's "finished" signal (#187) ────────────────────────
#
# A voice reply that had a cache pre-warm sent for it (finalize, #187)
# waits for that pre-warm to finish before its first Anthropic call. A
# cache entry exists only once the warm's request has been processed: a
# reply that calls earlier reads nothing from the cache and writes the
# whole prompt again, and the warm was paid for nothing. The pre-warm
# sets a key on every exit (wrote the cache, failed, skipped); the reply
# polls for it, up to PREWARM_WAIT_MAX_SECONDS. A reply with no token
# (no pre-warm was sent for it) never waits, and without Redis nothing
# waits.

_PREWARM_DONE_PREFIX = "wop:prewarm_done:"
# INTRODUCED CONSTANT: how long the signal stays readable after the
# pre-warm ends. It has to outlast finalize's wait for the last chunks
# (up to 10 min) and the reply's time in the queue: a reply that finds no
# signal waits the full cap. An hour, for keys of a few bytes.
PREWARM_DONE_TTL_SECONDS = 3600
# INTRODUCED CONSTANT: how often the reply checks for the signal; a
# finished warm adds at most this much to the reply's wait.
PREWARM_WAIT_POLL_SECONDS = 0.2
# The default of Config.PREWARM_WAIT_MAX_SECONDS (Peter, 2026-10-09: a
# bit over twice the longest warm call seen in prod logs, 4.4 s).
PREWARM_WAIT_MAX_SECONDS = 10.0


def new_prewarm_token():
    """A fresh id for one pre-warm: finalize hands it to the pre-warm,
    which signals under it, and to the reply, which waits for it. One per
    warm, so a later recording in the same thread never reads an earlier
    warm's signal."""
    return uuid.uuid4().hex


def mark_prewarm_done(config, token, result):
    """The pre-warm with *token* has finished; *result* is its return
    value ({"status": "ok" | "failed" | "skipped", "reason": ...}),
    recorded for the reply's log line. No token: nothing to signal.
    Failures are non-fatal (the reply then waits up to the cap)."""
    if not token:
        return
    status = (result or {}).get("status") or "failed"
    reason = (result or {}).get("reason")
    value = f"{status}:{reason}" if reason else status
    try:
        _client(config).setex(_PREWARM_DONE_PREFIX + token,
                              PREWARM_DONE_TTL_SECONDS, value)
    except Exception:
        logger.warning("Pre-warm finished signal not written",
                       exc_info=True)


def wait_for_prewarm(config, token, node_id):
    """Block until the pre-warm with *token* has signalled that it
    finished, or until PREWARM_WAIT_MAX_SECONDS (config) have passed.
    Returns the seconds waited. Logs one line per call, for *node_id*
    (the reply). Never raises: a Redis failure ends the wait at once."""
    if not token:
        return 0.0
    started = time.monotonic()
    try:
        max_wait = config.get("PREWARM_WAIT_MAX_SECONDS")
        # 0 turns the wait off.
        max_wait = float(PREWARM_WAIT_MAX_SECONDS if max_wait is None
                         else max_wait)
        client = _client(config)
        key = _PREWARM_DONE_PREFIX + token
        deadline = started + max_wait
        while True:
            value = client.get(key)
            now = time.monotonic()
            if value is not None:
                outcome = (value.decode("utf-8", "replace")
                           if isinstance(value, bytes) else str(value))
                logger.info(
                    "prewarm-wait node=%s waited=%.1fs finished=yes "
                    "prewarm=%s", node_id, now - started, outcome)
                return now - started
            if now >= deadline:
                logger.info(
                    "prewarm-wait node=%s waited=%.1fs finished=no "
                    "(the reply writes the cache itself)",
                    node_id, now - started)
                return now - started
            time.sleep(min(PREWARM_WAIT_POLL_SECONDS, deadline - now))
    except Exception:
        waited = time.monotonic() - started
        logger.warning(
            "prewarm-wait node=%s waited=%.1fs finished=unknown "
            "(signal unreadable, not waiting)", node_id, waited,
            exc_info=True)
        return waited

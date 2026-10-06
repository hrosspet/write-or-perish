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

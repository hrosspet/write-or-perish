"""Backend cache of the assembled agentic system prompt (#192).

Since #191 pinned all context artifacts to a per-session snapshot, the
system node's fully-rendered text is byte-identical on every turn of a
session. We render it once, store it in Redis (encrypted — it embeds
profile/todo/memory content that is encrypted at rest in the DB), and
reuse the exact bytes on subsequent turns. Reusing identical bytes also
guarantees the byte-stable prefix that provider-side prompt caching
(#187) requires — re-rendering each turn risks subtle nondeterminism.

Write-once per (node_id, updated_at): one-off prompt edits change
updated_at and so naturally take a fresh key. TTL bounds growth; an
expired entry just means one re-render.
"""
import logging

from backend.utils.encryption import decrypt_content, encrypt_content

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 24 * 3600
_KEY_PREFIX = "wop:sysprompt:"


def _client(config):
    import redis
    url = config.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
    return redis.Redis.from_url(
        url, socket_connect_timeout=2, socket_timeout=2)


def _key(node):
    stamp = (node.updated_at or node.created_at)
    return f"{_KEY_PREFIX}{node.id}:{stamp.isoformat() if stamp else '0'}"


def get_cached_render(config, node):
    """Return the cached rendered text for *node*, or None.

    Any Redis/decrypt failure degrades to a cache miss — the caller
    re-renders as before #192.
    """
    try:
        blob = _client(config).get(_key(node))
        if blob is None:
            return None
        return decrypt_content(blob.decode("utf-8"))
    except Exception:
        logger.warning("Prompt cache read failed; re-rendering",
                       exc_info=True)
        return None


def store_render(config, node, rendered_text):
    """Store the rendered text (encrypted). Failures are non-fatal."""
    try:
        _client(config).setex(
            _key(node), CACHE_TTL_SECONDS,
            encrypt_content(rendered_text).encode("utf-8"),
        )
    except Exception:
        logger.warning("Prompt cache write failed", exc_info=True)

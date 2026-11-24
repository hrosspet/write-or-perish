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
Every entry also stores the ids of the entries (nodes) the render holds,
and a hit reads as a miss when one of them has since been deleted, set
to AI usage None, or made unreadable to the user: the render is rebuilt
the way an uncached turn renders it.
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
# v3: renders end without trailing whitespace (system_block_text); a v2
# render ending in a newline would keep the ongoing-thread warm missing.
# v4: inline artifacts carry their version header (#604); a v3 render
# would leave an ongoing thread without the versions its writes name.
# v5: in a call with tools, the recent raw window names a quoted saved
# reference in one line instead of giving its text (2026-11-12); a v4
# render would keep the text for the rest of its TTL.
# Since #689 an entry may also carry "inputs" (X API input in its rows).
# The key stays v5: an account that has had an X API pre-fill renders
# under its own variant (llm_completion._render_variant's `x`), so it
# never reads an entry from before. Another account's render holds X API
# input only when it quotes someone else's public pre-filled post; one
# cached before #689 replays its verdict (the call still goes out on the
# chat key) but not the mark, for the rest of its 24 h TTL.
_KEY_PREFIX = "wop:sysprompt:v5:"

# *unlicensed* is why the render's own rows keep a payload off the
# training key (``ContextUsage.reason``), or None when they are all
# licensed.
# *references* are the saved references its recent raw window names by
# the name read_full opens them by (ContextUsage.named_references).
# *inputs* is {X_API_INPUT} when its rows held X API input
# (ContextUsage.inputs, #689): what a reply written from it is marked with.
CachedRender = namedtuple("CachedRender",
                          ["text", "unlicensed", "references", "inputs"],
                          defaults=((), ()))


def _client(config):
    import redis
    url = config.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
    return redis.Redis.from_url(
        url, socket_connect_timeout=2, socket_timeout=2)


def _key(node, variant=""):
    stamp = (node.updated_at or node.created_at)
    return (f"{_KEY_PREFIX}{node.id}:"
            f"{stamp.isoformat() if stamp else '0'}:{variant}")


def _reader(node, user_id):
    """Whom the render is for: *user_id*, else the node's owner."""
    return user_id if user_id is not None else (
        node.human_owner_id or node.user_id)


def _readable_entries(node_ids, user_id):
    """The ids among *node_ids* (entries a render holds) that a render
    for *user_id* may still hold: not deleted, AI usage Chat or Train,
    and the user's own or readable by them — what the uncached render's
    window and quotes apply. Metadata only: one query, nothing
    decrypted."""
    from backend.extensions import db
    from backend.models import Node
    from backend.utils.privacy import AI_ALLOWED, accessible_nodes_filter

    ids = list({int(i) for i in node_ids or ()})
    if not ids:
        return set()
    return {i for (i,) in db.session.query(Node.id).filter(
        Node.id.in_(ids),
        Node.ai_usage.in_(AI_ALLOWED),
        accessible_nodes_filter(Node, user_id),
    ).all()}


def get_cached_render(config, node, variant="", user_id=None):
    """Return the cached render of *node* under *variant* as a
    ``CachedRender``, or None.

    A render holds the entries of the user's (*user_id*, default: the
    node's owner) recent window and its quotes, and none of them changes
    the key. A hit re-checks them: when one has since been deleted, set
    to AI usage None or (someone else's) made unreadable to the user,
    it reads as a miss, and the render is rebuilt without it. An
    unchanged conversation keeps its bytes. Entries stored before this
    check carry no entry ids and are served until they expire.

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
        held = set(entry.get("nodes") or ())
        if held:
            withdrawn = held - _readable_entries(held, _reader(node, user_id))
            if withdrawn:
                logger.info("Cached render of node %s: %d of its entries "
                            "were withdrawn; re-rendering",
                            node.id, len(withdrawn))
                return None
        return CachedRender(entry["text"], unlicensed,
                            tuple(entry.get("references") or ()),
                            tuple(entry.get("inputs") or ()))
    except Exception:
        logger.warning("Prompt cache read failed; re-rendering",
                       exc_info=True)
        return None


def store_render(config, node, rendered_text, variant="",
                 unlicensed=None, rows=None, references=(), inputs=(),
                 user_id=None):
    """Store the rendered text (encrypted) with its training-key verdict
    (see ``CachedRender``), for a licensed one the rows it rests on
    (``ContextUsage.rows``), the saved references its window names
    (*references*) and whether its rows held X API input (*inputs*).
    With every verdict, the ids of the entries among *rows* that the
    render for *user_id* (default: the node's owner) may hold now, which
    a hit re-checks (get_cached_render). Failures are non-fatal: the
    render is then not stored."""
    try:
        nodes = _readable_entries((rows or {}).get("node"),
                                  _reader(node, user_id))
        entry = json.dumps({
            "text": rendered_text, "unlicensed": unlicensed,
            "rows": None if unlicensed else rows,
            "nodes": sorted(nodes),
            "references": sorted(references or ()),
            "inputs": sorted(inputs or ()),
        })
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

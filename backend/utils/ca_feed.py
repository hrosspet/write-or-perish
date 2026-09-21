"""The Community Archive feed reply (PoC, 2026-09-13; quote-tweet form
2026-09-16).

A prompt carrying {ca_tweets} asks one question of a day of tweets: is
there anything this person would benefit from reading? The batch call
answers in a fixed shape (FEED_SCHEMA): a prose verdict plus up to
MAX_PICKS picks, each a tweet number from the render with the model's
quote-tweet of it (its own words, addressed to the reader, on why this
tweet meets their situation), a relevance estimate and a recommend flag.

On collect, every pick becomes a saved reference (ExternalItem, source
'community_archive') so the user's read mark and good/bad verdict work
on it like on any other reference, and a FeedPick row keeps what the
model claimed, so the claims can be judged against the verdicts later.

The reply node's text is the whole rendered feed: the verdict, then each
quote-tweet followed by a {quote_ext:ID} marker for the tweet it quotes.
Everything the thread page shows is therefore in the node content, which
is what later turns (a second read under the same thread), exports and
search see; the marker resolves to the reference with the user's marks on
it (quotes.resolve_ext_quotes), so the next read knows what was already
surfaced, read and rated.

Turns of a read thread (2026-09-19, after the second feedback round):
the day of tweets is fed to the model only for a READ — the reply that
answers the read prompt, and any reply requested directly under a read
reply with nothing in between (a "read again": the model sees its
earlier picks and the user's marks on them, and whether to repeat a
pick is its call). A user message after a read reply makes the next
reply a CHAT turn: the picks and marks are in the context, the day of
tweets is not (see llm_completion._ca_turn). What the reader has already
seen — read-marked picks, X bookmarks, clipped tweets — is dropped from
the render before the model sees it (seen_tweet_ids), and each render's
numbering is pinned on the reply (FeedRender) so a batch collected hours
later resolves its numbers against what was actually sent.
"""
import json
import logging
from datetime import datetime

log = logging.getLogger(__name__)

MAX_PICKS = 20

# What the model reads in place of {ca_tweets} on a chat turn: the day
# was fed in once, for the read whose reply sits below the prompt.
CA_TWEETS_CHAT_STUB = (
    "(The day's tweets were provided for the read whose reply follows; "
    "they are not repeated in this turn.)")

# The user turn that closes a read-further request (the Read button
# anywhere in a read thread, or a reply asked for directly under a read
# reply). It replaces the generic "[continue]".
CA_READ_AGAIN_TURN = (
    "Read further: go through the day again against everything above — "
    "your earlier picks, my marks on them, and whatever I have written "
    "since. Tweets I have read are out of the list; an unread earlier "
    "pick is yours to repeat if it still stands. Beyond that, find what "
    "else is worth my time. Answer in the same shape, a verdict and the "
    "picks.")

# tool_calls_meta entry name on a placeholder the Read button created
# inside a read thread: the task reads it as "this turn is a read", not
# a chat about the picks (routes/read.py, llm_completion._ca_turn).
READ_FURTHER_MARKER = "_read"

# References the reader saved from X themselves: seen by definition. A
# clipped tweet is stored under the bookmark source (web_clip.classify_clip).
# Nothing writes twitter_like yet (the frontend already labels it); listed
# so a saved like counts as seen the day something does.
SEEN_SOURCES = ("twitter_bookmark", "twitter_like")

# The closing note of a chat turn in a read thread: the read prompt above
# still asks for a verdict and picks, this turn answers the message.
CA_CHAT_TURN_NOTE = (
    "[This turn is a conversation about the picks above, not another "
    "read: the day's tweets are not in this turn, and the answer is an "
    "ordinary reply to the last message, not a verdict with picks.]")

# Prompt keys of the two reading entry points (backend/routes/read.py).
READ_PROMPT_KEYS = ("read", "read_thread")

# The feed carries other people's public tweets, which Loore has no
# licence to train on: every node of a read (the prompt node and the
# reply) is created with this usage, and the node editor refuses to
# raise it (see is_feed_node).
FEED_AI_USAGE = "chat"

FEED_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "description": "One short paragraph: is there anything here "
                           "worth the reader's time today, and why or why "
                           "not.",
        },
        "picks": {
            "type": "array",
            "description": "The tweets worth the reader's time, best "
                           "first, at most 20. Do not pad: on most days "
                           "there are a few or none; an empty list is a "
                           "valid answer.",
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer",
                          "description": "The tweet's number in the "
                                         "corpus, e.g. 123 for #123."},
                    "qt": {"type": "string",
                           "description": "Your quote-tweet of it: two or "
                                          "three sentences in your own "
                                          "voice, addressed to the reader, "
                                          "saying what in this tweet meets "
                                          "their situation and what it "
                                          "would change. Shown above the "
                                          "tweet."},
                    "relevance": {"type": "integer",
                                  "description": "0-100: the probability "
                                                 "that reading it changes "
                                                 "what the reader does "
                                                 "this week."},
                    "recommend": {"type": "boolean",
                                  "description": "True only for the "
                                                 "tweets you would "
                                                 "actually tell the "
                                                 "reader to open today."},
                },
                "required": ["n", "qt", "relevance", "recommend"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["verdict", "picks"],
    "additionalProperties": False,
}


class FeedReplyError(ValueError):
    """The batch reply was not the JSON shape FEED_SCHEMA promised."""


def is_feed_node(node):
    """A node that belongs to a read: one of the two read prompts —
    stamped or linked with a read key, or, for the PoC threads of
    2026-09-13, carrying {ca_tweets} in its own text — or a reply that
    named picks. These never carry 'train' usage: the read quotes other
    people's public tweets. Same test as in_read_thread, so the editor
    and the cascade recognise every shape the read routes do.

    Decrypts the node once when the key test misses; callers apply it to
    a single node or to one thread's descendants (utils/node_settings
    prefetches their DEKs first), never to a list of many threads.
    """
    from backend.utils.placeholders import CA_TWEETS_PATTERN
    key = node.get_prompt_key() if hasattr(node, "get_prompt_key") else None
    if key in READ_PROMPT_KEYS:
        return True
    if bool(getattr(node, "feed_picks", None)):
        return True
    return bool(CA_TWEETS_PATTERN.search(node.get_content() or ""))


def is_read_reply(node):
    """A reply that answered a read: it has a pinned render (every read
    since 2026-09-19), a batch entry (the batch path before that) or
    picks (the live path before that; a pick-less live reply from then
    is the one shape this misses)."""
    if node is None or not (node.node_type == "llm" or node.llm_model):
        return False
    if '"_batch"' in (node.tool_calls_meta or ""):
        return True  # no query
    if getattr(node, "feed_render", None) is not None:
        return True
    return bool(getattr(node, "feed_picks", None))


def in_read_thread(node):
    """True when *node* sits in a read thread: it or an alive ancestor is
    a read prompt — stamped or linked with a read key, or, for the PoC
    threads of 2026-09-13 whose prompt text was copied into the node,
    carrying the {ca_tweets} placeholder in its content. The same test
    the completion task applies when it looks for the day (ca_node), so
    the Read button and the task agree on what a read thread is. Walks
    the chain and decrypts each ancestor once (a KMS call per node): a
    click on Read, not a list."""
    from backend.utils.placeholders import CA_TWEETS_PATTERN
    seen = set()
    current = node
    while current is not None and current.id not in seen:
        seen.add(current.id)
        if current.deleted_at is None:
            if current.get_prompt_key() in READ_PROMPT_KEYS:
                return True
            if CA_TWEETS_PATTERN.search(current.get_content() or ""):
                return True
        current = current.parent
    return False


def read_reply_ids(node_chain):
    """Ids of the read replies in *node_chain* (see is_read_reply), in two
    queries for the whole chain instead of two lazy loads per node."""
    from backend.extensions import db
    from backend.models import FeedPick, FeedRender
    llm_ids = [n.id for n in node_chain
               if n.node_type == "llm" or n.llm_model]
    if not llm_ids:
        return frozenset()
    found = {n.id for n in node_chain
             if n.id in llm_ids and '"_batch"' in (n.tool_calls_meta or "")}
    rest = [i for i in llm_ids if i not in found]
    if rest:
        found.update(r[0] for r in db.session.query(FeedRender.node_id)
                     .filter(FeedRender.node_id.in_(rest)).all())
        found.update(r[0] for r in db.session.query(FeedPick.node_id)
                     .filter(FeedPick.node_id.in_(rest)).distinct().all())
    return frozenset(found)


def seen_tweet_ids(user_id):
    """Tweet ids the reader has already seen, to drop from a render before
    the model sees the day: every reference they saved from X (bookmarks,
    likes, clipped tweets) and every archive pick they marked as read.
    An earlier pick they have NOT marked stays a candidate — inside the
    same thread the model sees it with its marks and decides itself
    whether to pick it again."""
    from sqlalchemy import and_, or_
    from backend.extensions import db
    from backend.models import ExternalItem
    rows = (db.session.query(ExternalItem.external_id)
            .filter(ExternalItem.user_id == user_id,
                    or_(ExternalItem.source.in_(SEEN_SOURCES),
                        and_(ExternalItem.source == "community_archive",
                             ExternalItem.read_at.isnot(None))))
            .all())
    return {r[0] for r in rows if r[0]}


def record_feed_render(node, stats, refs, days=1, scope="all"):
    """Pin what this reply's render sent the model (see FeedRender). Adds
    to the session (or updates the reply's existing row on a rerun); the
    caller's next commit lands it, before any batch is submitted."""
    from backend.extensions import db
    from backend.models import FeedRender
    row = FeedRender.query.filter_by(node_id=node.id).first()
    if row is None:
        row = FeedRender(node_id=node.id)
    row.export_id = stats.get("export_id")
    row.days = int(days)
    row.scope = scope or "all"
    row.window_start = stats.get("window_start_at")
    row.window_end = stats.get("window_end_at")
    row.tweet_count = int(stats.get("tweets") or 0)
    row.account_count = int(stats.get("accounts") or 0)
    row.excluded_count = int(stats.get("excluded") or 0)
    row.set_tweet_ids(refs[n]["tweet_id"] for n in sorted(refs))
    row.created_at = datetime.utcnow()
    db.session.add(row)
    return row


def read_window_fields(row):
    """The reply page's record of what a read covered, from its pinned
    render (None for replies from before renders were pinned)."""
    from backend.utils.timefmt import iso_utc
    if row is None:
        return None
    return {
        "export_id": row.export_id,
        "days": row.days,
        "scope": row.scope,
        "window_start": iso_utc(row.window_start),
        "window_end": iso_utc(row.window_end),
        "tweets": row.tweet_count,
        "accounts": row.account_count,
        "excluded": row.excluded_count,
    }


def pick_numbers(text):
    """The tweet numbers a feed reply cites, in its order (unknown shapes
    yield nothing; parse_feed_reply reports those)."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, dict) or not isinstance(data.get("picks"), list):
        return []
    numbers = []
    for raw in data["picks"]:
        if not isinstance(raw, dict):
            continue
        try:
            numbers.append(int(raw.get("n")))
        except (TypeError, ValueError):
            continue
    return numbers


def refs_from_render(row, reply_text, snapshot_dir):
    """The refs a pinned reply's picks need, without re-rendering the day:
    each cited number maps to the tweet id the model saw under it
    (FeedRender.tweet_id_for), and those few tweets are fetched by id
    from the snapshot — the current one; ids are stable across exports.
    A tweet the snapshot no longer holds drops its pick (parse_feed_reply
    logs it as an unknown number)."""
    from backend.utils.community_archive import (
        CA_CITATION_RE, fetch_tweets_by_id)
    numbers = pick_numbers(reply_text)
    # Numbers cited in the verdict prose too, so expand_ca_citations can
    # link them (they need no pick row, just the tweet behind them).
    try:
        verdict = (json.loads(reply_text) or {}).get("verdict") or ""
    except (TypeError, ValueError, AttributeError):
        verdict = ""
    numbers += [int(m.group(1)) for m in CA_CITATION_RE.finditer(verdict)]
    wanted = {}
    for n in numbers:
        tweet_id = row.tweet_id_for(n)
        if tweet_id:
            wanted[n] = tweet_id
    found = fetch_tweets_by_id(snapshot_dir, wanted.values()) if wanted else {}
    refs = {}
    for n, tweet_id in wanted.items():
        ref = found.get(tweet_id)
        if ref is None:
            log.warning("Feed pick #%s: tweet %s is not in the current "
                        "snapshot; dropping it", n, tweet_id)
            continue
        refs[n] = ref
    return refs


def legacy_batches_live():
    """Read replies still processing on a batch submitted before renders
    were pinned (no FeedRender row). Their collect re-renders the day
    from the snapshot and needs the numbering to match the submit, so
    the snapshot must not change under them. Empties out once those
    batches end; only matters across the deploy that introduced pinning."""
    from backend.models import Node, FeedRender
    rows = (Node.query
            .outerjoin(FeedRender, FeedRender.node_id == Node.id)
            .filter(Node.node_type == "llm",
                    Node.llm_task_status == "processing",
                    Node.deleted_at.is_(None),
                    Node.tool_calls_meta.like('%"_batch"%'),
                    FeedRender.id.is_(None))
            .all())
    for node in rows:
        try:
            meta = json.loads(node.tool_calls_meta or "[]") or []
        except (json.JSONDecodeError, TypeError):
            continue
        if any(isinstance(m, dict) and m.get("name") == "_batch"
               and m.get("status") in ("submitted", "cancelling")
               for m in meta):
            return True
    return False


def refresh_snapshot_for_read(snapshot_dir, log=log):
    """Bring the cached archive up to the latest nightly export (the
    Community Archive exports around 07:00 UTC) so "the last day" is the
    last day and not the day the snapshot was last fetched. Called by
    the read before it renders and by the beat sweep. Only maintains a
    snapshot that exists; a failed refresh is logged and the read goes
    on with the cached export (its reply shows the window it covered
    either way). Returns the export id in place, or None when nothing
    was checked."""
    from backend.utils import community_archive as ca
    if not ca.snapshot_export_id(snapshot_dir):
        return None
    if legacy_batches_live():
        log.info("Community Archive snapshot refresh skipped: a batch "
                 "submitted before renders were pinned is still live")
        return None
    try:
        export_id, refreshed = ca.refresh_snapshot(snapshot_dir)
    except Exception as e:  # noqa: BLE001 - the read proceeds on the cache
        log.warning("Community Archive snapshot refresh failed; reading "
                    "the cached export: %s", e)
        return None
    if refreshed:
        log.info("Community Archive snapshot refreshed to %s", export_id)
    return export_id


def parse_feed_reply(text, refs):
    """Parse the model's JSON reply against the render's ``refs``
    ({n: {username, tweet_id, text, posted_at}}).

    Returns (verdict, picks) with picks normalized: unknown or repeated
    numbers dropped, relevance clamped to 0-100, at most MAX_PICKS, in
    the model's order with ``rank`` 1..k. The quote-tweet text is
    ``qt`` (``why`` is read as a fallback for the PoC's shape). Raises
    FeedReplyError when the text is not the promised object."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError) as e:
        raise FeedReplyError(f"feed reply is not JSON: {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("picks"), list):
        raise FeedReplyError("feed reply lacks a picks list")
    verdict = (data.get("verdict") or "").strip()
    picks = []
    seen = set()
    for raw in data["picks"]:
        if not isinstance(raw, dict):
            continue
        try:
            n = int(raw.get("n"))
        except (TypeError, ValueError):
            continue
        ref = refs.get(n)
        if ref is None or n in seen:
            log.warning("Feed pick #%s dropped (%s)", n,
                        "unknown number" if ref is None else "repeated")
            continue
        seen.add(n)
        try:
            relevance = int(raw.get("relevance") or 0)
        except (TypeError, ValueError):
            relevance = 0
        picks.append({
            "n": n,
            "rank": len(picks) + 1,
            "qt": (raw.get("qt") or raw.get("why") or "").strip(),
            "relevance": max(0, min(100, relevance)),
            "recommend": bool(raw.get("recommend")),
            "ref": ref,
        })
        if len(picks) >= MAX_PICKS:
            break
    return verdict, picks


def tweet_url(username, tweet_id):
    return f"https://x.com/{username}/status/{tweet_id}"


def save_feed_picks(user_id, node, picks, picked_by=None):
    """Persist the picks of one reply: upsert each tweet as a saved
    reference (dedupes on tweet id against an earlier pick, a bookmark
    sync or a clip of the same tweet under this source) and write one
    FeedPick per tweet, stamped with who chose it (``picked_by``, default
    the reply node's model). Adds to the session; the caller commits.
    Returns the FeedPick rows in rank order.

    Surfacing history is not bumped here: the rendered reply quotes each
    tweet with {quote_ext:ID}, and the finalize path records a surfacing
    for every reference a reply quotes, the same as for any other turn."""
    from backend.extensions import db
    from backend.models import ExternalItem, FeedPick

    # Idempotent per reply: a redelivered collect (or a retry that re-ran
    # inline) must not insert the picks twice.
    existing = (FeedPick.query.filter_by(node_id=node.id)
                .order_by(FeedPick.rank.asc()).all())
    if existing:
        log.info("Feed picks for node %s already saved (%d); keeping them",
                 node.id, len(existing))
        return existing

    rows = []
    for pick in picks:
        ref = pick["ref"]
        item = ExternalItem.query.filter_by(
            user_id=user_id, source="community_archive",
            external_id=str(ref["tweet_id"])).first()
        if item is None:
            item = ExternalItem(
                user_id=user_id, source="community_archive",
                external_id=str(ref["tweet_id"]),
                author_handle=ref["username"],
                url=tweet_url(ref["username"], ref["tweet_id"]),
                posted_at=ref.get("posted_at"),
                public_source=True,  # rendered from the public archive
            )
            item.set_content(ref.get("text") or "")
            db.session.add(item)
            db.session.flush()
        row = FeedPick(
            user_id=user_id, node_id=node.id, external_item_id=item.id,
            rank=pick["rank"], relevance=pick["relevance"],
            recommended=pick["recommend"],
            picked_by=picked_by or getattr(node, "llm_model", None),
        )
        row.set_why(pick["qt"])
        db.session.add(row)
        rows.append(row)
    return rows


def render_feed_reply(verdict, entries):
    """The reply node's text: the verdict, then each pick as the model's
    quote-tweet followed by the {quote_ext:ID} marker of the tweet it
    quotes. ``entries`` is [(qt_text, external_item_id)] in rank order.
    The marker is what the thread page renders as the quoted tweet (with
    the reference's own read mark and good/bad verdict) and what a later
    turn resolves to the tweet plus the user's marks on it."""
    verdict = verdict or "Nothing here would change what you do next."
    parts = [verdict]
    for qt, item_id in entries:
        block = f"{{quote_ext:{int(item_id)}}}"
        if qt:
            block = f"{qt}\n\n{block}"
        parts.append(block)
    return "\n\n".join(parts)

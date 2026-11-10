"""The Community Archive feed reply (PoC, 2026-09-13; quote-tweet form
2026-09-16).

A prompt carrying {ca_tweets} asks one question of a day of tweets: is
there anything this person would benefit from reading? The batch call
answers in a fixed shape (FEED_SCHEMA): a prose verdict plus up to
MAX_PICKS picks, each a tweet number from the render with the model's
quote-tweet of it (its own words, addressed to the reader, on why this
tweet meets their situation), a relevance estimate and a recommend flag.

On collect, every pick gets an ExternalItem row so the user's read mark
and good/bad verdict work on it like on a reference: the tweet's own row
when the user already has one (a bookmark, a clip, an earlier pick),
else a new row with source READ_PICK_SOURCE, which is not a saved
reference (#352: the references list, digest, embeddings and search skip
it, and saving the tweet later turns that row into the reference in
place). A FeedPick row keeps what the model claimed, and the reference
log (utils/reference_log) what the reader did, so picks can be judged.

The reply node's text is the whole rendered feed: the verdict, then each
quote-tweet followed by a {quote_ext:ID} marker for the tweet it quotes.
Everything the thread page shows is therefore in the node content, which
is what later turns (a second read under the same thread), exports and
search see. For the model a later turn resolves each marker to the tweet
by its author and text, with no number (render_read_reply_for_model), and
the user's marks on it come in a note of their own, so the next read knows
what was already surfaced, read and rated. The numbers of a read's list
belong to that read alone: the next one, a day later or after the reader
marked something read, may number its tweets afresh (a live read again
on the same export keeps the list, see below, but the model is told the
same either way).

Turns of a read thread (2026-09-19, after the second feedback round):
the day of tweets is fed to the model only for a READ — the reply that
answers the read prompt, and any reply requested directly under a read
reply with nothing in between (a "read again": the model sees its
earlier picks and the user's marks on them, and whether to repeat a
pick is its call). A user message after a read reply makes the next
reply a CHAT turn: the picks and marks are in the context, the day of
tweets is not (see llm_completion._ca_turn). What the reader has already
read — any tweet row marked read, a pick, a bookmark or a clip — is
dropped from the render before the model sees it (seen_tweet_ids); a
bookmark or clip not marked read stays in, and each render's
numbering is pinned on the reply (FeedRender) so a batch collected hours
later resolves its numbers against what was actually sent. A live read
again on the export of the read before it renders that read's list
unchanged (list_to_keep, #511): the list is the large part of the
request, and a byte-identical one is read from the provider's prompt
cache. The tweets in it the reader has read since are named by number
in a note at the end of the prompt (read_since_note), and a pick of one
is dropped on collect.
"""
import json
import logging
import re
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
_READ_AGAIN_HEAD = (
    "Read further: go through today's tweets again against everything "
    "above — your earlier picks, my marks on them, and whatever I have "
    "written since. Your earlier picks are quoted above by author and "
    "text; they carry no numbers, as the numbers belong to today's list "
    "alone. ")
_READ_AGAIN_TAIL = (
    "an unread earlier pick is yours to repeat if it still stands and is "
    "in today's list, by its number there. Beyond that, find what else is "
    "worth my time. Answer in the same shape, a verdict and the picks.")
CA_READ_AGAIN_TURN = (
    _READ_AGAIN_HEAD + "Tweets I have read are out of that list; "
    + _READ_AGAIN_TAIL)
# The same request after read_since_note (#511): a kept list still holds
# the tweets read since it was made, so that sentence alone says so. Only
# in the closing turn, after the cached prefix.
CA_READ_AGAIN_TURN_KEPT = (
    _READ_AGAIN_HEAD + "Tweets I had read before that list was made are "
    "out of it, and the ones named above are in it but are not "
    "candidates; " + _READ_AGAIN_TAIL)

# tool_calls_meta entry name on a placeholder the Read button created
# inside a read thread: the task reads it as "this turn is a read", not
# a chat about the picks (routes/read.py, llm_completion._ca_turn).
READ_FURTHER_MARKER = "_read"

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
                           "not. Name a tweet by its author, never by "
                           "its number: the reader never sees the "
                           "numbers.",
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
    if has_read_picks(node):
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
    return has_read_picks(node)


def has_read_picks(node):
    """The node named Read picks. Not a quote row: an agentic reply that
    quoted a reference is logged in the same table (kind 'quote', #352)
    and is no read reply."""
    from backend.utils.reference_log import KIND_READ
    return any(p.kind == KIND_READ
               for p in (getattr(node, "feed_picks", None) or []))


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
                     .filter(FeedPick.node_id.in_(rest),
                             FeedPick.kind == "read").distinct().all())
    return frozenset(found)


def ca_turn(node_chain, ca_node, parent_node, reply_ids, requested=False):
    """Which turn of a read thread this reply is. The completion task and
    the placeholder guard (utils/llm_nodes.reply_read_turn, which picks
    the model) both apply this, so they cannot disagree (#355);
    *ca_node* is the newest read prompt in the chain, *reply_ids* the ids
    of the chain's read replies (ca_feed.read_reply_ids), *requested*
    whether the Read button asked for this reply (_read_requested):

    "read"       no reply has answered the read prompt yet: the day is
                 rendered and the model answers with a verdict and picks
                 (also when the user typed something under the prompt
                 before asking for the reply).
    "read_again" the Read button asked for it from anywhere in the
                 thread (*requested*), or the reply was asked for directly
                 under a read reply: another read, further into the day.
                 The day is rendered again (minus what the reader has seen
                 since); the earlier picks, the reader's marks on them and
                 whatever was written since are in the context, and
                 whether to repeat an unread pick is the model's call.
    "chat"       a user message came after a read reply: a conversation
                 about the picks. The day is not rendered; the placeholder
                 reads as a stub, the picks and marks stay in the context.
                 Unlike a read it may run under the agentic prompt (a
                 Text-mode session under the read reply, #323).
    """
    replies = []
    after_prompt = False
    for n in node_chain:
        if n is ca_node:
            after_prompt = True
            continue
        if after_prompt and n.deleted_at is None and n.id in reply_ids:
            replies.append(n)
    if not replies:
        return "read"
    if requested:
        return "read_again"
    if parent_node is not None and replies[-1].id == parent_node.id:
        return "read_again"
    return "chat"


def seen_tweet_ids(user_id):
    """Tweet ids to drop from a render before the model sees the day:
    every tweet the reader marked read, whatever row carries it — a Read
    pick, an X bookmark, a clipped tweet, an archive import (a verdict
    marks read too). A saved tweet not marked read stays in: saving it is
    not reading it, and a pick of it shows the saved reference's marks.
    An earlier pick they have NOT marked stays a candidate too — inside
    the same thread the model sees it with its marks and decides itself
    whether to pick it again."""
    from backend.extensions import db
    from backend.models import ExternalItem, TWEET_SOURCES
    rows = (db.session.query(ExternalItem.external_id)
            .filter(ExternalItem.user_id == user_id,
                    ExternalItem.source.in_(TWEET_SOURCES),
                    ExternalItem.read_at.isnot(None))
            .all())
    return {r[0] for r in rows if r[0]}


def list_to_keep(node_chain, ca_node, reply_ids, user_id, export_id, days,
                 scope):
    """The tweet ids of the list a read again keeps (#511): the list
    pinned on the newest earlier read reply of *user_id*'s in this
    thread (after *ca_node*, the newest read prompt; *reply_ids* from
    read_reply_ids) that was rendered from the same export, window and
    scope. Empty when there is none: the day is rendered afresh, minus
    everything the reader has read.

    The list sits in the read prompt's message, before every later turn,
    and is most of the request. Rendering it again minus the tweets the
    reader read in between (a verdict on a pick marks it read) renumbers
    every tweet after the first one dropped, so a Glean again after a
    rating missed the prompt cache from the list on. Kept, it is
    byte-identical: the render is deterministic for an export and an
    exclusion set, and with the kept ids taken out of the exclusions the
    same tweets are left out as before (unless the reader has cleared a
    read mark since: then that tweet comes back, as it should). The tweets
    of the list read since go in a note instead (read_since_note)."""
    from backend.models import FeedRender
    if not export_id:
        return frozenset()
    candidates = []
    after_prompt = False
    for n in node_chain:
        if n is ca_node:
            after_prompt = True
            continue
        if (after_prompt and n.deleted_at is None and n.id in reply_ids
                and (n.human_owner_id or n.user_id) == user_id):
            candidates.append(n.id)
    if not candidates:
        return frozenset()
    rows = {r.node_id: r for r in FeedRender.query.filter(
        FeedRender.node_id.in_(candidates)).all()}
    for node_id in reversed(candidates):
        row = rows.get(node_id)
        if (row is not None and row.export_id == export_id
                and row.days == int(days) and row.scope == (scope or "all")):
            return frozenset(i for i in (row.tweet_ids or "").split(",") if i)
    return frozenset()


def read_numbers(refs, seen):
    """The numbers in a render (*refs*) whose tweet the reader has read
    (*seen*, seen_tweet_ids): only a kept list (list_to_keep) has any.
    Never a pick: they go in read_since_note, and the collect drops a
    pick of one."""
    return sorted(n for n, ref in refs.items()
                  if str(ref.get("tweet_id")) in seen)


def read_since_note(numbers):
    """The closing note's line for the tweets of a kept list the reader
    has read since it was made (#511), or None. They are named by their
    number in the list, which is how the model names a pick."""
    if not numbers:
        return None
    listed = ", ".join(f"#{n}" for n in numbers)
    return ("[The user has read these tweets of today's list since it was "
            f"made: {listed}. Like every tweet the user has read, they are "
            "not candidates: do not pick them.]")


def record_feed_render(node, stats, refs, days=1, scope="all"):
    """Pin what this reply's render sent the model (see FeedRender). Adds
    to the session (or updates the reply's existing row on a rerun); the
    caller's next commit lands it, before any batch is submitted."""
    from sqlalchemy.exc import IntegrityError
    from backend.extensions import db
    from backend.models import FeedRender
    row = FeedRender.query.filter_by(node_id=node.id).first()
    if row is None:
        # One row per reply (a unique node_id). Two runs of the same reply
        # that render at the same time (a rerun started while the first
        # waited) must not fail on it: the row is inserted in a
        # savepoint, and when the other run's row landed first this one
        # updates that row instead (#435: Peter's double render).
        try:
            with db.session.begin_nested():
                db.session.add(FeedRender(node_id=node.id))
        except IntegrityError:
            pass
        row = FeedRender.query.filter_by(node_id=node.id).first()
    row.export_id = stats.get("export_id")
    row.days = int(days)
    row.scope = scope or "all"
    row.window_start = stats.get("window_start_at")
    row.window_end = stats.get("window_end_at")
    row.tweet_count = int(stats.get("tweets") or 0)
    row.account_count = int(stats.get("accounts") or 0)
    row.excluded_count = int(stats.get("excluded") or 0)
    row.set_tweet_ids(refs[n]["tweet_id"] for n in sorted(refs))
    # A rerun's collect counts its own drops.
    row.dropped_picks = 0
    row.created_at = datetime.utcnow()
    db.session.add(row)
    return row


def mark_read_reply_opened(node, user_id):
    """Record that the reply's owner opened a finished Read reply:
    FeedRender.opened_at, the first time only. Called where the reply's
    text is served to a page the owner is looking at: GET /nodes/<id> of
    the reply, and llm-status when it returns the finished reply to a
    thread page left open while the batch ran, but only for a poll the
    page marked visible (?visible=1): the page keeps polling in a
    background tab, which is not an open. Only the reply's human owner
    counts; another user or an admin
    who can see the node does not, and neither does a reply still being
    generated. One UPDATE matched only while opened_at is null, no
    content read; a node with no FeedRender row matches nothing. Commits
    when it set the time, so callers run it after their last read of
    the session's objects. Returns whether it did."""
    from backend.extensions import db
    from backend.models import FeedRender
    if (node is None or user_id is None
            or not (node.node_type == "llm" or node.llm_model)
            or node.llm_task_status != "completed"
            or node.human_owner_id != user_id):
        return False
    updated = (FeedRender.query
               .filter(FeedRender.node_id == node.id,
                       FeedRender.opened_at.is_(None))
               .update({FeedRender.opened_at: datetime.utcnow()},
                       synchronize_session=False))
    if updated:
        db.session.commit()
    return bool(updated)


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


def count_dropped_picks(reply_text, refs):
    """How many of the model's picks Loore could not show: each cited
    number once, whose tweet is not in *refs* (the render's refs, the ones
    that resolved). A number outside the render and a tweet the snapshot no
    longer holds both count, as parse_feed_reply drops both. A number
    cited only in the verdict is no pick. Shared by the batch collect
    (refs_from_render) and the admin's live rerun (_collect_feed_reply), so
    FeedRender.dropped_picks means the same on both."""
    return len(set(pick_numbers(reply_text)) - set(refs))


def refs_from_render(row, reply_text, snapshot_dir):
    """The refs a pinned reply's picks need, without re-rendering the day:
    each cited number maps to the tweet id the model saw under it
    (FeedRender.tweet_id_for), and those few tweets are fetched by id
    from the snapshot — the current one; ids are stable across exports.
    A tweet the snapshot no longer holds drops its pick, and so does a
    number outside the render (parse_feed_reply logs both as unknown
    numbers). How many of the model's picks were dropped either way is
    recorded on the render (FeedRender.dropped_picks), each number once;
    a number cited only in the verdict is not a pick and not counted, and
    not fetched: the verdict's words are stored as written, unlinked."""
    from backend.utils.community_archive import fetch_tweets_by_id
    numbers = pick_numbers(reply_text)
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
    row.dropped_picks = count_dropped_picks(reply_text, refs)
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
    the background refresh (the beat sweep, and the one a read queues,
    request_snapshot_refresh), never by a read itself: a read never waits
    for the download. Only maintains a snapshot that exists; a failed
    refresh is logged and reads go on with the cached export (a reply
    shows the window it covered either way). While another refresh is
    downloading, this one returns at once (ca.refresh_snapshot). Returns
    the export id in place, or None when nothing was checked."""
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


# The reason a read gives when no archive export is cached yet (#435
# review): plain words the user can act on, never the exception's text
# (it names a server path).
ARCHIVE_NOT_READY_TEXT = ("Glean is getting today's tweets ready. Try again "
                          "in a few minutes.")


class ArchiveNotReady(Exception):
    """No Community Archive export is cached for a read. Its text is the
    user's reason line (ARCHIVE_NOT_READY_TEXT)."""

    def __init__(self):
        super().__init__(ARCHIVE_NOT_READY_TEXT)


def request_snapshot_refresh(snapshot_dir, log=log, first_copy=False):
    """Queue the archive refresh in the background (the beat sweep's task,
    imports.refresh_community_archive_snapshot) and return at once. A
    read calls this instead of refreshing inline: a glean is a live call
    the user waits for (#435), and a new nightly export is a ~900 MB
    download, minutes on a slow link. The read goes on with the cached
    export (its reply shows the window it covered); the next read gets
    the new one. With no export cached, only *first_copy* (a read that
    found none) queues one, as the first fetch; the beat sweep never
    fetches a first copy. Best effort: a queue that can't be reached is
    logged, the read goes on (or fails with its own reason)."""
    from backend.utils import community_archive as ca
    if not ca.snapshot_export_id(snapshot_dir) and not first_copy:
        return False
    try:
        from backend.tasks.imports import refresh_community_archive_snapshot
        if first_copy:
            refresh_community_archive_snapshot.apply_async(
                kwargs={"first_copy": True}, retry=False)
        else:
            refresh_community_archive_snapshot.apply_async(retry=False)
    except Exception as e:  # noqa: BLE001 - the read proceeds on the cache
        log.warning("Could not queue the Community Archive refresh: %s", e)
        return False
    return True


def fetch_first_snapshot(snapshot_dir, log=log):
    """Fetch the archive's first copy when none is cached (the background
    task a read queues when it found none, #435 review). Returns the
    export id in place, or None. A download already running elsewhere is
    not waited for; a failure is logged (the next read asks again)."""
    from backend.utils import community_archive as ca
    current = ca.snapshot_export_id(snapshot_dir)
    if current:
        return current
    try:
        export_id = ca.ensure_snapshot(snapshot_dir, wait=False)
    except ca.SnapshotBusy:
        log.info("Community Archive first fetch: another download runs")
        return None
    except Exception as e:  # noqa: BLE001 - the next read asks again
        log.warning("Community Archive first fetch failed: %s", e)
        return None
    log.info("Community Archive snapshot fetched: %s", export_id)
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
    """Persist the picks of one reply: one ExternalItem per tweet — the
    user's own row for it when there is one (a bookmark, a clip, an
    import, an earlier pick: utils/reference_rows.find_tweet_row), else a
    new READ_PICK_SOURCE row, which is not a saved reference — and one
    FeedPick per tweet, stamped with who chose it (``picked_by``, default
    the reply node's model), when (the read's submit, FeedRender) and what
    the model could know of the reader's marks then. Adds to the session;
    the caller commits. Returns the FeedPick rows in rank order.

    Refused (WritingOnHold) while the reader's "Delete all my writing"
    waits or runs (#268): a pick would save tweets and marks the user
    asked to delete. The Read paths drop such a result before they get
    here (llm_completion); this is the last guard.

    Surfacing history is not bumped here: the rendered reply quotes each
    tweet with {quote_ext:ID}, and the finalize path records a surfacing
    for every reference a reply quotes, the same as for any other turn."""
    from backend.extensions import db
    from backend.models import ExternalItem, FeedPick, READ_PICK_SOURCE
    from backend.utils.reference_log import KIND_READ, stamp_prior
    from backend.utils.reference_rows import find_tweet_row
    from backend.utils.hidden_rows import WritingOnHold, writing_on_hold
    if writing_on_hold(user_id):
        raise WritingOnHold(f"user {user_id}: writing on hold for deletion")

    # Idempotent per reply: a redelivered collect (or a retry that re-ran
    # inline) must not insert the picks twice.
    existing = (FeedPick.query.filter_by(node_id=node.id, kind=KIND_READ)
                .order_by(FeedPick.rank.asc()).all())
    if existing:
        log.info("Feed picks for node %s already saved (%d); keeping them",
                 node.id, len(existing))
        return existing

    # The model chose when the read was sent; the collect can come hours
    # later, after the reader has rated a parallel read's picks.
    render = getattr(node, "feed_render", None)
    decided_at = ((render.created_at if render is not None else None)
                  or node.created_at or datetime.utcnow())
    rows = []
    for pick in picks:
        ref = pick["ref"]
        item = find_tweet_row(user_id, ref["tweet_id"])
        if item is None:
            item = ExternalItem(
                user_id=user_id, source=READ_PICK_SOURCE,
                external_id=str(ref["tweet_id"]),
                author_handle=ref["username"],
                author_name=ref.get("display_name"),
                url=tweet_url(ref["username"], ref["tweet_id"]),
                posted_at=ref.get("posted_at"),
                public_source=True,  # rendered from the public archive
            )
            item.set_content(ref.get("text") or "")
            db.session.add(item)
            db.session.flush()
        elif not item.author_name and ref.get("display_name"):
            # The tweet card's byline (#435): the user's own row of the
            # tweet (a bookmark, an import) gets the name it lacked.
            item.author_name = ref["display_name"]
        row = FeedPick(
            user_id=user_id, node_id=node.id, external_item_id=item.id,
            kind=KIND_READ, rank=pick["rank"], relevance=pick["relevance"],
            recommended=pick["recommend"],
            picked_by=picked_by or getattr(node, "llm_model", None),
        )
        row.set_why(pick["qt"])
        stamp_prior(row, item, decided_at)
        db.session.add(row)
        rows.append(row)
    return rows


def fill_pick_author_names(snapshot_dir, user_id=None, apply=False):
    """The one-time fill of display names for picks saved before
    ExternalItem.author_name existed (#435): every tweet row a Glean picked
    (a FeedPick of kind 'read') that has a handle and no name gets the
    name the snapshot's profiles.parquet has for that handle. Reads and
    writes metadata only (handles and public display names); no content
    is decrypted. Returns (rows without a name, rows filled). Writes only
    with *apply*; the caller commits."""
    from backend.extensions import db
    from backend.models import ExternalItem, FeedPick, TWEET_SOURCES
    from backend.utils.community_archive import fetch_display_names
    from backend.utils.reference_log import KIND_READ
    picked = (db.session.query(FeedPick.external_item_id)
              .filter(FeedPick.kind == KIND_READ))
    query = ExternalItem.query.filter(
        ExternalItem.id.in_(picked),
        ExternalItem.source.in_(TWEET_SOURCES),
        ExternalItem.author_name.is_(None),
        ExternalItem.author_handle.isnot(None))
    if user_id:
        query = query.filter(ExternalItem.user_id == user_id)
    rows = query.all()
    names = fetch_display_names(
        snapshot_dir, {r.author_handle for r in rows}) if rows else {}
    filled = 0
    for row in rows:
        name = names.get((row.author_handle or "").lower())
        if name:
            filled += 1
            if apply:
                row.author_name = name
    return len(rows), filled


# How much of a tweet's text names an earlier pick in the marks note
# (pick_label): enough to tell two picks of one author apart.
PICK_EXCERPT_CHARS = 60

# A citation link an earlier read wrote into its verdict, before verdict
# numbers stopped being linked (2026-10-13): "[#12](https://x.com/u/status/1)".
_STALE_CITATION_RE = re.compile(
    r"\[#\d{1,6}\]\(https://x\.com/([^/\s)]+)/status/[^\s)]+\)")


def _excerpt(text, limit=PICK_EXCERPT_CHARS):
    """The opening of *text* on one line, cut at a word boundary."""
    flat = " ".join((text or "").split())
    if len(flat) <= limit:
        return flat
    cut = flat[:limit].rsplit(" ", 1)[0] or flat[:limit]
    return cut + "…"


def pick_label(handle, text):
    """How the marks note names an earlier pick: its author and the
    opening of its text, never a number (see render_read_reply_for_model)."""
    who = f"@{handle}" if handle else "an unknown author"
    return f'{who}\'s tweet "{_excerpt(text)}"'


def render_read_reply_for_model(text, user_id):
    """An earlier read reply as the model reads it on a later turn of the
    thread (Glean again, a new read, a chat about the picks). Each pick's
    {quote_ext:ID} marker becomes the tweet itself, by its author and text:

        <picked_tweet author="@bob" posted_at="2026-10-12">
        the tweet's text
        </picked_tweet>

    with no number of any kind. The marker's id is a row id, and the
    model read it as a number of the day's list: after the archive
    refreshed, a Glean again told the user that the earlier picks
    "numbered 1126 through 1131" did not match today's tweets (#435,
    2026-10-13). A citation link an older verdict carries
    ("[#12](https://x.com/bob/status/…)") becomes "@bob's tweet": its
    number belonged to that read's list, not to today's.

    No marks here: the rendering is re-sent on every later turn and must
    stay byte-identical as the user marks things; the marks travel in the
    note at the end of the prompt (llm_completion._reference_marks_note),
    which names each pick with pick_label. Owner-only, like every
    {quote_ext} resolution. Returns (text, resolved_item_ids, labels),
    labels being {item_id: pick_label}."""
    from backend.utils.quotes import (
        EXT_QUOTE_PLACEHOLDER_PATTERN, find_ext_quote_ids, get_ext_quote_data)
    if not text:
        return text, [], {}
    text = _STALE_CITATION_RE.sub(lambda m: f"@{m.group(1)}'s tweet", text)
    item_ids = find_ext_quote_ids(text)
    if not item_ids:
        return text, [], {}
    data = get_ext_quote_data(item_ids, user_id)
    resolved, labels = [], {}

    def replace(match):
        item_id = int(match.group(1))
        d = data.get(item_id)
        if d is None:
            return "[A tweet picked here earlier is no longer available.]"
        resolved.append(item_id)
        handle = d.get("author_handle")
        labels[item_id] = pick_label(handle, d.get("content"))
        author = f"@{handle}" if handle else "unknown"
        posted = (f' posted_at="{d["posted_at"][:10]}"'
                  if d.get("posted_at") else "")
        return (f'<picked_tweet author="{author}"{posted}>\n'
                f'{d.get("content") or ""}\n</picked_tweet>')

    text = re.sub(EXT_QUOTE_PLACEHOLDER_PATTERN, replace, text)
    return text, resolved, labels


def render_feed_reply(verdict, entries):
    """The reply node's text: the verdict, then each pick as the model's
    quote-tweet followed by the {quote_ext:ID} marker of the tweet it
    quotes. ``entries`` is [(qt_text, external_item_id)] in rank order.
    The marker is what the thread page renders as the quoted tweet (with
    the reference's own read mark and good/bad verdict) and what a later
    turn resolves to the tweet (render_read_reply_for_model), the user's
    marks on it travelling in a note of their own. The model's own words
    are stored as written: a "#12" in them is not linked to a tweet, as
    the number belongs to this read's list alone."""
    verdict = verdict or "Nothing here would change what you do next."
    parts = [verdict]
    for qt, item_id in entries:
        block = f"{{quote_ext:{int(item_id)}}}"
        if qt:
            block = f"{qt}\n\n{block}"
        parts.append(block)
    return "\n\n".join(parts)

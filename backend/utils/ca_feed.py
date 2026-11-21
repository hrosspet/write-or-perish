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
belong to that read alone: the next one numbers its tweets afresh.

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
later resolves its numbers against what was actually sent.

A read takes the newest tweets of its window that fit its prompt, and
repeated reads in a thread page through the window (#717; Peter,
2026-11-20). A day of the archive grew past what one prompt may hold
(about 46,000 tweets on 2026-11-20, about 35 tokens each), and since
#648 a prompt over THREAD_PROMPT_TOKEN_LIMIT is refused. So the
completion task measures the rest of the prompt (thread, instructions,
profile, intentions) and keeps, newest first, as many of the window's
tweets as fit glean_prompt_budget (community_archive.newest_that_fit):
the thread limit, or the model's price step where that is lower (#733:
Haiku 5.5's 100k).
The next read in the same thread leaves out what the thread's earlier
reads of that window read (gleaned_tweet_ids, from their pinned
renders), so it reads the next older tweets; another thread, or a new
window (another export), starts again at the newest. When nothing of
the window is left, the read is refused with GLEAN_DAY_DONE_MESSAGE
before any model call. The seen filter, the reader's own handle, the
follows scope and the retweets are left out first: the budget counts
what remains. The admin's batch experiments follow the same rule. This
replaced #511's kept list (a read again on the same export re-sent the
earlier read's list for the prompt cache): a read again now reads other
tweets, so only the part before the list (the thread, the instructions,
the profile and the intentions) can come from the cache.
"""
import json
import logging
import re
from datetime import datetime, timedelta, timezone

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
# The request of a read again that reads on in the same window (#717):
# the list holds the next, older tweets, none of which an earlier read in
# the thread had, so no earlier pick is in it.
CA_READ_OLDER_TURN = (
    "Read further: the Community Archive list above now holds the next, "
    "older tweets of today; none of them was in your earlier lists in "
    "this conversation. Go through them against everything above — your "
    "earlier picks, my marks on them, and whatever I have written since. "
    "Your earlier picks are quoted above by author and text; they carry "
    "no numbers and are not in this list. Tweets I have read are out of "
    "it. Find what is worth my time. Answer in the same shape, a verdict "
    "and the picks.")

# A read again with nothing of its window left to read (#717): refused
# before any model call, with this text (the reply's error, a toast, and
# in Voice mode said aloud, utils/voice_refusals) and this code (the
# reply's REFUSED_MARKER entry; llm-status ``error_code``).
GLEAN_DAY_DONE_CODE = "glean_day_done"
GLEAN_DAY_DONE_MESSAGE = ("You've gleaned through all of today's tweets. "
                          "New ones come in once a day.")

# A Glean with no room for one tweet because its model's price step sets
# its budget (#733: Haiku 5.5's 100k), in a thread the thread limit still
# leaves room in: a Glean on another model, or a reply, still works here,
# so the thread-length sentences (which say none does) would be untrue.
# Refused before any call, nothing billed, with this text (the reply's
# error; both apps show the server's words for a code they do not know:
# a toast on the thread page, the failed gleaning's page from Voice) and
# this code (REFUSED_MARKER entry; llm-status ``error_code``), which no
# app reads as the thread's limit.
GLEAN_TOO_LONG_FOR_MODEL_CODE = "glean_too_long_for_model"
GLEAN_TOO_LONG_FOR_MODEL_MESSAGE = (
    "This thread is too long for a Glean on {model}. Pick another model "
    "for this Glean, or start a new thread.")

# Heuristic (#717): the share of a Glean's token budget kept free for
# what its count can miss: the provider's own tokenizer drifting from
# tiktoken's o200k_base, and the message framing. 2 % of the 250k limit
# is 5,000 tokens; of Haiku 5.5's 100k price step (#733), 2,000.
GLEAN_BUDGET_MARGIN = 0.02

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


def gleaned_tweet_ids(node_chain, reply_ids, user_id, export_id, days,
                      scope):
    """The tweets that the earlier reads of *user_id* in this thread read
    from this window (#717): the lists pinned on their read replies in
    *node_chain* (*reply_ids* from read_reply_ids) that were rendered from
    the same export, days and scope. The next read leaves them out, so it
    reads the window's next older tweets (the earlier reads took the
    newest), and two threads page through a day each on their own. A
    reply that was deleted or did not complete read nothing the user
    saw, and another user's reply in a shared thread is theirs: neither
    counts. Empty without an export id (a new window starts again at its
    newest tweets). One query."""
    from backend.models import FeedRender
    if not export_id:
        return frozenset()
    candidates = [n.id for n in node_chain
                  if n.id in reply_ids and n.deleted_at is None
                  and n.llm_task_status == "completed"
                  and (n.human_owner_id or n.user_id) == user_id]
    if not candidates:
        return frozenset()
    ids = set()
    for row in FeedRender.query.filter(
            FeedRender.node_id.in_(candidates)).all():
        if (row.export_id == export_id and row.days == int(days)
                and row.scope == (scope or "all")):
            ids.update(i for i in (row.tweet_ids or "").split(",") if i)
    return frozenset(ids)


def glean_prompt_budget(model_id, config):
    """The most tokens a read's prompt may hold (#717): the thread limit
    (THREAD_PROMPT_TOKEN_LIMIT, 250k; Peter, 2026-11-12), or the model's
    own input ceiling when that is lower (its window minus the answer's
    room, OpenAI's max_input_tokens, or its price step), less
    GLEAN_BUDGET_MARGIN. The tweets fill what the rest of the prompt
    leaves of it.

    The price step (#733; Peter, 2026-11-21) is the model's
    ``long_context_threshold`` in SUPPORTED_MODELS, the price table: a
    prompt over it bills the whole request at a higher rate. Haiku 5.5's
    is 100k, so a Haiku Glean stays under 98,000 tokens; the OpenAI
    models' 272k is above the limit and changes nothing; a model with no
    step in the table (Sonnet 5.5, flat across its 1M window) has the
    thread limit. None when neither is known."""
    return _glean_budget(model_id, config, price_tier=True)


def _glean_budget(model_id, config, price_tier):
    from backend.llm_providers import model_input_cap
    from backend.utils.thread_size import prompt_limit
    limits = []
    limit = prompt_limit(config)
    if limit:
        limits.append(limit)
    cfg = (config.get("SUPPORTED_MODELS") or {}).get(model_id)
    if cfg:
        limits.append(model_input_cap(cfg, price_tier=price_tier))
    if not limits:
        return None
    return int(min(limits) * (1 - GLEAN_BUDGET_MARGIN))


def glean_too_long_for_model(model_id, config, rest_tokens):
    """The refusal text for a Glean that has no room for one tweet because
    its model's price step sets its budget (#733), or None. *rest_tokens*:
    the prompt without the list, as _fit_read_list measured it. The text
    applies only while the budget without the step (the thread limit, or
    the model's window) would still leave the rest room: then a Glean on
    another model, or a reply, works in this thread. When the rest fills
    that budget too, the thread itself is too long and the thread-length
    sentences hold (None)."""
    stepped = _glean_budget(model_id, config, price_tier=True)
    unstepped = _glean_budget(model_id, config, price_tier=False)
    if not stepped or not unstepped or stepped >= unstepped:
        return None
    if rest_tokens is None or rest_tokens >= unstepped:
        return None
    cfg = (config.get("SUPPORTED_MODELS") or {}).get(model_id) or {}
    return GLEAN_TOO_LONG_FOR_MODEL_MESSAGE.format(
        model=cfg.get("display_name") or model_id)


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
    # A part of the window (#717): when its tweets were posted. None for
    # a render of the whole window, as before.
    row.page_start = stats.get("page_start_at")
    row.page_end = stats.get("page_end_at")
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


def render_was_stale(row):
    """Whether a pinned render read a stale archive: its window ends at the
    newest tweet it had, and that tweet was more than EXPORT_STALE_AFTER
    old when the render was made (#688)."""
    if row is None or row.window_end is None or row.created_at is None:
        return False
    return row.created_at - row.window_end > EXPORT_STALE_AFTER


def read_window_fields(row):
    """The reply page's record of what a read covered, from its pinned
    render (None for replies from before renders were pinned).
    ``window_start`` / ``window_end`` are the bounds of the tweets the
    read had: the window's, or, for a read of part of it (#717), when its
    oldest and newest tweet were posted, so "Tweets from … to …: N by M
    accounts" stays true. ``newest_at`` is the newest tweet the archive
    had (the window's end), which the stale note names. ``export_stale``
    tells the window line to say that the archive had nothing newer
    (render_was_stale)."""
    from backend.utils.timefmt import iso_utc
    if row is None:
        return None
    page_start = getattr(row, "page_start", None)
    page_end = getattr(row, "page_end", None)
    paged = page_start is not None and page_end is not None
    return {
        "export_id": row.export_id,
        "days": row.days,
        "scope": row.scope,
        "window_start": iso_utc(page_start if paged else row.window_start),
        "window_end": iso_utc(page_end if paged else row.window_end),
        "newest_at": iso_utc(row.window_end),
        "tweets": row.tweet_count,
        "accounts": row.account_count,
        "excluded": row.excluded_count,
        "export_stale": render_was_stale(row),
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
    not fetched: the verdict's words are stored as written, unlinked.
    A render of a day read from API v1 (#688) resolves its tweets from that
    day's cache, then from the export for any it no longer holds."""
    from backend.utils.community_archive import (
        fetch_tweets_by_id, snapshot_export_id, v1_day_dir_for_export)
    numbers = pick_numbers(reply_text)
    wanted = {}
    for n in numbers:
        tweet_id = row.tweet_id_for(n)
        if tweet_id:
            wanted[n] = tweet_id
    day = v1_day_dir_for_export(snapshot_dir, row.export_id) if wanted else None
    if day is not None:
        found = fetch_tweets_by_id(day, wanted.values())
        missing = [i for i in wanted.values() if i not in found]
        if missing and snapshot_export_id(snapshot_dir):
            found.update(fetch_tweets_by_id(snapshot_dir, missing))
    else:
        found = (fetch_tweets_by_id(snapshot_dir, wanted.values())
                 if wanted else {})
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


# An export whose newest tweet is older than this at Glean time is stale
# (#688): the read window line says so, the beat task reports it, and with
# an API key Glean reads the last day from API v1 instead. Heuristic: the
# archive exported once a day, around 07:00 UTC, so a healthy export's
# newest tweet is at most ~24 h old plus the download; 36 h is one missed
# export with half a day of margin.
EXPORT_STALE_AFTER = timedelta(hours=36)

# A broken archive is reported at ERROR (Sentry reports ERROR log lines)
# once per problem per day; the rest of a streak logs at WARNING, and a
# healthy run clears the claim, so a new streak reports at once. Claimed
# in Redis like the provider and disk alerts (provider_alerts.claim).
ARCHIVE_ALERT_REPEAT_SECONDS = 24 * 3600
ARCHIVE_ALERT_PREFIX = "loore:community-archive-alert:"
ALERT_EXPORT = "export"
ALERT_API_V1_DAY = "api-v1-day"

# export_newest_at's memo: {(snapshot dir, export id): newest tweet}. The
# max over the whole export is a scan of its created_at column; an
# export never changes once its id is written.
_newest_memo = {}


def _alert_config():
    try:
        from flask import current_app
        return current_app.config
    except RuntimeError:  # outside an app context (a script)
        return {}


def _report_archive_problem(kind, log, message, *args):
    """Log *message* at ERROR for the first report of *kind* in
    ARCHIVE_ALERT_REPEAT_SECONDS, at WARNING after that. Returns whether
    it was the ERROR."""
    from backend.utils.provider_alerts import claim
    if claim((kind,), ARCHIVE_ALERT_REPEAT_SECONDS, _alert_config(),
             prefix=ARCHIVE_ALERT_PREFIX):
        log.error(message, *args)
        return True
    log.warning(message, *args)
    return False


def _archive_problem_cleared(kind):
    from backend.utils.provider_alerts import release
    release((kind,), _alert_config(), prefix=ARCHIVE_ALERT_PREFIX)


def _describe_failure(e):
    """The exception's type and HTTP status, never its text (#540)."""
    code = getattr(e, "code", None)
    return f"{type(e).__name__} {code}" if code else type(e).__name__


def export_newest_at(snapshot_dir):
    """The newest tweet's time (naive UTC) in the export cached in
    *snapshot_dir*, or None when none is cached."""
    from backend.utils import community_archive as ca
    export_id = ca.snapshot_export_id(snapshot_dir)
    if not export_id:
        return None
    key = (str(snapshot_dir), export_id)
    if key not in _newest_memo:
        newest = ca.snapshot_newest_tweet_at(snapshot_dir)
        _newest_memo[key] = (datetime.strptime(newest, "%Y-%m-%d %H:%M:%S")
                             if newest else None)
    return _newest_memo[key]


def export_is_stale(snapshot_dir, now=None):
    """Whether the cached export's newest tweet is older than
    EXPORT_STALE_AFTER (no export cached counts as stale)."""
    newest = export_newest_at(snapshot_dir)
    now = now or datetime.utcnow()
    return newest is None or now - newest > EXPORT_STALE_AFTER


def glean_source_dir(snapshot_dir, days=1, now=None):
    """Where a Glean reads its day: the cached export, or — when an API key
    is set, the export is stale (export_is_stale) and the read is of one
    day — the newest day fetched from API v1 (ca.ensure_v1_day), as long
    as its newest tweet is newer than the export's. Both hold the same
    files, so the render and the selection are the same code either way.
    A Glean never fetches: with no v1 day cached yet it reads the stale
    export (its window line says so) and the background refresh it
    queues fetches the day for the next one."""
    from backend.utils import community_archive as ca
    if int(days) != 1 or not ca.v1_enabled():
        return snapshot_dir
    try:
        if not export_is_stale(snapshot_dir, now):
            return snapshot_dir
        day = ca.latest_v1_day(snapshot_dir)
        if day is None:
            return snapshot_dir
        newest = export_newest_at(snapshot_dir)
        day_newest = ca.snapshot_newest_tweet_at(day)
    except Exception as e:  # noqa: BLE001 - the export path still reads
        log.warning("Glean source: the API v1 day could not be checked "
                    "(%s); reading the export", _describe_failure(e))
        return snapshot_dir
    if not day_newest:
        return snapshot_dir
    if newest is not None and (datetime.strptime(
            day_newest, "%Y-%m-%d %H:%M:%S") <= newest):
        return snapshot_dir
    return day


# API v1 requests the background fetch of Glean's day may send per UTC day
# (#688), counted in Redis across processes (in this process alone without
# it). A day window takes about 115-130 requests; 1,000 leaves room for
# several failed runs, and is 5 % of a key's 20,000-a-day quota, so the
# pre-fills that share the key always keep 19,000. Heuristic.
V1_GLEAN_DAILY_REQUESTS = 1000
GLEAN_BUDGET_PREFIX = "loore:community-archive-api:glean-requests:"
_local_glean_requests = {}


def _count_glean_request(key):
    from backend.utils.provider_alerts import _redis
    try:
        client = _redis(_alert_config())
        if client is not None:
            n = client.incr(key)
            client.expire(key, 2 * 24 * 3600)
            return int(n)
    except Exception:  # noqa: BLE001 - count in this process instead
        log.warning("Glean request budget: Redis unavailable; counting in "
                    "this process only", exc_info=True)
    _local_glean_requests[key] = _local_glean_requests.get(key, 0) + 1
    return _local_glean_requests[key]


def glean_request_budget(now=None, limit=None):
    """The ``on_request`` hook of the Glean day fetch: counts each request
    against the UTC day's V1_GLEAN_DAILY_REQUESTS and raises
    ca.V1BudgetSpent once they are used, before the request is sent."""
    from backend.utils import community_archive as ca
    key = GLEAN_BUDGET_PREFIX + (now or datetime.utcnow()).strftime("%Y-%m-%d")
    limit = V1_GLEAN_DAILY_REQUESTS if limit is None else limit

    def spend():
        if _count_glean_request(key) > limit:
            raise ca.V1BudgetSpent(
                f"Glean's {limit} Community Archive API requests for the "
                "day are used up")
    return spend


def refresh_v1_day_for_read(snapshot_dir, log=log, now=None):
    """Background half of glean_source_dir: with an API key and a stale
    (or absent) export, make sure the current day window is cached from
    API v1. A failure is reported like a failed export refresh (one ERROR
    a day) and the next run tries again; Glean goes on with what is
    cached. Returns the window id in place, or None."""
    from backend.utils import community_archive as ca
    if not ca.v1_enabled():
        return None
    now = now or datetime.utcnow()
    try:
        stale = export_is_stale(snapshot_dir, now)
    except Exception:  # noqa: BLE001 - an unreadable export serves no Glean
        stale = True
    if not stale:
        return None
    try:
        window_id, fetched = ca.ensure_v1_day(
            snapshot_dir, now=now.replace(tzinfo=timezone.utc),
            on_request=glean_request_budget(now))
    except ca.SnapshotBusy:
        return None
    except ca.V1DayWaiting as e:
        log.info("Community Archive API: Glean's day waits after a failed "
                 "fetch (%s)", e)
        return None
    except Exception as e:  # noqa: BLE001 - Glean goes on with the cache
        _report_archive_problem(
            ALERT_API_V1_DAY, log,
            "Community Archive API: fetching the day for Glean failed (%s); "
            "Glean reads the cached tweets", _describe_failure(e))
        return None
    _archive_problem_cleared(ALERT_API_V1_DAY)
    if fetched:
        log.info("Community Archive API: Glean's day %s cached", window_id)
    return window_id


def refresh_snapshot_for_read(snapshot_dir, log=log, now=None):
    """Bring the cached archive up to the latest nightly export (the
    Community Archive exports around 07:00 UTC) so "the last day" is the
    last day and not the day the snapshot was last fetched. Called by
    the background refresh (the beat sweep, and the one a read queues,
    request_snapshot_refresh), never by a read itself: a read never waits
    for the download. Only maintains a snapshot that exists; a failed
    refresh is logged and reads go on with the cached export (a reply
    shows the window it covered either way). While another refresh is
    downloading, this one returns at once (ca.refresh_snapshot). Returns
    the export id in place, or None when nothing was checked.

    A failed refresh, and an export left stale (EXPORT_STALE_AFTER) by an
    archive that publishes no newer one, is reported at ERROR once a day
    while it lasts (#688): until then a dead export only logged a
    WARNING, and Glean read the same old day for ten days unnoticed."""
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
        _report_archive_problem(
            ALERT_EXPORT, log,
            "Community Archive export refresh failed (%s); Glean reads the "
            "cached export, whose newest tweet is from %s UTC",
            _describe_failure(e), _newest_label(snapshot_dir))
        return None
    if refreshed:
        log.info("Community Archive snapshot refreshed to %s", export_id)
    try:
        stale = export_is_stale(snapshot_dir, now)
    except Exception as e:  # noqa: BLE001 - an unreadable export: the render says so
        log.warning("Community Archive export %s: newest tweet unreadable "
                    "(%s)", export_id, _describe_failure(e))
        return export_id
    if stale:
        _report_archive_problem(
            ALERT_EXPORT, log,
            "Community Archive export %s is stale: its newest tweet is from "
            "%s UTC and the archive has published no newer export",
            export_id, _newest_label(snapshot_dir))
    else:
        _archive_problem_cleared(ALERT_EXPORT)
    return export_id


def _newest_label(snapshot_dir):
    try:
        newest = export_newest_at(snapshot_dir)
    except Exception:  # noqa: BLE001 - only for the log line
        newest = None
    return newest.strftime("%Y-%m-%d %H:%M") if newest else "unknown"


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

    Refused (ReadBeforeDeletion) when the reader's "Delete all my
    content" came after the Read was asked for (#590), or the account is
    being deleted: a pick would save tweets and marks the user asked to
    delete. The Read paths drop such a result before they get here
    (llm_completion); this is the last guard.

    Surfacing history is not bumped here: the rendered reply quotes each
    tweet with {quote_ext:ID}, and the finalize path records a surfacing
    for every reference a reply quotes, the same as for any other turn."""
    from backend.extensions import db
    from backend.models import ExternalItem, FeedPick, READ_PICK_SOURCE
    from backend.utils.reference_log import KIND_READ, stamp_prior
    from backend.utils.reference_rows import find_tweet_row
    from backend.utils.hidden_rows import (
        ReadBeforeDeletion, read_before_deletion,
    )
    if read_before_deletion(user_id, getattr(node, "created_at", None)):
        raise ReadBeforeDeletion(f"user {user_id}: content deleted since "
                                 "the Read was asked for")

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

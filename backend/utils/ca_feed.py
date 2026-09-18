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
"""
import json
import logging

log = logging.getLogger(__name__)

MAX_PICKS = 20

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
    """A node that belongs to a read: one of the two read prompts, or a
    reply that named picks. These never carry 'train' usage."""
    key = node.get_prompt_key() if hasattr(node, "get_prompt_key") else None
    if key in READ_PROMPT_KEYS:
        return True
    return bool(getattr(node, "feed_picks", None))


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

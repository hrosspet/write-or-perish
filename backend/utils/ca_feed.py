"""The Community Archive feed reply (PoC, 2026-09-13).

A prompt carrying {ca_tweets} asks one question of a day of tweets: is
there anything this person would benefit from reading? The batch call
answers in a fixed shape (FEED_SCHEMA): a prose verdict plus up to
MAX_PICKS picks, each a tweet number from the render with the model's
reason, relevance estimate and recommend flag. On collect, every pick
becomes a saved reference (ExternalItem, source 'community_archive') so
the user's read mark and good/bad verdict work on it like on any other
reference, and a FeedPick row keeps what the model claimed, so the
claims can be judged against the verdicts later. The reply node's text
is the verdict alone; the picks render from /api/nodes/<id>/feed-picks.
"""
import json
import logging
from datetime import datetime

log = logging.getLogger(__name__)

MAX_PICKS = 20

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
            "description": "Up to 20 tweets, best first. Do not pad: if "
                           "fewer are worth naming, name fewer; an empty "
                           "list is a valid answer.",
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer",
                          "description": "The tweet's number in the "
                                         "corpus, e.g. 123 for #123."},
                    "why": {"type": "string",
                            "description": "One line: why this matters "
                                           "to the reader."},
                    "relevance": {"type": "integer",
                                  "description": "0-100: the probability "
                                                 "that reading it changes "
                                                 "what the reader does "
                                                 "this week."},
                    "recommend": {"type": "boolean",
                                  "description": "True only for the "
                                                 "tweets you would "
                                                 "actually recommend "
                                                 "reading today."},
                },
                "required": ["n", "why", "relevance", "recommend"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["verdict", "picks"],
    "additionalProperties": False,
}


class FeedReplyError(ValueError):
    """The batch reply was not the JSON shape FEED_SCHEMA promised."""


def parse_feed_reply(text, refs):
    """Parse the model's JSON reply against the render's ``refs``
    ({n: {username, tweet_id, text, posted_at}}).

    Returns (verdict, picks) with picks normalized: unknown or repeated
    numbers dropped, relevance clamped to 0-100, at most MAX_PICKS, in
    the model's order with ``rank`` 1..k. Raises FeedReplyError when the
    text is not the promised object."""
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
            "why": (raw.get("why") or "").strip(),
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
    sync or a clip of the same tweet under this source), bump its
    surfacing history (this IS a surfacing), and write one FeedPick per
    tweet, stamped with who chose it (``picked_by``, default the reply
    node's model). Adds to the session; the caller commits. Returns the
    FeedPick rows in rank order."""
    from backend.extensions import db
    from backend.models import ExternalItem, FeedPick

    # Idempotent per reply: a redelivered collect (or a retry that re-ran
    # inline) must not insert the picks twice or bump surfacing again.
    existing = (FeedPick.query.filter_by(node_id=node.id)
                .order_by(FeedPick.rank.asc()).all())
    if existing:
        log.info("Feed picks for node %s already saved (%d); keeping them",
                 node.id, len(existing))
        return existing

    now = datetime.utcnow()
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
        item.surfaced_count = (item.surfaced_count or 0) + 1
        item.last_surfaced_at = now
        row = FeedPick(
            user_id=user_id, node_id=node.id, external_item_id=item.id,
            rank=pick["rank"], relevance=pick["relevance"],
            recommended=pick["recommend"],
            picked_by=picked_by or getattr(node, "llm_model", None),
        )
        row.set_why(pick["why"])
        db.session.add(row)
        rows.append(row)
    return rows


def render_feed_reply(verdict, picks):
    """The reply node's text: the verdict, then one plain line saying how
    many picks follow (the picks themselves render from their rows, so
    exports and later turns still see that there were some)."""
    verdict = verdict or "Nothing here would change what you do next."
    if not picks:
        return verdict
    starred = sum(1 for p in picks if p["recommend"])
    if starred:
        tail = (f"{len(picks)} tweets below, {starred} recommended."
                if len(picks) > 1 else "1 tweet below, recommended.")
    else:
        tail = (f"{len(picks)} tweets below, none I would recommend "
                "outright." if len(picks) > 1
                else "1 tweet below, not one I would recommend outright.")
    return f"{verdict}\n\n{tail}"

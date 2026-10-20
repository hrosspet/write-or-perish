"""How Loore's recommendations fared, per model (#352). Metadata only.

Every tweet a Read picked and every reference an agentic reply quoted is
a FeedPick row; what the reader did with it is in ReferenceAction. This
prints, per kind (Read pick / quote), model, variant and turn, how many were
shown and what came of them, with the verdicts that count for each
recommendation (backend/utils/reference_log.outcomes):

    shown      recommendations made
    opened     the post was opened (here, or in a parallel pick of it)
    read       marked read at the end (by an open, a mark or a verdict)
    good/bad   the verdict that counts; "+n shared" of them were given in
               another reply of the same group (a parallel Read), not here
    untouched  nothing done with it at all
    after      made when the reader had already rated the reference: the
               model saw that verdict, so it is judged on its own

Variant, for a Read pick, is the read prompt the reply answered:

    uncond     the home-page Read ('read' prompt): profile, intentions
               and the day's archive, nothing else
    cond       a Read started from a node ('read_thread' prompt): the
               same, read against the conversation above it
    ?          no read prompt found above the reply: the PoC threads of
               2026-09-13 carry the prompt text in the node itself, and
               this script does not decrypt content to find it

and turn is "first" for the reply that answered the prompt and "further"
for a later read in the same thread (the Read further button, or a reply
asked for directly under a read reply). That is the "read" / "read_again"
split of ca_feed.ca_turn, applied to the thread as it stood when the
reply was made: an earlier read reply deleted before it does not count.

For a quote, variant is the agentic reply's mode, voice or text: Voice
does not speak quotes (the TTS step strips the markers), so a quote in a
Voice reply is only seen on screen. Quotes have no turn. "untouched"
includes picks in replies the reader never opened (see "opened" below).

A second table counts Reads rather than picks, so a Read that showed
nothing counts too ("no recommendation is a good recommendation"). One
row per completed Read reply with a pinned render (FeedRender: every
Read since 2026-09-19), per model, variant and turn as above:

    reads      completed Read replies
    picked     of them, replies that show at least one pick
    empty      of them, replies that show no pick, split into
    nothing      the model picked nothing
    dropped      the model picked, but Loore could show none of the
                 picks: the tweet had left the archive snapshot by the
                 collect, or the number was outside the render
                 (FeedRender.dropped_picks;
                 replies collected before the column was deployed,
                 October 2026, count as "nothing")
    opened     replies their owner has opened (FeedRender.opened_at,
               recorded only since that deploy: a Read opened only before
               it counts as not opened), and their share of reads. An
               open is a load of the reply, or a poll from a visible web
               tab that returns it (a tab in the background when the Read
               finished does not count until the tab is shown again); the
               iPhone app's polls carry no visibility flag, so a thread
               screen it left open while the Read finished counts only
               from its next load of the reply

    cd /path/to/write-or-perish
    python backend/scripts/recommendation_report.py
    python backend/scripts/recommendation_report.py --user-id 6 --days 30
"""
import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, os.getcwd())

from backend import create_app, db  # noqa: E402
from backend.models import FeedPick, FeedRender, Node  # noqa: E402
from backend.utils.ca_feed import READ_PROMPT_KEYS, read_reply_ids  # noqa: E402
from backend.utils.reference_log import KIND_READ, outcomes  # noqa: E402
from backend.utils.thread_tree import ancestor_chain  # noqa: E402

CHUNK = 500


def _mode(node, cache):
    """'voice' | 'text' | '?' for a quoting reply: its own _mode entry,
    or the one on the final node its continuation chain ends in (an
    interim node carries only its tool results)."""
    seen = 0
    start = node.id
    while node is not None and seen < 12:
        if node.id in cache:
            return cache[node.id]
        try:
            meta = json.loads(node.tool_calls_meta or "[]")
        except (TypeError, ValueError):
            meta = []
        for entry in meta if isinstance(meta, list) else []:
            if isinstance(entry, dict) and entry.get("name") == "_mode":
                cache[start] = entry.get("source_mode") or "?"
                return cache[start]
        node = (db.session.get(Node, node.continuation_node_id)
                if node.continuation_node_id else None)
        seen += 1
    cache[start] = "?"
    return "?"


PROMPT_VARIANT = {"read": "uncond", "read_thread": "cond"}


def _read_variant(node, cache):
    """(variant, turn) of a Read reply; see the module docstring. Walks
    the reply's ancestors nearest first up to the newest read prompt; the
    read replies among the nodes in between make it a "further" turn."""
    if node.id in cache:
        return cache[node.id]
    chain = ancestor_chain(node.id)
    variant, between = "?", []
    for n in chain[1:]:
        key = n.get_prompt_key()
        if key in READ_PROMPT_KEYS:
            variant = PROMPT_VARIANT[key]
            break
        if n.deleted_at is None or (
                node.created_at is not None and n.deleted_at > node.created_at):
            between.append(n)
    turn = "further" if read_reply_ids(between) else "first"
    cache[node.id] = (variant, turn)
    return cache[node.id]


def report(user_id=None, since=None):
    q = FeedPick.query
    if user_id:
        q = q.filter(FeedPick.user_id == user_id)
    if since:
        q = q.filter(FeedPick.created_at >= since)
    recs = q.order_by(FeedPick.id).all()
    rows = defaultdict(lambda: defaultdict(int))
    modes, variants = {}, {}
    for start in range(0, len(recs), CHUNK):
        chunk = recs[start:start + CHUNK]
        counted = outcomes(chunk)
        for rec in chunk:
            out = counted[rec.id]
            if rec.kind == KIND_READ:
                variant, turn = _read_variant(rec.node, variants)
            else:
                variant, turn = _mode(rec.node, modes), "-"
            r = rows[(rec.kind, rec.picked_by or "?", variant, turn)]
            r["shown"] += 1
            r["opened"] += out.opened
            r["read"] += out.read
            if out.verdict in ("good", "bad"):
                r[out.verdict] += 1
                r[out.verdict + "_shared"] += out.verdict_shared
            r["untouched"] += not (out.opened or out.read or out.verdict)
            r["after"] += not out.blind
    return rows


def read_report(user_id=None, since=None):
    """The Reads table (see the module docstring), keyed by (model,
    variant, turn). A Read is a completed reply with a pinned render;
    *since* applies to the render's submit time. Metadata only: no
    node content is read."""
    q = (db.session.query(Node, FeedRender.dropped_picks,
                          FeedRender.opened_at)
         .join(FeedRender, FeedRender.node_id == Node.id)
         .filter(Node.llm_task_status == "completed"))
    if user_id:
        q = q.filter(Node.human_owner_id == user_id)
    if since:
        q = q.filter(FeedRender.created_at >= since)
    reads = q.order_by(Node.id).all()
    rows = defaultdict(lambda: defaultdict(int))
    variants = {}
    for start in range(0, len(reads), CHUNK):
        chunk = reads[start:start + CHUNK]
        picked = {r[0] for r in (
            db.session.query(FeedPick.node_id)
            .filter(FeedPick.node_id.in_([n.id for n, _, _ in chunk]),
                    FeedPick.kind == KIND_READ)
            .distinct().all())}
        for node, dropped, opened_at in chunk:
            variant, turn = _read_variant(node, variants)
            r = rows[(node.llm_model or "?", variant, turn)]
            r["reads"] += 1
            r["opened"] += opened_at is not None
            if node.id in picked:
                r["picked"] += 1
            else:
                r["empty"] += 1
                r["dropped" if dropped else "nothing"] += 1
    return rows


def print_reads(rows):
    head = (f"{'model':24} {'variant':7} {'turn':7} {'reads':>6} "
            f"{'picked':>7} {'empty':>6} {'nothing':>8} {'dropped':>8} "
            f"{'opened':>7} {'opened%':>8}")
    print(head)
    print("-" * len(head))
    for (model, variant, turn), r in sorted(rows.items()):
        share = f"{100 * r['opened'] / r['reads']:.0f}%" if r["reads"] else "-"
        print(f"{model[:24]:24} {variant:7} {turn:7} {r['reads']:>6} "
              f"{r['picked']:>7} {r['empty']:>6} {r['nothing']:>8} "
              f"{r['dropped']:>8} {r['opened']:>7} {share:>8}")
    print("\nempty = nothing + dropped: no pick shown. dropped = the model "
          "picked, but none of its picks could be shown (tweet gone from "
          "the snapshot, or a number outside the render).\nopened "
          "= the owner opened the reply (recorded since the October 2026 "
          "deploy; a web tab in the background when the Read finished "
          "counts once it is shown).")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--user-id", type=int, default=None)
    parser.add_argument("--days", type=int, default=None,
                        help="only recommendations (and Reads) from the "
                             "last N days")
    args = parser.parse_args(argv)
    since = (datetime.utcnow() - timedelta(days=args.days)
             if args.days else None)
    app = create_app()
    with app.app_context():
        rows = report(args.user_id, since)
        head = (f"{'kind':6} {'model':24} {'variant':7} {'turn':7} "
                f"{'shown':>6} "
                f"{'opened':>7} {'read':>6} {'good':>12} {'bad':>12} "
                f"{'untouched':>10} {'after':>6}")
        print(head)
        print("-" * len(head))
        for (kind, model, variant, turn), r in sorted(rows.items()):
            good = f"{r['good']} (+{r['good_shared']})"
            bad = f"{r['bad']} (+{r['bad_shared']})"
            print(f"{kind:6} {model[:24]:24} {variant:7} {turn:7} "
                  f"{r['shown']:>6} "
                  f"{r['opened']:>7} {r['read']:>6} {good:>12} {bad:>12} "
                  f"{r['untouched']:>10} {r['after']:>6}")
        print("\ngood/bad (+n): n of them were given in another reply of "
              "the same group (a parallel Read), not in this one.")
        print("\nReads: one row per completed Read reply, pick-less "
              "ones included.\n")
        print_reads(read_report(args.user_id, since))


if __name__ == "__main__":
    main()

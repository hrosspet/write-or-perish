"""How Loore's recommendations fared, per model (#352). Metadata only.

Every tweet a Read picked and every reference an agentic reply quoted is
a FeedPick row; what the reader did with it is in ReferenceAction. This
prints, per kind (Read pick / quote), model and mode, how many were
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

Mode splits Voice from Text for quotes: Voice does not speak quotes (the
TTS step strips the markers), so a quote in a Voice reply is only seen on
screen. A Read pick has no mode. Nothing records whether the reader
looked at a reply at all, so "untouched" includes replies never opened.

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
from backend.models import FeedPick, Node  # noqa: E402
from backend.utils.reference_log import KIND_READ, outcomes  # noqa: E402

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


def report(user_id=None, since=None):
    q = FeedPick.query
    if user_id:
        q = q.filter(FeedPick.user_id == user_id)
    if since:
        q = q.filter(FeedPick.created_at >= since)
    recs = q.order_by(FeedPick.id).all()
    rows = defaultdict(lambda: defaultdict(int))
    modes = {}
    for start in range(0, len(recs), CHUNK):
        chunk = recs[start:start + CHUNK]
        counted = outcomes(chunk)
        for rec in chunk:
            out = counted[rec.id]
            mode = "-" if rec.kind == KIND_READ else _mode(rec.node, modes)
            r = rows[(rec.kind, rec.picked_by or "?", mode)]
            r["shown"] += 1
            r["opened"] += out.opened
            r["read"] += out.read
            if out.verdict in ("good", "bad"):
                r[out.verdict] += 1
                r[out.verdict + "_shared"] += out.verdict_shared
            r["untouched"] += not (out.opened or out.read or out.verdict)
            r["after"] += not out.blind
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--user-id", type=int, default=None)
    parser.add_argument("--days", type=int, default=None,
                        help="only recommendations from the last N days")
    args = parser.parse_args(argv)
    since = (datetime.utcnow() - timedelta(days=args.days)
             if args.days else None)
    app = create_app()
    with app.app_context():
        rows = report(args.user_id, since)
        head = (f"{'kind':6} {'model':24} {'mode':5} {'shown':>6} "
                f"{'opened':>7} {'read':>6} {'good':>12} {'bad':>12} "
                f"{'untouched':>10} {'after':>6}")
        print(head)
        print("-" * len(head))
        for (kind, model, mode), r in sorted(rows.items()):
            good = f"{r['good']} (+{r['good_shared']})"
            bad = f"{r['bad']} (+{r['bad_shared']})"
            print(f"{kind:6} {model[:24]:24} {mode:5} {r['shown']:>6} "
                  f"{r['opened']:>7} {r['read']:>6} {good:>12} {bad:>12} "
                  f"{r['untouched']:>10} {r['after']:>6}")
        print("\ngood/bad (+n): n of them were given in another reply of "
              "the same group (a parallel Read), not in this one.")


if __name__ == "__main__":
    main()

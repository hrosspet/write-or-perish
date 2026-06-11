#!/usr/bin/env python3
"""
Find users whose profiles were built from incomplete data due to the
chunk-cursor bug (fixed in ce9570e), and mark them for a full rebuild.

The bug (introduced 2026-04-22 in 814cdc7/#116): the incremental export
reported `latest_node_created_at` over the full in-scope node set
instead of the budget-selected window, so chunked profile generation
jumped its resume cursor to the user's newest node after the first
incremental chunk. Every affected from-scratch regen therefore
terminated at exactly 2 chunks (chunk 1 takes the legacy path and is
correct; chunk 2 jumps to the present and ends the loop), regardless of
corpus size.

Detection signals, per user:
  A. chain-shrink (structural, high confidence): a post-2026-04-22
     from-scratch episode with exactly 2 chunks, while some pre-bug
     from-scratch episode needed >= 3 — the corpus only grows, so the
     chunk count cannot legitimately shrink to 2.
  B. token-gap (heuristic fallback, for users with no pre-bug
     baseline and for truncated big-import update chunks): a chunk row
     whose claimed coverage window holds far more estimated corpus
     tokens than the chunk consumed. Estimates (Node.token_count) are
     fuzzy — thresholds absorb tokenizer mismatch and prompt overhead.

An "episode" is a maximal run of non-integration profile rows starting
at a parentless row, where each row follows its predecessor by <= 24h
(separates a regen run from later incremental updates chained onto it).

--execute sets `profile_needs_full_regen` and lets the hourly batch
seeder rebuild via the Batch API (50% cost). Requires the seeder fix
that honors the flag (same deploy as this script) — without it the
flag is ignored and swallowed.

Usage (on prod, from repo root):
    python backend/scripts/regen_profiles_cursor_bug.py            # dry run
    python backend/scripts/regen_profiles_cursor_bug.py --execute  # set flags
"""

import argparse
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from sqlalchemy import func, or_

from backend.app import create_app
from backend.extensions import db
from backend.models import User, UserProfile, Node
from backend.utils.privacy import AI_ALLOWED

BUG_SHIPPED = datetime(2026, 4, 22)
EPISODE_MAX_GAP = timedelta(hours=24)
# Signal B: window must hold 1.5x more estimated tokens than consumed
# AND the absolute gap must exceed 20k.
RATIO_THRESHOLD = 1.5
ABS_THRESHOLD = 20_000


def window_tokens(user_id, start, end):
    """Estimated AI-readable corpus tokens in (start, end]."""
    q = db.session.query(
        func.coalesce(func.sum(Node.token_count), 0)
    ).filter(
        or_(Node.user_id == user_id, Node.human_owner_id == user_id),
        Node.deleted_at.is_(None),
        Node.ai_usage.in_(AI_ALLOWED),
        Node.created_at <= end,
    )
    if start is not None:
        q = q.filter(Node.created_at > start)
    return q.scalar() or 0


def episodes(rows):
    """Group non-integration profile rows into generation episodes.

    Returns [{'rows': [...], 'from_scratch': bool}]. A new episode
    starts at a parentless row or after a >24h gap.
    """
    eps = []
    for r in rows:
        new_episode = (
            r.parent_profile_id is None
            or not eps
            or (r.created_at - eps[-1]["rows"][-1].created_at)
            > EPISODE_MAX_GAP
        )
        if new_episode:
            eps.append({"rows": [r],
                        "from_scratch": r.parent_profile_id is None})
        else:
            eps[-1]["rows"].append(r)
    return eps


def check_user(user):
    """Return a finding dict if the user shows either signal, else None."""
    rows = (UserProfile.query
            .filter(UserProfile.user_id == user.id,
                    UserProfile.generation_type.in_(
                        ("initial", "iterative", "update")))
            .order_by(UserProfile.created_at.asc())
            .all())
    if not rows:
        return None

    eps = episodes(rows)
    pre_bug_max = max(
        (len(e["rows"]) for e in eps
         if e["from_scratch"] and e["rows"][0].created_at <= BUG_SHIPPED),
        default=None)
    post_bug_truncated = [
        e for e in eps
        if e["from_scratch"] and e["rows"][0].created_at > BUG_SHIPPED
        and len(e["rows"]) == 2
    ]
    chain_shrink = bool(
        post_bug_truncated and pre_bug_max is not None and pre_bug_max >= 3)

    # Signal B: per-row coverage-window vs consumed-tokens gap.
    worst_gap = 0
    for p in rows:
        if p.created_at <= BUG_SHIPPED or p.source_data_cutoff is None:
            continue
        parent = p.parent_profile
        parent_cutoff = parent.source_data_cutoff if parent else None
        consumed = ((p.source_tokens_used or 0)
                    - ((parent.source_tokens_used or 0) if parent else 0))
        if consumed < 0:
            consumed = p.source_tokens_used or 0
        est = window_tokens(user.id, parent_cutoff, p.source_data_cutoff)
        if est > consumed * RATIO_THRESHOLD and est - consumed > ABS_THRESHOLD:
            worst_gap = max(worst_gap, est - consumed)

    if not chain_shrink and not worst_gap:
        return None
    signal = ("A+B" if chain_shrink and worst_gap
              else "A" if chain_shrink else "B")
    return {
        "user": user, "signal": signal,
        "pre_bug_chunks": pre_bug_max,
        "post_bug_chunks": (len(post_bug_truncated[0]["rows"])
                            if post_bug_truncated else None),
        "est_missing": worst_gap,
    }


def main(execute):
    app = create_app()
    with app.app_context():
        from backend.tasks.profile_batch import use_batch_for_user

        findings = []
        for user in User.profile_eligible_query().all():
            f = check_user(user)
            if f:
                findings.append(f)
        findings.sort(key=lambda f: (f["signal"] != "A+B",
                                     -f["est_missing"]))

        if not findings:
            print("No affected users found.")
            return

        print(f"=== {len(findings)} affected user(s) ===")
        print("signal A = chain-shrink (structural), "
              "B = token-gap (heuristic)\n")
        header = (f"{'user_id':>7} {'username':<20} {'signal':<6} "
                  f"{'pre_bug':>7} {'post_bug':>8} {'est_missing_tok':>15}")
        print(header)
        print("-" * len(header))
        for f in findings:
            u = f["user"]
            print(f"{u.id:>7} {u.username:<20} {f['signal']:<6} "
                  f"{f['pre_bug_chunks'] or '—':>7} "
                  f"{f['post_bug_chunks'] or '—':>8} "
                  f"{f['est_missing'] or '—':>15}")
        print()

        if not execute:
            print("Dry run — nothing changed. Re-run with --execute to set "
                  "profile_needs_full_regen (hourly batch seeder rebuilds "
                  "via the Batch API).")
            return

        for f in findings:
            u = f["user"]
            notes = []
            if u.profile_batch_pending:
                notes.append("batch in flight — flag will survive it and "
                             "trigger the rebuild after it collects")
            if not use_batch_for_user(u, app.config):
                notes.append("NOT batch-selected — sync fallback only "
                             "fires past its 80k-new-tokens gate; "
                             "consider the regen button or batch allowlist")
            u.profile_needs_full_regen = True
            suffix = f"  ({'; '.join(notes)})" if notes else ""
            print(f"user {u.id} ({u.username}): flag set{suffix}")
        db.session.commit()
        print(f"\n{len(findings)} flag(s) set. The hourly seed_profile_"
              f"batches task picks them up next run.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Flag cursor-bug victims for full profile rebuild")
    parser.add_argument("--execute", action="store_true",
                        help="set flags (default: dry run, display only)")
    args = parser.parse_args()
    main(args.execute)

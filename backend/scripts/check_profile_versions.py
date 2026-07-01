#!/usr/bin/env python3
"""
Inspect stored profile versions for a user — metadata only, no content.

Context (issue: user reports re-generated profile shows outdated facts):
A full regen saves intermediate "iterative" chunk profiles as real
UserProfile rows (oldest source data first) and only at the end saves the
final "integration" profile. The dashboard serves the newest row by
created_at with no generation_type filter, so a user who opens their
profile mid-regen sees an early-chunk iteration built from old data.

This script shows the version chain + in-flight regen state so we can
tell whether that's what happened.

Usage (on prod, from repo root):
    python backend/scripts/check_profile_versions.py [USER_ID]

USER_ID defaults to 27.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from backend.app import create_app
from backend.extensions import db
from backend.models import User, UserProfile, ProfileBatchJob


def fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "—"


def check(user_id):
    app = create_app()
    with app.app_context():
        user = db.session.get(User, user_id)
        if not user:
            print(f"No user with id={user_id}")
            return

        print(f"=== User id={user.id} username={user.username!r} ===\n")

        # 1. In-flight regen state on the user row
        print("--- regen state flags ---")
        print(f"profile_generation_task_id:            "
              f"{user.profile_generation_task_id or '—'}")
        print(f"profile_generation_task_dispatched_at: "
              f"{fmt(user.profile_generation_task_dispatched_at)} UTC")
        print(f"profile_needs_full_regen:              "
              f"{user.profile_needs_full_regen}")
        print(f"profile_batch_pending:                 "
              f"{user.profile_batch_pending}")
        print(f"profile_batch_attempts:                "
              f"{user.profile_batch_attempts}")
        print()

        # 2. All profile versions, newest first (what the dashboard serves
        #    is the top row)
        profiles = (UserProfile.query
                    .filter_by(user_id=user.id)
                    .order_by(UserProfile.created_at.desc())
                    .all())
        total = len(profiles)
        print(f"--- {total} profile versions (newest first; "
              f"dashboard serves the top row) ---")
        header = (f"{'ver':>4} {'id':>6} {'created_at (UTC)':<20} "
                  f"{'gen_type':<12} {'generated_by':<24} {'parent':>6} "
                  f"{'out_tok':>8} {'src_tok':>9} {'chars':>7} "
                  f"{'source_data_cutoff (UTC)':<20}")
        print(header)
        print("-" * len(header))
        for i, p in enumerate(profiles):
            try:
                chars = len(p.get_content())
            except Exception:
                chars = -1  # decryption failed
            print(f"{total - i:>4} {p.id:>6} {fmt(p.created_at):<20} "
                  f"{p.generation_type or '—':<12} "
                  f"{p.generated_by:<24} "
                  f"{p.parent_profile_id or '—':>6} "
                  f"{p.tokens_used:>8} {p.source_tokens_used or 0:>9} "
                  f"{chars:>7} {fmt(p.source_data_cutoff):<20}")
        print()

        # 3. Verdict on the hypothesis
        if profiles:
            latest = profiles[0]
            print("--- diagnosis ---")
            if latest.generation_type == "integration":
                print(f"Latest version (id={latest.id}) IS the final "
                      f"'integration' profile, finished {fmt(latest.created_at)} UTC.")
                print("If the user looked BEFORE that timestamp, he saw an "
                      "intermediate 'iterative' chunk — check the rows above "
                      "for iterative versions created shortly before it.")
            elif latest.generation_type == "iterative":
                print(f"Latest version (id={latest.id}) is an intermediate "
                      f"'iterative' chunk — regen is STILL IN PROGRESS "
                      f"(or died mid-way). The user is currently seeing "
                      f"old-data-only content. Hypothesis confirmed.")
            else:
                print(f"Latest version (id={latest.id}) has "
                      f"generation_type={latest.generation_type!r} — the "
                      f"mid-regen hypothesis doesn't apply; look at "
                      f"source_data_cutoff above to see what data it covers.")
        print()

        # 4. Batch jobs that include items for this user
        print("--- profile batch jobs touching this user (newest first) ---")
        jobs = (ProfileBatchJob.query
                .order_by(ProfileBatchJob.submitted_at.desc())
                .limit(50)
                .all())
        found = 0
        for job in jobs:
            mine = [it for it in (job.items or [])
                    if it.get("user_id") == user.id]
            if not mine:
                continue
            found += 1
            kinds = ", ".join(
                f"{it.get('kind')}(chunk={it.get('chunk_num')})"
                for it in mine)
            print(f"job={job.id} provider={job.provider_key} "
                  f"status={job.status} submitted={fmt(job.submitted_at)} "
                  f"collected={fmt(job.collected_at)} items=[{kinds}]")
        if not found:
            print("(none in the 50 most recent jobs)")


if __name__ == "__main__":
    uid = int(sys.argv[1]) if len(sys.argv) > 1 else 27
    check(uid)

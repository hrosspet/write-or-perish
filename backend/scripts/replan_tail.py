#!/usr/bin/env python3
"""Re-plan the unread tail of pre-filled accounts (docs/design/chunk-planner.md).

Before the planner, the chunk loop deferred any remainder below the
minimum chunk to "the next update cycle" — which a pre-filled corpus
never gets — so those profiles end months before the newest tweets
("reads as 2025"). The planner now covers every remainder, and the
continue rule picks such an account up on its own: its unread tail is
older than the chain's tip, so the next seed pass plans the tail into
a chunk. But a tail that is well below a full chunk would then become
one small, uneven update. This script moves the chain's tip BACK to the
latest version from which the remainder plans into full-size chunks,
so the tail is folded in with even weight.

Per account: walk the non-integration chain back from its tip; for every
version measure the units remaining after its cutoff (the export
window's own scope) and plan them; pick the LATEST version whose plan
yields chunks of at least FULL_CHUNK_FRACTION x CHUNK_TARGET_UNITS (the
lower edge of the band the planner guarantees for k >= 2). Branching
writes a "revert" copy of that version — the mechanism imports already
use — which becomes the chain tip: the continue rule then seeds the
account, the planner covers the remainder in equal chunks, and the chain
re-integrates. The superseded versions stay as history.

Dry run by default: prints every account's chain, tail and plan, and
writes nothing. --apply writes the revert rows; --seed also dispatches
an immediate batch seed for each applied account (pinned accounts only
ever take the Batch path). Metadata only — no profile or node content
is read or printed.

    python backend/scripts/replan_tail.py --user xiq                 # dry run, one account
    python backend/scripts/replan_tail.py --all-prefilled            # dry run, every pre-filled account
    python backend/scripts/replan_tail.py --user xiq --apply --seed
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from backend.utils.chunk_plan import CHUNK_TARGET_UNITS, plan_chunks  # noqa: E402

# A branch point must plan into chunks at least this fraction of T: the
# floor of the size band the planner guarantees from k = 2 up (±25 %).
# Below it the tail would become one undersized update.
FULL_CHUNK_FRACTION = 0.75


def choose_branch(versions_with_remaining, target=CHUNK_TARGET_UNITS):
    """``versions_with_remaining``: [(version, remaining_units)] ordered tip
    first. Returns (version, k, size) for the LATEST version whose remainder
    plans into chunks of at least FULL_CHUNK_FRACTION x target, or None when
    no version qualifies (the whole corpus is smaller than that)."""
    for version, remaining in versions_with_remaining:
        k, size = plan_chunks(remaining, target=target)
        if k and size >= FULL_CHUNK_FRACTION * target:
            return version, k, size
    return None


def plan_repair(user):
    """Inspect one account. Returns a dict with the chain (tip first), the
    tail after the tip, the chosen branch point and what applying it
    means — or a ``status`` when there is nothing to do."""
    from backend.routes.export_data import count_remaining_units
    from backend.tasks.exports import _collect_iterative_chain
    from backend.tasks.profile_batch import _latest_non_integration_profile

    tip = _latest_non_integration_profile(user.id)
    if tip is None:
        return {"status": "no profile"}
    if tip.source_data_cutoff is None:
        return {"status": "tip has no cutoff (user-written / legacy)"}
    tail = count_remaining_units(user.id, tip.source_data_cutoff)
    if tail == 0:
        return {"status": "complete — nothing beyond the tip's cutoff"}
    chain = list(reversed(_collect_iterative_chain(tip.id)))   # tip first
    versions = [
        (v, count_remaining_units(user.id, v.source_data_cutoff))
        for v in chain if v.source_data_cutoff is not None
    ]
    choice = choose_branch(versions)
    out = {"tip": tip, "tail": tail, "versions": versions, "choice": choice}
    if choice is None:
        out["status"] = (f"no version plans into a chunk >= "
                         f"{FULL_CHUNK_FRACTION:.0%} of T — corpus too small")
    elif choice[0].id == tip.id:
        out["status"] = ("tail already plans into full chunks — the continue "
                         "rule picks it up; nothing to write")
    else:
        out["status"] = "branch"
        out["superseded"] = [v for v, _ in versions
                             if v.created_at > choice[0].created_at]
    return out


def apply_branch(user, version):
    """Write the revert copy that makes ``version`` the chain tip (same row
    shape as revert_profile_for_import)."""
    from backend.extensions import db
    from backend.models import UserProfile
    from backend.utils.privacy import PrivacyLevel
    copy = UserProfile(
        user_id=user.id,
        generated_by=version.generated_by,
        tokens_used=0,
        privacy_level=PrivacyLevel.PRIVATE,
        ai_usage=version.ai_usage,
        source_tokens_used=version.source_tokens_used,
        source_data_cutoff=version.source_data_cutoff,
        source_origin_stats=version.source_origin_stats,
        generation_type="revert",
        parent_profile_id=version.id,
    )
    copy.set_content(version.get_content())
    db.session.add(copy)
    db.session.commit()
    return copy


def _fmt_version(v, remaining):
    k, size = plan_chunks(remaining)
    cutoff = v.source_data_cutoff.strftime("%Y-%m-%d") if v.source_data_cutoff else "—"
    return (f"    v{v.id:<6} {v.generation_type or '-':<11} cutoff {cutoff}  "
            f"remaining {remaining:>9,} units → {k} × {size:,.0f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", action="append", default=[],
                    help="username (repeatable)")
    ap.add_argument("--all-prefilled", action="store_true",
                    help="every account with a prefilled_handle")
    ap.add_argument("--apply", action="store_true",
                    help="write the revert rows (default: dry run)")
    ap.add_argument("--seed", action="store_true",
                    help="with --apply: dispatch an immediate batch seed")
    args = ap.parse_args()
    if not args.user and not args.all_prefilled:
        ap.error("give --user NAME (repeatable) or --all-prefilled")

    from backend import create_app
    app = create_app()
    with app.app_context():
        from backend.models import User
        users = []
        if args.all_prefilled:
            users = User.query.filter(User.prefilled_handle.isnot(None)).all()
        for name in args.user:
            u = User.query.filter_by(username=name).first()
            if not u:
                sys.exit(f"no user {name!r}")
            if u not in users:
                users.append(u)

        applied = []
        for user in users:
            plan = plan_repair(user)
            print(f"@{user.username} (id {user.id}, pinned={user.profile_force_batch}, "
                  f"handle={user.prefilled_handle}, in flight={user.profile_batch_pending})")
            if "versions" in plan:
                print(f"  tip v{plan['tip'].id}: {plan['tail']:,} units unread after "
                      f"{plan['tip'].source_data_cutoff:%Y-%m-%d}")
                for v, remaining in plan["versions"]:
                    print(_fmt_version(v, remaining))
            print(f"  → {plan['status']}")
            if plan.get("status") != "branch":
                print()
                continue
            version, k, size = plan["choice"]
            print(f"    branch from v{version.id} (cutoff "
                  f"{version.source_data_cutoff:%Y-%m-%d}): {k} chunk(s) of "
                  f"{size:,.0f} units; supersedes "
                  f"{len(plan['superseded'])} version(s)")
            if args.apply:
                if user.profile_batch_pending:
                    print("    SKIPPED: a batch step is in flight")
                else:
                    copy = apply_branch(user, version)
                    print(f"    wrote revert v{copy.id} → chain tip")
                    applied.append(user)
            print()

        if args.apply and args.seed:
            from backend.tasks.profile_batch import seed_profile_batch_for_user
            for user in applied:
                seed_profile_batch_for_user.delay(user.id)
                print(f"@{user.username}: immediate batch seed dispatched")
        elif not args.apply:
            print("dry run — nothing written (use --apply)")


if __name__ == "__main__":
    main()

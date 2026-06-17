#!/usr/bin/env python3
"""
Backfill ai_preferences UserArtifact rows from legacy UserAIPreferences (#158
Slice 5).

AI interaction preferences were folded into the UserArtifact model (kind
"ai_preferences"). This is the expand-contract DATA step: for each user, copy
every UserAIPreferences version into a UserArtifact(kind="ai_preferences")
version, preserving content, created_at, generated_by, tokens_used, ai_usage,
and privacy_level so the version history carries over intact.

The old user_ai_preferences table is left in place (dropped later — #219);
get_user_ai_preferences_content falls back to it until this runs, so the
deploy is safe before the backfill.

Idempotent: any user who already has an ai_preferences UserArtifact is
skipped, so re-running is safe. (A user who edited their prefs via the new
path after deploy but before this ran keeps only that new version here — their
legacy history stays in UserAIPreferences until #219.)

Usage (on prod, from repo root):
    python backend/scripts/backfill_ai_preferences_artifacts.py            # dry run
    python backend/scripts/backfill_ai_preferences_artifacts.py --execute
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from backend.extensions import db
from backend.models import (
    UserAIPreferences, UserArtifact, NodeContextArtifact,
)

KIND = "ai_preferences"
TITLE = UserArtifact.DEFAULT_KINDS[KIND]
DESCRIPTION = UserArtifact.DEFAULT_DESCRIPTIONS.get(KIND)


def _repoint_node_pins(execute):
    """Migrate the node *references*: each legacy NodeContextArtifact pin of
    type 'ai_preferences' (→ a UserAIPreferences row) is repointed to the
    backfilled UserArtifact, matched by (user_id, created_at) — which the row
    backfill preserves exactly, so the match is unambiguous. After this, no
    node references ai_preferences via the legacy pin type, so the legacy code
    paths become dead and can be dropped (#219) as the migration's
    verification. Non-destructive: the UserAIPreferences rows are kept (the
    pin pointer is updated, reversible by the same created_at match). An
    already-repointed pin is no longer 'ai_preferences' type, so re-runs are
    idempotent. Returns (repointed, unmatched)."""
    pins = NodeContextArtifact.query.filter_by(
        artifact_type="ai_preferences").all()
    if not execute:
        # Dry run: the target UserArtifact rows aren't created yet (the row
        # pass is also dry), so a created_at match would find nothing. Report
        # the legacy-pin count as the would-repoint magnitude instead.
        print(f"would repoint: {len(pins)} legacy ai_preferences node pins "
              f"-> user_artifact (matched on execute, once rows exist)")
        return len(pins), 0
    repointed = 0
    unmatched = 0
    for pin in pins:
        legacy = UserAIPreferences.query.get(pin.artifact_id)
        art = None
        if legacy is not None:
            art = UserArtifact.query.filter_by(
                user_id=legacy.user_id, kind=KIND,
                created_at=legacy.created_at,
            ).order_by(UserArtifact.id.asc()).first()
        if art is None:
            unmatched += 1
            continue
        repointed += 1
        pin.artifact_type = "user_artifact"
        pin.artifact_id = art.id
    db.session.commit()
    print(f"repointed: {repointed} legacy ai_preferences node pins -> "
          f"user_artifact"
          + (f"; {unmatched} unmatched (no backfilled artifact)"
             if unmatched else ""))
    return repointed, unmatched


def run_backfill(execute):
    """Core backfill — assumes an active app context (the caller provides it,
    so this stays importable for tests). Migrates rows AND node pins, both
    non-destructively. Returns (users_migrated, rows_made, pins_repointed)."""
    # Users who already have an ai_preferences artifact — skip (idempotent).
    migrated_user_ids = {
        uid for (uid,) in db.session.query(UserArtifact.user_id)
        .filter(UserArtifact.kind == KIND).distinct().all()
    }
    pref_user_ids = [
        uid for (uid,) in db.session.query(UserAIPreferences.user_id)
        .distinct().all()
    ]
    to_migrate = [u for u in pref_user_ids if u not in migrated_user_ids]
    print(f"{len(pref_user_ids)} users with legacy AI preferences; "
          f"{len(migrated_user_ids)} already have an ai_preferences artifact "
          f"(skipped); {len(to_migrate)} to migrate")

    total_rows = 0
    decrypt_failures = 0
    for uid in to_migrate:
        prefs = (UserAIPreferences.query.filter_by(user_id=uid)
                 .order_by(UserAIPreferences.created_at.asc(),
                           UserAIPreferences.id.asc()).all())
        for p in prefs:
            try:
                content = p.get_content()
            except Exception:
                decrypt_failures += 1
                continue
            total_rows += 1
            if execute:
                art = UserArtifact(
                    user_id=uid,
                    kind=KIND,
                    title=TITLE,
                    description=DESCRIPTION,
                    generated_by=p.generated_by,
                    tokens_used=p.tokens_used,
                    created_at=p.created_at,
                    privacy_level=p.privacy_level,
                    ai_usage=p.ai_usage,
                )
                art.set_content(content)
                db.session.add(art)
        if execute:
            db.session.commit()  # atomic per user

    print(f"{'created' if execute else 'would create'}: {total_rows} "
          f"ai_preferences artifact versions across {len(to_migrate)} users")
    if decrypt_failures:
        print(f"WARNING: {decrypt_failures} pref rows failed to decrypt "
              f"(skipped)")

    # Migrate the node references too (must run after the rows exist so the
    # created_at match finds them).
    repointed, _ = _repoint_node_pins(execute)

    if not execute:
        print("\nDry run — re-run with --execute to apply.")
    return len(to_migrate), total_rows, repointed


def main(execute):
    from backend.app import create_app  # local: keep run_backfill importable
    app = create_app()
    with app.app_context():
        run_backfill(execute)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Backfill ai_preferences UserArtifact rows from "
                    "UserAIPreferences (#158 Slice 5)")
    parser.add_argument("--execute", action="store_true",
                        help="apply changes (default: dry run)")
    args = parser.parse_args()
    main(args.execute)

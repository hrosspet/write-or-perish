"""Backfill permalink slugs for public root nodes that predate #229.

Slugs are normally assigned at publish (share pipeline) or at creation
(direct public roots, since the same commit as this script). Anything
public+root from before gets one here.

Usage (dry-run by default):
    python backend/scripts/backfill_public_slugs.py [--execute]

Light: one filtered query over root nodes; safe on the prod VM.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")))

from backend import create_app  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true",
                    help="Write slugs (default: dry run)")
    args = ap.parse_args()

    app = create_app()
    with app.app_context():
        from backend.extensions import db
        from backend.models import Node
        from backend.utils.slugs import generate_unique_public_slug

        rows = Node.query.filter(
            Node.parent_id.is_(None),
            Node.privacy_level == "public",
            Node.deleted_at.is_(None),
            Node.public_slug.is_(None),
        ).order_by(Node.id.asc()).all()
        print(f"{len(rows)} public roots without a slug")
        for node in rows:
            owner_id = node.human_owner_id or node.user_id
            slug = generate_unique_public_slug(
                owner_id, node.get_content() or "")
            print(f"  node {node.id} (owner {owner_id}) -> {slug}")
            if args.execute:
                node.public_slug = slug
                db.session.flush()
        if args.execute:
            db.session.commit()
            print("COMMITTED")
        else:
            print("DRY RUN — re-run with --execute to write")


if __name__ == "__main__":
    main()

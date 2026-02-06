#!/usr/bin/env python3
"""
Migrate all existing free-plan users to the alpha plan.

Usage:
    python backend/scripts/migrate_users_to_alpha.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from backend.app import create_app
from backend.extensions import db
from sqlalchemy import text


def migrate_users_to_alpha():
    app = create_app()

    with app.app_context():
        # Show current state
        result = db.session.execute(
            text("SELECT plan, COUNT(*) as count FROM \"user\" GROUP BY plan")
        )

        print("Current user plans:")
        print("-" * 40)
        for row in result:
            print(f"  {row.plan}: {row.count} users")
        print()

        # Count users to update
        free_count = db.session.execute(
            text("SELECT COUNT(*) FROM \"user\" WHERE plan = 'free'")
        ).scalar()

        if free_count == 0:
            print("No free-plan users to migrate.")
            return

        print(f"Migrating {free_count} users from 'free' to 'alpha'...")

        result = db.session.execute(
            text("UPDATE \"user\" SET plan = 'alpha' WHERE plan = 'free'")
        )
        db.session.commit()

        print(f"Updated {result.rowcount} users.")
        print()

        # Show updated state
        result = db.session.execute(
            text("SELECT plan, COUNT(*) as count FROM \"user\" GROUP BY plan")
        )

        print("Updated user plans:")
        print("-" * 40)
        for row in result:
            print(f"  {row.plan}: {row.count} users")
        print()
        print("Done!")


if __name__ == "__main__":
    migrate_users_to_alpha()

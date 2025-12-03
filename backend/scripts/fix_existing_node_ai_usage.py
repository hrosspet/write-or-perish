#!/usr/bin/env python3
"""
Fix existing nodes to have ai_usage='train' instead of 'none'.

This script updates nodes that were created before the privacy feature
to use ai_usage='train', maintaining the original value proposition where
users contribute training data.

Usage:
    python backend/scripts/fix_existing_node_ai_usage.py
"""

import sys
import os

# Add backend directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from backend.app import create_app
from backend.extensions import db
from backend.models import Node
from sqlalchemy import text


def fix_existing_nodes():
    """Update existing nodes to have ai_usage='train'."""
    app = create_app()

    with app.app_context():
        # Check current state
        result = db.session.execute(
            text("SELECT privacy_level, ai_usage, COUNT(*) as count FROM node GROUP BY privacy_level, ai_usage")
        )

        print("Current node privacy settings:")
        print("-" * 50)
        for row in result:
            print(f"  privacy_level={row.privacy_level}, ai_usage={row.ai_usage}: {row.count} nodes")
        print()

        # Count nodes that need updating
        nodes_to_update = db.session.execute(
            text("SELECT COUNT(*) FROM node WHERE ai_usage = 'none'")
        ).scalar()

        if nodes_to_update == 0:
            print("✓ All nodes already have appropriate ai_usage settings!")
            return

        print(f"Found {nodes_to_update} nodes with ai_usage='none'")
        print("Updating to ai_usage='train' (maintains training data value proposition)...")

        # Update all nodes with ai_usage='none' to 'train'
        result = db.session.execute(
            text("UPDATE node SET ai_usage = 'train' WHERE ai_usage = 'none'")
        )
        db.session.commit()

        print(f"✓ Successfully updated {result.rowcount} nodes")
        print()

        # Show updated state
        result = db.session.execute(
            text("SELECT privacy_level, ai_usage, COUNT(*) as count FROM node GROUP BY privacy_level, ai_usage")
        )

        print("Updated node privacy settings:")
        print("-" * 50)
        for row in result:
            print(f"  privacy_level={row.privacy_level}, ai_usage={row.ai_usage}: {row.count} nodes")
        print()
        print("✓ Migration complete!")


if __name__ == "__main__":
    fix_existing_nodes()

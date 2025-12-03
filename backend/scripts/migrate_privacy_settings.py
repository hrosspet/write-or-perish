#!/usr/bin/env python3
"""
Data migration script for privacy settings.

This script updates existing nodes to have appropriate privacy settings:
- Existing nodes: default to privacy_level='private' and ai_usage='train'
  (maintains the original value proposition where users contribute training data)
- This is run once after deploying the privacy level feature

Usage:
    python backend/scripts/migrate_privacy_settings.py
"""

import sys
import os

# Add backend directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from backend.app import create_app
from backend.extensions import db
from backend.models import Node
from backend.utils.privacy import PrivacyLevel, AIUsage


def migrate_existing_nodes():
    """Migrate existing nodes to have privacy settings."""
    app = create_app()

    with app.app_context():
        # Find nodes that don't have privacy settings yet
        # (In practice, this will be all existing nodes after adding the columns)
        nodes_to_update = Node.query.all()

        if not nodes_to_update:
            print("No nodes found to migrate.")
            return

        print(f"Found {len(nodes_to_update)} nodes to migrate")

        updated_count = 0
        for node in nodes_to_update:
            # Set privacy level to private (only owner can see)
            # Set AI usage to 'train' for existing nodes to maintain the
            # original value proposition where users contribute training data
            node.privacy_level = PrivacyLevel.PRIVATE
            node.ai_usage = AIUsage.TRAIN
            updated_count += 1

            if updated_count % 100 == 0:
                print(f"Updated {updated_count} nodes...")
                db.session.commit()

        # Final commit
        db.session.commit()
        print(f"Successfully migrated {updated_count} nodes")
        print(f"  privacy_level: {PrivacyLevel.PRIVATE}")
        print(f"  ai_usage: {AIUsage.TRAIN}")


if __name__ == "__main__":
    migrate_existing_nodes()

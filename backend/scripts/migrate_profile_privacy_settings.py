#!/usr/bin/env python3
"""
Data migration script for user profile privacy settings.

This script updates existing user profiles to have appropriate privacy settings:
- Existing profiles: default to privacy_level='private' and ai_usage='chat'
  (profiles are useful for AI to understand the user for responses)
- This is run once after deploying the privacy level feature

Usage:
    python backend/scripts/migrate_profile_privacy_settings.py
"""

import sys
import os

# Add backend directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from backend.app import create_app
from backend.extensions import db
from backend.models import UserProfile
from backend.utils.privacy import PrivacyLevel, AIUsage


def migrate_existing_profiles():
    """Migrate existing user profiles to have privacy settings."""
    app = create_app()

    with app.app_context():
        # Find profiles that don't have privacy settings yet
        profiles_to_update = UserProfile.query.all()

        if not profiles_to_update:
            print("No profiles found to migrate.")
            return

        print(f"Found {len(profiles_to_update)} profiles to migrate")

        updated_count = 0
        for profile in profiles_to_update:
            # Set privacy level to private (only owner can see)
            # Set AI usage to 'chat' for existing profiles so AI can use them
            # to understand the user when generating responses
            profile.privacy_level = PrivacyLevel.PRIVATE
            profile.ai_usage = AIUsage.CHAT
            updated_count += 1

            if updated_count % 100 == 0:
                print(f"Updated {updated_count} profiles...")
                db.session.commit()

        # Final commit
        db.session.commit()
        print(f"Successfully migrated {updated_count} profiles")
        print(f"  privacy_level: {PrivacyLevel.PRIVATE}")
        print(f"  ai_usage: {AIUsage.CHAT}")


if __name__ == "__main__":
    migrate_existing_profiles()

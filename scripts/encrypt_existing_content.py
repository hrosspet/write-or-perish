#!/usr/bin/env python3
"""
Migration script to encrypt existing content in the database.

This script:
1. Backs up the database before making changes
2. Encrypts all existing node content, versions, drafts, profiles, and transcript chunks
3. Optionally encrypts audio files (with backup)

Usage:
    python scripts/encrypt_existing_content.py --backup-only   # Just create backups
    python scripts/encrypt_existing_content.py --encrypt-db    # Encrypt database content
    python scripts/encrypt_existing_content.py --encrypt-audio # Encrypt audio files
    python scripts/encrypt_existing_content.py --all           # All of the above

IMPORTANT: Run --backup-only first and verify backups before running encryption!
"""

import os
import sys
import argparse
import subprocess
from datetime import datetime

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def get_db_connection_info():
    """Extract database connection info from DATABASE_URL."""
    db_url = os.environ.get("DATABASE_URL", "postgresql://localhost/writeorperish")
    # Parse postgresql://user:password@host/database
    if "@" in db_url:
        auth_part, host_part = db_url.split("://")[1].rsplit("@", 1)
        if ":" in auth_part:
            user, password = auth_part.split(":", 1)
        else:
            user, password = auth_part, None
    else:
        host_part = db_url.split("://")[1]
        user, password = None, None

    if "/" in host_part:
        host, database = host_part.rsplit("/", 1)
    else:
        host, database = "localhost", host_part

    return {
        "user": user,
        "password": password,
        "host": host,
        "database": database
    }


def backup_database(backup_dir: str) -> str:
    """Create a PostgreSQL database backup using pg_dump."""
    db_info = get_db_connection_info()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = os.path.join(backup_dir, f"db_backup_{timestamp}.sql")

    print(f"Creating database backup: {backup_file}")

    cmd = ["pg_dump"]
    if db_info["user"]:
        cmd.extend(["-U", db_info["user"]])
    if db_info["host"]:
        cmd.extend(["-h", db_info["host"]])
    cmd.extend(["-f", backup_file, db_info["database"]])

    env = os.environ.copy()
    if db_info["password"]:
        env["PGPASSWORD"] = db_info["password"]

    try:
        subprocess.run(cmd, env=env, check=True, capture_output=True, text=True)
        print(f"Database backup created successfully: {backup_file}")
        return backup_file
    except subprocess.CalledProcessError as e:
        print(f"Database backup failed: {e.stderr}")
        raise


def backup_audio_files(backup_dir: str) -> str:
    """Create a zip backup of all audio files."""
    audio_dir = os.environ.get("AUDIO_STORAGE_PATH", "data/audio")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = os.path.join(backup_dir, f"audio_backup_{timestamp}.zip")

    if not os.path.exists(audio_dir):
        print(f"Audio directory not found: {audio_dir}")
        return None

    print(f"Creating audio backup: {backup_file}")
    print(f"This may take a while for large audio collections...")

    # Convert to absolute path since zip runs with cwd=audio_dir
    backup_file_abs = os.path.abspath(backup_file)

    try:
        # Use zip to preserve directory structure
        subprocess.run(
            ["zip", "-r", backup_file_abs, "."],
            cwd=audio_dir,
            check=True,
            capture_output=True,
            text=True
        )
        print(f"Audio backup created successfully: {backup_file}")
        return backup_file
    except subprocess.CalledProcessError as e:
        print(f"Audio backup failed: {e.stderr}")
        raise


def encrypt_database_content():
    """Encrypt all existing content in the database using v2 envelope encryption.

    Decrypts any v1-encrypted content first, then re-encrypts everything with v2.
    """
    from backend import create_app
    from backend.extensions import db
    from backend.models import Node, NodeVersion, Draft, UserProfile, NodeTranscriptChunk
    from backend.utils.encryption import (
        encrypt_content, decrypt_content, is_encryption_enabled,
        ENCRYPTED_PREFIX_V1, ENCRYPTED_PREFIX_V2,
    )

    if not is_encryption_enabled():
        print("ERROR: Encryption is not enabled!")
        print("Please set GCP_KMS_KEY_NAME environment variable.")
        return False

    def encrypt_field(record, field_name, label, stats):
        """Encrypt a single field, decrypting v1 first if needed."""
        value = getattr(record, field_name)
        if not value:
            return
        if value.startswith(ENCRYPTED_PREFIX_V2):
            stats["skipped"] += 1
            return
        if value.startswith(ENCRYPTED_PREFIX_V1):
            value = decrypt_content(value)
            stats["re_encrypted"] += 1
        else:
            stats["newly_encrypted"] += 1
        setattr(record, field_name, encrypt_content(value))
        stats["total"] += 1

    app = create_app()
    with app.app_context():
        models_fields = [
            (Node, "content", "Node"),
            (NodeVersion, "content", "NodeVersion"),
            (Draft, "content", "Draft"),
            (UserProfile, "content", "UserProfile"),
            (NodeTranscriptChunk, "text", "NodeTranscriptChunk"),
        ]

        for model_class, field_name, label in models_fields:
            print(f"\nEncrypting {label} {field_name}...")
            records = model_class.query.all()
            stats = {"total": 0, "newly_encrypted": 0, "re_encrypted": 0, "skipped": 0}
            for i, record in enumerate(records, 1):
                encrypt_field(record, field_name, label, stats)
                if i % 100 == 0:
                    print(f"  Processed {i} {label} records...")
                    db.session.commit()
            db.session.commit()
            print(f"  {label}: {stats['newly_encrypted']} new, "
                  f"{stats['re_encrypted']} re-encrypted (v1->v2), "
                  f"{stats['skipped']} already v2")

        print("\nDatabase content encryption complete!")
        return True


def encrypt_audio_files():
    """Encrypt all existing audio files using envelope encryption."""
    from backend.utils.encryption import encrypt_file, is_encryption_enabled

    if not is_encryption_enabled():
        print("ERROR: Encryption is not enabled!")
        print("Please set GCP_KMS_KEY_NAME environment variable.")
        return False

    audio_dir = os.environ.get("AUDIO_STORAGE_PATH", "data/audio")
    if not os.path.exists(audio_dir):
        print(f"Audio directory not found: {audio_dir}")
        return True  # Not an error - just no files to encrypt

    print(f"\nEncrypting audio files in {audio_dir}...")

    encrypted_count = 0
    skipped_count = 0

    for root, dirs, files in os.walk(audio_dir):
        for filename in files:
            filepath = os.path.join(root, filename)

            if filename.endswith('.enc'):
                skipped_count += 1
                continue

            if filename.endswith('.json'):
                continue

            try:
                encrypt_file(filepath)
                encrypted_count += 1
                if encrypted_count % 10 == 0:
                    print(f"  Encrypted {encrypted_count} audio files...")
            except Exception as e:
                print(f"  ERROR encrypting {filepath}: {e}")

    print(f"  Total audio files encrypted: {encrypted_count}")
    print(f"  Skipped (already encrypted): {skipped_count}")
    print("\nAudio file encryption complete!")
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Encrypt existing content in the database and audio files."
    )
    parser.add_argument(
        "--backup-only",
        action="store_true",
        help="Only create backups, don't encrypt"
    )
    parser.add_argument(
        "--encrypt-db",
        action="store_true",
        help="Encrypt database content"
    )
    parser.add_argument(
        "--encrypt-audio",
        action="store_true",
        help="Encrypt audio files"
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Create backups and encrypt everything"
    )
    parser.add_argument(
        "--backup-dir",
        type=str,
        default="backups",
        help="Directory to store backups (default: ./backups)"
    )

    args = parser.parse_args()

    # Validate that at least one action is specified
    if not any([args.backup_only, args.encrypt_db, args.encrypt_audio, args.all]):
        parser.print_help()
        print("\nError: Please specify at least one action.")
        sys.exit(1)

    # Create backup directory
    os.makedirs(args.backup_dir, exist_ok=True)

    # Create backups first
    if args.backup_only or args.all:
        print("=" * 60)
        print("STEP 1: Creating backups")
        print("=" * 60)

        try:
            db_backup = backup_database(args.backup_dir)
            audio_backup = backup_audio_files(args.backup_dir)
            print("\nBackups created successfully!")
            print(f"  Database: {db_backup}")
            if audio_backup:
                print(f"  Audio: {audio_backup}")
        except Exception as e:
            print(f"\nBackup failed: {e}")
            sys.exit(1)

        if args.backup_only:
            print("\n--backup-only specified, exiting.")
            return

    # Encrypt database content
    if args.encrypt_db or args.all:
        print("\n" + "=" * 60)
        print("STEP 2: Encrypting database content")
        print("=" * 60)

        if not encrypt_database_content():
            print("\nDatabase encryption failed!")
            sys.exit(1)

    # Encrypt audio files
    if args.encrypt_audio or args.all:
        print("\n" + "=" * 60)
        print("STEP 3: Encrypting audio files")
        print("=" * 60)

        if not encrypt_audio_files():
            print("\nAudio encryption failed!")
            sys.exit(1)

    print("\n" + "=" * 60)
    print("ENCRYPTION COMPLETE!")
    print("=" * 60)


if __name__ == "__main__":
    main()

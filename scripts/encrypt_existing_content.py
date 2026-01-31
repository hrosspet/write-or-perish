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

    try:
        # Use zip to preserve directory structure
        subprocess.run(
            ["zip", "-r", backup_file, "."],
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
    """Encrypt all existing content in the database."""
    from backend import create_app
    from backend.extensions import db
    from backend.models import Node, NodeVersion, Draft, UserProfile, NodeTranscriptChunk
    from backend.utils.encryption import encrypt_content, is_encryption_enabled, is_content_encrypted

    if not is_encryption_enabled():
        print("ERROR: Encryption is not enabled!")
        print("Please set GCP_KMS_KEY_NAME environment variable.")
        return False

    app = create_app()
    with app.app_context():
        # Encrypt Node content
        print("\nEncrypting Node content...")
        nodes = Node.query.all()
        encrypted_count = 0
        for node in nodes:
            if node.content and not is_content_encrypted(node.content):
                node.content = encrypt_content(node.content)
                encrypted_count += 1
                if encrypted_count % 100 == 0:
                    print(f"  Encrypted {encrypted_count} nodes...")
                    db.session.commit()
        db.session.commit()
        print(f"  Total nodes encrypted: {encrypted_count}")

        # Encrypt NodeVersion content
        print("\nEncrypting NodeVersion content...")
        versions = NodeVersion.query.all()
        encrypted_count = 0
        for version in versions:
            if version.content and not is_content_encrypted(version.content):
                version.content = encrypt_content(version.content)
                encrypted_count += 1
                if encrypted_count % 100 == 0:
                    print(f"  Encrypted {encrypted_count} versions...")
                    db.session.commit()
        db.session.commit()
        print(f"  Total versions encrypted: {encrypted_count}")

        # Encrypt Draft content
        print("\nEncrypting Draft content...")
        drafts = Draft.query.all()
        encrypted_count = 0
        for draft in drafts:
            if draft.content and not is_content_encrypted(draft.content):
                draft.content = encrypt_content(draft.content)
                encrypted_count += 1
        db.session.commit()
        print(f"  Total drafts encrypted: {encrypted_count}")

        # Encrypt UserProfile content
        print("\nEncrypting UserProfile content...")
        profiles = UserProfile.query.all()
        encrypted_count = 0
        for profile in profiles:
            if profile.content and not is_content_encrypted(profile.content):
                profile.content = encrypt_content(profile.content)
                encrypted_count += 1
        db.session.commit()
        print(f"  Total profiles encrypted: {encrypted_count}")

        # Encrypt NodeTranscriptChunk text
        print("\nEncrypting NodeTranscriptChunk text...")
        chunks = NodeTranscriptChunk.query.all()
        encrypted_count = 0
        for chunk in chunks:
            if chunk.text and not is_content_encrypted(chunk.text):
                chunk.text = encrypt_content(chunk.text)
                encrypted_count += 1
                if encrypted_count % 100 == 0:
                    print(f"  Encrypted {encrypted_count} chunks...")
                    db.session.commit()
        db.session.commit()
        print(f"  Total chunks encrypted: {encrypted_count}")

        print("\nDatabase content encryption complete!")
        return True


def encrypt_audio_files():
    """Encrypt all existing audio files using GCP KMS."""
    from backend.utils.encryption import is_encryption_enabled

    if not is_encryption_enabled():
        print("ERROR: Encryption is not enabled!")
        print("Please set GCP_KMS_KEY_NAME environment variable.")
        return False

    audio_dir = os.environ.get("AUDIO_STORAGE_PATH", "data/audio")
    if not os.path.exists(audio_dir):
        print(f"Audio directory not found: {audio_dir}")
        return True  # Not an error - just no files to encrypt

    print(f"\nEncrypting audio files in {audio_dir}...")
    print("This may take a while for large audio collections...")

    # Import KMS encryption
    from backend.utils.encryption import _get_kms_client, get_kms_key_name
    client = _get_kms_client()
    key_name = get_kms_key_name()

    encrypted_count = 0
    skipped_count = 0

    for root, dirs, files in os.walk(audio_dir):
        for filename in files:
            filepath = os.path.join(root, filename)

            # Skip already encrypted files (they have .enc extension)
            if filename.endswith('.enc'):
                skipped_count += 1
                continue

            # Skip metadata files
            if filename.endswith('.json'):
                continue

            try:
                # Read the audio file
                with open(filepath, 'rb') as f:
                    plaintext = f.read()

                # Encrypt using KMS
                response = client.encrypt(
                    request={
                        "name": key_name,
                        "plaintext": plaintext,
                    }
                )

                # Write encrypted file with .enc extension
                encrypted_filepath = filepath + '.enc'
                with open(encrypted_filepath, 'wb') as f:
                    f.write(response.ciphertext)

                # Remove original file
                os.remove(filepath)

                encrypted_count += 1
                if encrypted_count % 10 == 0:
                    print(f"  Encrypted {encrypted_count} audio files...")

            except Exception as e:
                print(f"  ERROR encrypting {filepath}: {e}")
                # Don't remove original if encryption failed

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

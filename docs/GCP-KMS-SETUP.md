# GCP Cloud KMS Setup Guide

This guide walks you through setting up Google Cloud KMS for encrypting content at rest in Write-or-Perish (Loore).

## Overview

GCP Cloud KMS provides application-level encryption for:
- Node content (user writings, transcriptions)
- NodeVersion content (version history)
- Draft content
- UserProfile content
- NodeTranscriptChunk text
- Audio files (voice recordings)

This is a defense-in-depth measure that adds encryption on top of database-level encryption.

## Prerequisites

- Google Cloud Platform account
- `gcloud` CLI installed and configured
- Access to create KMS keys and service accounts

## Step 1: Create a GCP Project (if needed)

```bash
# Create a new project (or use an existing one)
gcloud projects create loore-production --name="Loore Production"

# Set as current project
gcloud config set project loore-production
```

## Step 2: Enable the Cloud KMS API

```bash
gcloud services enable cloudkms.googleapis.com
```

## Step 3: Create a Key Ring

Key rings are regional containers for keys. Choose a region close to your servers.

```bash
# Create a key ring in us-central1 (adjust region as needed)
gcloud kms keyrings create loore-keyring \
    --location=us-central1
```

## Step 4: Create an Encryption Key

```bash
# Create a symmetric encryption key
gcloud kms keys create content-encryption-key \
    --location=us-central1 \
    --keyring=loore-keyring \
    --purpose=encryption
```

Note the full key name format:
```
projects/loore-production/locations/us-central1/keyRings/loore-keyring/cryptoKeys/content-encryption-key
```

## Step 5: Create a Service Account

```bash
# Create service account for the application
gcloud iam service-accounts create loore-kms-sa \
    --display-name="Loore KMS Service Account"
```

## Step 6: Grant Permissions

```bash
# Grant encrypt/decrypt permissions to the service account
gcloud kms keys add-iam-policy-binding content-encryption-key \
    --location=us-central1 \
    --keyring=loore-keyring \
    --member="serviceAccount:loore-kms-sa@loore-production.iam.gserviceaccount.com" \
    --role="roles/cloudkms.cryptoKeyEncrypterDecrypter"
```

## Step 7: Create and Download Service Account Key

```bash
# Create a JSON key file
gcloud iam service-accounts keys create ./kms-credentials.json \
    --iam-account=loore-kms-sa@loore-production.iam.gserviceaccount.com
```

**IMPORTANT**: Keep this file secure! Never commit it to git.

## Step 8: Configure the Application

Add these environment variables to your production server:

```bash
# The full path to the KMS key
export GCP_KMS_KEY_NAME="projects/loore-production/locations/us-central1/keyRings/loore-keyring/cryptoKeys/content-encryption-key"

# Path to the service account credentials
export GOOGLE_APPLICATION_CREDENTIALS="/path/to/kms-credentials.json"
```

For systemd services, add these to your service file:
```ini
[Service]
Environment="GCP_KMS_KEY_NAME=projects/loore-production/locations/us-central1/keyRings/loore-keyring/cryptoKeys/content-encryption-key"
Environment="GOOGLE_APPLICATION_CREDENTIALS=/path/to/kms-credentials.json"
```

## Step 9: Install the GCP Client Library

The application needs the `google-cloud-kms` library:

```bash
pip install google-cloud-kms
```

Add to `requirements.txt`:
```
google-cloud-kms>=2.0.0
```

---

# Migration Guide: Encrypting Existing Data

**CRITICAL**: Before running encryption, you MUST back up your data!

## Step 1: Back Up the Database

### Option A: Using the Migration Script

```bash
# Run the backup step only first
python scripts/encrypt_existing_content.py --backup-only --backup-dir /path/to/backups
```

This creates:
- `db_backup_YYYYMMDD_HHMMSS.sql` - PostgreSQL database dump
- `audio_backup_YYYYMMDD_HHMMSS.zip` - All audio files

### Option B: Manual Backup

```bash
# Create a directory for backups
mkdir -p /path/to/backups

# Backup the database
pg_dump -U your_user -h localhost writeorperish > /path/to/backups/db_backup_$(date +%Y%m%d_%H%M%S).sql

# Backup audio files
cd /path/to/data/audio
zip -r /path/to/backups/audio_backup_$(date +%Y%m%d_%H%M%S).zip .
```

### Verify Your Backups!

```bash
# Check database backup size
ls -lh /path/to/backups/db_backup_*.sql

# Check audio backup size
ls -lh /path/to/backups/audio_backup_*.zip

# Optionally, test restore to a temporary database
createdb writeorperish_test
psql -U your_user writeorperish_test < /path/to/backups/db_backup_XXXXXX.sql
# Then verify data, then drop the test database
dropdb writeorperish_test
```

## Step 2: Encrypt Database Content

Once backups are verified:

```bash
# Set required environment variables
export GCP_KMS_KEY_NAME="projects/loore-production/locations/us-central1/keyRings/loore-keyring/cryptoKeys/content-encryption-key"
export GOOGLE_APPLICATION_CREDENTIALS="/path/to/kms-credentials.json"
export DATABASE_URL="postgresql://user:password@localhost/writeorperish"

# Run database encryption
python scripts/encrypt_existing_content.py --encrypt-db
```

This encrypts:
- All Node content
- All NodeVersion content
- All Draft content
- All UserProfile content
- All NodeTranscriptChunk text

## Step 3: Encrypt Audio Files (Optional)

Audio file encryption is more storage-intensive (encrypted files are slightly larger) and adds latency when serving files. Consider whether this is needed for your security requirements.

```bash
# Run audio file encryption
python scripts/encrypt_existing_content.py --encrypt-audio
```

This:
- Encrypts each audio file and adds `.enc` extension
- Removes the original unencrypted files
- The media endpoint automatically decrypts when serving

## Step 4: Verify Encryption

After encryption, verify the application works:

1. Start the application
2. Load an existing node - content should display correctly
3. Create a new node - verify it saves and displays
4. If using audio encryption, play an audio file

---

# Disaster Recovery

## Restoring from Database Backup

```bash
# Stop the application
sudo systemctl stop loore-backend

# Restore the database
psql -U your_user -h localhost writeorperish < /path/to/backups/db_backup_XXXXXX.sql

# Restart the application
sudo systemctl start loore-backend
```

## Restoring Audio Files

```bash
# Stop the application
sudo systemctl stop loore-backend

# Restore audio files
cd /path/to/data/audio
rm -rf *  # Be careful!
unzip /path/to/backups/audio_backup_XXXXXX.zip

# Restart the application
sudo systemctl start loore-backend
```

---

# Local Development

For local development without GCP, disable encryption:

```bash
export ENCRYPTION_DISABLED=true
```

Or use a separate development KMS key:

```bash
export GCP_KMS_KEY_NAME="projects/loore-development/locations/us-central1/keyRings/dev-keyring/cryptoKeys/dev-key"
export GOOGLE_APPLICATION_CREDENTIALS="/path/to/dev-credentials.json"
```

---

# Key Rotation

GCP KMS supports automatic key rotation. When a key is rotated:
- New data is encrypted with the new primary key version
- Old data can still be decrypted (KMS handles version lookup automatically)

To enable automatic rotation:

```bash
gcloud kms keys update content-encryption-key \
    --location=us-central1 \
    --keyring=loore-keyring \
    --rotation-period=90d \
    --next-rotation-time=$(date -u -d "+1 day" +%Y-%m-%dT%H:%M:%SZ)
```

---

# Troubleshooting

## "google.auth.exceptions.DefaultCredentialsError"
- Ensure `GOOGLE_APPLICATION_CREDENTIALS` is set and points to a valid JSON file
- Check that the service account has the required permissions

## "Permission denied" when encrypting/decrypting
- Verify the service account has `cloudkms.cryptoKeyEncrypterDecrypter` role
- Check that `GCP_KMS_KEY_NAME` is correct

## "Key not found"
- Verify the full key path is correct
- Check that the key hasn't been destroyed

## Application starts but content shows "ENC:v1:..."
- Encryption is working but decryption isn't
- Check that the KMS credentials are available to the application
- Verify the key hasn't been disabled

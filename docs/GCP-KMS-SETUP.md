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

**Authentication**: This guide uses Workload Identity (the VM's attached service account) instead of a downloaded JSON key file. This means there are no credentials stored on disk — the VM authenticates to GCP automatically.

## Prerequisites

- Google Cloud Platform account
- `gcloud` CLI installed and configured
- A GCE VM (your production server) in project `dauntless-arc-365912`

## Step 1: Set Your GCP Project

```bash
# Set as current project (project ID: dauntless-arc-365912, display name: "Loore Production")
gcloud config set project dauntless-arc-365912
```

## Step 2: Enable the Cloud KMS API

```bash
gcloud services enable cloudkms.googleapis.com
```

## Step 3: Create a Key Ring

Key rings are regional containers for keys. Choose a region close to your servers.

```bash
# Create a key ring (adjust region as needed)
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
projects/dauntless-arc-365912/locations/us-central1/keyRings/loore-keyring/cryptoKeys/content-encryption-key
```

## Step 5: Create a Service Account for the VM

If your VM doesn't already have a dedicated service account, create one:

```bash
# Create service account
gcloud iam service-accounts create loore-vm-sa \
    --display-name="Loore VM Service Account"
```

## Step 6: Grant KMS Permissions to the Service Account

```bash
# Grant encrypt/decrypt permissions
gcloud kms keys add-iam-policy-binding content-encryption-key \
    --location=us-central1 \
    --keyring=loore-keyring \
    --member="serviceAccount:loore-vm-sa@dauntless-arc-365912.iam.gserviceaccount.com" \
    --role="roles/cloudkms.cryptoKeyEncrypterDecrypter"
```

## Step 7: Attach the Service Account to Your VM

This is the key step — instead of downloading a JSON key file, you attach the service account directly to the VM. The VM then authenticates automatically.

```bash
# Stop the VM first (required to change service account)
gcloud compute instances stop instance-20250317-165954 --zone=us-central1-c

# Set the service account on the VM
gcloud compute instances set-service-account instance-20250317-165954 \
    --zone=us-central1-c \
    --service-account=loore-vm-sa@dauntless-arc-365912.iam.gserviceaccount.com \
    --scopes=https://www.googleapis.com/auth/cloudkms

# Start the VM
gcloud compute instances start instance-20250317-165954 --zone=us-central1-c
```

Replace `YOUR_VM_NAME` and `YOUR_ZONE` with your actual VM name and zone (e.g., `us-central1-a`). You can find these with:

```bash
gcloud compute instances list
```

> **Note**: If the VM already has a service account with other scopes, you can combine them:
> `--scopes=https://www.googleapis.com/auth/cloudkms,https://www.googleapis.com/auth/cloud-platform`

## Step 8: Configure the Application

The only environment variable needed on the production server is the key name. **No credentials file is needed** — the VM authenticates automatically via its attached service account.

```bash
export GCP_KMS_KEY_NAME="projects/dauntless-arc-365912/locations/us-central1/keyRings/loore-keyring/cryptoKeys/content-encryption-key"
```

For systemd services, add to your service file:
```ini
[Service]
Environment="GCP_KMS_KEY_NAME=projects/dauntless-arc-365912/locations/us-central1/keyRings/loore-keyring/cryptoKeys/content-encryption-key"
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

## Step 10: Verify Authentication

SSH into the VM and verify the service account is attached:

```bash
# Check which service account the VM is using
curl -H "Metadata-Flavor: Google" \
    http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email

# Should print: loore-vm-sa@dauntless-arc-365912.iam.gserviceaccount.com
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
export GCP_KMS_KEY_NAME="projects/dauntless-arc-365912/locations/us-central1/keyRings/loore-keyring/cryptoKeys/content-encryption-key"
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

For local development **with** encryption (e.g., testing), you can use a JSON key file since Workload Identity is only available on GCE VMs:

```bash
# Create a separate dev service account and key
gcloud iam service-accounts create loore-kms-dev \
    --display-name="Loore KMS Dev"

gcloud kms keys add-iam-policy-binding content-encryption-key \
    --location=us-central1 \
    --keyring=loore-keyring \
    --member="serviceAccount:loore-kms-dev@dauntless-arc-365912.iam.gserviceaccount.com" \
    --role="roles/cloudkms.cryptoKeyEncrypterDecrypter"

gcloud iam service-accounts keys create ./kms-dev-credentials.json \
    --iam-account=loore-kms-dev@dauntless-arc-365912.iam.gserviceaccount.com

export GCP_KMS_KEY_NAME="projects/dauntless-arc-365912/locations/us-central1/keyRings/loore-keyring/cryptoKeys/content-encryption-key"
export GOOGLE_APPLICATION_CREDENTIALS="./kms-dev-credentials.json"
```

> **Note**: `kms-dev-credentials.json` is in `.gitignore`. Never commit credential files.

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

# Security Model

## What KMS + Workload Identity protects against

| Threat | Protected? | Notes |
|--------|-----------|-------|
| Database dump leaked (SQL injection, stolen backup) | Yes | Content is ciphertext without KMS access |
| Disk/storage snapshot accessed separately | Yes | Encrypted at application level |
| VM fully compromised (root shell) | Partially | Attacker can call KMS API while they have access, but can't extract a key file to use later offline |
| Insider with GCP console access | Audit trail | All KMS operations are logged in Cloud Audit Logs |
| Key file stolen and used offline | N/A | No key file exists — Workload Identity only |

## Comparison with JSON key file approach

With a JSON key file on disk, an attacker who compromises the VM can copy the key file and decrypt data offline at their leisure, even after you revoke access. With Workload Identity, credentials are short-lived tokens rotated automatically — revoking the service account immediately cuts off access.

---

# Troubleshooting

## "google.auth.exceptions.DefaultCredentialsError"
- Verify the VM has a service account attached: `curl -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email`
- For local dev, ensure `GOOGLE_APPLICATION_CREDENTIALS` is set and points to a valid JSON file

## "Permission denied" when encrypting/decrypting
- Verify the service account has `cloudkms.cryptoKeyEncrypterDecrypter` role
- Check that `GCP_KMS_KEY_NAME` is correct
- Ensure the VM was started with the `cloudkms` scope

## "Key not found"
- Verify the full key path is correct
- Check that the key hasn't been destroyed

## Application starts but content shows "ENC:v1:..."
- Encryption is working but decryption isn't
- Check that the KMS credentials are available to the application
- Verify the key hasn't been disabled

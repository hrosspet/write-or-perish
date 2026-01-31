"""
GCP Cloud KMS encryption utilities for content encryption at rest.

This module provides encrypt/decrypt functions using Google Cloud KMS.
For local development without GCP, set ENCRYPTION_DISABLED=true to bypass encryption.

The encryption is transparent to the application - content is encrypted before
being stored in the database and decrypted when read.
"""
import base64
import os
from functools import lru_cache

# Encryption marker prefix to identify encrypted content
ENCRYPTED_PREFIX = "ENC:v1:"


def is_encryption_enabled() -> bool:
    """Check if encryption is enabled (GCP KMS is configured)."""
    if os.environ.get("ENCRYPTION_DISABLED", "").lower() in ("true", "1", "yes"):
        return False
    return bool(os.environ.get("GCP_KMS_KEY_NAME"))


def get_kms_key_name() -> str:
    """
    Get the full KMS key resource name.

    Format: projects/{project}/locations/{location}/keyRings/{keyring}/cryptoKeys/{key}
    """
    return os.environ.get("GCP_KMS_KEY_NAME", "")


@lru_cache(maxsize=1)
def _get_kms_client():
    """Get a cached KMS client instance."""
    from google.cloud import kms
    return kms.KeyManagementServiceClient()


def encrypt_content(plaintext: str) -> str:
    """
    Encrypt content using GCP KMS.

    Args:
        plaintext: The content to encrypt

    Returns:
        Encrypted content with prefix marker, or original if encryption disabled
    """
    if not plaintext:
        return plaintext

    if not is_encryption_enabled():
        return plaintext

    # Don't double-encrypt
    if plaintext.startswith(ENCRYPTED_PREFIX):
        return plaintext

    try:
        client = _get_kms_client()
        key_name = get_kms_key_name()

        # Encode plaintext to bytes
        plaintext_bytes = plaintext.encode("utf-8")

        # Encrypt
        response = client.encrypt(
            request={
                "name": key_name,
                "plaintext": plaintext_bytes,
            }
        )

        # Encode ciphertext to base64 for safe storage in text field
        ciphertext_b64 = base64.b64encode(response.ciphertext).decode("ascii")

        return f"{ENCRYPTED_PREFIX}{ciphertext_b64}"

    except Exception as e:
        # Log error but don't fail - store unencrypted as fallback
        # In production, you might want to fail instead
        import logging
        logging.getLogger(__name__).error(f"Encryption failed: {e}")
        raise


def decrypt_content(ciphertext: str) -> str:
    """
    Decrypt content using GCP KMS.

    Args:
        ciphertext: The encrypted content (with ENC:v1: prefix)

    Returns:
        Decrypted plaintext, or original if not encrypted or encryption disabled
    """
    if not ciphertext:
        return ciphertext

    # Check if content is encrypted
    if not ciphertext.startswith(ENCRYPTED_PREFIX):
        return ciphertext  # Not encrypted, return as-is

    if not is_encryption_enabled():
        # Content is encrypted but encryption is disabled
        # This shouldn't happen in production
        import logging
        logging.getLogger(__name__).warning(
            "Found encrypted content but encryption is disabled. "
            "Cannot decrypt - returning as-is."
        )
        return ciphertext

    try:
        client = _get_kms_client()
        key_name = get_kms_key_name()

        # Remove prefix and decode base64
        ciphertext_b64 = ciphertext[len(ENCRYPTED_PREFIX):]
        ciphertext_bytes = base64.b64decode(ciphertext_b64)

        # Decrypt
        response = client.decrypt(
            request={
                "name": key_name,
                "ciphertext": ciphertext_bytes,
            }
        )

        return response.plaintext.decode("utf-8")

    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Decryption failed: {e}")
        raise


def is_content_encrypted(content: str) -> bool:
    """Check if content is encrypted (has the encryption prefix)."""
    if not content:
        return False
    return content.startswith(ENCRYPTED_PREFIX)


def encrypt_file(filepath: str) -> str:
    """
    Encrypt a file in place using GCP KMS.

    The file is replaced with its encrypted version and given a .enc extension.

    Args:
        filepath: Path to the file to encrypt

    Returns:
        Path to the encrypted file (with .enc extension)
    """
    if not is_encryption_enabled():
        return filepath

    # Skip if already encrypted
    if filepath.endswith('.enc'):
        return filepath

    import os

    try:
        client = _get_kms_client()
        key_name = get_kms_key_name()

        # Read the file
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

        return encrypted_filepath

    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"File encryption failed: {e}")
        raise


def decrypt_file(filepath: str) -> bytes:
    """
    Decrypt a file from disk using GCP KMS.

    Args:
        filepath: Path to the encrypted file (with .enc extension)

    Returns:
        Decrypted file contents as bytes
    """
    if not is_encryption_enabled():
        # If encryption is disabled, just read the file
        with open(filepath, 'rb') as f:
            return f.read()

    # If file doesn't have .enc extension, it's not encrypted
    if not filepath.endswith('.enc'):
        with open(filepath, 'rb') as f:
            return f.read()

    try:
        client = _get_kms_client()
        key_name = get_kms_key_name()

        # Read the encrypted file
        with open(filepath, 'rb') as f:
            ciphertext = f.read()

        # Decrypt using KMS
        response = client.decrypt(
            request={
                "name": key_name,
                "ciphertext": ciphertext,
            }
        )

        return response.plaintext

    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"File decryption failed: {e}")
        raise


def is_file_encrypted(filepath: str) -> bool:
    """Check if a file is encrypted (has .enc extension)."""
    return filepath.endswith('.enc')

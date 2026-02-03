"""
GCP Cloud KMS envelope encryption utilities for content encryption at rest.

Uses envelope encryption to avoid the 64KB KMS plaintext limit:
- A random AES-256 data encryption key (DEK) is generated per encrypt call
- The DEK encrypts the content locally (AES-GCM)
- KMS encrypts only the DEK (small, well under 64KB limit)
- Both the wrapped DEK and ciphertext are stored together

Format: ENC:v2:<base64-wrapped-dek>:<base64(nonce + ciphertext + tag)>

Legacy format (v1) is still supported for decryption only.
"""
import base64
import os
from functools import lru_cache

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# Encryption marker prefixes
ENCRYPTED_PREFIX_V1 = "ENC:v1:"
ENCRYPTED_PREFIX_V2 = "ENC:v2:"

# AES-256 key size in bytes
DEK_SIZE = 32
# AES-GCM nonce size in bytes
NONCE_SIZE = 12


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


def _wrap_dek(dek: bytes) -> bytes:
    """Encrypt a DEK using KMS."""
    client = _get_kms_client()
    response = client.encrypt(
        request={"name": get_kms_key_name(), "plaintext": dek}
    )
    return response.ciphertext


def _unwrap_dek(wrapped_dek: bytes) -> bytes:
    """Decrypt a DEK using KMS."""
    client = _get_kms_client()
    response = client.decrypt(
        request={"name": get_kms_key_name(), "ciphertext": wrapped_dek}
    )
    return response.plaintext


def encrypt_content(plaintext: str) -> str:
    """
    Encrypt content using envelope encryption (AES-GCM + KMS-wrapped DEK).

    Args:
        plaintext: The content to encrypt

    Returns:
        Encrypted content with v2 prefix, or original if encryption disabled
    """
    if not plaintext:
        return plaintext

    if not is_encryption_enabled():
        return plaintext

    # Don't double-encrypt
    if plaintext.startswith((ENCRYPTED_PREFIX_V1, ENCRYPTED_PREFIX_V2)):
        return plaintext

    try:
        # Generate a random DEK
        dek = os.urandom(DEK_SIZE)

        # Wrap the DEK with KMS
        wrapped_dek = _wrap_dek(dek)

        # Encrypt content locally with AES-GCM
        nonce = os.urandom(NONCE_SIZE)
        aesgcm = AESGCM(dek)
        ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)

        # Encode both parts as base64
        wrapped_dek_b64 = base64.b64encode(wrapped_dek).decode("ascii")
        payload_b64 = base64.b64encode(nonce + ciphertext).decode("ascii")

        return f"{ENCRYPTED_PREFIX_V2}{wrapped_dek_b64}:{payload_b64}"

    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Encryption failed: {e}")
        raise


def decrypt_content(ciphertext: str) -> str:
    """
    Decrypt content. Supports both v1 (direct KMS) and v2 (envelope) formats.

    Args:
        ciphertext: The encrypted content (with ENC:v1: or ENC:v2: prefix)

    Returns:
        Decrypted plaintext, or original if not encrypted
    """
    if not ciphertext:
        return ciphertext

    # Not encrypted
    if not ciphertext.startswith((ENCRYPTED_PREFIX_V1, ENCRYPTED_PREFIX_V2)):
        return ciphertext

    if not is_encryption_enabled():
        import logging
        logging.getLogger(__name__).warning(
            "Found encrypted content but encryption is disabled. "
            "Cannot decrypt - returning as-is."
        )
        return ciphertext

    try:
        if ciphertext.startswith(ENCRYPTED_PREFIX_V2):
            return _decrypt_v2(ciphertext)
        else:
            return _decrypt_v1(ciphertext)
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Decryption failed: {e}")
        raise


def _decrypt_v1(ciphertext: str) -> str:
    """Decrypt legacy v1 format (direct KMS encryption)."""
    client = _get_kms_client()
    ciphertext_b64 = ciphertext[len(ENCRYPTED_PREFIX_V1):]
    ciphertext_bytes = base64.b64decode(ciphertext_b64)
    response = client.decrypt(
        request={"name": get_kms_key_name(), "ciphertext": ciphertext_bytes}
    )
    return response.plaintext.decode("utf-8")


def _decrypt_v2(ciphertext: str) -> str:
    """Decrypt v2 envelope encryption format."""
    # Parse: ENC:v2:<wrapped_dek_b64>:<payload_b64>
    remainder = ciphertext[len(ENCRYPTED_PREFIX_V2):]
    wrapped_dek_b64, payload_b64 = remainder.split(":", 1)

    # Unwrap DEK via KMS
    wrapped_dek = base64.b64decode(wrapped_dek_b64)
    dek = _unwrap_dek(wrapped_dek)

    # Decrypt content locally
    payload = base64.b64decode(payload_b64)
    nonce = payload[:NONCE_SIZE]
    ct = payload[NONCE_SIZE:]
    aesgcm = AESGCM(dek)
    plaintext_bytes = aesgcm.decrypt(nonce, ct, None)

    return plaintext_bytes.decode("utf-8")


def is_content_encrypted(content: str) -> bool:
    """Check if content is encrypted (has an encryption prefix)."""
    if not content:
        return False
    return content.startswith((ENCRYPTED_PREFIX_V1, ENCRYPTED_PREFIX_V2))


def encrypt_file(filepath: str) -> str:
    """
    Encrypt a file in place using envelope encryption.

    The file is replaced with its encrypted version and given a .enc extension.
    File format: 4-byte wrapped DEK length + wrapped DEK + nonce + ciphertext

    Args:
        filepath: Path to the file to encrypt

    Returns:
        Path to the encrypted file (with .enc extension)
    """
    if not is_encryption_enabled():
        return filepath

    if filepath.endswith('.enc'):
        return filepath

    try:
        with open(filepath, 'rb') as f:
            plaintext = f.read()

        # Generate and wrap DEK
        dek = os.urandom(DEK_SIZE)
        wrapped_dek = _wrap_dek(dek)

        # Encrypt content locally
        nonce = os.urandom(NONCE_SIZE)
        aesgcm = AESGCM(dek)
        ciphertext = aesgcm.encrypt(nonce, plaintext, None)

        # Write: [4-byte wrapped DEK length][wrapped DEK][nonce][ciphertext]
        encrypted_filepath = filepath + '.enc'
        with open(encrypted_filepath, 'wb') as f:
            f.write(len(wrapped_dek).to_bytes(4, 'big'))
            f.write(wrapped_dek)
            f.write(nonce)
            f.write(ciphertext)

        os.remove(filepath)
        return encrypted_filepath

    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"File encryption failed: {e}")
        raise


def decrypt_file(filepath: str) -> bytes:
    """
    Decrypt a file from disk using envelope encryption.

    Args:
        filepath: Path to the encrypted file (with .enc extension)

    Returns:
        Decrypted file contents as bytes
    """
    if not is_encryption_enabled():
        with open(filepath, 'rb') as f:
            return f.read()

    if not filepath.endswith('.enc'):
        with open(filepath, 'rb') as f:
            return f.read()

    try:
        with open(filepath, 'rb') as f:
            # Read wrapped DEK length and wrapped DEK
            dek_len = int.from_bytes(f.read(4), 'big')
            wrapped_dek = f.read(dek_len)
            # Read nonce and ciphertext
            nonce = f.read(NONCE_SIZE)
            ciphertext = f.read()

        # Unwrap DEK via KMS
        dek = _unwrap_dek(wrapped_dek)

        # Decrypt locally
        aesgcm = AESGCM(dek)
        return aesgcm.decrypt(nonce, ciphertext, None)

    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"File decryption failed: {e}")
        raise


def is_file_encrypted(filepath: str) -> bool:
    """Check if a file is encrypted (has .enc extension)."""
    return filepath.endswith('.enc')

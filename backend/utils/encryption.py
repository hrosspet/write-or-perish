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
import binascii
import logging
import os
import re
import threading
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)

# Encryption marker prefixes
ENCRYPTED_PREFIX_V1 = "ENC:v1:"
ENCRYPTED_PREFIX_V2 = "ENC:v2:"

# AES-256 key size in bytes
DEK_SIZE = 32
# AES-GCM nonce size in bytes
NONCE_SIZE = 12

# In-memory LRU cache for unwrapped DEKs: wrapped_dek_b64 -> dek_bytes
# Avoids repeated KMS calls for the same wrapped DEK.
_DEK_CACHE_MAX = 4096
_dek_cache = {}
_dek_cache_order = []
_dek_cache_lock = threading.Lock()

# KMS client singleton with reconnection support
_kms_client = None
_kms_client_lock = threading.Lock()


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


def _get_kms_client():
    """Get the KMS client, creating one if needed."""
    global _kms_client
    if _kms_client is None:
        with _kms_client_lock:
            if _kms_client is None:
                from google.cloud import kms
                _kms_client = kms.KeyManagementServiceClient(
                    transport="rest",
                )
    return _kms_client


def _reset_kms_client():
    """Force recreation of the KMS client (e.g. after a stale connection)."""
    global _kms_client
    with _kms_client_lock:
        _kms_client = None


def _cache_get(wrapped_dek_b64: str):
    """Look up a DEK in the in-memory cache."""
    with _dek_cache_lock:
        return _dek_cache.get(wrapped_dek_b64)


def _cache_put(wrapped_dek_b64: str, dek: bytes):
    """Store a DEK in the in-memory cache with LRU eviction."""
    with _dek_cache_lock:
        if wrapped_dek_b64 in _dek_cache:
            _dek_cache_order.remove(wrapped_dek_b64)
        elif len(_dek_cache) >= _DEK_CACHE_MAX:
            oldest = _dek_cache_order.pop(0)
            del _dek_cache[oldest]
        _dek_cache[wrapped_dek_b64] = dek
        _dek_cache_order.append(wrapped_dek_b64)


# Transient KMS failures — Google-side blips that the API itself says to
# retry ("try again in 30s"). The gRPC/REST client's default retry covers
# 503/DEADLINE_EXCEEDED but NOT 502 BadGateway / 500 Internal, so a single
# 502 crashed a 61k-node import mid-run (2026-08-27). We wrap every KMS
# call in a bounded exponential backoff over the full transient set. The
# stale-connection reconnect (DEADLINE_EXCEEDED) is folded in.
_KMS_MAX_RETRIES = 6
_KMS_BACKOFF_BASE = 0.5   # 0.5, 1, 2, 4, 8, 16s — ~30s total before giving up
_TRANSIENT_MARKERS = (
    "Deadline Exceeded", "DEADLINE_EXCEEDED", "ServiceUnavailable",
    "Service Unavailable", "Bad Gateway", "BadGateway", "Gateway Timeout",
    "GatewayTimeout", "Internal Server Error", "InternalServerError",
)
# google.api_core formats GoogleAPICallError as "<code> <message>", so a
# leading status code is a reliable signal. A bare "502"/"500" substring is
# NOT: resource names carry numeric project ids ("projects/123502/...") and
# would turn a hard PermissionDenied into a 30s retry loop.
_TRANSIENT_STATUS_RE = re.compile(r"^\s*50[0234]\b")


def _is_transient_kms_error(e):
    try:
        from google.api_core import exceptions as gexc
        if isinstance(e, (gexc.ServiceUnavailable, gexc.BadGateway,
                          gexc.GatewayTimeout, gexc.InternalServerError,
                          gexc.DeadlineExceeded, gexc.RetryError)):
            return True
        if isinstance(e, gexc.GoogleAPICallError):
            return False  # a known, non-transient API error (403/404/400...)
    except ImportError:
        pass
    msg = str(e)
    return bool(_TRANSIENT_STATUS_RE.match(msg)) or any(m in msg for m in _TRANSIENT_MARKERS)


def _kms_call(op, request):
    """Run a KMS ``encrypt``/``decrypt`` with bounded retry on transient
    (Google-side) errors and a reconnect on deadline/stale-connection."""
    last = None
    for attempt in range(_KMS_MAX_RETRIES + 1):
        try:
            return getattr(_get_kms_client(), op)(request=request)
        except Exception as e:  # noqa: BLE001 — re-raised below if not transient
            last = e
            if not _is_transient_kms_error(e):
                raise
            if "Deadline Exceeded" in str(e) or "DEADLINE_EXCEEDED" in str(e):
                _reset_kms_client()
            if attempt == _KMS_MAX_RETRIES:
                break
            sleep_s = _KMS_BACKOFF_BASE * (2 ** attempt)
            logger.warning(
                "Transient KMS error on %s (attempt %d/%d), retrying in %.1fs: %s",
                op, attempt + 1, _KMS_MAX_RETRIES, sleep_s, str(e)[:120])
            time.sleep(sleep_s)
    raise last


def _kms_encrypt(request):
    return _kms_call("encrypt", request)


def _kms_decrypt(request):
    return _kms_call("decrypt", request)


def _wrap_dek(dek: bytes) -> bytes:
    """Encrypt a DEK using KMS."""
    response = _kms_encrypt({"name": get_kms_key_name(), "plaintext": dek})
    return response.ciphertext


def _unwrap_dek(wrapped_dek: bytes) -> bytes:
    """Decrypt a DEK using KMS, with in-memory caching."""
    # Check cache first
    cache_key = base64.b64encode(wrapped_dek).decode("ascii")
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    # Cache miss — call KMS
    response = _kms_decrypt({"name": get_kms_key_name(), "ciphertext": wrapped_dek})
    dek = response.plaintext
    _cache_put(cache_key, dek)
    return dek


# ---------------------------------------------------------------------------
# Batched DEK unwrapping
#
# One KMS decrypt is ~80 ms from the prod VM whatever the transport (measured
# 2026-09-02: REST and gRPC both 60–120 ms, while a raw HTTPS request to the
# same endpoint is 22 ms — the latency is inside KMS). A cold thread open
# paid that per node, in sequence. The calls are pure waiting, so N of them
# in flight cost about one: 8 unwraps went 672 ms → 115 ms on the 2-vCPU VM.
#
# The executor is created lazily, after gunicorn has forked. Under the gevent
# worker its "threads" are greenlets on the request loop (the REST client
# goes through `requests`, which gevent makes cooperative); in Celery they
# are real threads parked on sockets. Either way no CPU is involved.
#
# INTRODUCED CONSTANT: 8 in flight per process. Not tuned beyond the
# measurement above; KMS quota is 60k ops/min, so there is headroom to
# raise it if deep subtrees still feel slow.
# ---------------------------------------------------------------------------
DEK_PREFETCH_CONCURRENCY = 8
_unwrap_pool = None
_unwrap_pool_lock = threading.Lock()


def _get_unwrap_pool():
    global _unwrap_pool
    if _unwrap_pool is None:
        with _unwrap_pool_lock:
            if _unwrap_pool is None:
                from concurrent.futures import ThreadPoolExecutor
                _unwrap_pool = ThreadPoolExecutor(
                    max_workers=DEK_PREFETCH_CONCURRENCY,
                    thread_name_prefix="dek-unwrap",
                )
    return _unwrap_pool


def wrapped_dek_of(ciphertext):
    """The KMS-wrapped DEK inside a v2 envelope, or None for anything
    else (plaintext, v1 direct-KMS blobs, None, malformed)."""
    if not isinstance(ciphertext, str) or not ciphertext.startswith(ENCRYPTED_PREFIX_V2):
        return None
    wrapped_b64 = ciphertext[len(ENCRYPTED_PREFIX_V2):].split(":", 1)[0]
    try:
        return base64.b64decode(wrapped_b64, validate=True) or None
    except (ValueError, binascii.Error):
        return None


def _unwrap_quietly(wrapped_dek):
    """Best-effort unwrap for the prefetch pool: a failure here is
    swallowed so the serial decrypt that follows raises (and logs) the
    way it always has, instead of the prefetch changing error behaviour."""
    try:
        _unwrap_dek(wrapped_dek)
    except Exception as e:  # noqa: BLE001 — deliberately best-effort
        logger.debug("DEK prefetch failed (serial path will retry): %s", str(e)[:120])


def prefetch_deks(ciphertexts):
    """Unwrap, concurrently, every DEK among *ciphertexts* that is not in
    the cache yet, so the decrypt_content() calls that follow are cache
    hits. Duplicates collapse (many rows share a DEK only if they were
    encrypted together, but the check is free). Returns the number of KMS
    calls issued."""
    if not is_encryption_enabled():
        return 0
    missing = {}
    for ciphertext in ciphertexts:
        wrapped = wrapped_dek_of(ciphertext)
        if wrapped is None:
            continue
        key = base64.b64encode(wrapped).decode("ascii")
        if key not in missing and _cache_get(key) is None:
            missing[key] = wrapped
    if not missing:
        return 0
    # list() waits for every unwrap; failures were swallowed per item.
    list(_get_unwrap_pool().map(_unwrap_quietly, missing.values()))
    return len(missing)


def is_encrypted(value) -> bool:
    """True when *value* carries a v1/v2 encryption envelope."""
    return bool(value) and isinstance(value, str) and value.startswith(
        (ENCRYPTED_PREFIX_V1, ENCRYPTED_PREFIX_V2))


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
    ciphertext_b64 = ciphertext[len(ENCRYPTED_PREFIX_V1):]
    ciphertext_bytes = base64.b64decode(ciphertext_b64)
    response = _kms_decrypt({"name": get_kms_key_name(), "ciphertext": ciphertext_bytes})
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


def decrypt_file_to_temp(filepath: str) -> str:
    """
    Decrypt a .enc file to a temporary file and return the temp path.

    Useful for passing decrypted audio to external APIs (e.g. OpenAI)
    that need to read from a file path.

    Args:
        filepath: Path to the encrypted file (with .enc extension)

    Returns:
        Path to a temporary file containing the decrypted content.
        Caller is responsible for deleting the temp file.
    """
    import tempfile

    # Determine original extension (strip .enc)
    original_path = filepath
    if filepath.endswith('.enc'):
        original_path = filepath[:-4]

    ext = os.path.splitext(original_path)[1] or '.bin'

    decrypted_bytes = decrypt_file(filepath)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
    try:
        tmp.write(decrypted_bytes)
        tmp.close()
        return tmp.name
    except Exception:
        tmp.close()
        os.unlink(tmp.name)
        raise


def is_file_encrypted(filepath: str) -> bool:
    """Check if a file is encrypted (has .enc extension)."""
    return filepath.endswith('.enc')

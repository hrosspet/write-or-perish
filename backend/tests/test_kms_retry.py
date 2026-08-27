"""KMS calls retry transient Google-side errors (502/503/500/504/deadline)
with bounded backoff, instead of crashing a large import on one blip
(2026-08-27: a single 502 killed a 61k-node load mid-run).
"""
import os
import sys
from unittest.mock import MagicMock

os.environ.setdefault("SECRET_KEY", "test-secret")
sys.modules.setdefault("celery", MagicMock())

import pytest  # noqa: E402
from backend.utils import encryption as enc  # noqa: E402


class _Boom(Exception):
    pass


@pytest.fixture(autouse=True)
def _fast_and_enabled(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_DISABLED", "false")
    monkeypatch.setenv("GCP_KMS_KEY_NAME",
                       "projects/t/locations/l/keyRings/r/cryptoKeys/k")
    monkeypatch.setattr(enc.time, "sleep", lambda *_: None)


def _client_failing(n_times, exc):
    """A fake KMS client whose encrypt fails n_times then succeeds."""
    calls = {"n": 0}

    class _Resp:
        ciphertext = b"wrapped"
        plaintext = b"dek"

    def encrypt(request=None):
        calls["n"] += 1
        if calls["n"] <= n_times:
            raise exc
        return _Resp()
    client = MagicMock()
    client.encrypt.side_effect = encrypt
    return client, calls


def test_retries_transient_then_succeeds(monkeypatch):
    client, calls = _client_failing(3, _Boom("502 Bad Gateway"))
    monkeypatch.setattr(enc, "_get_kms_client", lambda: client)
    monkeypatch.setattr(enc, "_reset_kms_client", lambda: None)
    resp = enc._kms_encrypt({"name": "k", "plaintext": b"x"})
    assert resp.ciphertext == b"wrapped"
    assert calls["n"] == 4  # 3 failures + 1 success


def test_gives_up_after_max_retries(monkeypatch):
    client, calls = _client_failing(999, _Boom("503 ServiceUnavailable"))
    monkeypatch.setattr(enc, "_get_kms_client", lambda: client)
    monkeypatch.setattr(enc, "_reset_kms_client", lambda: None)
    with pytest.raises(_Boom):
        enc._kms_encrypt({"name": "k", "plaintext": b"x"})
    assert calls["n"] == enc._KMS_MAX_RETRIES + 1


def test_non_transient_error_is_not_retried(monkeypatch):
    client, calls = _client_failing(999, _Boom("PermissionDenied: no access"))
    monkeypatch.setattr(enc, "_get_kms_client", lambda: client)
    monkeypatch.setattr(enc, "_reset_kms_client", lambda: None)
    with pytest.raises(_Boom):
        enc._kms_encrypt({"name": "k", "plaintext": b"x"})
    assert calls["n"] == 1  # raised immediately, no retry


def test_status_code_only_counts_at_message_start():
    assert enc._is_transient_kms_error(_Boom("502 Bad Gateway"))
    assert enc._is_transient_kms_error(_Boom("503 try again in 30s"))
    # Digits inside resource names must not look like a status code.
    assert not enc._is_transient_kms_error(
        _Boom("PermissionDenied on projects/123502/locations/l/keyRings/500"))
    assert not enc._is_transient_kms_error(_Boom("404 key not found"))


def test_known_google_error_classes():
    gexc = pytest.importorskip("google.api_core.exceptions")
    assert enc._is_transient_kms_error(gexc.BadGateway("x"))
    assert enc._is_transient_kms_error(gexc.InternalServerError("x"))
    assert enc._is_transient_kms_error(gexc.DeadlineExceeded("x"))
    assert enc._is_transient_kms_error(gexc.RetryError("Deadline of 60.0s exceeded", None))
    # A typed non-transient error is never retried, whatever its message says.
    assert not enc._is_transient_kms_error(
        gexc.PermissionDenied("502 in the message but really a 403"))


def test_encrypt_content_round_trips_through_retry(monkeypatch):
    """A transient failure on the first wrap still yields a valid
    envelope that decrypt_content round-trips."""
    fail = {"n": 0}

    def encrypt(request=None):
        fail["n"] += 1
        if fail["n"] <= 2:
            raise _Boom("502 Bad Gateway")
        r = MagicMock()
        r.ciphertext = b"w:" + request["plaintext"]
        return r

    def decrypt(request=None):
        r = MagicMock()
        r.plaintext = request["ciphertext"][len(b"w:"):]
        return r

    client = MagicMock()
    client.encrypt.side_effect = encrypt
    client.decrypt.side_effect = decrypt
    monkeypatch.setattr(enc, "_get_kms_client", lambda: client)
    monkeypatch.setattr(enc, "_reset_kms_client", lambda: None)
    ct = enc.encrypt_content("hello secret")
    assert ct.startswith(enc.ENCRYPTED_PREFIX_V2)
    assert enc.decrypt_content(ct) == "hello secret"

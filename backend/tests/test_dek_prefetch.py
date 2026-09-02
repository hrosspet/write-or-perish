"""Batched DEK unwrapping (backend.utils.encryption.prefetch_deks).

A cold thread open used to unwrap one DEK per rendered node in sequence
(~80 ms each on prod). prefetch_deks collects the wrapped DEKs of a
render set, skips cached ones, and unwraps the rest concurrently.
"""
import base64

import pytest

from backend.utils import encryption


def _v2(wrapped: bytes, payload: bytes = b"payload") -> str:
    return (encryption.ENCRYPTED_PREFIX_V2
            + base64.b64encode(wrapped).decode() + ":"
            + base64.b64encode(payload).decode())


@pytest.fixture
def kms(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_DISABLED", "false")
    monkeypatch.setenv("GCP_KMS_KEY_NAME", "projects/t/locations/l/keyRings/r/cryptoKeys/k")
    encryption._dek_cache.clear()
    encryption._dek_cache_order.clear()
    calls = []

    def fake_unwrap(wrapped):
        calls.append(wrapped)
        if wrapped == b"boom":
            raise RuntimeError("kms down")
        dek = b"dek:" + wrapped
        encryption._cache_put(base64.b64encode(wrapped).decode("ascii"), dek)
        return dek

    monkeypatch.setattr(encryption, "_unwrap_dek", fake_unwrap)
    assert encryption.is_encryption_enabled()
    return calls


def test_wrapped_dek_of_parses_only_v2_envelopes():
    assert encryption.wrapped_dek_of(_v2(b"k1")) == b"k1"
    assert encryption.wrapped_dek_of("plain text") is None
    assert encryption.wrapped_dek_of(encryption.ENCRYPTED_PREFIX_V1 + "abcd") is None
    assert encryption.wrapped_dek_of(None) is None
    assert encryption.wrapped_dek_of("") is None
    assert encryption.wrapped_dek_of(encryption.ENCRYPTED_PREFIX_V2 + "not*base64:x") is None


def test_prefetch_unwraps_each_missing_dek_once(kms):
    texts = [_v2(b"k1"), _v2(b"k1", b"other row"), _v2(b"k2"),
             "plaintext", None, encryption.ENCRYPTED_PREFIX_V1 + "legacy"]
    assert encryption.prefetch_deks(texts) == 2
    assert sorted(kms) == [b"k1", b"k2"]
    # Everything is cached now: a second pass costs nothing.
    assert encryption.prefetch_deks(texts) == 0
    assert len(kms) == 2
    # And the serial decrypt path finds the cache warm.
    assert encryption._cache_get(base64.b64encode(b"k1").decode()) == b"dek:k1"


def test_prefetch_swallows_failures_for_the_serial_path(kms):
    # A failing unwrap must not turn the prefetch into the error surface;
    # the decrypt that follows raises (and logs) as before.
    assert encryption.prefetch_deks([_v2(b"boom"), _v2(b"k3")]) == 2
    assert sorted(kms) == [b"boom", b"k3"]
    assert encryption._cache_get(base64.b64encode(b"boom").decode()) is None


def test_prefetch_is_a_noop_without_encryption(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_DISABLED", "true")
    monkeypatch.setattr(encryption, "_unwrap_dek",
                        lambda w: (_ for _ in ()).throw(AssertionError("called")))
    assert encryption.prefetch_deks([_v2(b"k1")]) == 0

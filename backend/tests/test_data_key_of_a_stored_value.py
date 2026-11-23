"""encryption.dek_of: the data key of a stored value, for a second value
of the same row; and encrypt_content keeps the key it made in the
process's cache, as new_dek does."""
import types

import pytest

from backend.utils import encryption


class _KeyService:
    def __init__(self):
        self.wraps = 0
        self.unwraps = 0

    def encrypt(self, request):
        self.wraps += 1
        return types.SimpleNamespace(ciphertext=b"w:" + request["plaintext"])

    def decrypt(self, request):
        self.unwraps += 1
        return types.SimpleNamespace(plaintext=request["ciphertext"][2:])

    def forget(self):
        encryption._dek_cache.clear()
        del encryption._dek_cache_order[:]
        self.wraps = self.unwraps = 0


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_DISABLED", "false")
    monkeypatch.setenv("GCP_KMS_KEY_NAME",
                       "projects/t/locations/l/keyRings/r/cryptoKeys/k")
    service = _KeyService()
    monkeypatch.setattr(encryption, "_kms_encrypt", service.encrypt)
    monkeypatch.setattr(encryption, "_kms_decrypt", service.decrypt)
    service.forget()
    yield service
    service.forget()


def test_the_process_that_stored_a_value_reads_it_without_a_call(keys):
    stored = encryption.encrypt_content("some text")
    assert (keys.wraps, keys.unwraps) == (1, 0)
    assert encryption.decrypt_content(stored) == "some text"
    assert (keys.wraps, keys.unwraps) == (1, 0)


def test_a_second_value_under_the_first_ones_key_needs_no_call(keys):
    first = encryption.encrypt_content("the text")
    second = encryption.encrypt_with_dek("its title", encryption.dek_of(first))
    assert (keys.wraps, keys.unwraps) == (1, 0)
    assert second != first and second.startswith("ENC:v2:")
    assert encryption.wrapped_dek_of(second) == encryption.wrapped_dek_of(first)
    keys.forget()                       # another process
    assert encryption.decrypt_content(first) == "the text"
    assert encryption.decrypt_content(second) == "its title"
    assert (keys.wraps, keys.unwraps) == (0, 1)


def test_the_key_of_a_value_another_process_stored_is_one_call(keys):
    stored = encryption.encrypt_content("the text")
    keys.forget()
    key = encryption.dek_of(stored)
    assert (keys.wraps, keys.unwraps) == (0, 1)
    assert encryption.decrypt_content(
        encryption.encrypt_with_dek("more", key)) == "more"
    assert (keys.wraps, keys.unwraps) == (0, 1)


@pytest.mark.parametrize("value", [
    None, "", "plain words", "ENC:v1:AAAA", "ENC:v2:not base64:x", 7])
def test_a_value_that_is_no_envelope_has_no_key(keys, value):
    assert encryption.dek_of(value) is None
    assert (keys.wraps, keys.unwraps) == (0, 0)


def test_a_key_the_service_does_not_give_is_none(keys, monkeypatch):
    stored = encryption.encrypt_content("the text")
    keys.forget()

    def refuse(request):
        raise RuntimeError("no")

    monkeypatch.setattr(encryption, "_kms_decrypt", refuse)
    assert encryption.dek_of(stored) is None


def test_with_encryption_off_there_is_no_key(keys, monkeypatch):
    stored = encryption.encrypt_content("the text")
    # Off is the line alone: with a key name beside it nothing starts.
    monkeypatch.setenv("GCP_KMS_KEY_NAME", "")
    monkeypatch.setenv("ENCRYPTION_DISABLED", "true")
    assert encryption.dek_of(stored) is None

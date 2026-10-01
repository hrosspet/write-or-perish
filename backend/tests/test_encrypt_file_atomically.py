"""encrypt_file_atomically (#371): two requests storing the same audio
chunk at once (a hidden page's sendBeacon + normal upload, #88) must not
read, encrypt or delete each other's file."""
import os

os.environ.setdefault("ENCRYPTION_DISABLED", "true")

import pytest  # noqa: E402

from backend.utils import encryption  # noqa: E402


@pytest.fixture
def kms(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_DISABLED", "false")
    monkeypatch.setenv("GCP_KMS_KEY_NAME", "projects/t/locations/l/keyRings/r/cryptoKeys/k")
    monkeypatch.setattr(encryption, "_unwrap_dek", lambda w: w[len(b"wrapped:"):])
    assert encryption.is_encryption_enabled()
    return monkeypatch


def test_second_writer_during_the_first_ones_kms_call(kms, tmp_path):
    dest = tmp_path / "chunk_0005.webm"
    first = tmp_path / "upload-a.webm"
    second = tmp_path / "upload-b.webm"
    first.write_bytes(b"audio")
    second.write_bytes(b"audio")
    calls = []

    def wrap(dek):
        calls.append(dek)
        if len(calls) == 1:
            # The other copy is stored completely while this one waits
            # on KMS.
            encryption.encrypt_file_atomically(second, dest)
        return b"wrapped:" + dek

    kms.setattr(encryption, "_wrap_dek", wrap)
    final = encryption.encrypt_file_atomically(first, dest)

    assert final == str(dest) + ".enc"
    assert encryption.decrypt_file(final) == b"audio"
    assert [p.name for p in tmp_path.iterdir()] == ["chunk_0005.webm.enc"]


def test_failure_leaves_no_plaintext(kms, tmp_path):
    src = tmp_path / "upload-a.webm"
    src.write_bytes(b"audio")

    def wrap(dek):
        raise RuntimeError("KMS down")

    kms.setattr(encryption, "_wrap_dek", wrap)
    with pytest.raises(RuntimeError):
        encryption.encrypt_file_atomically(src, tmp_path / "chunk_0005.webm")
    assert list(tmp_path.iterdir()) == []


def test_without_encryption_the_file_is_moved(monkeypatch, tmp_path):
    monkeypatch.setenv("ENCRYPTION_DISABLED", "true")
    src = tmp_path / "upload-a.webm"
    src.write_bytes(b"audio")
    final = encryption.encrypt_file_atomically(src, tmp_path / "chunk_0005.webm")
    assert final == str(tmp_path / "chunk_0005.webm")
    assert [p.name for p in tmp_path.iterdir()] == ["chunk_0005.webm"]

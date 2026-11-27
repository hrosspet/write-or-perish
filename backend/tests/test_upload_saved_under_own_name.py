"""An uploaded audio file (routes/nodes._save_audio_file) is written under
a random name of its own, measured there, and reaches its final name only
once encrypted."""
import io

import pytest
from werkzeug.datastructures import FileStorage


@pytest.fixture
def storage(tmp_path, monkeypatch):
    from backend.routes import nodes
    from backend.utils import encryption
    monkeypatch.delenv("ENCRYPTION_DISABLED", raising=False)
    monkeypatch.setenv("GCP_KMS_KEY_NAME", "test/key")
    monkeypatch.setattr(encryption, "_wrap_dek", lambda dek: b"W" + dek)
    monkeypatch.setattr(encryption, "_unwrap_dek", lambda w: w[1:])
    monkeypatch.setattr(nodes, "AUDIO_STORAGE_ROOT", tmp_path)
    return nodes, encryption, tmp_path


def _upload(name="memo.m4a", data=b"audio bytes" * 50):
    return FileStorage(stream=io.BytesIO(data), filename=name)


def test_upload_is_measured_under_its_own_name_then_encrypted(storage):
    nodes, encryption, root = storage
    folder = root / "user/3/node/9"
    seen = []

    def measure(path):
        seen.append((path.name, sorted(p.name for p in folder.iterdir())))

    url = nodes._save_audio_file(_upload(), 3, 9, "original",
                                 before_encrypt=measure)

    assert url == "/media/user/3/node/9/original.m4a"
    [(measured, present)] = seen
    assert measured.startswith("original-upload-")
    assert measured.endswith(".m4a")
    assert present == [measured]          # no file under the final name yet
    assert sorted(p.name for p in folder.iterdir()) == ["original.m4a.enc"]
    assert encryption.decrypt_file(str(folder / "original.m4a.enc")) == (
        b"audio bytes" * 50)


def test_upload_whose_measuring_fails_leaves_no_file(storage):
    nodes, _encryption, root = storage

    def measure(path):
        raise RuntimeError("measuring failed")

    with pytest.raises(RuntimeError):
        nodes._save_audio_file(_upload(), 3, 9, "original",
                               before_encrypt=measure)
    assert list((root / "user/3/node/9").iterdir()) == []

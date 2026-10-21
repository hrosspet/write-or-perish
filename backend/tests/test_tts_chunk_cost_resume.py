"""A TTS job records each chunk's cost as the chunk is made, and a retry
keeps the chunks an earlier run made (#526).

Before, the one cost row was written after the last chunk, so a job that
failed partway recorded nothing for the audio already billed, and the
retry made every chunk again from the first. Runs the real
backend.tasks.tts on in-memory sqlite, with fakes for the TTS call
(synthesize_to_file), pydub and file encryption: no TTS call is made.
"""
import hashlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

os.environ["ENCRYPTION_DISABLED"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("TWITTER_API_KEY", "fake")
os.environ.setdefault("TWITTER_API_SECRET", "fake")

sys.modules.setdefault("celery", MagicMock())
sys.modules.setdefault("celery.utils", MagicMock())
sys.modules.setdefault("celery.utils.log", MagicMock())
sys.modules.setdefault("celery.result", MagicMock())

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

from backend.extensions import db as _db  # noqa: E402
from backend.models import (  # noqa: E402
    APICostLog, Node, TTSChunk, User, UserProfile)
from backend.tests.test_storage_ids_and_route_scope import (  # noqa: E402
    _load_tts_tasks)

SENTENCE = ("This is a reasonably long sentence used to pad sections so the "
            "chunker has real material to work with. ")
# Five chunks: two in Alpha (the second closes the chapter), three in Beta.
TEXT = f"## Alpha\n{SENTENCE * 12}\n## Beta\n{SENTENCE * 90}"


class FakeSegment:
    """Audio as the texts it speaks, 10 ms per character."""

    def __init__(self, parts):
        self.parts = list(parts)

    def __len__(self):
        return sum(10 * len(p) for p in self.parts)

    def __add__(self, other):
        return FakeSegment(self.parts + other.parts)

    def __radd__(self, other):
        assert other == 0   # sum()
        return self

    def export(self, path, format):
        Path(path).write_text(json.dumps(self.parts))


class FakeAudioSegment:
    @staticmethod
    def from_file(f, format):
        return FakeSegment(json.loads(f.read()))


class FakeTTS:
    """synthesize_to_file: one call per chunk; the calls numbered in
    *fail_calls* (from 1) fail every attempt."""

    def __init__(self, tts, fail_calls=()):
        self._tts = tts
        self.fail_calls = set(fail_calls)
        self.calls = []

    def __call__(self, client, text, path, section_end=False, mark=None):
        self.calls.append(text)
        if len(self.calls) in self.fail_calls:
            raise self._tts.TTSCallFailed("every attempt failed")
        segment = FakeSegment([text + (" [pause]" if section_end else "")])
        segment.export(path, format="mp3")
        return segment


def fake_encrypt(filepath):
    if filepath.endswith(".enc"):
        return filepath
    data = Path(filepath).read_bytes()
    Path(filepath + ".enc").write_bytes(b"ENC" + data)
    os.remove(filepath)
    return filepath + ".enc"


def fake_decrypt(filepath):
    data = Path(filepath).read_bytes()
    if filepath.endswith(".enc"):
        assert data.startswith(b"ENC")
        return data[3:]
    return data


@pytest.fixture
def app(tmp_path):
    app = Flask(__name__)
    app.config.update(
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
        SECRET_KEY="test-secret", TESTING=True, OPENAI_API_KEY="sk-test",
        AUDIO_PRICING={"gpt-4o-mini-tts": {"price_per_minute_usd": 0.015}})
    _db.init_app(app)
    app.audio_root = tmp_path / "audio"
    app.audio_root.mkdir()
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()


@pytest.fixture
def tts(app, monkeypatch):
    tts = _load_tts_tasks(app)
    monkeypatch.setattr(tts, "OpenAI", MagicMock())
    monkeypatch.setattr(tts, "AudioSegment", FakeAudioSegment)
    # Encrypted at rest, as on prod: parts are read back through .enc.
    monkeypatch.setattr(tts, "encrypt_file", fake_encrypt)
    monkeypatch.setattr(tts, "decrypt_file", fake_decrypt)
    return tts


@pytest.fixture
def users(app):
    alice = User(username="alice", approved=True, plan="alpha")
    bob = User(username="bob", approved=True, plan="alpha")
    _db.session.add_all([alice, bob])
    _db.session.commit()
    return alice, bob


def _node(user, content=TEXT):
    node = Node(user_id=user.id, human_owner_id=user.id, node_type="user",
                privacy_level="private", ai_usage="chat", token_count=1)
    node.set_content(content)
    _db.session.add(node)
    _db.session.commit()
    return node


def _speak(tts, app, node, fake, requesting_user_id=None):
    tts.synthesize_to_file = fake
    return tts.generate_tts_audio(MagicMock(), node.id, str(app.audio_root),
                                  requesting_user_id=requesting_user_id)


def _fail(tts, app, node, fake, requesting_user_id=None):
    with pytest.raises(tts.TTSCallFailed):
        _speak(tts, app, node, fake, requesting_user_id)
    _db.session.expire_all()
    assert node.tts_task_status == "failed"


def _rows(fk):
    _db.session.expire_all()
    return TTSChunk.query.filter_by(**fk).order_by(TTSChunk.chunk_index).all()


def _costs(user_id=None):
    q = APICostLog.query.filter_by(request_type="tts")
    if user_id is not None:
        q = q.filter_by(user_id=user_id)
    return q.order_by(APICostLog.id).all()


def _joined(app, node):
    """The parts the final tts.mp3 speaks."""
    path = (app.audio_root / f"user/{node.user_id}/node/{node.id}"
            / "tts.mp3.enc")
    return json.loads(fake_decrypt(str(path)))


def _token(url):
    return url.split("?v=", 1)[1]


def _fresh_chunks(tts, app, user, text):
    """The chunks a fresh run of *text* makes."""
    fake = FakeTTS(tts)
    _speak(tts, app, _node(user, text), fake)
    return fake.calls


def test_a_failed_job_records_each_chunk_it_made(app, tts, users):
    alice, bob = users
    node = _node(alice)

    _fail(tts, app, node, FakeTTS(tts, fail_calls={3}),
          requesting_user_id=bob.id)

    made = [r for r in _rows({"node_id": node.id}) if r.status == "completed"]
    assert len(made) == 2
    costs = _costs()
    # One row per chunk made, on the account that asked for the speech,
    # as the job's single row was before.
    assert [c.user_id for c in costs] == [bob.id, bob.id]
    assert [c.audio_duration_seconds for c in costs] == [
        r.duration for r in made]
    assert all(c.model_id == "gpt-4o-mini-tts" and c.cost_microdollars > 0
               for c in costs)


def test_without_a_requester_the_owner_pays(app, tts, users):
    alice, _ = users
    node = _node(alice)
    fake = FakeTTS(tts)

    _speak(tts, app, node, fake)

    assert len(_costs(alice.id)) == len(fake.calls)


def test_a_retry_makes_only_the_missing_chunks_and_sounds_like_a_fresh_run(
        app, tts, users):
    alice, bob = users
    fresh = _node(alice)
    fresh_tts = FakeTTS(tts)
    _speak(tts, app, fresh, fresh_tts)
    chunks = fresh_tts.calls
    assert len(chunks) >= 4
    fresh_rows = _rows({"node_id": fresh.id})
    fresh_seconds = sum(c.audio_duration_seconds for c in _costs())

    node = _node(alice)
    _fail(tts, app, node, FakeTTS(tts, fail_calls={3}),
          requesting_user_id=bob.id)
    failed_urls = {r.chunk_index: r.audio_url
                   for r in _rows({"node_id": node.id}) if r.audio_url}
    assert sorted(failed_urls) == [0, 1]

    retry = FakeTTS(tts)
    result = _speak(tts, app, node, retry, requesting_user_id=alice.id)

    # The chunk that failed and the ones after it, nothing twice.
    assert retry.calls == chunks[2:]
    assert result["status"] == "completed"
    # The same audio as a fresh run: the parts in order, with the same
    # chapter pauses, and the same chapters (built from these rows).
    assert _joined(app, node) == _joined(app, fresh)
    rows = _rows({"node_id": node.id})
    assert [(r.chunk_index, r.section_index, r.section_title, r.duration,
             r.status) for r in rows] == [
        (r.chunk_index, r.section_index, r.section_title, r.duration,
         r.status) for r in fresh_rows]
    # Each chunk paid once, by whoever asked when it was made.
    assert [c.user_id for c in _costs()[len(chunks):]] == (
        [bob.id] * 2 + [alice.id] * (len(chunks) - 2))
    node_seconds = sum(c.audio_duration_seconds
                       for c in _costs()[len(chunks):])
    assert node_seconds == pytest.approx(fresh_seconds)
    # Kept parts keep their URLs; the new ones and tts.mp3 carry a token
    # no URL of the failed run had (#66).
    assert {i: rows[i].audio_url for i in (0, 1)} == failed_urls
    new_token = _token(rows[2].audio_url)
    assert new_token != _token(failed_urls[0])
    assert {_token(r.audio_url) for r in rows[2:]} == {new_token}
    _db.session.refresh(node)
    assert _token(node.audio_tts_url) == new_token
    # Every part stays encrypted at rest.
    node_dir = app.audio_root / f"user/{alice.id}/node/{node.id}"
    assert sorted(p.name for p in node_dir.iterdir()) == sorted(
        [f"tts_chunk_{i}.mp3.enc" for i in range(len(chunks))]
        + ["tts.mp3.enc"])


def test_changed_text_is_made_again_from_the_first_changed_chunk(
        app, tts, users):
    # Edits clear a node's audio (#66); this is text that changed some
    # other way, e.g. how replies are spoken changed in a deploy.
    alice, _ = users
    node = _node(alice)
    first = FakeTTS(tts)
    _speak(tts, app, node, first)
    beta = next(i for i, r in enumerate(_rows({"node_id": node.id}))
                if r.section_title == "Beta")

    node = _node(alice)
    _fail(tts, app, node, FakeTTS(tts, fail_calls={beta + 2}))
    changed = TEXT.replace(f"## Beta\n{SENTENCE}", "## Beta\nNew words. ")
    node.set_content(changed)
    _db.session.commit()

    retry = FakeTTS(tts)
    _speak(tts, app, node, retry)

    # Alpha's chunks are kept; Beta's first chunk was made, but from the
    # old text, so it is made again.
    assert retry.calls[0].startswith("Beta.\n\nNew words.")
    assert first.calls[:beta] + retry.calls == _fresh_chunks(
        tts, app, alice, changed)


@pytest.mark.parametrize("damage", ["missing", "unreadable"])
def test_a_part_missing_or_unreadable_is_made_again(app, tts, users,
                                                    damage):
    alice, _ = users
    node = _node(alice)
    _fail(tts, app, node, FakeTTS(tts, fail_calls={3}))
    part = (app.audio_root / f"user/{alice.id}/node/{node.id}"
            / "tts_chunk_1.mp3.enc")
    if damage == "missing":
        part.unlink()
    else:
        part.write_bytes(b"ENCnot audio")

    retry = FakeTTS(tts)
    _speak(tts, app, node, retry)

    fresh = _node(alice)
    fresh_tts = FakeTTS(tts)
    _speak(tts, app, fresh, fresh_tts)
    assert retry.calls == fresh_tts.calls[1:]
    assert _joined(app, node) == _joined(app, fresh)


def test_a_part_left_unencrypted_is_kept_and_encrypted(
        app, tts, users, monkeypatch):
    # The chunk's row was committed, then its encryption failed.
    alice, _ = users
    node = _node(alice)
    calls = []

    def encrypt_fails_once(filepath):
        calls.append(filepath)
        if len(calls) == 2:
            raise RuntimeError("kms down")
        return fake_encrypt(filepath)
    monkeypatch.setattr(tts, "encrypt_file", encrypt_fails_once)
    with pytest.raises(RuntimeError):
        _speak(tts, app, node, FakeTTS(tts))
    node_dir = app.audio_root / f"user/{alice.id}/node/{node.id}"
    assert (node_dir / "tts_chunk_1.mp3").is_file()
    assert len(_costs()) == 2   # both chunks were billed and recorded

    retry = FakeTTS(tts)
    _speak(tts, app, node, retry)

    assert not (node_dir / "tts_chunk_1.mp3").exists()
    assert (node_dir / "tts_chunk_1.mp3.enc").is_file()
    assert retry.calls == _fresh_chunks(tts, app, alice, TEXT)[2:]


def test_rows_without_a_digest_are_not_reused(app, tts, users):
    # Rows from before #526 (and any whose digest can't be checked).
    alice, _ = users
    node = _node(alice)
    _fail(tts, app, node, FakeTTS(tts, fail_calls={3}))
    TTSChunk.query.filter_by(node_id=node.id).update({"text_digest": None})
    _db.session.commit()

    retry = FakeTTS(tts)
    _speak(tts, app, node, retry)

    assert retry.calls == _fresh_chunks(tts, app, alice, TEXT)


def test_digest_is_keyed_and_covers_the_chapter_pause(app, tts):
    a = tts.tts_chunk_digest("Some text.", False)
    assert len(a) == 64 and a == tts.tts_chunk_digest("Some text.", False)
    assert a != tts.tts_chunk_digest("Some text.", True)
    assert a != tts.tts_chunk_digest("Some text!", False)
    assert "Some text." not in a
    assert a != hashlib.sha256(b"Some text.").hexdigest()
    app.config["SECRET_KEY"] = "another-secret"
    assert tts.tts_chunk_digest("Some text.", False) != a
    app.config["SECRET_KEY"] = ""
    assert tts.tts_chunk_digest("Some text.", False) is None


def test_a_single_chunk_that_failed_is_made_on_retry(app, tts, users):
    alice, _ = users
    node = _node(alice, "A short entry.")
    _fail(tts, app, node, FakeTTS(tts, fail_calls={1}))
    assert _costs() == []

    retry = FakeTTS(tts)
    _speak(tts, app, node, retry)

    assert retry.calls == ["A short entry."]
    assert len(_costs()) == 1


def test_a_single_chunk_already_made_is_not_made_again(app, tts, users):
    # The run stopped after its only chunk (e.g. the last commit failed).
    alice, _ = users
    node = _node(alice, "A short entry.")
    _speak(tts, app, node, FakeTTS(tts))
    (row,) = _rows({"node_id": node.id})
    node.audio_tts_url = None
    node.tts_task_status = "failed"
    _db.session.commit()

    retry = FakeTTS(tts)
    _speak(tts, app, node, retry)

    assert retry.calls == []
    assert len(_costs()) == 1
    _db.session.refresh(node)
    assert node.audio_tts_url == row.audio_url   # the same file, same URL


def test_a_profile_retry_resumes_too(app, tts, users):
    alice, _ = users
    chunks = _fresh_chunks(tts, app, alice, TEXT)
    before = len(_costs(alice.id))
    profile = UserProfile(user_id=alice.id, generated_by="user",
                          tokens_used=0, ai_usage="chat")
    profile.set_content(TEXT)
    _db.session.add(profile)
    _db.session.commit()

    tts.synthesize_to_file = FakeTTS(tts, fail_calls={2})
    with pytest.raises(tts.TTSCallFailed):
        tts.generate_tts_audio_for_profile(
            MagicMock(), profile.id, str(app.audio_root),
            requesting_user_id=alice.id)
    assert len(_costs(alice.id)) == before + 1

    retry = FakeTTS(tts)
    tts.synthesize_to_file = retry
    tts.generate_tts_audio_for_profile(
        MagicMock(), profile.id, str(app.audio_root),
        requesting_user_id=alice.id)

    assert retry.calls == chunks[1:]
    assert len(_costs(alice.id)) == before + len(chunks)

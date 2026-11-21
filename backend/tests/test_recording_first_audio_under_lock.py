"""The first audio of a recording is counted under the account's lock
right before its chunk row is stored (routes/drafts.upload_streaming_chunk,
utils/recording_limits.audio_refusal).

The count of open recordings (MAX_OPEN_RECORDINGS) and the audio waiting
for its transcript (the usage limit) are read from the chunk rows. The
chunk route asks them before the body is read and the file is encrypted,
and asks them again once the file is staged, with the account's lock held
through the chunk row's commit. So first chunks of several recordings that
arrive together are counted one after the other: no more than
MAX_OPEN_RECORDINGS of them are taken, and none past the usage limit.

SQLite takes no lock: there the overlap comes from sending each request
from inside the encrypt step of the one before (where requests at the same
moment are). The same on PostgreSQL with real parallel requests is in
test_recording_first_audio_under_lock_postgres.py.
"""
from sqlalchemy import event

from backend.tests.test_recording_limits import (  # noqa: F401
    app, tasks, alice, client, _init_recording, _chunk, _payload, _limit,
    _cost, WEB_CHUNK_BYTES, limits, _db, NodeTranscriptChunk)

N = 20


def _starts(client, n):  # noqa: F811
    sids = []
    for _ in range(n):
        r = _init_recording(client)
        assert r.status_code == 201, r.get_data(as_text=True)
        sids.append(r.get_json()["session_id"])
    return sids


def _each_inside_the_one_before(app, alice, client, sids, monkeypatch):  # noqa: F811
    """Send chunk 1 of every session in *sids*, each from inside the
    encrypt step of the one before. Returns [(session id, status)]."""
    import backend.routes.drafts as drafts_mod
    real_stage = drafts_mod.stage_encrypted_file
    answers = []
    rest = iter(sids[1:])

    def stage(upload_path, chunk_path):
        nxt = next(rest, None)
        if nxt is not None:
            other = app.test_client()
            with other.session_transaction() as sess:
                sess["_user_id"] = str(alice.id)
                sess["_fresh"] = True
            answers.append((nxt, _chunk(
                other, nxt, 1, _payload(WEB_CHUNK_BYTES)).status_code))
        return real_stage(upload_path, chunk_path)

    monkeypatch.setattr(drafts_mod, "stage_encrypted_file", stage)
    answers.append((sids[0], _chunk(
        client, sids[0], 1, _payload(WEB_CHUNK_BYTES)).status_code))
    monkeypatch.setattr(drafts_mod, "stage_encrypted_file", real_stage)
    return answers


def _chunk_files(app, alice, sid):  # noqa: F811
    folder = app.media_root / f"drafts/{alice.id}/{sid}"
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


def test_the_lock_is_held_from_the_last_count_to_the_chunk_row(
        app, client, alice, monkeypatch):  # noqa: F811
    trace = []
    real_lock = limits.lock_account
    monkeypatch.setattr(
        limits, "lock_account",
        lambda user_id: (trace.append("lock"), real_lock(user_id))[1])

    def after_commit(session):
        trace.append("commit")

    def before_insert(mapper, connection, target):
        trace.append("chunk-row")

    sid = _starts(client, 1)[0]
    del trace[:]                             # the init's own lock
    event.listen(_db.session, "after_commit", after_commit)
    event.listen(NodeTranscriptChunk, "before_insert", before_insert)
    try:
        assert _chunk(client, sid, 1,
                      _payload(WEB_CHUNK_BYTES)).status_code == 202
    finally:
        event.remove(_db.session, "after_commit", after_commit)
        event.remove(NodeTranscriptChunk, "before_insert", before_insert)

    last_lock = len(trace) - 1 - trace[::-1].index("lock")
    row = trace.index("chunk-row")
    assert last_lock < row
    assert "commit" not in trace[last_lock + 1:row]


def test_first_chunks_together_stop_at_the_open_recordings_count(
        app, client, alice, monkeypatch):  # noqa: F811
    sids = _starts(client, N)

    answers = _each_inside_the_one_before(app, alice, client, sids,
                                          monkeypatch)

    assert sorted(code for _sid, code in answers) == (
        [202] * limits.MAX_OPEN_RECORDINGS
        + [429] * (N - limits.MAX_OPEN_RECORDINGS))
    assert limits.open_recordings(alice.id) == limits.MAX_OPEN_RECORDINGS
    assert NodeTranscriptChunk.query.count() == limits.MAX_OPEN_RECORDINGS
    # A refused chunk leaves no file behind.
    for sid, code in answers:
        files = [f for f in _chunk_files(app, alice, sid)
                 if f.startswith(("chunk_", "upload-"))]
        assert files == ([] if code == 429 else ["chunk_0001.webm"])


def test_first_chunks_together_stop_at_the_usage_limit(
        app, client, alice, monkeypatch):  # noqa: F811
    # A usage limit that three waiting chunks reach (15 s each).
    _limit(app, alice, 0.004)
    sids = _starts(client, N + 1)[:N]     # the newest is left out: its
    #                                       first audio skips the usage limit

    answers = _each_inside_the_one_before(app, alice, client, sids,
                                          monkeypatch)

    assert sorted(code for _sid, code in answers) \
        == [202] * 3 + [503] * (N - 3)
    assert _cost(limits.pending_audio_seconds(alice.id)) \
        == 3 * _cost(limits.STREAMING_CHUNK_SECONDS)


def test_more_audio_of_a_recording_takes_no_lock(
        app, client, alice, monkeypatch):  # noqa: F811
    # Control: a recording that holds audio is never counted again.
    sid = _starts(client, 1)[0]
    assert _chunk(client, sid, 1, _payload(WEB_CHUNK_BYTES)).status_code \
        == 202
    locked = []
    monkeypatch.setattr(limits, "lock_account", locked.append)
    assert _chunk(client, sid, 2, _payload(WEB_CHUNK_BYTES)).status_code \
        == 202
    assert locked == []


def test_two_copies_of_a_first_chunk_store_one(
        app, client, alice, monkeypatch):  # noqa: F811
    # Control: a hidden page sends a chunk by sendBeacon and by the normal
    # upload (#88); the copy that comes second is answered as a copy.
    sid = _starts(client, 1)[0]
    answers = _each_inside_the_one_before(app, alice, client, [sid, sid],
                                          monkeypatch)
    assert [code for _sid, code in answers] == [202, 200]
    assert NodeTranscriptChunk.query.filter_by(session_id=sid).count() == 1


def test_one_after_the_other_the_count_holds(app, client, alice):  # noqa: F811
    # Control: unchanged when nothing overlaps.
    sids = _starts(client, N)
    codes = [_chunk(client, sid, 1, _payload(WEB_CHUNK_BYTES)).status_code
             for sid in sids]
    assert codes == [202] * limits.MAX_OPEN_RECORDINGS \
        + [429] * (N - limits.MAX_OPEN_RECORDINGS)

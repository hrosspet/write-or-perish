"""Replies shown — and in voice, spoken — while they are written (#367).

Runs the real generate_llm_response on the test_retrieval_loop harness,
with a provider stand-in that feeds the listener before returning."""
import pytest

from backend.tests.test_retrieval_loop import (  # noqa: F401 (fixture)
    _FakeSelf, _build_chain, _db, _fresh, _llm_task_mod, _resp, app)
from backend.models import APICostLog, TTSChunk
from backend.utils import llm_stream, tts_stream
from backend.utils.llm_stream import CUT_OFF_NOTE

generate_llm_response = _llm_task_mod.generate_llm_response


class _Tool:
    def __init__(self, name):
        self.name = name


class _LiveProvider:
    """Each call replays a script: text pieces go to the listener, a _Tool
    announces a tool call, a callable runs mid-stream (to look at the
    database), an Exception is raised; then the response is returned."""
    scripts = []
    calls = 0

    @classmethod
    def reset(cls, scripts):
        cls.scripts = list(scripts)
        cls.calls = 0

    @classmethod
    def get_completion(cls, model_id, messages, api_keys, tools=None,
                       listener=None, **kwargs):
        cls.calls += 1
        pieces, response = cls.scripts.pop(0)
        for piece in pieces:
            if isinstance(piece, Exception):
                raise piece
            if isinstance(piece, _Tool):
                listener.on_tool_call(piece.name)
            elif callable(piece):
                piece(listener)
            else:
                listener.on_text(piece)
        response = dict(response)
        response["cache_comparison_sent"] = False
        return response


@pytest.fixture
def live(app, monkeypatch):  # noqa: F811 (the imported fixture)
    monkeypatch.setattr(_llm_task_mod, "LLMProvider", _LiveProvider)
    monkeypatch.setattr(llm_stream, "PARTIAL_WRITE_INTERVAL_SECS", 0)
    monkeypatch.setattr(_llm_task_mod, "CONTINUATION_RETRY_DELAYS", ())
    return app


def _run(user_node, llm_node, alice, mode="textmode"):
    return generate_llm_response(
        _FakeSelf(), user_node.id, llm_node.id, "gpt-5", alice.id,
        source_mode=mode)


def test_partial_text_is_on_the_node_while_it_streams(live):
    alice, _, user_node, llm_node = _build_chain("textmode")
    seen = {}

    def mark(_listener):
        seen["before"] = _fresh(llm_node.id).updated_at

    def look(_listener):
        node = _fresh(llm_node.id)
        seen["partial"] = node.get_streaming_content()
        seen["content"] = node.get_content()
        seen["updated_at"] = node.updated_at

    _LiveProvider.reset([(
        [mark, "[2026-09-28 10:00 UTC] Hello ", "wor", "ld. See {quo", look,
         "te:1} more."],
        _resp("[2026-09-28 10:00 UTC] Hello world. See {quote:1} more."),
    )])
    _run(user_node, llm_node, alice)

    # The display text: edge stamp dropped, half a marker held back.
    assert seen["partial"] == "Hello world. See "
    assert seen["content"] == "[LLM response generation pending...]"
    # Partial writes don't bump updated_at (the embedding sweep keys on it).
    assert seen["updated_at"] == seen["before"]
    node = _fresh(llm_node.id)
    assert node.streaming_content is None
    assert node.get_content() == "Hello world. See {quote:1} more."
    assert node.llm_task_status == "completed"


def test_a_restart_clears_the_partial_text(live):
    alice, _, user_node, llm_node = _build_chain("textmode")
    seen = {}

    def restart(listener):
        assert listener.on_restart() is True
        seen["after_restart"] = _fresh(llm_node.id).streaming_content

    _LiveProvider.reset([(["Half a thou", restart, "Full reply."],
                          _resp("Full reply."))])
    _run(user_node, llm_node, alice)
    assert seen["after_restart"] is None
    assert _fresh(llm_node.id).get_content() == "Full reply."


def test_failure_after_text_completes_as_cut_off(live):
    alice, _, user_node, llm_node = _build_chain("textmode")
    _LiveProvider.reset([(["Half a rep", RuntimeError("overloaded")],
                          None)])
    _run(user_node, llm_node, alice)
    node = _fresh(llm_node.id)
    assert node.llm_task_status == "completed"
    assert node.get_content() == "Half a rep" + CUT_OFF_NOTE
    assert node.streaming_content is None
    # The call never completed: no usage to log.
    assert APICostLog.query.filter_by(
        request_type="conversation").count() == 0


def test_failure_before_any_text_still_fails(live):
    alice, _, user_node, llm_node = _build_chain("textmode")
    _LiveProvider.reset([([RuntimeError("overloaded")], None)])
    with pytest.raises(RuntimeError):
        _run(user_node, llm_node, alice)
    node = _fresh(llm_node.id)
    assert node.llm_task_status == "failed"
    assert node.streaming_content is None


def test_streaming_off_writes_no_partial_text(live, monkeypatch):
    live.config["STREAMING_REPLIES"] = False
    try:
        alice, _, user_node, llm_node = _build_chain("textmode")
        seen = {}
        _LiveProvider.reset([(
            [lambda listener: seen.setdefault("listener", listener)],
            _resp("Done."))])
        _run(user_node, llm_node, alice)
        assert seen["listener"] is None
    finally:
        live.config["STREAMING_REPLIES"] = True


# ── Voice: spoken while written ──────────────────────────────────────────

class _Segment:
    def __init__(self, text):
        self.text = text


class _FakeAudio:
    spoken = []

    def __init__(self, api_key=None):
        pass

    def synthesize(self, text, path, section_end):
        _FakeAudio.spoken.append((text, section_end))
        path.write_bytes(b"mp3")
        return _Segment(text)

    @staticmethod
    def duration(segment):
        return len(segment.text) * 0.062

    @staticmethod
    def join(segments, path):
        path.write_bytes(b"mp3" * len(segments))


@pytest.fixture
def voice(live, monkeypatch, tmp_path):
    monkeypatch.setenv("AUDIO_STORAGE_PATH", str(tmp_path))
    monkeypatch.setattr(tts_stream, "OpenAITTSAudio", _FakeAudio)
    monkeypatch.setattr(_llm_task_mod, "TTS_STREAM_THREADED", False)
    batch = []
    monkeypatch.setattr(_llm_task_mod, "_dispatch_voice_tts",
                        lambda node_id, user_id: batch.append(node_id))
    live.config["STREAMING_VOICE_TTS"] = True
    _FakeAudio.spoken = []
    yield batch
    live.config["STREAMING_VOICE_TTS"] = False


def _chunks(node_id):
    return TTSChunk.query.filter_by(node_id=node_id).order_by(
        TTSChunk.chunk_index).all()


def test_voice_turn_is_spoken_through_a_tool_round(voice):
    alice, _, user_node, llm_node = _build_chain("voice")
    seen = {}

    def look(_listener):
        seen["tts"] = _fresh(llm_node.id).tts_task_status

    answer = ("# Plan\nFirst do this. Then that.\n\n"
              "### Completed\n- old task\n### Note\nNice work.\n"
              "## Later\nThe rest.")
    _LiveProvider.reset([
        (["Let me check your list.", look, _Tool("read_todo")],
         _resp("Let me check your list.", tool_calls=[{
             "id": "t1", "name": "read_todo", "input": {}}])),
        ([answer[:9], answer[9:40], answer[40:]], _resp(answer)),
    ])
    _run(user_node, llm_node, alice, mode="voice")

    assert seen["tts"] == "processing"   # the browser can attach early
    assert voice == []                   # no batch TTS dispatched
    interim = _fresh(llm_node.id)
    answer_node = _fresh(interim.continuation_node_id)
    for node in (interim, answer_node):
        assert node.tts_task_status == "completed"
        assert node.audio_tts_url and "?v=" in node.audio_tts_url
    assert [c.status for c in _chunks(interim.id)] == ["completed"]
    rows = _chunks(answer_node.id)
    assert [(r.section_title, r.section_index) for r in rows] == [
        ("Plan", 0), ("Later", 1)]
    texts = [t for t, _ in _FakeAudio.spoken]
    assert texts[0] == "Let me check your list."
    assert texts[1].startswith("Plan.\n\nFirst do this. Then that.")
    assert "Nice work." in texts[1] and "old task" not in texts[1]
    assert texts[2] == "Later.\n\nThe rest."
    # The chunk closing a chapter gets the chapter pause; the last doesn't.
    assert [end for _, end in _FakeAudio.spoken] == [False, True, False]


def test_textless_tool_round_speaks_its_fallback_line(voice):
    alice, _, user_node, llm_node = _build_chain("voice")
    _LiveProvider.reset([
        ([_Tool("read_todo")], _resp("", tool_calls=[{
            "id": "t1", "name": "read_todo", "input": {}}])),
        (["Here it is."], _resp("Here it is.")),
    ])
    _run(user_node, llm_node, alice, mode="voice")
    texts = [t for t, _ in _FakeAudio.spoken]
    assert texts == ["(looking that up…)", "Here it is."]


def test_voice_failure_marks_the_open_node_tts_failed(voice):
    alice, _, user_node, llm_node = _build_chain("voice")
    _LiveProvider.reset([([RuntimeError("down")], None)])
    with pytest.raises(RuntimeError):
        _run(user_node, llm_node, alice, mode="voice")
    node = _fresh(llm_node.id)
    assert node.llm_task_status == "failed"
    assert node.tts_task_status == "failed"


def test_worker_thread_speaks_as_text_arrives(live, tmp_path):
    """The real thread: chunks are cut while the text still streams,
    stay in order, and together speak the whole text."""
    alice, _, user_node, llm_node = _build_chain("voice")
    node = _fresh(llm_node.id)
    node.tts_task_status = "processing"
    _db.session.commit()
    _FakeAudio.spoken = []
    turn = tts_stream.VoiceTTSStream(
        live, alice.id, tmp_path, audio=_FakeAudio(), threaded=True)
    speech = turn.open_node(node)
    sentence = "This sentence is about forty characters. "
    for _ in range(60):
        speech.feed(sentence)
    speech.release()
    turn.finish()
    texts = [t for t, _ in _FakeAudio.spoken]
    assert len(texts) >= 2
    assert len(texts[0]) <= 318
    assert " ".join(texts) == (sentence * 60).strip()
    _db.session.expire_all()
    assert [r.chunk_index for r in _chunks(node.id)] == list(
        range(len(texts)))
    assert _fresh(node.id).tts_task_status == "completed"

"""A Voice reply whose write calls wait for approval keeps its own audio as
it is (Peter, 2026-11-12):

"the announcement should be TTSed as a separate chunk and appended to the
queue. So that when it's not pending, it's not voiced out anymore"

The spoken announcement is its own cached clip that the players append
after the reply while its calls wait (utils/announcement_audio,
test_voice_approval_routes). The reply's audio is what it would be without
any waiting call: the same chunks, sections and joined file, live and in
the batch task. Runs the real task body on the test_retrieval_loop /
test_live_streaming harness.
"""
import copy
import sys
import types
from unittest.mock import MagicMock

import pytest

from backend.tests.test_retrieval_loop import (  # noqa: F401 (fixture)
    app, _FakeSelf, _db, _fresh, _llm_task_mod, _mk_artifact, _mk_user,
    _resp)
from backend.tests.test_live_streaming import (  # noqa: F401 (fixtures)
    live, voice, _FakeAudio, _Tool, _chunks)
from backend.tests.test_personal_context_owner import (
    _llm, _node, _system_node)
from backend.models import Draft, UserArtifact
from backend.utils import tool_approvals, tts_stream
from backend.utils.announcement_audio import ANNOUNCEMENT_TEXTS

generate_llm_response = _llm_task_mod.generate_llm_response


class _Recorder:
    """The provider stand-in: replays a script per call (text to the
    listener, a _Tool announces a tool call) and keeps every payload."""
    scripts = []
    calls = []

    @classmethod
    def reset(cls, scripts):
        cls.scripts = list(scripts)
        cls.calls = []

    @classmethod
    def get_completion(cls, model_id, messages, api_keys, tools=None,
                       listener=None, **kwargs):
        cls.calls.append({"messages": copy.deepcopy(messages),
                          "tools": copy.deepcopy(tools)})
        pieces, response = cls.scripts.pop(0)
        for piece in pieces:
            if listener is None:
                continue
            if isinstance(piece, _Tool):
                listener.on_tool_call(piece.name)
            else:
                listener.on_text(piece)
        response = dict(response)
        response["cache_comparison_sent"] = False
        return response


class _Audio(_FakeAudio):
    """The fake TTS, also keeping what each node's joined file (the
    replay) is made of."""
    joined = []

    @staticmethod
    def join(segments, path):
        _Audio.joined.append([s.text for s in segments])
        path.write_bytes(b"mp3" * len(segments))


@pytest.fixture
def spoken(voice, monkeypatch):
    """Voice turns spoken while written, through _Recorder and _Audio."""
    monkeypatch.setattr(_llm_task_mod, "LLMProvider", _Recorder)
    monkeypatch.setattr(tts_stream, "OpenAITTSAudio", _Audio)
    # The approval code imports the task module lazily: the harness's.
    monkeypatch.setitem(sys.modules, "backend.tasks.llm_completion",
                        _llm_task_mod)
    _Audio.joined = []
    _FakeAudio.spoken = []
    return voice


def _people():
    ben = _mk_user("ben", approved=True, plan="alpha")
    anna = _mk_user("anna", approved=True, plan="alpha")
    _mk_user("gpt-5", twitter_id="llm-gpt-5")
    _mk_artifact(ben.id, "memory", "- likes tea", title="Memory")
    _db.session.commit()
    return ben, anna


def _thread(ben, anna=None):
    """Ben's thread, with Anna's reply in it when *anna* is given; returns
    his last entry."""
    system = _system_node(ben)
    last = _node(ben, system, "ben thinks out loud")
    if anna is not None:
        last = _node(anna, last, "anna replies")
    entry = _node(ben, last, "remember that I trust her judgement")
    _db.session.commit()
    return entry


def _talk(entry, ben, scripts):
    """One Voice turn under *entry*; returns its first reply node."""
    reply = _llm(entry, ben)
    reply.privacy_level = "private"
    _db.session.commit()
    _Recorder.reset(scripts)
    generate_llm_response(_FakeSelf(), entry.id, reply.id, "gpt-5", ben.id,
                          source_mode="voice")
    return _fresh(reply.id)


def _write(text, call_id="w1"):
    return {"id": call_id, "name": "update_artifact",
            "input": {"kind": "memory", "updated_content": text,
                      "base_version": 1}}


def _turn(answer="I asked before changing your memory.", writes=1):
    calls = [_write(f"- likes tea\n- note {i}", f"w{i}")
             for i in range(writes)]
    return [
        (["Noting that.", _Tool("update_artifact")],
         _resp("Noting that.", tool_calls=calls)),
        ([answer], _resp(answer)),
    ]


def _texts():
    return [t for t, _ in _FakeAudio.spoken]


def _memory(ben):
    _db.session.expire_all()
    return UserArtifact.latest_for(ben.id, "memory").get_content()


def _rows(node_id):
    return [(r.chunk_index, r.section_index, r.section_title, r.duration)
            for r in _chunks(node_id)]


def _heard_turn(anna_in_thread):
    """A fresh database; one Voice turn whose memory write waits (Anna's
    reply in the thread) or runs (Ben's own thread). Returns what the TTS
    was asked to speak, the joined files, and each node's chunk rows."""
    _db.session.remove()
    _db.drop_all()
    _db.create_all()
    _FakeAudio.spoken = []
    _Audio.joined = []
    ben, anna = _people()
    reply = _talk(_thread(ben, anna if anna_in_thread else None), ben,
                  _turn())
    answer = _fresh(reply.continuation_node_id)
    return {
        "spoken": list(_FakeAudio.spoken),
        "joined": list(_Audio.joined),
        "rows": [_rows(reply.id), _rows(answer.id)],
        "urls": [reply.audio_tts_url, answer.audio_tts_url],
        "held": Draft.query.filter_by(label="tool_pending").count(),
        "texts": [reply.get_content(), answer.get_content()],
    }


def test_the_reply_audio_is_the_same_whether_or_not_a_change_waits(
        app, spoken):
    """The reply's audio holds no announcement: what the TTS speaks, the
    chunks and their sections, and the joined file a replay plays are
    those of the same reply with nothing waiting (as on main)."""
    waiting = _heard_turn(anna_in_thread=True)
    own = _heard_turn(anna_in_thread=False)

    assert waiting["held"] == 1 and own["held"] == 0
    assert waiting["spoken"] == own["spoken"] == [
        ("Noting that.", False),
        ("I asked before changing your memory.", False)]
    assert waiting["joined"] == own["joined"]
    assert waiting["rows"] == own["rows"]
    # Each node's audio file is made and kept (paths name the node ids).
    assert all(waiting["urls"]) and all(own["urls"])
    assert waiting["texts"] == own["texts"] == [
        "Noting that.", "I asked before changing your memory."]
    for text in ANNOUNCEMENT_TEXTS.values():
        assert not any(text in t for t, _ in waiting["spoken"])


def test_a_reply_whose_last_round_holds_the_call(app, spoken, monkeypatch):
    """No tool round left: the call is held in the final answer. Its
    audio is still the reply alone, and the reply counts the call."""
    monkeypatch.setattr(_llm_task_mod, "MAX_RETRIEVAL_ROUNDS", 0)
    ben, anna = _people()
    reply = _talk(_thread(ben, anna), ben, [
        (["Noting that.", _Tool("update_artifact")],
         _resp("Noting that.", tool_calls=[_write("- likes tea\n- x")])),
    ])

    assert _texts() == ["Noting that."]
    assert _memory(ben) == "- likes tea"
    assert tool_approvals.announcement_count(_fresh(reply.id)) == 1


def test_the_reply_counts_its_waiting_calls(app, spoken):
    """announcement_count, what the players ask before appending the
    clip: the final node of a Voice reply counts the calls of its interim
    steps; the interim step itself, and a decided call, count nothing."""
    ben, anna = _people()
    reply = _talk(_thread(ben, anna), ben, _turn(writes=2))
    answer = _fresh(reply.continuation_node_id)

    assert tool_approvals.announcement_count(answer) == 2
    assert tool_approvals.announcement_count(_fresh(reply.id)) == 0
    first, second = Draft.query.filter_by(label="tool_pending").order_by(
        Draft.id).all()
    tool_approvals.decide(first, ben.id, approve=False)
    _db.session.commit()
    assert tool_approvals.announcement_count(_fresh(answer.id)) == 1
    tool_approvals.decide(_db.session.get(Draft, second.id), ben.id,
                          approve=False)
    _db.session.commit()
    assert tool_approvals.announcement_count(_fresh(answer.id)) == 0


# ── The batch path (a reply not spoken while written) ──────────────────

def _load_tts_tasks(app):
    """backend.tasks.tts with its tasks as plain functions running against
    *app* (as test_storage_ids_and_route_scope does); sys.modules and the
    package attribute are restored."""
    import backend.utils.audio_processing  # noqa: F401  (keeps real pydub)
    import backend.tasks as tasks_pkg
    celery_stub = MagicMock()
    celery_stub.Task = object
    celery_app = types.ModuleType("backend.celery_app")
    celery_app.celery = MagicMock()
    celery_app.celery.task = lambda *a, **k: (lambda f: f)
    celery_app.flask_app = app
    glue = {"celery": celery_stub, "celery.utils": MagicMock(),
            "celery.utils.log": MagicMock(), "pydub": MagicMock(),
            "backend.celery_app": celery_app}
    names = list(glue) + ["backend.tasks.tts"]
    saved = {k: sys.modules.get(k) for k in names}
    had_attr = hasattr(tasks_pkg, "tts")
    saved_attr = getattr(tasks_pkg, "tts", None)
    try:
        sys.modules.update(glue)
        sys.modules.pop("backend.tasks.tts", None)
        import backend.tasks.tts as tts
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        if had_attr:
            tasks_pkg.tts = saved_attr
        elif hasattr(tasks_pkg, "tts"):
            delattr(tasks_pkg, "tts")
    return tts


def test_batch_audio_is_the_reply_alone(app, spoken, tmp_path):
    app.config["STREAMING_VOICE_TTS"] = False
    ben, anna = _people()
    reply = _talk(_thread(ben, anna), ben, _turn())
    answer = _fresh(reply.continuation_node_id)
    assert spoken == [reply.id, answer.id]    # each node's TTS queued
    tts = _load_tts_tasks(app)
    texts = []

    def speak(task, node, text, *args, **kwargs):
        texts.append(text)
        return "/media/tts.mp3"

    tts._generate_tts_chunks = speak
    tts.generate_tts_audio(None, reply.id, str(tmp_path))
    tts.generate_tts_audio(None, answer.id, str(tmp_path))
    assert texts == ["Noting that.", "I asked before changing your memory."]

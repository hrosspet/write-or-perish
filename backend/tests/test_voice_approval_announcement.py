"""A Voice reply whose write calls wait for approval ends its audio saying
so (Peter, 2026-11-12):

"A spoken announcement should also be added after the model reply,
informing the user they need to confirm, or reject the tool called. Also,
the lockscreen controls shouldn't allow next recording (replay of the
reply should be still possible, but the announcement should still be
there after the LLM reply)"

The sentence is spoken by the reply's own TTS after its last words, in its
last chapter: it is in the reply's audio file (a replay says it too), the
stored text does not hold it (so later prompts are byte-identical), and
the chapter list stays as it was. Runs the real task body on the
test_retrieval_loop / test_live_streaming harness.
"""
import copy
import json
import re
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
from backend.utils.audio_processing import tts_chapters
from backend.utils.tool_approvals import approval_announcement
from backend.utils.tts_stream_text import ChunkPlanner

ONE = approval_announcement(1)
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


# ── The live path: spoken while written ─────────────────────────────────


def test_the_reply_ends_saying_a_change_waits(app, spoken):
    ben, anna = _people()
    reply = _talk(_thread(ben, anna), ben, _turn())

    answer = _fresh(reply.continuation_node_id)
    assert _memory(ben) == "- likes tea"          # the call waits
    # The written reply is as the model wrote it.
    assert reply.get_content() == "Noting that."
    assert answer.get_content() == "I asked before changing your memory."
    # Heard first time: the interim step, then the answer and the sentence.
    assert _texts() == [
        "Noting that.", f"I asked before changing your memory.\n\n{ONE}"]
    # The replay is the joined file, which holds the sentence too.
    assert _Audio.joined[-1] == [
        f"I asked before changing your memory.\n\n{ONE}"]
    assert answer.tts_task_status == "completed" and answer.audio_tts_url
    # One untitled section: no chapter list, as without the sentence.
    rows = _chunks(answer.id)
    assert {(r.section_index, r.section_title) for r in rows} == {(0, None)}
    assert tts_chapters(rows) == []


def test_several_waiting_calls_are_counted(app, spoken):
    ben, anna = _people()
    reply = _talk(_thread(ben, anna), ben, _turn(writes=2))

    assert len(Draft.query.filter_by(label="tool_pending").all()) == 2
    assert _texts()[-1].endswith(approval_announcement(2))
    assert approval_announcement(2).startswith(
        "2 changes to your data are waiting.")
    assert _fresh(reply.continuation_node_id).get_content() == (
        "I asked before changing your memory.")


def test_own_thread_reply_is_spoken_as_before(app, spoken):
    """Only Ben's text in the thread: the write runs, nothing is said."""
    ben, _ = _people()
    _talk(_thread(ben), ben, _turn())

    assert _memory(ben) == "- likes tea\n- note 0"
    assert _texts() == ["Noting that.", "I asked before changing your memory."]
    assert Draft.query.filter_by(label="tool_pending").count() == 0


def test_chapters_keep_their_starts(app, spoken):
    """The sentence joins the last chapter: the chapter list and each
    chapter's start are what they would be without it."""
    ben, anna = _people()
    answer_text = "# Plan\nFirst do this. Then that.\n\n## Later\nThe rest."
    reply = _talk(_thread(ben, anna), ben, _turn(answer=answer_text))

    answer = _fresh(reply.continuation_node_id)
    assert answer.get_content() == answer_text
    rows = _chunks(answer.id)
    assert [(r.section_title, r.section_index) for r in rows] == [
        ("Plan", 0), ("Later", 1)]
    texts = _texts()
    assert texts[1] == "Plan.\n\nFirst do this. Then that."
    assert texts[2] == f"Later.\n\nThe rest.\n\n{ONE}"
    chapters = tts_chapters(rows)
    assert [c["title"] for c in chapters] == ["Plan", "Later"]
    # Chapter II starts where chapter I's audio ends, as before.
    assert chapters[1]["start_time"] == round(rows[0].duration, 2)
    # The chapter pause stays on the chunk that closes chapter I only.
    assert [end for _, end in _FakeAudio.spoken] == [False, True, False]


def test_a_reply_whose_last_round_holds_the_call(app, spoken, monkeypatch):
    """No tool round left: the call is made and held in the final answer,
    whose speech a tool call has closed already. The sentence follows."""
    monkeypatch.setattr(_llm_task_mod, "MAX_RETRIEVAL_ROUNDS", 0)
    ben, anna = _people()
    reply = _talk(_thread(ben, anna), ben, [
        (["Noting that.", _Tool("update_artifact")],
         _resp("Noting that.", tool_calls=[_write("- likes tea\n- x")])),
    ])

    assert reply.get_content() == "Noting that."
    assert _texts() == [f"Noting that.\n\n{ONE}"]
    assert _memory(ben) == "- likes tea"


# ── Later prompts are byte-identical ───────────────────────────────────

_STAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?: ?UTC|Z)?")


def _next_turn_payload(app, announce, monkeypatch):
    """A Voice turn in Ben's thread with Anna's reply holds a memory
    write; Ben rejects it and talks on. Returns the next turn's provider
    payload (messages and tools, timestamps normalised), from a fresh
    database each time so the ids match. Run with *announce* first."""
    _db.session.remove()
    _db.drop_all()
    _db.create_all()
    _FakeAudio.spoken = []
    if not announce:
        monkeypatch.setattr(tool_approvals, "approval_announcement_for",
                            lambda node: None)
    ben, anna = _people()
    reply = _talk(_thread(ben, anna), ben, _turn())
    heard = _texts()
    [held] = Draft.query.filter_by(label="tool_pending").all()
    tool_approvals.decide(held, ben.id, approve=False)
    _db.session.commit()
    answer = _fresh(reply.continuation_node_id)
    again = _node(ben, answer, "fine, leave it")
    _db.session.commit()
    _talk(again, ben, [(["All right."], _resp("All right."))])
    payload = json.dumps(_Recorder.calls[0], sort_keys=True, default=str)
    return heard, _STAMP.sub("<time>", payload)


def test_later_prompts_are_byte_identical(app, spoken, monkeypatch):
    heard_with, with_sentence = _next_turn_payload(app, True, monkeypatch)
    heard_without, without = _next_turn_payload(app, False, monkeypatch)

    # The sentence was spoken in the first run only ...
    assert heard_with[-1].endswith(ONE)
    assert not any(ONE in t for t in heard_without)
    # ... and the next turn sent the same bytes either way.
    assert with_sentence == without
    assert ONE not in with_sentence
    assert "The user declined it; nothing was done." in with_sentence


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


def test_batch_audio_says_it_too_while_the_call_waits(
        app, spoken, tmp_path):
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
    assert texts == ["Noting that.",
                     f"I asked before changing your memory.\n\n{ONE}"]

    # Decided: an audio made again (e.g. regenerated) no longer says it.
    [held] = Draft.query.filter_by(label="tool_pending").all()
    tool_approvals.decide(held, ben.id, approve=False)
    _db.session.commit()
    answer = _fresh(answer.id)
    answer.audio_tts_url = None
    _db.session.commit()
    tts.generate_tts_audio(None, answer.id, str(tmp_path))
    assert texts[-1] == "I asked before changing your memory."


# ── The planner on its own ──────────────────────────────────────────────


def _drain(planner):
    out = []
    while True:
        chunk = planner.take(10_000)
        if chunk is None:
            return out
        out.append(chunk)


def test_coda_added_before_close_ends_the_last_section():
    planner = ChunkPlanner()
    planner.add([("heading", "A"), ("text", "Body a. "),
                 ("heading", "B"), ("text", "Body b.")])
    planner.add_coda("Coda.")
    planner.close()
    chunks = _drain(planner)
    assert [(c.text, c.section_title, c.section_index, c.section_end)
            for c in chunks] == [
        ("A.\n\nBody a.", "A", 0, True),
        ("B.\n\nBody b.\n\nCoda.", "B", 1, False)]


def test_coda_after_everything_was_cut_keeps_the_last_section():
    planner = ChunkPlanner()
    planner.add([("heading", "A"), ("text", "Body a. More of it. ")])
    first = planner.take(12, jit=True)
    assert first.text == "A.\n\nBody a."
    planner.close()
    rest = _drain(planner)
    assert [c.text for c in rest] == ["More of it."]
    planner.add_coda("Coda.")
    [coda] = _drain(planner)
    assert (coda.text, coda.section_title, coda.section_index,
            coda.section_end) == ("Coda.", "A", 0, False)
    assert planner.done


def test_coda_alone_when_nothing_was_spoken():
    planner = ChunkPlanner()
    planner.add_coda("Coda.")
    planner.close()
    [coda] = _drain(planner)
    assert (coda.text, coda.section_title, coda.section_index) == (
        "Coda.", None, 0)

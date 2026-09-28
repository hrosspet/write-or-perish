"""Speak a voice turn's replies while they are written (#367 part 4).

VoiceTTSStream is one turn's TTS worker: a thread inside the LLM task
(it sees the text as it arrives and knows when it is free, and uses no
second Celery slot). Each LLM node of the turn — the interim step of a
tool round, then its continuation — gets a NodeSpeech in playback order.
The LLM thread feeds it text; the worker takes the next chunk whenever it
is free (sized by SpeechSchedule: the time left before the browser's
queue runs out), synthesizes it and stores a completed TTSChunk row, which
the existing /tts-stream SSE delivers. When a node's text is complete and
the node is committed, its chunks are joined into tts.mp3 exactly as the
batch task does.

Nothing about a node's TTS is written by the LLM thread after it opens
the node ('processing'); from then on the worker owns the tts fields.
"""
import logging
import os
import pathlib
import threading
import time
from collections import deque
from datetime import datetime

from backend.extensions import db
from backend.models import Node, TTSChunk
from backend.utils.encryption import encrypt_file
from backend.utils.tts_stream_text import (
    ChunkPlanner, SpeechSchedule, SpokenTextProjector)

logger = logging.getLogger(__name__)

# Upper bound on waiting for the worker at the end of a turn: the tail of
# a long reply is minutes of TTS, never this.
FINISH_TIMEOUT_SECS = 30 * 60


class OpenAITTSAudio:
    """The real audio side: OpenAI TTS to MP3, pydub for durations and
    the joined file. Swapped for a fake in tests."""

    def __init__(self, api_key):
        from openai import OpenAI
        self._client = OpenAI(api_key=api_key)

    def synthesize(self, text, path, section_end):
        from backend.tasks.tts import synthesize_to_file
        return synthesize_to_file(self._client, text, path, section_end)

    @staticmethod
    def duration(segment):
        return len(segment) / 1000.0

    @staticmethod
    def join(segments, path):
        combined = segments[0]
        for segment in segments[1:]:
            combined += segment
        combined.export(str(path), format="mp3")


class NodeSpeech:
    """One node's reply as it is written: fed by the LLM thread, drained
    by the turn's worker. All state is guarded by the turn's lock."""

    def __init__(self, turn, node):
        self._turn = turn
        self.node_id = node.id
        self.dir_rel = pathlib.Path(f"user/{node.user_id}/node/{node.id}")
        self.projector = SpokenTextProjector(proposals=True)
        self.planner = ChunkPlanner()
        self.released = False   # the node is committed (links included)
        self.dropped = False    # stop: failed, deleted or abandoned
        self.next_index = 0
        self.cache_bust = None
        self.segments = []
        self.durations = []

    # ── called by the LLM thread ────────────────────────────────────────
    def feed(self, text):
        with self._turn.cv:
            if not self.planner.closed:
                self.planner.add(self.projector.feed(text))
                self._turn.cv.notify_all()

    def close(self, extra_text=None):
        """The node's text is complete (*extra_text*: text the reply's
        own stream didn't carry, e.g. a tool round's fallback line)."""
        with self._turn.cv:
            if self.planner.closed:
                return
            if extra_text:
                self.planner.add(self.projector.feed(extra_text))
            self.planner.add(self.projector.close())
            self.planner.close()
            self._turn.cv.notify_all()

    def release(self):
        """The node is committed: once its chunks are done, its TTS can
        be completed (the SSE's all_complete then carries the node's
        continuation link)."""
        with self._turn.cv:
            self.close()
            self.released = True
            self._turn.cv.notify_all()

    def restart(self):
        """The model call is starting over. Refused once a chunk has been
        cut: that text may already be playing."""
        with self._turn.cv:
            if self.planner.cut or self.planner.closed:
                return False
            self.projector = SpokenTextProjector(proposals=True)
            self.planner = ChunkPlanner()
            return True


class VoiceTTSStream:
    """One voice turn's TTS worker (see module docstring).

    ``threaded=False`` runs the worker in the caller's thread at finish()
    — used by tests, whose in-memory database a second thread can't see."""

    def __init__(self, app, user_id, audio_root, audio=None,
                 threaded=True, clock=time.monotonic):
        self._app = app
        self._user_id = user_id
        self._audio_root = pathlib.Path(audio_root)
        self._audio = audio
        self._clock = clock
        self.cv = threading.Condition()
        self._nodes = deque()
        self._by_id = {}
        self._schedule = SpeechSchedule()
        self._turn_closed = False
        self._thread = None
        if threaded:
            self._thread = threading.Thread(
                target=self._run_in_app, daemon=True,
                name=f"voice-tts-{user_id}")
            self._thread.start()

    # ── called by the LLM thread ────────────────────────────────────────
    def open_node(self, node):
        """Start speaking *node*; its tts_task_status must already be
        committed as 'processing' (the SSE refuses otherwise)."""
        speech = NodeSpeech(self, node)
        with self.cv:
            self._nodes.append(speech)
            self._by_id[node.id] = speech
            self.cv.notify_all()
        return speech

    def node(self, node_id):
        return self._by_id.get(node_id)

    def abort(self):
        """The turn failed: stop after the current chunk. Nodes whose TTS
        isn't complete are marked failed."""
        with self.cv:
            for speech in self._nodes:
                speech.dropped = True
            self._turn_closed = True
            self.cv.notify_all()

    def finish(self):
        """The turn is over: wait for the remaining audio. A node never
        released (the turn ended early, e.g. a deleted node) is dropped."""
        with self.cv:
            for speech in self._nodes:
                if not speech.released:
                    speech.dropped = True
            self._turn_closed = True
            self.cv.notify_all()
        if self._thread is None:
            self._run()
            return
        self._thread.join(FINISH_TIMEOUT_SECS)
        if self._thread.is_alive():
            logger.error("Voice TTS worker still running after %ss",
                         FINISH_TIMEOUT_SECS)

    # ── the worker ──────────────────────────────────────────────────────
    def _run_in_app(self):
        with self._app.app_context():
            try:
                self._run()
            except Exception:
                logger.exception("Voice TTS worker crashed")
                self._fail_open_nodes()
            finally:
                db.session.remove()

    def _next_job(self):
        """Under the lock: the next thing to do, waiting until there is
        one. ('chunk', speech, index, chunk) | ('finish', speech) |
        ('drop', speech) | None when the turn is done."""
        while True:
            speech = self._nodes[0] if self._nodes else None
            if speech is None:
                if self._turn_closed:
                    return None
                self.cv.wait()
                continue
            if speech.dropped:
                self._nodes.popleft()
                return ("drop", speech)
            now = self._clock()
            limit, jit, wake_at = self._schedule.plan(
                now, speech.planner.pending_chars)
            chunk = speech.planner.take(limit, jit)
            if chunk is not None:
                index = speech.next_index
                speech.next_index += 1
                return ("chunk", speech, index, chunk)
            if speech.planner.done and speech.released:
                self._nodes.popleft()
                return ("finish", speech)
            if self._thread is None:
                # Inline (tests): everything was fed before finish().
                speech.close()
                speech.released = True
                continue
            self.cv.wait(None if wake_at is None
                         else max(0.05, wake_at - now))

    def _run(self):
        while True:
            with self.cv:
                job = self._next_job()
            if job is None:
                return
            kind, speech = job[0], job[1]
            try:
                if kind == "chunk":
                    self._speak(speech, job[2], job[3])
                elif kind == "finish":
                    self._finish_node(speech)
                else:
                    self._mark_failed(speech.node_id)
            except Exception:
                logger.exception("Voice TTS failed for node %s",
                                 speech.node_id)
                db.session.rollback()
                with self.cv:
                    speech.dropped = True
                    if speech in self._nodes:
                        self._nodes.remove(speech)
                self._mark_failed(speech.node_id)

    def _speak(self, speech, index, chunk):
        node = db.session.get(Node, speech.node_id)
        if node is None or node.deleted_at is not None:
            raise RuntimeError("node deleted mid-generation")
        row = TTSChunk(node_id=speech.node_id, chunk_index=index,
                       section_index=chunk.section_index,
                       section_title=(chunk.section_title or None)
                       and chunk.section_title[:256],
                       status="processing")
        db.session.add(row)
        db.session.commit()
        if speech.cache_bust is None:
            # The media path is fixed per node; a per-run token keeps the
            # browser from replaying a cached older file (#66).
            speech.cache_bust = row.id
        target_dir = self._audio_root / speech.dir_rel
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"tts_chunk_{index}.mp3"
        segment = self._audio.synthesize(chunk.text, path, chunk.section_end)
        duration = self._audio.duration(segment)
        encrypt_file(str(path))
        row.audio_url = self._media_url(speech, path.name)
        row.duration = duration
        row.status = "completed"
        row.completed_at = datetime.utcnow()
        db.session.commit()
        speech.segments.append(segment)
        speech.durations.append(duration)
        with self.cv:
            self._schedule.played(duration, self._clock())
        logger.info("Voice TTS node %s chunk %s: %s chars, %.1fs",
                    speech.node_id, index, len(chunk.text), duration)

    def _finish_node(self, speech):
        from backend.tasks.tts import log_tts_cost
        node = db.session.get(Node, speech.node_id)
        if node is None:
            return
        if speech.segments:
            path = self._audio_root / speech.dir_rel / "tts.mp3"
            self._audio.join(speech.segments, path)
            encrypt_file(str(path))
            node.audio_tts_url = self._media_url(speech, path.name)
            node.audio_mime_type = "audio/mpeg"
            log_tts_cost(self._user_id, sum(speech.durations))
        node.tts_task_status = "completed"
        node.tts_task_progress = 100
        db.session.commit()
        speech.segments = []

    def _mark_failed(self, node_id):
        node = db.session.get(Node, node_id)
        if node is not None and node.tts_task_status != "completed":
            node.tts_task_status = "failed"
            db.session.commit()

    def _fail_open_nodes(self):
        with self.cv:
            open_ids = [s.node_id for s in self._nodes]
            self._nodes.clear()
        for node_id in open_ids:
            try:
                self._mark_failed(node_id)
            except Exception:
                db.session.rollback()

    def _media_url(self, speech, filename):
        return f"/media/{(speech.dir_rel / filename).as_posix()}" \
               f"?v={speech.cache_bust}"


def audio_root_path():
    """Where node audio lives (same setting as the batch TTS task)."""
    return pathlib.Path(
        os.environ.get("AUDIO_STORAGE_PATH", "data/audio")).resolve()

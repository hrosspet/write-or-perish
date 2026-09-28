"""Where a voice turn's wait goes (#371 step 0).

The wait from the end of a recording to the first audio crosses the
browser, the web server and two Celery tasks. Each process marks the
moments it sees with a wall-clock time, under the id of the turn's first
reply node:

- a log line, ``voice-timing node=<id> stage=<name> t=<epoch secs>``;
- a Redis hash, so that the web server can join the backend's marks with
  the ones the browser sends (clock-corrected) once the first audio
  plays. The browser's report also adds the node to the user's list of
  recent turns, which GET /api/voice/timing returns with the median of
  each stage. (Redis, not the log, is what joins the processes: the
  production web server keeps no application log.)

Only the first mark of a stage counts. Timing never breaks a turn: a
Redis failure is logged and ignored.
"""
import logging
import time

import redis
from flask import current_app

logger = logging.getLogger(__name__)

TTL_SECONDS = 7 * 24 * 3600
_PREFIX = "voice_timing:"
_USER_PREFIX = "voice_timing_user:"
# How many of a user's turns are kept in their list of recent turns.
RECENT_TURNS = 50

# The stages, in the order a turn passes them: (name, from, to). The
# letters are the ones #371 uses; a1-a3 and b1-b2 split a and b.
STAGES = (
    ("a", "rec_stop", "llm_task_start"),
    ("a1", "rec_stop", "finalize_start"),
    ("a2", "finalize_start", "transcribed"),
    ("a3", "transcribed", "llm_task_start"),
    ("b", "llm_task_start", "first_text"),
    ("b1", "llm_task_start", "llm_request"),
    ("b2", "llm_request", "first_text"),
    ("c", "first_text", "chunk_cut"),
    ("d", "chunk_cut", "tts_first_byte"),
    ("e", "tts_first_byte", "tts_last_byte"),
    ("f", "tts_last_byte", "chunk_published"),
    ("g", "chunk_published", "chunk_ready"),
    ("h", "chunk_ready", "playing"),
    ("total", "rec_stop", "playing"),
)

# Marks the browser may send. finalize_acked, llm_node_known and
# tts_attach are not stage boundaries; they show where inside a and g the
# browser was waiting.
BROWSER_MARKS = frozenset({
    "rec_stop", "finalize_acked", "llm_node_known", "tts_attach",
    "chunk_ready", "playing",
})

_client = None


def _redis():
    global _client
    try:
        config = current_app.config
    except RuntimeError:  # outside an app context
        return None
    if config.get("TESTING"):
        return None
    if _client is None:
        url = config.get("CELERY_BROKER_URL")
        if not url:
            return None
        _client = redis.Redis.from_url(
            url, socket_timeout=0.5, socket_connect_timeout=0.5,
            decode_responses=True)
    return _client


def mark(node_id, stage, t=None, **facts):
    """Record that *node_id*'s turn reached *stage* at *t* (epoch
    seconds; now by default). *facts* are stored with it (e.g. the first
    chunk's length)."""
    if node_id is None:
        return
    t = time.time() if t is None else t
    extra = "".join(f" {k}={v}" for k, v in facts.items())
    logger.info("voice-timing node=%s stage=%s t=%.3f%s",
                node_id, stage, t, extra)
    _write(node_id, {f"t:{stage}": f"{t:.3f}"}, facts)


def note(node_id, **facts):
    """Store *facts* with *node_id*'s turn, with no mark."""
    if facts:
        _write(node_id, {}, facts)


def _write(node_id, marks, facts):
    """One round trip: *marks* only if not set yet, *facts* always."""
    try:
        r = _redis()
        if r is None:
            return
        key = f"{_PREFIX}{node_id}"
        pipe = r.pipeline(transaction=False)
        for field, value in marks.items():
            pipe.hsetnx(key, field, value)
        if facts:
            pipe.hset(key, mapping={f"x:{k}": str(v)
                                    for k, v in facts.items()})
        pipe.expire(key, TTL_SECONDS)
        pipe.execute()
    except Exception:  # timing never breaks a turn
        logger.warning("voice-timing node=%s: Redis write failed", node_id,
                       exc_info=True)


def stage_seconds(marks):
    """{stage: seconds} for every stage whose two marks exist."""
    out = {}
    for name, start, end in STAGES:
        if start in marks and end in marks:
            out[name] = round(marks[end] - marks[start], 3)
    return out


def record(node_id):
    """The turn's marks, facts and stage lengths so far."""
    marks, facts = {}, {}
    try:
        r = _redis()
        raw = r.hgetall(f"{_PREFIX}{node_id}") if r is not None else {}
    except Exception:  # timing never breaks a turn
        logger.warning("voice-timing node=%s: Redis read failed", node_id,
                       exc_info=True)
        raw = {}
    for field, value in raw.items():
        kind, _, name = field.partition(":")
        if kind == "t":
            try:
                marks[name] = float(value)
            except ValueError:
                continue
        elif kind == "x":
            facts[name] = value
    return {"node_id": node_id, "marks": marks, "facts": facts,
            "stages": stage_seconds(marks)}


def log_summary(rec):
    """One line with every stage, for grepping a turn out of the log."""
    stages = " ".join(f"{k}={v:.2f}" for k, v in rec["stages"].items())
    facts = " ".join(f"{k}={v}" for k, v in sorted(rec["facts"].items()))
    logger.info("voice-timing node=%s summary %s %s",
                rec["node_id"], stages, facts)


def remember_turn(user_id, node_id):
    """Add *node_id* to *user_id*'s recent turns (newest last)."""
    try:
        r = _redis()
        if r is None:
            return
        key = f"{_USER_PREFIX}{user_id}"
        r.zadd(key, {str(node_id): time.time()})
        r.zremrangebyrank(key, 0, -RECENT_TURNS - 1)
        r.expire(key, TTL_SECONDS)
    except Exception:  # timing never breaks a turn
        logger.warning("voice-timing user=%s: Redis write failed", user_id,
                       exc_info=True)


def recent_turns(user_id, limit=RECENT_TURNS):
    """*user_id*'s recent turn node ids, newest first."""
    try:
        r = _redis()
        if r is None:
            return []
        ids = r.zrevrange(f"{_USER_PREFIX}{user_id}", 0, limit - 1)
    except Exception:  # timing never breaks a turn
        logger.warning("voice-timing user=%s: Redis read failed", user_id,
                       exc_info=True)
        return []
    return [int(i) for i in ids]


def medians(records):
    """{stage: median seconds} over the records that have the stage."""
    out = {}
    for name, _start, _end in STAGES:
        values = sorted(r["stages"][name] for r in records
                        if name in r["stages"])
        if not values:
            continue
        mid = len(values) // 2
        out[name] = (values[mid] if len(values) % 2
                     else round((values[mid - 1] + values[mid]) / 2, 3))
    return out

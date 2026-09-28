"""The text side of speaking a voice reply while it is written (#367):
SpokenTextProjector (what is spoken), ChunkPlanner (where chunks end) and
SpeechSchedule (how big, when)."""
import random

import pytest

from backend.utils.audio_processing import section_aware_chunk_text
from backend.utils.tts_stream_text import (
    FIRST_CHUNK_CHARS, O, P, R, ChunkPlanner, SpeechSchedule,
    SpokenTextProjector)


def _project(pieces, proposals=True):
    proj = SpokenTextProjector(proposals=proposals)
    events = []
    for piece in pieces:
        events += proj.feed(piece)
    events += proj.close()
    return events


def _flatten(events):
    """Events as one comparable string (headings marked)."""
    out = []
    for kind, value in events:
        out.append(value if kind == "text" else f"\n<H:{value}>\n")
    return "".join(out)


def _spoken(text, proposals=True):
    return _flatten(_project([text], proposals))


def _random_splits(text, rng):
    cuts = sorted(rng.sample(range(1, len(text)), min(len(text) - 1,
                                                       rng.randint(1, 12))))
    return [text[a:b] for a, b in zip([0] + cuts, cuts + [len(text)])]


FIXTURES = [
    "[2026-09-28 10:00 UTC] Hello there. How are you?",
    "  [2026-09-28 10:00 CEST]\n[unknown time]\n# Title\nBody text.",
    "It ends with a stamp. [2026-09-28 10:00 UTC]",
    "A stamp [2026-09-28 10:00 UTC] in the middle stays.",
    "See {quote:A} and {quote_ext:12} here, {quote:345} too.",
    "Not a marker {curly} or {quote without end",
    "Intro.\n\n:::share insight\nThe shared piece.\n### Its heading\n:::\n\nOutro.",
    "Lead-in.\n:::share need\nUnclosed piece runs to the end.",
    "Here are your updates.\n\n### Completed\n- one\n### New Tasks\n- two\n"
    "### Priority Order\n1. two\n### Note\nKeep going!",
    "I'd file this.\n\n### Issue Title\nBug\n### Description\nIt breaks.\n"
    "### Category\nbug\nLet me know if that works.",
    "#hashtag at the start is text.\n#### h4 is text too\n### h3 plain",
    "# One\n# Two\nOnly two has a body.\n## Three\nAnd three.",
    "Preamble first.\n\n# Chapter\nChapter body. Second sentence!",
    "Windows\r\nline endings.\r\n# Heading\r\nBody.\r\n",
]


@pytest.mark.parametrize("text", FIXTURES)
def test_split_invariance(text):
    """However the deltas are cut, the spoken text is the same."""
    whole = _spoken(text)
    rng = random.Random(len(text))
    for _ in range(40):
        assert _flatten(_project(_random_splits(text, rng))) == whole
    assert _flatten(_project(list(text))) == whole


def test_edge_stamps():
    assert _spoken(FIXTURES[0]).strip() == "Hello there. How are you?"
    assert _spoken(FIXTURES[1]) == "\n<H:Title>\nBody text."
    assert _spoken(FIXTURES[2]).strip() == "It ends with a stamp."
    # Only edges are scrubbed, as strip_edge_timestamps does.
    assert "[2026-09-28 10:00 UTC]" in _spoken(FIXTURES[3])


def test_trailing_stamp_is_held_until_decided():
    proj = SpokenTextProjector()
    assert _flatten(proj.feed("Bye. [2026-09-28 10:00")) == "Bye. "
    assert _flatten(proj.feed(" UTC]")) == ""
    assert _flatten(proj.feed(" and more")) == \
        "[2026-09-28 10:00 UTC] and more"


def test_quote_markers_dropped_even_split_mid_token():
    proj = SpokenTextProjector()
    out = _flatten(proj.feed("See {quo"))
    assert out == "See "
    out += _flatten(proj.feed("te:A} and {quote_ext:1"))
    out += _flatten(proj.feed("2} here."))
    out += _flatten(proj.close())
    assert "{" not in out and "quote" not in out
    assert "{curly}" in _spoken(FIXTURES[5])


def test_share_fences_never_spoken():
    spoken = _spoken(FIXTURES[6])
    assert "Intro." in spoken and "Outro." in spoken
    assert "shared piece" not in spoken and "share" not in spoken
    assert "Its heading" not in spoken
    unclosed = _spoken(FIXTURES[7])
    assert "Lead-in." in unclosed and "Unclosed" not in unclosed
    # Also without proposal handling (a share fence is never spoken).
    assert "shared piece" not in _spoken(FIXTURES[6], proposals=False)


def test_todo_proposal_speaks_intro_and_note_only():
    spoken = _spoken(FIXTURES[8])
    assert "Here are your updates." in spoken and "Keep going!" in spoken
    for hidden in ("one", "two", "Completed", "Priority"):
        assert hidden not in spoken


def test_issue_proposal_speaks_trailing_commentary():
    spoken = _spoken(FIXTURES[9])
    assert "I'd file this." in spoken
    assert "Let me know if that works." in spoken
    for hidden in ("Bug", "It breaks", "bug\n"):
        assert hidden not in spoken


def test_plain_headings_and_hashtags():
    spoken = _spoken(FIXTURES[10])
    assert "#hashtag at the start is text." in spoken
    assert "#### h4 is text too" in spoken
    assert "### h3 plain" in spoken   # no proposal heading: spoken


# ── ChunkPlanner ─────────────────────────────────────────────────────────

def _plan_all(text, limit=10_000):
    planner = ChunkPlanner()
    planner.add(_project([text]))
    planner.close()
    chunks = []
    while (c := planner.take(limit)) is not None:
        chunks.append(c)
    assert planner.done
    return chunks


def test_sections_titles_and_empty_sections():
    chunks = _plan_all(FIXTURES[11])
    # "# One" has no body: no chunk, but it takes index 0 (as batch).
    assert [(c.section_title, c.section_index) for c in chunks] == [
        ("Two", 1), ("Three", 2)]
    assert chunks[0].text.startswith("Two.\n\nOnly two has a body.")
    assert [c.section_end for c in chunks] == [True, False]


def test_section_end_is_known_when_the_chunk_is_cut():
    planner = ChunkPlanner()
    proj = SpokenTextProjector()
    planner.add(proj.feed("First sentence here. Second one."))
    # Only the first boundary is confirmed; the one after "Second one."
    # has nothing after it yet, and a heading may follow.
    first = planner.take(1000, jit=True)
    assert first.text == "First sentence here." and not first.section_end
    assert planner.take(1000, jit=True) is None
    planner.add(proj.feed("\n# Ne"))            # heading not decided yet
    assert planner.take(1000, jit=True) is None
    planner.add(proj.feed("xt\nBody."))
    second = planner.take(1000)
    assert second.text == "Second one." and second.section_end is True
    planner.add(proj.close())
    planner.close()
    last = planner.take(1000)
    assert last.text == "Next.\n\nBody." and last.section_end is False


def test_open_section_waits_for_the_limit_or_jit():
    planner = ChunkPlanner()
    planner.add(SpokenTextProjector().feed(
        "Short one. Another sentence follows"))
    assert planner.take(1000) is None            # under the limit
    chunk = planner.take(1000, jit=True)         # queue about to run out
    assert chunk.text == "Short one."
    assert planner.take(1000, jit=True) is None  # no confirmed boundary


def _batch_parity(text):
    """Drive planner + schedule on a simulated clock with the whole text
    at t=0; returns (chunks, stalled)."""
    planner = ChunkPlanner()
    planner.add(_project([text], proposals=False))
    planner.close()
    schedule, now, out, stalled = SpeechSchedule(), 0.0, [], False
    while True:
        limit, jit, _ = schedule.plan(now, planner.pending_chars)
        chunk = planner.take(limit, jit)
        if chunk is None:
            assert planner.done
            return out, stalled
        now += O + len(chunk.text) / R
        if schedule.drain_at is not None and now > schedule.drain_at:
            stalled = True
        schedule.played(len(chunk.text) * P, now)
        out.append((chunk.text, chunk.section_title, chunk.section_index))


def _prose(rng, words):
    vocab = "the a loore voice chunk reply model audio user text".split()
    out, sent = [], []
    for _ in range(words):
        sent.append(rng.choice(vocab))
        if len(sent) > rng.randint(6, 22):
            out.append(" ".join(sent).capitalize() + rng.choice(".!?"))
            sent = []
    if sent:
        out.append(" ".join(sent).capitalize() + ".")
    return " ".join(out)


def test_parity_with_batch_chunking():
    rng = random.Random(7)
    compared = 0
    for _ in range(300):
        parts = []
        if rng.random() < 0.7:
            parts.append(_prose(rng, rng.randint(3, 400)))
        for h in range(rng.randint(0, 5)):
            parts.append(f"{'#' * rng.randint(1, 3)} Heading {h}")
            if rng.random() < 0.9:
                parts.append(_prose(rng, rng.randint(1, 900)))
        text = "\n\n".join(parts)
        streamed, stalled = _batch_parity(text)
        if stalled:
            continue   # re-anchoring after a stall differs, by design
        assert streamed == section_aware_chunk_text(text)
        compared += 1
    assert compared > 250


def test_schedule_cold_then_warm():
    schedule = SpeechSchedule()
    assert schedule.plan(0.0, 5000) == (FIRST_CHUNK_CHARS, False, None)
    schedule.played(20.0, 10.0)            # 20 s of audio at t=10
    limit, jit, wake_at = schedule.plan(10.0, 100)
    assert limit == int((20.0 - O) * R) and jit is False
    assert wake_at == pytest.approx(30.0 - O - 100 / R - 1.0)
    assert schedule.plan(wake_at + 0.1, 100)[1] is True
    # Nothing can land before the queue runs out: cold again.
    assert schedule.plan(29.0, 100)[0] == FIRST_CHUNK_CHARS
    # After a stall playback resumes when the next chunk lands.
    schedule.played(5.0, 40.0)
    assert schedule.drain_at == 45.0

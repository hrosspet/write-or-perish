"""Listen speaks links as their text and addresses short (#461).

speak_links is what the batch TTS task sends; LinkSpeaker (inside
SpokenTextProjector) is the same thing while a reply streams. Both must
give the same spoken text however the stream is cut, and the chapters
(sections, titles, chunk boundaries) must stay as batch makes them.
"""
import random
import time

import pytest

from backend.utils.audio_processing import section_aware_chunk_text
from backend.utils.spoken_links import (
    CODE_BLOCK_SPOKEN, MAX_ADDRESS, MAX_BRACKET_DEPTH, MAX_LINE_HEAD,
    MAX_LINK_SOURCE, MAX_LINK_TEXT, LinkSpeaker, speak_address, speak_links)
from backend.utils.tts_stream_text import (
    AUDIO_PER_CHAR, GEN_RATE, OVERHEAD, ChunkPlanner, SpeechSchedule,
    SpokenTextProjector)

PR = "https://github.com/hrosspet/write-or-perish/pull/460"
ISSUE = "https://github.com/hrosspet/write-or-perish/issues/423"


# ── What is spoken ──────────────────────────────────────────────────────

@pytest.mark.parametrize("text, spoken", [
    # Markdown links: their text.
    (f"Merged [459]({PR[:-3]}459) today.", "Merged 459 today."),
    ("See [the docs](https://example.com/a/b \"Docs\") first.",
     "See the docs first."),
    ("[a [nested] text](https://n.com) ok", "a [nested] text ok"),
    ("[x](<https://a.com/with space>) y", "x y"),
    # Images: alt text, or nothing.
    ("A ![a cat](https://x.com/cat.png) here.", "A a cat here."),
    ("A ![](https://x.com/cat.png) here.", "A  here."),
    ("[![badge](https://img.shields.io/x)](https://ci.com)", "badge"),
    # Bare addresses: GitHub PR / issue by number, else the domain.
    (PR, "PR 460"),
    (f"{ISSUE}#issuecomment-1 is it.", "issue 423 is it."),
    (f"{PR}/files", "PR 460"),
    ("Read https://www.example.com/a/b?c=d, then rest.",
     "Read a link to example.com, then rest."),
    ("Try www.Example.com/path.", "Try a link to example.com."),
    ("http://localhost:3001/x", "a link to localhost"),
    ("(see https://en.wikipedia.org/wiki/Foo_(bar)) and (https://x.com/a).",
     "(see a link to en.wikipedia.org) and (a link to x.com)."),
    ("**https://x.com/a**", "**a link to x.com**"),
    # Autolinks: like bare addresses.
    ("Autolink <https://example.org/x/y> here.",
     "Autolink a link to example.org here."),
    (f"<{PR}>", "PR 460"),
    # A link whose text is its own address reads as the address.
    (f"[{PR}]({PR})", "PR 460"),
    ("[github.com/o/r/pull/5](https://github.com/o/r/pull/5)", "PR 5"),
    ("[README.md](README.md) and [backend/tasks/tts.py](backend/tasks/tts.py)",
     "README.md and backend/tasks/tts.py"),
    # Not links: left alone.
    ("[not a link] (x) and [2026-09-28 10:00 UTC] stays.",
     "[not a link] (x) and [2026-09-28 10:00 UTC] stays."),
    ("xwww.foo.com and showhttps://a.com", "xwww.foo.com and showhttps://a.com"),
    ("Mail me@www.site.com", "Mail me@www.site.com"),
    ("www. alone and https:// alone", "www. alone and https:// alone"),
    ("<b>bold</b> and <mailto:a@b.c>", "<b>bold</b> and <mailto:a@b.c>"),
    ("A note [unclosed (https://a.com/b",
     "A note [unclosed (a link to a.com"),
])
def test_spoken(text, spoken):
    assert speak_links(text) == spoken


def test_inline_code_is_read_as_written_and_a_code_block_is_one_phrase():
    """#646: a fenced code block is spoken as CODE_BLOCK_SPOKEN, once,
    nothing of its content; inline code is read as written."""
    text = ("Inline `[x](https://a.com)` and ``a ` [y](https://b.com)`` "
            "then [z](https://c.com).\n"
            "```python\n[x](https://a.com) https://b.com\n# comment\n"
            "def f():\n    return 1\n```\n"
            "~~~\nhttps://d.com\n\n~~~\n"
            "after [y](https://c.com)")
    assert speak_links(text) == (
        "Inline `[x](https://a.com)` and ``a ` [y](https://b.com)`` "
        "then z.\n"
        f"{CODE_BLOCK_SPOKEN}\n"
        f"{CODE_BLOCK_SPOKEN}\n"
        "after y")


def test_unclosed_code_block_runs_to_the_end():
    text = "Before [a](https://a.com).\n```\n[b](https://b.com)\n"
    assert speak_links(text) == f"Before a.\n{CODE_BLOCK_SPOKEN}\n"


@pytest.mark.parametrize("text, spoken", [
    # In a list item, indented by 3, 4 or more spaces, or a tab; the
    # closing fence indented as the opening one.
    ("1. Install:\n   ```bash\n   pip install x\n   ```\n2. Run it.",
     f"1. Install:\n{CODE_BLOCK_SPOKEN}\n2. Run it."),
    ("10. Install:\n    ```bash\n    pip install x\n    ```\n11. Run it.",
     f"10. Install:\n{CODE_BLOCK_SPOKEN}\n11. Run it."),
    ("- a\n    - b\n        ```\n        x = 1\n        ```\n    - c",
     f"- a\n    - b\n{CODE_BLOCK_SPOKEN}\n    - c"),
    ("Run:\n\t```\n\tls\n\t```\nDone.", f"Run:\n{CODE_BLOCK_SPOKEN}\nDone."),
    # A closing run must be as long as the opening one, of the same
    # character, with nothing after it but spaces.
    ("````md\n```\nnot closed\n```\n````\nAfter.",
     f"{CODE_BLOCK_SPOKEN}\nAfter."),
    ("~~~\n```\n~~~\nAfter.", f"{CODE_BLOCK_SPOKEN}\nAfter."),
    ("```\nx\n``` not a close\n```  \nAfter.",
     f"{CODE_BLOCK_SPOKEN}\nAfter."),
    # Each block once; the text between them is spoken.
    ("A:\n```\none\n```\nB:\n```\ntwo\n```\n",
     f"A:\n{CODE_BLOCK_SPOKEN}\nB:\n{CODE_BLOCK_SPOKEN}\n"),
    # Not a fence: backticks in the info string (inline code), or fewer
    # than three. Controls: read as before #646.
    ("```js``` is inline code.", "```js``` is inline code."),
    ("``\nnot code\n``", "``\nnot code\n``"),
])
def test_code_blocks(text, spoken):
    assert speak_links(text) == spoken
    assert "".join(_stream(list(text))) == spoken


def test_chapter_heading_lines():
    assert speak_links(f"## Fix [459]({PR})\nBody.") == "## Fix 459\nBody."
    assert speak_links(f"# {PR}\nBody.") == "# PR 460\nBody."
    # A heading that would be left empty stays as written.
    assert speak_links("## ![](https://x.com/y.png)\nBody.") == \
        "## ![](https://x.com/y.png)\nBody."
    # h3 and deeper are ordinary lines.
    assert speak_links(f"### See [it]({PR})") == "### See it"


def test_speak_address():
    assert speak_address("https://GitHub.com/o/r/pull/7/") == "PR 7"
    assert speak_address("https://github.com/o/r/pulls") == \
        "a link to github.com"
    assert speak_address("https://user:pw@www.Site.org:8080/x") == \
        "a link to site.org"


# ── The same while streaming ────────────────────────────────────────────

def _stream(pieces):
    speaker = LinkSpeaker()
    out = [speaker.feed(p) for p in pieces]
    out.append(speaker.close())
    return out


STREAM_FIXTURES = [
    f"Merged [459]({PR[:-3]}459) and [460]({PR}) today.",
    f"PRs:\n{PR}\n{ISSUE}\nhttps://www.example.com/a/b, done.",
    "Image ![alt](https://x.com/i.png) and ![](https://x.com/j.png).",
    "Autolink <https://example.org/x> and <b>tag</b> and a < b.",
    "Code `[x](https://a.com)` and\n```\nhttps://b.com\n```\n[y](https://c.com)",
    "[a [nested] text](https://n.com 'title') and \\[esc\\](https://e.com)",
    "unclosed [text (https://a.com/b) and h w ht www",
    "Wow![x](https://x.com) hi! [2026-09-28 10:00 UTC] [y]",
    # A fence whose opening line arrives in pieces (found by the fuzz test).
    "~~~``~~~\n (www.```http://<@ ",
    "~~~ \nhttps://in.code\n~~~\nhttps://out.side",
]

_FUZZ_PIECES = [
    "[", "]", "(", ")", "!", "<", ">", "`", "``", "```", "~~~", "\\", " ",
    "\n", "\t", "https://", "http://", "www.", "h", "w", "x", ".", ",", "#",
    "## ", "# ", "github.com/o/r/pull/5", "a.com", "\"t\"", "'", "@", "/",
    "\r\n", "   ```"]


def test_fuzz_streaming_matches_batch():
    """Odd mixes of link syntax, cut at random points and char by char."""
    rng = random.Random(4611)
    for _ in range(3000):
        text = "".join(rng.choice(_FUZZ_PIECES)
                       for _ in range(rng.randint(2, 40)))
        whole = speak_links(text)
        cuts = sorted(rng.sample(range(1, len(text)),
                                 min(len(text) - 1, rng.randint(1, 8))))
        pieces = [text[a:b] for a, b in zip([0] + cuts, cuts + [len(text)])]
        assert "".join(_stream(pieces)) == whole, (text, pieces)
        assert "".join(_stream(list(text))) == whole, text


@pytest.mark.parametrize("text", STREAM_FIXTURES)
def test_every_two_piece_split_matches_batch(text):
    whole = speak_links(text)
    for cut in range(1, len(text)):
        assert "".join(_stream([text[:cut], text[cut:]])) == whole, cut
    assert "".join(_stream(list(text))) == whole


def test_a_split_link_is_held_until_complete():
    """Split inside the link text, at the ](, and inside the address:
    nothing of the link is spoken until it is decided, and the address
    never is."""
    speaker = LinkSpeaker()
    assert speaker.feed("Merged [45") == "Merged "
    assert speaker.feed("9]") == ""
    assert speaker.feed("(https://github.com/hrosspet/wr") == ""
    assert speaker.feed("ite-or-perish/pull/459") == ""
    assert speaker.feed(") today") == "459 today"
    assert speaker.close() == ""


def test_a_split_bare_address_is_held_until_it_ends():
    speaker = LinkSpeaker()
    assert speaker.feed("See htt") == "See "
    assert speaker.feed("ps://www.exa") == ""
    assert speaker.feed("mple.com/a/b") == ""
    assert speaker.feed(". Next") == "a link to example.com. Next"
    speaker = LinkSpeaker()
    assert speaker.feed("Ends with https://x.com/q") == "Ends with "
    assert speaker.close() == "a link to x.com"


def test_text_that_only_looks_like_a_link_start_is_released():
    speaker = LinkSpeaker()
    assert speaker.feed("[note] then") == "[note] then"
    assert speaker.feed(" the word ") == " the word "
    assert speaker.feed("<b") == "<b"


# ── Through the projector and the chunk planner ─────────────────────────

def _project(pieces, proposals=True):
    proj = SpokenTextProjector(proposals=proposals)
    events = []
    for piece in pieces:
        events += proj.feed(piece)
    events += proj.close()
    return events


def _flatten(events):
    return "".join(v if k == "text" else f"\n<H:{v}>\n" for k, v in events)


def _random_splits(text, rng):
    count = min(len(text) - 1, rng.randint(1, 30))
    cuts = sorted(rng.sample(range(1, len(text)), count))
    return [text[a:b] for a, b in zip([0] + cuts, cuts + [len(text)])]


def test_projector_speaks_links_in_text_and_headings():
    text = (f"Intro with [459]({PR[:-3]}459).\n\n"
            f"## Fix [460]({PR})\nBody {ISSUE}.\n\n"
            "# Plain\nSee <https://example.com/x>.")
    events = _project([text])
    assert ("heading", "Fix 460") in events
    assert ("heading", "Plain") in events
    spoken = _flatten(events)
    assert "https" not in spoken and "github" not in spoken
    assert "Intro with 459." in spoken
    assert "Body issue 423." in spoken
    assert "See a link to example.com." in spoken
    rng = random.Random(461)
    for _ in range(60):
        assert _flatten(_project(_random_splits(text, rng))) == spoken
    assert _flatten(_project(list(text))) == spoken


def test_proposal_reply_links():
    text = (f"I'd file this, see [the PR]({PR}).\n\n"
            "### Issue Title\nBug\n### Description\nIt breaks.\n"
            f"### Category\nbug\nMore at {ISSUE}.")
    spoken = _flatten(_project([text]))
    assert "I'd file this, see the PR." in spoken
    assert "More at issue 423." in spoken
    assert "It breaks" not in spoken


def _plan(events):
    """Planner + schedule on a simulated clock with the whole text at
    t=0, as test_tts_stream_text._batch_parity; returns (chunks, stalled)."""
    planner = ChunkPlanner()
    planner.add(events)
    planner.close()
    schedule, now, out, stalled = SpeechSchedule(), 0.0, [], False
    while True:
        limit, jit, _ = schedule.plan(now, planner.pending_chars)
        chunk = planner.take(limit, jit)
        if chunk is None:
            assert planner.done
            return out, stalled
        now += OVERHEAD + len(chunk.text) / GEN_RATE
        if schedule.drain_at is not None and now > schedule.drain_at:
            stalled = True
        schedule.played(len(chunk.text) * AUDIO_PER_CHAR, now)
        out.append((chunk.text, chunk.section_title, chunk.section_index))


def test_chapters_keep_their_sections_and_get_spoken_titles():
    """A heading with a link is still a chapter at the same index; its
    title (spoken and shown in the player) is the link text, in batch and
    while streaming alike."""
    text = (f"Preamble.\n\n## Fix [459]({PR[:-3]}459)\nFirst body.\n\n"
            f"# Then {PR}\nSecond body.\n\n## Last\nThird body.")
    batch = section_aware_chunk_text(speak_links(text))
    assert [(t, i) for _c, t, i in batch] == [
        (None, 0), ("Fix 459", 1), ("Then PR 460", 2), ("Last", 3)]
    assert batch[1][0] == "Fix 459.\n\nFirst body."
    # The same sections as the stored text has.
    assert [i for _c, _t, i in batch] == \
        [i for _c, _t, i in section_aware_chunk_text(text)]
    assert _plan(_project([text], proposals=False))[0] == batch
    rng = random.Random(145)
    for _ in range(30):
        assert _plan(_project(_random_splits(text, rng),
                              proposals=False))[0] == batch


def test_a_comment_in_a_code_block_is_no_chapter():
    """#646: a ``# comment`` line in a code block is code: batch drops it
    with the block, and a streamed reply cuts no chapter there (before,
    both made it a chapter and spoke the comment as its title)."""
    text = ("Preamble.\n\n## Setup\nInstall it:\n\n```python\n# step one\n"
            "import x\n## not a chapter\n# step two\nx.run()\n```\n\n"
            "Then run it.\n\n## Use\nCall it.")
    batch = section_aware_chunk_text(speak_links(text))
    assert batch == [
        ("Preamble.", None, 0),
        (f"Setup.\n\nInstall it:\n\n{CODE_BLOCK_SPOKEN}\n\nThen run it.",
         "Setup", 1),
        ("Use.\n\nCall it.", "Use", 2)]
    events = _project([text], proposals=False)
    assert [v for k, v in events if k == "heading"] == ["Setup", "Use"]
    assert _plan(events)[0] == batch
    rng = random.Random(646)
    for _ in range(40):
        assert _plan(_project(_random_splits(text, rng),
                              proposals=False))[0] == batch
    assert _plan(_project(list(text), proposals=False))[0] == batch


def test_a_split_code_block_speaks_its_phrase_once_the_fence_line_ends():
    speaker = LinkSpeaker()
    assert speaker.feed("Run:\n``") == "Run:\n"
    assert speaker.feed("`py") == ""
    assert speaker.feed("thon\nprint(1)") == f"{CODE_BLOCK_SPOKEN}\n"
    assert speaker.feed("\n# a comment\n``") == ""
    assert speaker.feed("`\nDone.") == "Done."
    assert speaker.close() == ""


_CODE_BLOCKS = [
    "```\n[raw](https://raw.com) https://raw.com\n```",
    "```python\n# a comment\nx = 1\n## also code\n```",
    "~~~\n# tilde\n```\nstill code\n~~~",
    "1. Step:\n    ```bash\n    # install\n    pip install x\n    ```\n2. Next.",
    "```\nunclosed\n# to the end",
]


def _linky_prose(rng, words):
    vocab = ("the a loore voice chunk reply model audio user text "
             "when where how what").split()
    extras = [
        lambda: f"[{rng.choice(vocab)} {rng.randint(1, 999)}]"
                f"(https://github.com/o/r/pull/{rng.randint(1, 999)})",
        lambda: f"https://github.com/o/r/issues/{rng.randint(1, 999)}",
        lambda: f"https://www.site{rng.randint(1, 9)}.org/p?q=1",
        lambda: f"<https://ex.com/{rng.randint(1, 9)}>",
        lambda: "![pic](https://x.com/p.png)",
        lambda: "![](https://x.com/p.png)",
        lambda: "`[c](https://code.com)`",
        lambda: "[aside]",
    ]
    out, sent = [], []
    for _ in range(words):
        sent.append(rng.choice(extras)() if rng.random() < 0.08
                    else rng.choice(vocab))
        if len(sent) > rng.randint(6, 22):
            out.append(" ".join(sent).capitalize() + rng.choice(".!?"))
            sent = []
    if sent:
        out.append(" ".join(sent).capitalize() + ".")
    return " ".join(out)


def test_parity_with_batch_on_random_replies():
    """Random replies with links, addresses, images and code, cut at
    random points: the streamed chunks equal batch chunking of the batch
    spoken text."""
    rng = random.Random(461)
    compared = 0
    for _ in range(150):
        parts = []
        if rng.random() < 0.7:
            parts.append(_linky_prose(rng, rng.randint(3, 300)))
        for h in range(rng.randint(0, 4)):
            title = rng.choice([f"Heading {h}", f"See [PR {h}]({PR})",
                                f"Link {ISSUE}"])
            parts.append(f"{'#' * rng.randint(1, 3)} {title}")
            if rng.random() < 0.9:
                parts.append(_linky_prose(rng, rng.randint(1, 600)))
        for _ in range(rng.choice([0, 0, 1, 2])):
            # Code blocks (#646), with "# comment" lines that are no
            # chapter, anywhere among the sections.
            parts.insert(rng.randint(0, len(parts)), rng.choice(_CODE_BLOCKS))
        text = "\n\n".join(parts)
        pieces = _random_splits(text, rng) if len(text) > 1 else [text]
        streamed, stalled = _plan(_project(pieces, proposals=False))
        if stalled:
            continue   # re-anchoring after a stall differs, by design
        # Streaming cuts chapters at h1/h2 only (it can't see ahead).
        assert streamed == section_aware_chunk_text(
            speak_links(text), adaptive_level=False)
        compared += 1
    assert compared > 120


# ── Bounded link matching ───────────────────────────────────────────────

_URL_AT_LIMIT = "https://x.com/" + "a" * (MAX_ADDRESS - len("https://x.com/"))
_TEXT_AT_LIMIT = "t" * MAX_LINK_TEXT


@pytest.mark.parametrize("text, spoken", [
    # At a limit: still a link / an address.
    (f"[{_TEXT_AT_LIMIT}](https://x.com) end", f"{_TEXT_AT_LIMIT} end"),
    (f"[t]({_URL_AT_LIMIT}) end", "t end"),
    (f"see {_URL_AT_LIMIT} end", "see a link to x.com end"),
    (f"see <{_URL_AT_LIMIT}> end", "see a link to x.com end"),
    (f'[t](u "{_TEXT_AT_LIMIT}") end', "t end"),
    ("[" * MAX_BRACKET_DEPTH + "x" + "]" * MAX_BRACKET_DEPTH + "(u)",
     "[" * (MAX_BRACKET_DEPTH - 1) + "x" + "]" * (MAX_BRACKET_DEPTH - 1)),
    # One past: read as written (a bare address in it still counts).
    (f"[{_TEXT_AT_LIMIT}t](https://x.com) end",
     f"[{_TEXT_AT_LIMIT}t](a link to x.com) end"),
    (f"[t]({_URL_AT_LIMIT}a) end", f"[t]({_URL_AT_LIMIT}a) end"),
    (f"see {_URL_AT_LIMIT}a end", f"see {_URL_AT_LIMIT}a end"),
    (f"see <{_URL_AT_LIMIT}a> end", f"see <{_URL_AT_LIMIT}a> end"),
    (f'[t](u "{_TEXT_AT_LIMIT}t") end', f'[t](u "{_TEXT_AT_LIMIT}t") end'),
    ("[" * (MAX_BRACKET_DEPTH + 1) + "x" + "]" * (MAX_BRACKET_DEPTH + 1)
     + "(u)",
     "[" * (MAX_BRACKET_DEPTH + 1) + "x" + "]" * (MAX_BRACKET_DEPTH + 1)
     + "(u)"),
])
def test_limits(text, spoken):
    assert speak_links(text) == spoken
    for step in (1, 7, 100):
        assert "".join(_stream([text[i:i + step]
                                for i in range(0, len(text), step)])) == spoken


def test_over_long_address_starts_no_address_in_the_rest_of_its_run():
    long_run = "https://a.b/" + "c" * MAX_ADDRESS + ":https://b.c"
    assert speak_links(f"x:{long_run} then https://e.f") == \
        f"x:{long_run} then a link to e.f"


def test_long_heading_and_fence_lines_are_ordinary_lines():
    pad = "x" * MAX_LINE_HEAD
    # The last line, a fence alone, opens a block that runs to the end.
    assert speak_links(f"```{pad}\n[a](https://x.com)\n```") == \
        f"```{pad}\na\n{CODE_BLOCK_SPOKEN}"
    fence = "```" + "x" * (MAX_LINE_HEAD - 3)
    assert speak_links(f"{fence}\n[a](https://x.com)\n```\nb") == \
        f"{CODE_BLOCK_SPOKEN}\nb"


_N = 200_000
_WORST_CASES = {
    "unclosed brackets": "[" * _N,
    "nested brackets": "[" * (_N // 2) + "]" * (_N // 2),
    "brackets before a close": ("[" * 400 + "](x)") * (_N // 404),
    "images": "![" * (_N // 2),
    "link openings": "[a](" * (_N // 4),
    "destination never closed": "[a](" + "x" * _N,
    "parentheses in a destination": "[a](" + "(" * _N,
    "title never closed": '[a](u "' + "x" * _N,
    "spaces in a link": "[a](" + " " * _N,
    "long address": "https://example.com/" + "a" * _N,
    "chained addresses": "x:https://a.b" * (_N // 13),
    "closing parentheses after an address": "https://x.co/" + ")" * _N,
    "angle brackets": "<" * _N,
    "autolink never closed": "<https://" + "a" * _N,
    "backticks": "`" * _N,
    "long heading line": "## " + "x" * _N,
    "long code line": "```\n" + "`" * _N + "\n```",
}


@pytest.mark.parametrize("name", list(_WORST_CASES))
def test_long_hostile_text_takes_linear_time(name):
    """~200k characters built to make a link scan go back over the text:
    batch and streamed (small deltas, as a reply arrives) each finish well
    under a second, the stream never holds back more than one link, and
    both say the same thing."""
    text = _WORST_CASES[name]
    start = time.process_time()
    whole = speak_links(text)
    assert time.process_time() - start < 1.0

    rng = random.Random(461)
    speaker, out, i, held = LinkSpeaker(), [], 0, 0
    start = time.process_time()
    while i < len(text):
        step = rng.randint(1, 32)
        out.append(speaker.feed(text[i:i + step]))
        held = max(held, len(speaker._buf))
        i += step
    out.append(speaker.close())
    assert time.process_time() - start < 1.0
    assert held <= MAX_LINK_SOURCE
    assert "".join(out) == whole

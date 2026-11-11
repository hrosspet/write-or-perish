"""Which lines of an older round of profile edits a rebuild still merges
(#421, option (c); Peter, voice review 2026-11-10: "let's go with c)").

A rebuild merges every round of the user's own edits, oldest first, each
at its date (tasks/exports.user_versions_to_merge). The newest round is
merged whole. An older round contributes only what no newer round changed:
its version without every line a newer round of the user's changed or
deleted (or inserted lines inside), and no merge at all when none of its
own changes is left. So a sentence the user added in one edit and deleted
in a later one is not brought back, while an older correction on lines the
user did not touch again still reaches the rebuilt profile.

Pure text work: no database, no model call. Lines are compared with their
whitespace collapsed, so a change of whitespace alone (spaces, blank lines)
is no change.

The rule, line by line:

- A round's changes are the lines it removed from the version it edited
  and the lines it wrote there (difflib's line diff). A line of a block
  the round replaced belongs to the whole block: the lines it removed touch
  the lines it wrote in their place, and the other way round.
- A newer round "touches" a line of the older round's version when the
  line maps onto a line the newer round removed or replaced, or onto lines
  with the place where it inserted lines strictly inside them; a line
  that a model step between them removed maps to a place, touched by a
  change at that same place. A line next to a changed line is not touched.
- The mapping: a line diff of the older version against the version the
  newer round edited, over their non-blank lines only (blank lines would
  pair paragraphs by position: review of #569). A line in an unchanged
  stretch maps onto its copy. A stretch a model step rewrote is paired line
  by line when it has as many lines on both sides; otherwise every line of
  it maps onto the whole rewritten stretch.
- Moved lines: a line is also touched when it is a line the newer round
  removed or wrote, or as similar to one as difflib's Differ counts two
  lines as versions of one: SIMILAR_LINE_RATIO over the characters of the
  whitespace-collapsed lines.
- What the merge reads: the older version without its touched lines. An
  own change whose lines are touched is dropped; none left means no merge.
"""
import difflib
import re
from dataclasses import dataclass

# difflib.Differ's cutoff for two lines being versions of one line, applied
# as Differ applies it, to characters (of the whitespace-collapsed lines).
# Used only to find a line a newer round moved or reworded (heuristic, #421).
SIMILAR_LINE_RATIO = 0.75

_WORD = re.compile(r"\w")


def line_key(line):
    """A line compared without its whitespace: runs collapsed, ends
    stripped. A blank line's key is ""."""
    return " ".join(line.split())


def _keys(text):
    return [line_key(line) for line in (text or "").splitlines()]


def _opcodes(a, b):
    return difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()


@dataclass(frozen=True)
class LineChange:
    """One line an edit removed or wrote. ``before``: the lines of the
    edited version it stands for, ``after``: those of the new version
    (half-open ranges; an empty range is a place between lines)."""
    before: tuple
    after: tuple
    key: str
    added: bool


def line_changes(before_keys, after_keys):
    """The changed lines from ``before_keys`` to ``after_keys`` (line
    keys), blank lines left out. A replaced block's removed lines stand
    for the lines written in their place, and each written line for the
    lines removed."""
    out = []
    for tag, i1, i2, j1, j2 in _opcodes(before_keys, after_keys):
        if tag == "equal":
            continue
        for i in range(i1, i2):
            if before_keys[i]:
                out.append(LineChange((i, i + 1), (j1, j2), before_keys[i],
                                      False))
        for j in range(j1, j2):
            if after_keys[j]:
                out.append(LineChange((i1, i2), (j, j + 1), after_keys[j],
                                      True))
    return out


def _place_pieces(at, ops):
    """A place between lines of a, in b: a place in an unchanged block, or
    the changed block it is in or at the edge of."""
    for tag, i1, i2, j1, j2 in ops:
        if i1 <= at <= i2:
            yield ((j1 + at - i1,) * 2 if tag == "equal" else (j1, j2))


def _range_pieces(lo, hi, ops):
    """Lines [lo, hi) of a, in b: each line of an unchanged block onto its
    copy, a line of a changed block onto the whole block, and lines b has
    inside the range."""
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            s, e = max(lo, i1), min(hi, i2)
            if s < e:
                yield (j1 + s - i1, j1 + e - i1)
        elif (i1 < hi and lo < i2) if i1 < i2 else lo < i1 < hi:
            yield (j1, j2)


def _map(region, ops):
    """``region`` (lines of a) as lines of b, through ``ops`` (a -> b)."""
    lo, hi = region
    pieces = list(_place_pieces(lo, ops) if lo == hi
                  else _range_pieces(lo, hi, ops))
    if not pieces:
        return (0, 0)
    return (min(p[0] for p in pieces), max(p[1] for p in pieces))


def _overlaps(r, q):
    """Whether two ranges of the same lines touch: they share a line; or
    one is a place strictly inside the other; or both are the same place."""
    (a, b), (c, d) = r, q
    if a < b and c < d:
        return a < d and c < b
    if a < b:
        return a < c < b
    if c < d:
        return c < a < d
    return a == c


def _similar(a, b):
    """Whether two line keys are the same line, moved or reworded: equal,
    or as similar as SIMILAR_LINE_RATIO over their characters (the ratio
    difflib's Differ uses to pair two lines)."""
    if a == b:
        return True
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return (sm.real_quick_ratio() >= SIMILAR_LINE_RATIO
            and sm.quick_ratio() >= SIMILAR_LINE_RATIO
            and sm.ratio() >= SIMILAR_LINE_RATIO)


def _nonblank_before(keys):
    """For each place 0..len(keys) between lines: how many non-blank lines
    come before it. Turns a range of lines into a range of non-blank ones."""
    out = [0]
    for key in keys:
        out.append(out[-1] + (1 if key else 0))
    return out


def _paired(ops):
    """``ops`` with every rewritten stretch that has as many lines on both
    sides split into one-line pairs. Other rewritten stretches stay whole,
    so each of their lines maps onto the whole stretch."""
    out = []
    for tag, i1, i2, j1, j2 in ops:
        if tag == "replace" and i2 - i1 == j2 - j1:
            out += [("replace", i1 + k, i1 + k + 1, j1 + k, j1 + k + 1)
                    for k in range(i2 - i1)]
        else:
            out.append((tag, i1, i2, j1, j2))
    return out


def _distinctive(key):
    """Whether a line can be told apart by its words (a line of markup
    alone, as "---", cannot)."""
    return bool(_WORD.search(key))


@dataclass
class OlderEdit:
    """``text``: what the merge reads (None: no merge); ``changes`` and
    ``kept``: the round's own changed lines, and those left; ``removed``:
    the lines of its version left out."""
    text: object
    changes: int
    kept: int
    removed: int


class _Newer:
    """A newer round's changes, and the mapping from the older version's
    lines onto the version the newer round edited. The mapping works on
    non-blank lines only: with blank lines in it, the blank lines between
    rewritten paragraphs are the only equal lines, and paragraphs pair by
    position, one off when the rewrite added or dropped a paragraph."""

    def __init__(self, older_keys, base_text, text):
        base_keys = _keys(base_text)
        self.changes = line_changes(base_keys, _keys(text))
        self.keys = [c.key for c in self.changes if _distinctive(c.key)]
        self._older = _nonblank_before(older_keys)
        base = _nonblank_before(base_keys)
        self._before = [(base[c.before[0]], base[c.before[1]])
                        for c in self.changes]
        self.ops = _paired(_opcodes([k for k in older_keys if k],
                                    [k for k in base_keys if k]))

    def touches_place(self, region):
        """Whether ``region`` (lines of the older version) maps onto lines
        this round changed."""
        lo, hi = region
        mapped = _map((self._older[lo], self._older[hi]), self.ops)
        return any(_overlaps(mapped, q) for q in self._before)


def older_edit(base_text, text, newer):
    """What a rebuild merges for an older round of the user's edits.

    ``text``: the round's version; ``base_text``: the version the round
    edited ("" when there is none, as for a profile written by hand);
    ``newer``: (base text, text) of each newer save of the user's. Returns
    an OlderEdit whose ``text`` is ``text`` itself when no line is touched,
    ``text`` without its touched lines (blank runs closed up) when some
    own change is left, and None when none is."""
    lines = (text or "").splitlines()
    keys = [line_key(line) for line in lines]
    own = line_changes(_keys(base_text), keys)
    rounds = [r for r in (_Newer(keys, b, t) for b, t in newer) if r.changes]
    newer_keys = [k for r in rounds for k in r.keys]
    similar = {}

    def same_line(key):
        if key not in similar:
            similar[key] = any(_similar(key, n) for n in newer_keys)
        return similar[key]

    def touched(region, ks):
        return (any(r.touches_place(region) for r in rounds)
                or any(same_line(k) for k in ks))

    hit = [bool(k) and touched((t, t + 1), [k] if _distinctive(k) else [])
           for t, k in enumerate(keys)]
    kept = 0
    for change in own:
        lo, hi = change.after
        if change.added:
            dropped = hit[lo]
        else:
            dropped = touched(change.after, [change.key] if _distinctive(
                change.key) else []) or any(hit[t] for t in range(lo, hi))
        kept += not dropped
    removed = sum(hit)
    if not kept:
        return OlderEdit(None, len(own), 0, removed)
    if not removed:
        return OlderEdit(text, len(own), kept, 0)
    return OlderEdit(_without(lines, hit), len(own), kept, removed)


def _without(lines, gone):
    """``lines`` without those marked ``gone``, runs of blank lines closed
    up and none at either end."""
    out = []
    for line, drop in zip(lines, gone):
        if drop or (not line.strip() and (not out or not out[-1].strip())):
            continue
        out.append(line)
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out)

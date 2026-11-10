"""Which lines of an older round of profile edits a rebuild still merges
(#421, option (c); Peter, voice review 2026-11-10: "let's go with c)").

A rebuild merges every round of the user's own edits, oldest first, each
at its date (tasks/exports.user_versions_to_merge). The newest round is
merged whole. An older round contributes only what no newer round changed:
its version without every line a newer round of the user's changed or
deleted (or inserted lines inside), and no merge at all when none of its
own changes is left. So a sentence the user added in one edit and deleted in a later one
is not brought back, while an older correction on lines the user did not
touch again still reaches the rebuilt profile.

Pure text work: no database, no model call. Lines are compared with their
whitespace collapsed, so a change of whitespace alone (spaces, blank lines)
is no change.

The rule, line by line:

- A round's changes are the lines it removed from the version it edited
  and the lines it wrote there (difflib's line diff). A line of a block
  the round replaced belongs to the whole block: the lines it removed touch
  the lines it wrote in their place, and the other way round.
- A newer round "touches" a line of the older round's version when, in the
  newer round's diff, the line maps (through a line diff of the older
  version against the version the newer round edited) onto a line the
  newer round removed or replaced, or onto the place where it inserted
  lines, if that place is strictly inside the mapped lines; a line that a
  model step between them removed maps to a place, touched by a change at
  that same place. A line next to a changed line is not touched.
- Moved lines: a line is also touched when its words are those of a line
  the newer round removed or wrote, or as similar as difflib's Differ
  counts two lines as versions of one (SIMILAR_LINE_RATIO).
- What the merge reads: the older version without its touched lines. An
  own change whose lines are touched is dropped; none left means no merge.
"""
import difflib
import re
from dataclasses import dataclass

# difflib.Differ's cutoff for two lines being versions of one line, applied
# to the lines' words. Used only to find a line a newer round moved or
# reworded (heuristic, #421).
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
    """Whether two line keys are the same line, moved or reworded: the
    same words, or words as similar as SIMILAR_LINE_RATIO."""
    if a == b:
        return True
    sm = difflib.SequenceMatcher(None, a.split(), b.split(), autojunk=False)
    return (sm.real_quick_ratio() >= SIMILAR_LINE_RATIO
            and sm.quick_ratio() >= SIMILAR_LINE_RATIO
            and sm.ratio() >= SIMILAR_LINE_RATIO)


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
    """A newer round's changes, mapped from the older version's lines."""

    def __init__(self, older_keys, base_text, text):
        base_keys = _keys(base_text)
        self.changes = line_changes(base_keys, _keys(text))
        self.ops = _opcodes(older_keys, base_keys)
        self.keys = [c.key for c in self.changes if _distinctive(c.key)]

    def touches(self, region, keys):
        mapped = _map(region, self.ops)
        if any(_overlaps(mapped, c.before) for c in self.changes):
            return True
        return any(_similar(k, n) for k in keys for n in self.keys)


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

    def touched(region, ks):
        return any(r.touches(region, ks) for r in rounds)

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
    out = []
    for line, gone in zip(lines, hit):
        if gone or (not line.strip() and (not out or not out[-1].strip())):
            continue
        out.append(line)
    while out and not out[-1].strip():
        out.pop()
    return OlderEdit("\n".join(out), len(own), kept, removed)

/**
 * Line-based diff for artifact version history (#239 follow-up).
 *
 * computeLineDiff(oldText, newText) → [{ type: 'same'|'add'|'del', text }]
 * refineWordDiffs(ops) → same ops, with paired del/add lines gaining
 *   `segments: [{changed, text}]` (word-level highlighting à la GitHub;
 *   uses jsdiff's diffWordsWithSpace within pairs).
 * collapseUnchanged(ops, context) → display rows where long runs of
 *   unchanged lines are folded into { type: 'skip', count } separators,
 *   keeping `context` lines around each change.
 *
 * Implementation: trim the common prefix/suffix first (artifact versions
 * are incremental, so most of the text is shared), then LCS DP on the
 * middle. If the middle is still huge the DP would be O(n·m); past a cap
 * we fall back to rendering it as one del-block + add-block, which is
 * still a correct (if coarse) diff.
 */

import { diffWordsWithSpace } from 'diff';

const LCS_LINE_CAP = 1500;

export function computeLineDiff(oldText, newText) {
  const a = (oldText || '').split('\n');
  const b = (newText || '').split('\n');

  // Common prefix
  let start = 0;
  while (start < a.length && start < b.length && a[start] === b[start]) {
    start++;
  }
  // Common suffix (not overlapping the prefix)
  let endA = a.length;
  let endB = b.length;
  while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) {
    endA--;
    endB--;
  }

  const midA = a.slice(start, endA);
  const midB = b.slice(start, endB);

  const ops = [];
  for (let i = 0; i < start; i++) ops.push({ type: 'same', text: a[i] });

  if (midA.length > LCS_LINE_CAP || midB.length > LCS_LINE_CAP) {
    // Too large for DP — coarse fallback.
    midA.forEach((t) => ops.push({ type: 'del', text: t }));
    midB.forEach((t) => ops.push({ type: 'add', text: t }));
  } else if (midA.length || midB.length) {
    // LCS table (lengths), then backtrack.
    const n = midA.length;
    const m = midB.length;
    // (n+1) x (m+1) table as typed rows for speed.
    const table = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
    for (let i = n - 1; i >= 0; i--) {
      for (let j = m - 1; j >= 0; j--) {
        table[i][j] = midA[i] === midB[j]
          ? table[i + 1][j + 1] + 1
          : Math.max(table[i + 1][j], table[i][j + 1]);
      }
    }
    let i = 0;
    let j = 0;
    while (i < n && j < m) {
      if (midA[i] === midB[j]) {
        ops.push({ type: 'same', text: midA[i] });
        i++;
        j++;
      } else if (table[i + 1][j] >= table[i][j + 1]) {
        ops.push({ type: 'del', text: midA[i] });
        i++;
      } else {
        ops.push({ type: 'add', text: midB[j] });
        j++;
      }
    }
    while (i < n) ops.push({ type: 'del', text: midA[i++] });
    while (j < m) ops.push({ type: 'add', text: midB[j++] });
  }

  for (let i = endA; i < a.length; i++) ops.push({ type: 'same', text: a[i] });
  return ops;
}

// Word-level refinement (GitHub-style): within each change block, paired
// del/add lines get `segments: [{changed, text}]` marking the words that
// actually differ, rendered as saturated sub-spans on the tinted line.
// Only applied when the paired lines are similar enough — refining two
// unrelated lines highlights every word (confetti), which reads worse
// than plain line coloring.
const WORD_REFINE_MIN_SIMILARITY = 0.4;

function refinePair(delOp, addOp) {
  const parts = diffWordsWithSpace(delOp.text, addOp.text);
  const commonChars = parts
    .filter(p => !p.added && !p.removed)
    .reduce((n, p) => n + p.value.length, 0);
  const similarity = commonChars
    / Math.max(delOp.text.length, addOp.text.length, 1);
  if (similarity < WORD_REFINE_MIN_SIMILARITY) return;
  delOp.segments = parts
    .filter(p => !p.added)
    .map(p => ({ changed: !!p.removed, text: p.value }));
  addOp.segments = parts
    .filter(p => !p.removed)
    .map(p => ({ changed: !!p.added, text: p.value }));
}

export function refineWordDiffs(ops) {
  // Walk change blocks (a run of dels followed by a run of adds) and pair
  // them positionally: i-th deleted line with i-th added line.
  let i = 0;
  while (i < ops.length) {
    if (ops[i].type !== 'del') { i++; continue; }
    const delStart = i;
    while (i < ops.length && ops[i].type === 'del') i++;
    const addStart = i;
    while (i < ops.length && ops[i].type === 'add') i++;
    const pairs = Math.min(addStart - delStart, i - addStart);
    for (let k = 0; k < pairs; k++) {
      refinePair(ops[delStart + k], ops[addStart + k]);
    }
  }
  return ops;
}

export function collapseUnchanged(ops, context = 2) {
  const rows = [];
  let sameRun = [];

  const flushRun = (isLast) => {
    if (!sameRun.length) return;
    const keepHead = rows.length ? context : 0; // no leading context at doc start
    const keepTail = isLast ? 0 : context;
    if (sameRun.length > keepHead + keepTail + 1) {
      sameRun.slice(0, keepHead).forEach((r) => rows.push(r));
      rows.push({ type: 'skip', count: sameRun.length - keepHead - keepTail });
      sameRun.slice(sameRun.length - keepTail).forEach((r) => rows.push(r));
    } else {
      sameRun.forEach((r) => rows.push(r));
    }
    sameRun = [];
  };

  for (const op of ops) {
    if (op.type === 'same') {
      sameRun.push(op);
    } else {
      flushRun(false);
      rows.push(op);
    }
  }
  flushRun(true);
  return rows;
}

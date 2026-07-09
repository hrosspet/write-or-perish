import { computeLineDiff, collapseUnchanged } from './diff';

const join = (ops) => ops.map(o => `${o.type[0]}:${o.text}`).join('|');

describe('computeLineDiff', () => {
  test('identical texts produce only same lines', () => {
    const ops = computeLineDiff('a\nb\nc', 'a\nb\nc');
    expect(ops.every(o => o.type === 'same')).toBe(true);
    expect(ops).toHaveLength(3);
  });

  test('appended line at the end', () => {
    const ops = computeLineDiff('a\nb', 'a\nb\nc');
    expect(join(ops)).toBe('s:a|s:b|a:c');
  });

  test('changed line is del + add', () => {
    const ops = computeLineDiff('a\nold\nc', 'a\nnew\nc');
    expect(join(ops)).toBe('s:a|d:old|a:new|s:c');
  });

  test('removed line', () => {
    const ops = computeLineDiff('a\nb\nc', 'a\nc');
    expect(join(ops)).toBe('s:a|d:b|s:c');
  });

  test('insertion in the middle of a long common text', () => {
    const base = Array.from({ length: 50 }, (_, i) => `line ${i}`);
    const modified = [...base.slice(0, 25), 'INSERTED', ...base.slice(25)];
    const ops = computeLineDiff(base.join('\n'), modified.join('\n'));
    expect(ops.filter(o => o.type === 'add')).toEqual([{ type: 'add', text: 'INSERTED' }]);
    expect(ops.filter(o => o.type === 'del')).toHaveLength(0);
    expect(ops).toHaveLength(51);
  });

  test('empty old text = all additions (creation)', () => {
    const ops = computeLineDiff('', 'a\nb');
    // '' splits to [''] — one empty line; tolerate it as del or same-empty.
    const adds = ops.filter(o => o.type === 'add').map(o => o.text);
    expect(adds).toEqual(expect.arrayContaining(['a', 'b']));
  });
});

describe('collapseUnchanged', () => {
  test('folds long unchanged runs, keeping context around changes', () => {
    const base = Array.from({ length: 30 }, (_, i) => `line ${i}`);
    const modified = [...base.slice(0, 15), 'NEW', ...base.slice(15)];
    const rows = collapseUnchanged(
      computeLineDiff(base.join('\n'), modified.join('\n')), 2);

    const skips = rows.filter(r => r.type === 'skip');
    expect(skips).toHaveLength(2);
    // 15 leading same lines → skip 13, keep 2 before the change.
    expect(skips[0].count).toBe(13);
    // 15 trailing same lines → keep 2 after the change, skip 13.
    expect(skips[1].count).toBe(13);
    const addIdx = rows.findIndex(r => r.type === 'add');
    expect(rows[addIdx - 1].type).toBe('same');
    expect(rows[addIdx - 2].type).toBe('same');
    expect(rows[addIdx - 3].type).toBe('skip');
  });

  test('short unchanged runs are not folded', () => {
    const rows = collapseUnchanged(
      computeLineDiff('a\nb\nold', 'a\nb\nnew'), 2);
    expect(rows.some(r => r.type === 'skip')).toBe(false);
  });
});

describe('refineWordDiffs', () => {
  const { refineWordDiffs } = require('./diff');

  test('paired similar lines gain word segments', () => {
    const ops = refineWordDiffs(computeLineDiff(
      'you write a brief acknowledgment of what you are doing',
      'you should write one short sentence naming what you are doing'));
    const del = ops.find(o => o.type === 'del');
    const add = ops.find(o => o.type === 'add');
    expect(del.segments).toBeDefined();
    expect(add.segments).toBeDefined();
    // Unchanged words are unmarked; changed words are marked.
    expect(del.segments.find(s => s.text.includes('acknowledgment')).changed).toBe(true);
    expect(add.segments.find(s => s.text.includes('sentence')).changed).toBe(true);
    expect(add.segments.filter(s => !s.changed).map(s => s.text).join(''))
      .toContain('what you are doing');
    // Reassembled segments reproduce the full lines.
    expect(del.segments.map(s => s.text).join('')).toBe(del.text);
    expect(add.segments.map(s => s.text).join('')).toBe(add.text);
  });

  test('dissimilar pairs are left unrefined (no confetti)', () => {
    const ops = refineWordDiffs(computeLineDiff(
      'completely unrelated old line about apples',
      'nothing shared here whatsoever xyz'));
    expect(ops.find(o => o.type === 'del').segments).toBeUndefined();
    expect(ops.find(o => o.type === 'add').segments).toBeUndefined();
  });

  test('unpaired adds/dels stay unrefined', () => {
    const ops = refineWordDiffs(computeLineDiff('a\nb', 'a\nb\npure addition'));
    const add = ops.find(o => o.type === 'add');
    expect(add.segments).toBeUndefined();
  });

  test('multi-line blocks pair positionally', () => {
    const ops = refineWordDiffs(computeLineDiff(
      'first old line here\nsecond old line here',
      'first new line here\nsecond new line here'));
    const dels = ops.filter(o => o.type === 'del');
    const adds = ops.filter(o => o.type === 'add');
    expect(dels).toHaveLength(2);
    expect(adds).toHaveLength(2);
    expect(dels.every(o => o.segments)).toBe(true);
    expect(adds.every(o => o.segments)).toBe(true);
    expect(adds[0].segments.find(s => s.changed).text).toContain('new');
  });
});

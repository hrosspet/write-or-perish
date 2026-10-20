import { decodeEntities, highlightRuns } from './searchHighlight';

test('splits <mark> runs and decodes the escaped text', () => {
  expect(highlightRuns('Notes &lt;i&gt; on <mark>equinox</mark> &amp; tides')).toEqual([
    { text: 'Notes <i> on ', marked: false },
    { text: 'equinox', marked: true },
    { text: ' & tides', marked: false },
  ]);
});

test('decodes each entity once, in one pass', () => {
  expect(decodeEntities('&amp;lt; &quot;a&quot; &#x27;b&#39;')).toBe('&lt; "a" \'b\'');
});

test('a match inside escaped markup stays text', () => {
  expect(highlightRuns('&lt;span title=&quot;<mark>equinox</mark>&quot;&gt;')).toEqual([
    { text: '<span title="', marked: false },
    { text: 'equinox', marked: true },
    { text: '">', marked: false },
  ]);
});

test('any other tag is kept as literal text', () => {
  expect(highlightRuns('<b>x</b>')).toEqual([{ text: '<b>x</b>', marked: false }]);
});

test('empty or missing input gives no runs', () => {
  expect(highlightRuns('')).toEqual([]);
  expect(highlightRuns(null)).toEqual([]);
  expect(highlightRuns(undefined)).toEqual([]);
});

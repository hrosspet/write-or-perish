import { stripInlineMarkdown, toggleCheckbox, insertItemAfter } from './markdown';

// The Todo page parses raw source lines itself, so its item labels keep their
// inline markdown. The helpers match lines by stripped plain text; keying on
// the stripped label is what makes a link-bearing item toggleable.
const content = [
  '## Financial investments',
  '- [ ] Monitor markets',
  '- [ ] CC artifact with my [financials](https://claude.ai/code/artifact/abc?via=x)',
].join('\n');
const rawLabel = 'CC artifact with my [financials](https://claude.ai/code/artifact/abc?via=x)';

test('stripInlineMarkdown reduces a link to its text', () => {
  expect(stripInlineMarkdown(rawLabel)).toBe('CC artifact with my financials');
});

test('a link-bearing item toggles when keyed by its stripped label', () => {
  const out = toggleCheckbox(content, stripInlineMarkdown(rawLabel).trim(), false);
  expect(out.split('\n')[2]).toBe(`- [x] ${rawLabel}`);
  // The raw label does not match, which is why the Todo page must strip first.
  expect(toggleCheckbox(content, rawLabel, false)).toBe(content);
});

test('inserting after a link-bearing item lands below it', () => {
  const out = insertItemAfter(content, stripInlineMarkdown(rawLabel).trim(), 'New task');
  expect(out.split('\n')[3]).toBe('- [ ] New task');
});

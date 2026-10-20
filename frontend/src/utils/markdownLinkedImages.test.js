import { normalizeLinkedImages } from './markdownLinkedImages';

const IMG = '![A pic](https://cdn.example.com/image/fetch/w_1456/https%3A%2F%2Fmedia.example%2Fpic.jpeg)';
const HREF = 'https://cdn.example.com/image/fetch/https%3A%2F%2Fmedia.example%2Fpic.jpeg';
const BROKEN = `[\n\n${IMG}\n\n](${HREF})`;
const FIXED = `[${IMG}](${HREF})`;

test('the broken clipper shape becomes a link with an image inside', () => {
  expect(normalizeLinkedImages(`Intro\n\n${BROKEN}\n\nOutro`)).toBe(`Intro\n\n${FIXED}\n\nOutro`);
});

test('several broken images in one text are all repaired', () => {
  expect(normalizeLinkedImages(`${BROKEN}\n\ntext\n\n${BROKEN}`)).toBe(`${FIXED}\n\ntext\n\n${FIXED}`);
});

test('an image with a title is repaired', () => {
  const img = '![a](https://x.example/p.png "T")';
  expect(normalizeLinkedImages(`[\n\n${img}\n\n](https://y.example/)`)).toBe(`[${img}](https://y.example/)`);
});

test('a fenced code block holding the shape is unchanged', () => {
  const text = `Before\n\n\`\`\`\n${BROKEN}\n\`\`\`\n\nAfter`;
  expect(normalizeLinkedImages(text)).toBe(text);
});

test('a tilde fence and an unclosed fence are unchanged', () => {
  const tilde = `~~~md\n${BROKEN}\n~~~`;
  expect(normalizeLinkedImages(tilde)).toBe(tilde);
  const open = `\`\`\`\n${BROKEN}`;
  expect(normalizeLinkedImages(open)).toBe(open);
});

test('inline code holding the shape is unchanged, text after it is repaired', () => {
  const inline = `Use \`${BROKEN.replace(/\n/g, ' ')}\` like so`;
  expect(normalizeLinkedImages(inline)).toBe(inline);
  expect(normalizeLinkedImages(`\`x\`\n\n${BROKEN}`)).toBe(`\`x\`\n\n${FIXED}`);
});

test('a repair after a closed fence still happens', () => {
  expect(normalizeLinkedImages(`\`\`\`\ncode\n\`\`\`\n\n${BROKEN}`)).toBe(`\`\`\`\ncode\n\`\`\`\n\n${FIXED}`);
});

test('normal links, images and linked images are unchanged', () => {
  for (const text of [
    'See [this](https://a.example/x) and [that](https://b.example).',
    IMG,
    FIXED,
    `[ ${IMG} text ](${HREF})`,
    `[\n\n${IMG}\n\n${IMG}\n\n](${HREF})`,
    '- [ ] a task\n- [x] done',
    '[ ]\n\n![a](b)\n\n(c)',
  ]) {
    expect(normalizeLinkedImages(text)).toBe(text);
  }
});

test('non-strings pass through', () => {
  expect(normalizeLinkedImages(undefined)).toBeUndefined();
});

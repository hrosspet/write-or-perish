// react-markdown is ESM (not transformed by CRA jest) — mock it and remark-gfm
// so importing MarkdownBody works; these tests exercise only the exported
// remarkHtmlAsCode plugin on hand-built mdast trees.
jest.mock('react-markdown', () => () => null);
jest.mock('remark-gfm', () => () => {});

import { remarkHtmlAsCode } from './MarkdownBody';

const transform = remarkHtmlAsCode();

// Regression for the raw-HTML flattening bug: text like `<user-archive>` around
// an export template was parsed as an HTML block and rendered with its newlines
// collapsed. The plugin turns such nodes into code so the literal text shows.

test('block-level html becomes a code block, preserving the raw value', () => {
  const value = '<user-archive>\n{user_export?days=92}\n</user-archive>';
  const tree = {
    type: 'root',
    children: [
      { type: 'paragraph', children: [{ type: 'text', value: 'hello' }] },
      { type: 'html', value },
    ],
  };
  transform(tree);
  expect(tree.children[1]).toEqual({ type: 'code', lang: null, value });
});

test('inline html inside a paragraph becomes inline code', () => {
  const tree = {
    type: 'root',
    children: [
      {
        type: 'paragraph',
        children: [
          { type: 'text', value: 'before ' },
          { type: 'html', value: '<user-archive>' },
          { type: 'text', value: 'x' },
          { type: 'html', value: '</user-archive>' },
        ],
      },
    ],
  };
  transform(tree);
  const para = tree.children[0];
  expect(para.children[1]).toEqual({ type: 'inlineCode', value: '<user-archive>' });
  expect(para.children[3]).toEqual({ type: 'inlineCode', value: '</user-archive>' });
});

test('pure html comments are dropped, not shown as code', () => {
  const tree = {
    type: 'root',
    children: [
      { type: 'html', value: '<!-- id: my-feature -->' },
      { type: 'paragraph', children: [{ type: 'text', value: 'body' }] },
    ],
  };
  transform(tree);
  expect(tree.children).toHaveLength(1);
  expect(tree.children[0].type).toBe('paragraph');
});

test('recurses into nested containers like blockquotes', () => {
  const tree = {
    type: 'root',
    children: [
      {
        type: 'blockquote',
        children: [{ type: 'html', value: '<note>\nquoted\n</note>' }],
      },
    ],
  };
  transform(tree);
  expect(tree.children[0].children[0].type).toBe('code');
});

test('non-html nodes are untouched', () => {
  const para = { type: 'paragraph', children: [{ type: 'text', value: 'plain' }] };
  const tree = { type: 'root', children: [para] };
  transform(tree);
  expect(tree.children[0]).toBe(para);
  expect(para.children[0]).toEqual({ type: 'text', value: 'plain' });
});

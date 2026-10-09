// react-markdown is ESM (not transformed by CRA jest); render the code-block
// component the `pre` renderer uses directly, built the way react-markdown does.
jest.mock('react-markdown', () => () => null);
jest.mock('remark-gfm', () => () => {});

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { CodeBlock } from './MarkdownBody';

beforeEach(() => {
  Object.assign(navigator, { clipboard: { writeText: jest.fn(() => Promise.resolve()) } });
});

test('a fenced block has a copy button that copies the exact block text', async () => {
  const code = 'line one\n\n  indented <b>\nlast';
  render(<CodeBlock><code className="language-js">{code + '\n'}</code></CodeBlock>);
  fireEvent.click(screen.getByTitle('Copy code'));
  await waitFor(() => expect(navigator.clipboard.writeText).toHaveBeenCalledWith(code));
});

test('inline code has no copy button', () => {
  // Inline code uses the plain `code` renderer, not CodeBlock.
  render(<p>use <code>npm test</code> here</p>);
  expect(screen.queryByTitle('Copy code')).toBeNull();
});

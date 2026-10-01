// react-markdown is ESM (not transformed by CRA jest), so it is replaced by a
// minimal renderer that understands only `- [ ]` / `- [x]` task lines and
// renders them through the `components` map, the way react-markdown does.
// That is enough to check what #321 depends on: the element types MarkdownBody
// hands to ReactMarkdown stay the same across re-renders, so React updates the
// list in place instead of remounting it.
import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';

jest.mock('react-markdown', () => {
  const R = require('react');
  return function FakeMarkdown({ children, components }) {
    const Ul = components.ul;
    const Li = components.li;
    const items = String(children).split('\n').filter((l) => /^- \[[ x]\] /.test(l));
    return R.createElement(
      Ul,
      { className: 'contains-task-list', node: { tagName: 'ul' } },
      items.map((line, i) => R.createElement(
        Li,
        { key: `li-${i}`, className: 'task-list-item', node: { tagName: 'li' } },
        R.createElement('input', { type: 'checkbox', checked: line[3] === 'x', readOnly: true }),
        ' ',
        line.slice(6),
      )),
    );
  };
});
jest.mock('remark-gfm', () => () => {});
jest.mock('react-router-dom', () => ({
  useInRouterContext: () => false,
  useNavigate: () => () => {},
}));

// eslint-disable-next-line import/first
import MarkdownBody from './MarkdownBody';

const list = (a, b, c) => [`- [${a}] one`, `- [${b}] two`, `- [${c}] three`].join('\n');

test('toggling one item keeps the other checkboxes mounted and focused (#321)', () => {
  const onToggle = jest.fn();
  const { rerender } = render(
    <MarkdownBody onCheckboxToggle={onToggle} onAddTask={() => {}}>{list(' ', ' ', ' ')}</MarkdownBody>,
  );
  const boxes = screen.getAllByRole('checkbox');
  expect(boxes).toHaveLength(3);
  boxes[1].focus();
  fireEvent.click(boxes[1]);
  expect(onToggle).toHaveBeenCalledWith('two', false);

  // The parent re-renders with the toggled content and, as NodeDetail does,
  // new callback identities.
  rerender(
    <MarkdownBody onCheckboxToggle={jest.fn()} onAddTask={() => {}}>{list(' ', 'x', ' ')}</MarkdownBody>,
  );
  const after = screen.getAllByRole('checkbox');
  expect(after[0]).toBe(boxes[0]);
  expect(after[1]).toBe(boxes[1]);
  expect(after[1]).toHaveAttribute('aria-checked', 'true');
  expect(document.activeElement).toBe(boxes[1]);
});

test('a half-typed "+" item survives a parent re-render', () => {
  const { rerender } = render(
    <MarkdownBody onCheckboxToggle={() => {}} onAddTask={() => {}}>{list(' ', ' ', ' ')}</MarkdownBody>,
  );
  fireEvent.click(screen.getAllByRole('button', { name: 'Add an item below' })[0]);
  const input = screen.getByPlaceholderText('New item…');
  fireEvent.change(input, { target: { value: 'half typed' } });

  rerender(
    <MarkdownBody onCheckboxToggle={() => {}} onAddTask={() => {}}>{list(' ', ' ', ' ')}</MarkdownBody>,
  );
  expect(screen.getByPlaceholderText('New item…')).toBe(input);
  expect(input).toHaveValue('half typed');
});

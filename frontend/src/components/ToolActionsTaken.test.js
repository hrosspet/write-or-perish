// "Actions taken" under an AI reply. Its owner gets every detail of each
// action (search query, links, artifact kind); anyone else gets each
// action's name and outcome from the server and sees a line that names
// the action and links nowhere.
jest.mock('./MarkdownBody', () => ({ children }) => <div>{children}</div>);
jest.mock('../api', () => ({ __esModule: true, default: { get: jest.fn(), post: jest.fn(), put: jest.fn() } }));

import React from 'react';
import { render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import ToolActionsTaken from './ToolActionsTaken';
import ProposalInline from './ProposalInline';

const renderActions = (props) => render(
  <MemoryRouter>
    <ToolActionsTaken expanded onToggle={() => {}} {...props} />
  </MemoryRouter>,
);

// The shape the server sends someone who is not the reply's owner.
const SHARED = [
  { name: 'semantic_search', status: 'success' },
  { name: 'read_full', status: 'success' },
  { name: 'read_full', status: 'error' },
  { name: 'update_artifact', status: 'success' },
  { name: 'read_artifact', status: 'success' },
  { name: 'read_todo', status: 'success' },
  { name: 'apply_todo_changes', status: 'success' },
  { name: 'apply_share', status: 'success' },
  { name: 'propose_todo', status: 'success' },
  { name: '_batch', status: 'ended' },
  { name: '_mode' },
];

test('someone else sees one plain line per action, with no links', () => {
  const { container } = renderActions({ toolCallsMeta: SHARED, detailed: false });
  expect(screen.getByText(/Actions taken \(9\)/)).toBeInTheDocument();
  const lines = Array.from(container.querySelectorAll('button ~ div > div'))
    .map(el => el.textContent);
  expect(lines).toEqual([
    '✓ Searched archive & references',
    '✓ Read in full',
    '✗ Read in full (failed)',
    '✓ Wrote an artifact',
    '✓ Read an artifact',
    '✓ Read the todo list',
    '✓ Todo changes confirmed',
    '✓ Share saved as a draft',
    '✓ Todo update proposed',
  ]);
  expect(container.querySelector('a')).toBeNull();
  expect(container.textContent).not.toMatch(/undefined|entry #/);
});

test('the owner sees the details and links as before', () => {
  const { container } = renderActions({
    detailed: true,
    toolCallsMeta: [
      { name: 'semantic_search', status: 'success', query: 'my sister' },
      { name: 'read_full', status: 'success', kind: 'external', ref_id: 7,
        url: 'https://x.com/a/status/1', author_handle: 'a' },
      { name: 'read_full', status: 'success', kind: 'node', ref_id: 9001 },
      { name: 'update_artifact', status: 'success', kind: 'memory', created: true },
      { name: 'read_artifact', status: 'error', kind: 'plans', error: 'No readable artifact' },
      { name: '_client', client: 'web' },
    ],
  });
  expect(screen.getByText(/Actions taken \(5\)/)).toBeInTheDocument();
  expect(container.textContent).toContain('“my sister”');
  expect(screen.getByText("@a's post").closest('a'))
    .toHaveAttribute('href', 'https://x.com/a/status/1');
  expect(screen.getByText('entry #9001').closest('a'))
    .toHaveAttribute('href', '/node/9001');
  expect(container.textContent).toContain('Created artifact memory');
  expect(container.textContent).toContain('— No readable artifact');
});

test('nothing to show: no block', () => {
  const { container } = renderActions({ toolCallsMeta: [{ name: '_mode' }], detailed: false });
  expect(container).toBeEmptyDOMElement();
  const none = renderActions({ toolCallsMeta: null, detailed: false });
  expect(none.container).toBeEmptyDOMElement();
});

test('collapsed: only the count', () => {
  render(
    <MemoryRouter>
      <ToolActionsTaken toolCallsMeta={SHARED} detailed={false} expanded={false} onToggle={() => {}} />
    </MemoryRouter>,
  );
  expect(screen.getByText(/Actions taken \(9\)/)).toBeInTheDocument();
  expect(screen.queryByText(/Read in full/)).toBeNull();
});

// A proposal in someone else's reply: its text shows, its accept buttons
// and their status do not.
const PROPOSAL = [
  'Here is what I would change.',
  '### Completed',
  '- Call the bank',
  '### New Tasks',
  '- Book the dentist',
  '### Feedback',
  'Voice mode is lovely.',
  '### Feedback category',
  'praise',
  ':::share insight',
  'A thought worth sharing.',
  ':::',
].join('\n');

test('a proposal shows its accept buttons to the owner only', () => {
  const meta = [{ name: 'propose_todo', status: 'success' }, { name: 'propose_feedback', status: 'success' }];
  const owner = render(
    <MemoryRouter>
      <ProposalInline content={PROPOSAL} nodeId={5} toolCallsMeta={meta} />
    </MemoryRouter>,
  );
  expect(within(owner.container).getByText('Apply changes to my Todo')).toBeInTheDocument();
  expect(within(owner.container).getByText('Send feedback')).toBeInTheDocument();
  expect(within(owner.container).getByText('Save to shares')).toBeInTheDocument();
  owner.unmount();

  const other = render(
    <MemoryRouter>
      <ProposalInline content={PROPOSAL} nodeId={5} toolCallsMeta={meta} canAct={false} />
    </MemoryRouter>,
  );
  expect(other.container.textContent).toContain('Book the dentist');
  expect(other.container.textContent).toContain('Voice mode is lovely.');
  expect(other.container.textContent).toContain('A thought worth sharing.');
  expect(other.container.querySelector('button')?.textContent || '').not.toMatch(/Apply|Send|Save/);
  expect(within(other.container).queryByText(/Apply changes|Send feedback|Save to shares|Todo updated/))
    .toBeNull();
});

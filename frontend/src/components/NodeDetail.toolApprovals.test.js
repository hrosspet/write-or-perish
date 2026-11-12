// Peter, 2026-11-12: the AI's calls that change the user's data wait for
// approval when the thread holds text they didn't write. The thread page
// opens one dialog per reply with every waiting call, its diff, and
// Approve / Reject for each; closing it decides nothing.
const mockGet = jest.fn();
const mockPost = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    get: (...args) => mockGet(...args),
    post: (...args) => mockPost(...args),
    put: jest.fn(() => Promise.resolve({ data: {} })),
    delete: jest.fn(() => Promise.resolve({ data: {} })),
  },
}));

let mockUser;
jest.mock('../contexts/UserContext', () => ({ useUser: () => ({ user: mockUser }) }));
jest.mock('../contexts/ToastContext', () => ({
  useToast: () => ({ addToast: () => {}, removeToast: () => {} }),
}));
jest.mock('../hooks/useSSE', () => ({
  useLlmTextStream: () => ({ text: '', done: false }),
}));
jest.mock('./NodeForm', () => () => null);
jest.mock('./MarkdownBody', () => ({ children }) => <div>{children}</div>);
jest.mock('./NodeFormModal', () => () => null);
jest.mock('./ModelSelector', () => () => null);
jest.mock('./SpeakerIcon', () => () => null);
jest.mock('./DownloadAudioIcon', () => () => null);
jest.mock('./SemanticNeighbors', () => () => null);
jest.mock('./FeedPicks', () => () => null);
jest.mock('./NodeFooter', () => ({ children }) => <div>{children}</div>);
jest.mock('./QuotedContent', () => ({ content }) => <div>{content}</div>);

import React from 'react';
import { render, screen, fireEvent, act, within } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import NodeDetailWrapper from './NodeDetailWrapper';
import { approvalReason, groupByTurn } from './ToolApprovalDialog';

const ME = { id: 1, username: 'ben', craft_mode: false, is_admin: false, glean_enabled: false };

const MEMORY = {
  id: 11, node_id: 90, turn_node_id: 90, tool: 'update_artifact',
  kinds: ['other_user'], title: 'Change your Memory', kind: 'memory',
  artifact_title: 'Memory', created: false, base_version: 1,
  current_version: 1, stale: false,
  lines: [{ type: 'same', text: '- likes tea' }, { type: 'add', text: '- trusts anna' }],
};
const SCRATCH = {
  ...MEMORY, id: 12, title: 'Change your Scratchpad', kind: 'scratchpad',
  artifact_title: 'Scratchpad', lines: [{ type: 'add', text: 'more notes' }],
};

const reply = (overrides = {}) => ({
  id: 90, node_type: 'llm', llm_model: 'gpt-5', user: { id: 3, username: 'gpt-5' },
  user_id: 3, parent_user_id: 1, content: 'I proposed two changes.',
  ai_usage: 'chat', privacy_level: 'private', llm_task_status: 'completed',
  ancestors: [], children: [], child_count: 0, in_read_thread: false,
  read_reply_above: false, glean_thread: false, ...overrides,
});

beforeAll(() => {
  Element.prototype.scrollIntoView = () => {};
});

let routes;
beforeEach(() => {
  mockUser = ME;
  routes = {};
  mockGet.mockReset();
  mockPost.mockReset();
  localStorage.clear();
  mockGet.mockImplementation((url) => {
    if (url in routes) return Promise.resolve({ data: routes[url] });
    return Promise.reject(new Error(`unexpected GET ${url}`));
  });
  jest.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
  console.error.mockRestore();
});

const renderAt = (path) => render(
  <MemoryRouter initialEntries={[path]}>
    <Routes>
      <Route path="/node/:id" element={<NodeDetailWrapper />} />
    </Routes>
  </MemoryRouter>,
);

test('the reason names the kinds of text', () => {
  expect(approvalReason(['other_user', 'saved_reference'])).toBe(
    "This thread holds text you didn't write (another person's writing and "
    + 'a saved reference), so Loore asks before the AI changes your data.');
});

test('calls are grouped by reply, oldest first', () => {
  const other = { ...MEMORY, id: 5, turn_node_id: 70 };
  expect(groupByTurn([SCRATCH, MEMORY, other]).map((g) => g.map((i) => i.id)))
    .toEqual([[5], [11, 12]]);
});

test('one dialog shows every waiting call of the reply with its diff', async () => {
  routes['/nodes/90'] = reply({ pending_approvals: [MEMORY, SCRATCH] });
  renderAt('/node/90');

  const dialog = await screen.findByRole('dialog');
  expect(within(dialog).getByText('Change your Memory')).toBeInTheDocument();
  expect(within(dialog).getByText('Change your Scratchpad')).toBeInTheDocument();
  expect(within(dialog).getByText(/trusts anna/)).toBeInTheDocument();
  expect(within(dialog).getAllByText('Approve')).toHaveLength(2);
});

test('each call is decided on its own; the dialog closes with the last', async () => {
  routes['/nodes/90'] = reply({ pending_approvals: [MEMORY, SCRATCH] });
  mockPost.mockImplementation((url) => {
    if (url === '/tool-approvals/11/approve') {
      return Promise.resolve({ data: { outcome: 'approved', pending: [SCRATCH] } });
    }
    if (url === '/tool-approvals/12/reject') {
      return Promise.resolve({ data: { outcome: 'rejected', pending: [] } });
    }
    return Promise.reject(new Error(`unexpected POST ${url}`));
  });
  renderAt('/node/90');
  await screen.findByRole('dialog');

  await act(async () => {
    fireEvent.click(within(screen.getByTestId('approval-11')).getByText('Approve'));
  });
  expect(mockPost).toHaveBeenCalledWith('/tool-approvals/11/approve');
  expect(screen.queryByTestId('approval-11')).toBeNull();

  await act(async () => {
    fireEvent.click(within(screen.getByTestId('approval-12')).getByText('Reject'));
  });
  expect(mockPost).toHaveBeenCalledWith('/tool-approvals/12/reject');
  expect(screen.queryByRole('dialog')).toBeNull();
});

test('closing decides nothing and a line reopens it', async () => {
  routes['/nodes/90'] = reply({ pending_approvals: [MEMORY] });
  renderAt('/node/90');
  await screen.findByRole('dialog');

  fireEvent.click(screen.getByText('Later'));

  expect(screen.queryByRole('dialog')).toBeNull();
  expect(mockPost).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText(/1 change waits for your approval/));
  expect(screen.getByRole('dialog')).toBeInTheDocument();
});

test('a failed run keeps the call and says why', async () => {
  routes['/nodes/90'] = reply({ pending_approvals: [MEMORY] });
  mockPost.mockResolvedValue({
    data: { outcome: 'failed', pending: [MEMORY], error: "'memory' changed since v1, now v2. Nothing was saved." },
  });
  renderAt('/node/90');
  await screen.findByRole('dialog');

  await act(async () => {
    fireEvent.click(screen.getByText('Approve'));
  });

  expect(screen.getByTestId('approval-11')).toBeInTheDocument();
  expect(screen.getByText(/changed since v1, now v2/)).toBeInTheDocument();
});

test('a stale write cannot be approved, only rejected', async () => {
  routes['/nodes/90'] = reply({
    pending_approvals: [{ ...MEMORY, stale: true, current_version: 2 }],
  });
  renderAt('/node/90');
  await screen.findByRole('dialog');

  expect(screen.getByText('Approve')).toBeDisabled();
  expect(screen.getByText(/changed since \(now v2\)/)).toBeInTheDocument();
  expect(screen.getByText('Reject')).not.toBeDisabled();
});

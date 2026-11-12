// Peter, 2026-11-12: "autogenerate is off in public threads and circles
// and can't be turned on". The automatic reply after an entry says it is
// automatic (the server refuses it under a shared node), and is not asked
// at all for a public or circles entry. The toggle shows off and is
// disabled on a circles node (a public node hides it, as before).
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
const mockTextStream = jest.fn();
jest.mock('../hooks/useSSE', () => ({
  useLlmTextStream: (...args) => mockTextStream(...args),
}));

let mockFormProps;
jest.mock('./NodeForm', () => (props) => { mockFormProps = props; return null; });
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
import { render, screen, act } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import NodeDetailWrapper from './NodeDetailWrapper';

const ME = { id: 1, username: 'peter', craft_mode: true, is_admin: false, glean_enabled: false };

const entry = (id, content, overrides = {}) => ({
  id, node_type: 'user', llm_model: null,
  user: { id: 1, username: 'peter' }, user_id: 1,
  content, ai_usage: 'chat', privacy_level: 'private',
  ancestors: [], children: [], child_count: 0,
  in_read_thread: false, read_reply_above: false, glean_thread: false,
  ...overrides,
});

beforeAll(() => {
  Element.prototype.scrollIntoView = () => {};
});

let routes;
beforeEach(() => {
  mockUser = ME;
  mockFormProps = null;
  routes = {};
  mockGet.mockReset();
  mockPost.mockReset();
  mockTextStream.mockReset();
  mockTextStream.mockReturnValue({ text: '', done: false });
  localStorage.clear();
  localStorage.setItem('loore_auto_generate', 'true');
  mockGet.mockImplementation((url) => {
    if (url.endsWith('/llm-status')) return new Promise(() => {});
    if (url in routes) return Promise.resolve({ data: routes[url] });
    return Promise.reject(new Error(`unexpected GET ${url}`));
  });
  mockPost.mockResolvedValue({ data: { node_id: 80, status: 'pending' } });
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

test('the automatic reply after a private entry says it is automatic', async () => {
  routes['/nodes/10'] = entry(10, 'Earlier entry.');
  routes['/nodes/70'] = entry(70, 'New entry.');
  renderAt('/node/10');
  await screen.findByText('Earlier entry.');

  await act(async () => {
    await mockFormProps.onSuccess({ id: 70, privacy_level: 'private' });
  });

  expect(mockPost).toHaveBeenCalledTimes(1);
  expect(mockPost.mock.calls[0][0]).toBe('/nodes/70/llm');
  expect(mockPost.mock.calls[0][1]).toEqual(
    expect.objectContaining({ auto_generate: true }));
});

test('a public entry written on a private page gets no automatic reply', async () => {
  routes['/nodes/10'] = entry(10, 'Earlier entry.');
  routes['/nodes/70'] = entry(70, 'New entry.', { privacy_level: 'public' });
  renderAt('/node/10');
  await screen.findByText('Earlier entry.');

  await act(async () => {
    await mockFormProps.onSuccess({ id: 70, privacy_level: 'public' });
  });

  expect(mockPost).not.toHaveBeenCalled();
});

test('under a circles node there is no automatic reply and the toggle is off and disabled', async () => {
  routes['/nodes/10'] = entry(10, 'Shared with circles.', { privacy_level: 'circles' });
  routes['/nodes/70'] = entry(70, 'New entry.', { privacy_level: 'circles' });
  renderAt('/node/10');
  await screen.findByText('Shared with circles.');

  const toggle = screen.getByText('Auto-generate').closest('button');
  expect(toggle).toBeDisabled();
  expect(toggle).toHaveAttribute('aria-pressed', 'false');

  await act(async () => {
    await mockFormProps.onSuccess({ id: 70, privacy_level: 'circles' });
  });
  expect(mockPost).not.toHaveBeenCalled();
  // The stored preference for private threads is untouched.
  expect(localStorage.getItem('loore_auto_generate')).toBe('true');
});

test('a refused automatic reply leaves the page on the entry without an error', async () => {
  routes['/nodes/10'] = entry(10, 'Earlier entry.');
  routes['/nodes/70'] = entry(70, 'New entry.');
  mockPost.mockRejectedValue({
    response: { status: 409, data: { code: 'auto_generate_off', error: 'off' } },
  });
  renderAt('/node/10');
  await screen.findByText('Earlier entry.');

  await act(async () => {
    await mockFormProps.onSuccess({ id: 70, privacy_level: 'private' });
  });

  expect(await screen.findByText('New entry.')).toBeInTheDocument();
});


const aiReply = (id, overrides = {}) => ({
  id, node_type: 'llm', llm_model: 'gpt-5', username: 'gpt-5', user_id: 3,
  parent_user_id: 1, content: `AI reply ${id}`, privacy_level: 'private',
  ai_usage: 'chat', llm_task_status: 'completed', children: [], child_count: 0,
  ...overrides,
});

test('my private AI reply under a public entry says only I see it', async () => {
  routes['/nodes/10'] = entry(10, 'A public entry.', {
    privacy_level: 'public',
    children: [aiReply(11)],
  });
  renderAt('/node/10');
  await screen.findByText('A public entry.');

  expect(screen.getByText(/Only you see this reply/)).toBeInTheDocument();
});

test('no note under a private entry, nor on a public reply', async () => {
  routes['/nodes/10'] = entry(10, 'A private entry.', {
    children: [aiReply(11)],
  });
  routes['/nodes/20'] = entry(20, 'A public entry.', {
    privacy_level: 'public',
    children: [aiReply(21, { privacy_level: 'public' })],
  });
  const first = renderAt('/node/10');
  await screen.findByText('A private entry.');
  expect(screen.queryByText(/Only you see this reply/)).toBeNull();
  first.unmount();

  renderAt('/node/20');
  await screen.findByText('A public entry.');
  expect(screen.queryByText(/Only you see this reply/)).toBeNull();
});

test('the focal AI reply carries the note under a circles entry', async () => {
  routes['/nodes/11'] = aiReply(11, {
    user: { id: 3, username: 'gpt-5' },
    ancestors: [entry(10, 'Shared with circles.', { privacy_level: 'circles' })],
    in_read_thread: false, read_reply_above: false, glean_thread: false,
  });
  renderAt('/node/11');
  await screen.findByText('AI reply 11');

  expect(screen.getByText(/Only you see this reply/)).toBeInTheDocument();
});

test("someone else's private AI reply shows no note", async () => {
  mockUser = { ...ME, id: 2, username: 'anna' };
  routes['/nodes/10'] = entry(10, 'A public entry.', {
    privacy_level: 'public', children: [aiReply(11)],
  });
  renderAt('/node/10');
  await screen.findByText('A public entry.');

  expect(screen.queryByText(/Only you see this reply/)).toBeNull();
});

// #648 (Peter, 2026-11-12): a notice at 90 % of the limit on the size of
// the next AI reply's prompt, and the limit itself. The thread's API
// response carries `thread_length`; the reply form shows a calm notice,
// and at the limit the limit's message with the way to a new thread in
// place of LLM Response. No price, no token numbers.
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
const mockAddToast = jest.fn();
jest.mock('../contexts/ToastContext', () => ({
  useToast: () => ({ addToast: mockAddToast, removeToast: () => {} }),
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
import { render, screen, act, fireEvent } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import NodeDetailWrapper from './NodeDetailWrapper';
import {
  THREAD_LIMIT_TEXT, THREAD_NOTICE_TEXT,
} from '../utils/threadLength';

const ME = { id: 1, username: 'peter', craft_mode: true, is_admin: false, glean_enabled: false };

const entry = (id, content, overrides = {}) => ({
  id, node_type: 'user', llm_model: null,
  user: { id: 1, username: 'peter' }, user_id: 1,
  content, ai_usage: 'chat', privacy_level: 'private',
  ancestors: [], children: [], child_count: 0,
  in_read_thread: false, read_reply_above: false, glean_thread: false,
  thread_length: null,
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
  mockAddToast.mockReset();
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

test('under the notice size nothing is shown and LLM Response is offered', async () => {
  routes['/nodes/10'] = entry(10, 'A short thread.');
  renderAt('/node/10');
  await screen.findByText('A short thread.');

  expect(screen.queryByText(THREAD_NOTICE_TEXT)).not.toBeInTheDocument();
  expect(screen.queryByText(THREAD_LIMIT_TEXT)).not.toBeInTheDocument();
  expect(screen.getByText('LLM Response')).toBeInTheDocument();
});

test('from 90 % of the limit the reply form shows a calm notice', async () => {
  routes['/nodes/10'] = entry(10, 'A long thread.', { thread_length: 'notice' });
  renderAt('/node/10');
  await screen.findByText('A long thread.');

  expect(screen.getByText(THREAD_NOTICE_TEXT)).toBeInTheDocument();
  // Still a normal thread: LLM Response is offered, writing too.
  expect(screen.getByText('LLM Response')).toBeInTheDocument();
  expect(mockFormProps).not.toBeNull();
  const link = screen.getByText('Start a new thread');
  expect(link.closest('a')).toHaveAttribute('href', '/textmode');
  // No price, no token numbers, no percentages.
  expect(THREAD_NOTICE_TEXT).not.toMatch(/\d|\$|%|token/i);
});

test('at the limit the form says so, offers a new thread and no LLM Response', async () => {
  routes['/nodes/10'] = entry(10, 'A very long thread.', { thread_length: 'limit' });
  renderAt('/node/10');
  await screen.findByText('A very long thread.');

  expect(screen.getByText(THREAD_LIMIT_TEXT)).toBeInTheDocument();
  expect(screen.queryByText('LLM Response')).not.toBeInTheDocument();
  expect(screen.getByText('Start a new thread').closest('a'))
    .toHaveAttribute('href', '/textmode');
  // Writing in the thread still works.
  expect(mockFormProps).not.toBeNull();
  // The message makes sense for a thread holding one large document too.
  expect(THREAD_LIMIT_TEXT).toMatch(/including what it quotes/);
  expect(THREAD_LIMIT_TEXT).not.toMatch(/\d|\$|%|token/i);
});

test('the notice is for the owner only', async () => {
  routes['/nodes/10'] = entry(10, 'Someone else\'s thread.', {
    user_id: 2, user: { id: 2, username: 'other' }, privacy_level: 'public',
    thread_length: 'limit',
  });
  renderAt('/node/10');
  await screen.findByText('Someone else\'s thread.');
  expect(screen.queryByText(THREAD_LIMIT_TEXT)).not.toBeInTheDocument();
});

test('a reply refused for the thread length turns the form to the limit, no toast', async () => {
  routes['/nodes/10'] = entry(10, 'Grew past it.');
  mockPost.mockRejectedValue({
    response: {
      status: 409,
      data: { code: 'thread_too_long', error: 'Server text for the limit.' },
    },
  });
  renderAt('/node/10');
  await screen.findByText('Grew past it.');

  await act(async () => {
    fireEvent.click(screen.getByText('LLM Response'));
  });

  expect(await screen.findByText('Server text for the limit.')).toBeInTheDocument();
  expect(screen.queryByText('LLM Response')).not.toBeInTheDocument();
  expect(mockAddToast).not.toHaveBeenCalled();
});

test('at the limit an entry is written without asking for an automatic reply', async () => {
  routes['/nodes/10'] = entry(10, 'At the limit.', { thread_length: 'limit' });
  routes['/nodes/70'] = entry(70, 'New entry.', { thread_length: 'limit' });
  renderAt('/node/10');
  await screen.findByText('At the limit.');

  await act(async () => {
    await mockFormProps.onSuccess({ id: 70, privacy_level: 'private' });
  });

  expect(mockPost).not.toHaveBeenCalled();
  expect(await screen.findByText('New entry.')).toBeInTheDocument();
});

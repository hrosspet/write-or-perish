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
import {
  render, screen, act, fireEvent, waitFor,
} from '@testing-library/react';
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

test('a reply finishing in place brings the thread length with it (this turn, not the next)', async () => {
  routes['/nodes/80'] = entry(80, 'Thinking…', {
    node_type: 'llm', llm_model: 'gpt-5', llm_task_status: 'processing',
    user: { id: 9, username: 'gpt-5' }, user_id: 9, parent_user_id: 1,
    thread_length: null,
  });
  mockGet.mockImplementation((url) => {
    if (url === '/nodes/80/llm-status') {
      return Promise.resolve({ data: {
        node_id: 80, status: 'completed', content: 'The answer.',
        thread_length: 'notice',
      } });
    }
    if (url in routes) return Promise.resolve({ data: routes[url] });
    return Promise.resolve({ data: {} });
  });
  renderAt('/node/80?awaitLlm=80');

  expect(await screen.findByText('The answer.')).toBeInTheDocument();
  expect(await screen.findByText(THREAD_NOTICE_TEXT)).toBeInTheDocument();
});

describe('a reply refused for the thread length while its own page watches it', () => {
  const VOICE_ROOT = {
    id: 9, username: 'peter', user_id: 1, node_type: 'user', content: '',
    is_system_prompt: true, prompt_key: 'voice', ai_usage: 'chat',
    privacy_level: 'private', child_count: 1,
  };
  const ENTRY = {
    id: 20, username: 'peter', user_id: 1, node_type: 'user',
    content: 'I keep going back and forth.', ai_usage: 'chat',
    privacy_level: 'private', child_count: 1,
  };
  const READ_PROMPT = {
    id: 31, username: 'peter', user_id: 1, node_type: 'user', content: '',
    is_system_prompt: true, prompt_key: 'read_thread', ai_usage: 'chat',
    privacy_level: 'private', child_count: 1,
  };
  // The Glean entry, open: since #717 a refused Glean never counts as the
  // thread's length, so the server answers no limit here.
  const gleanEntry = {
    ...READ_PROMPT, user: { id: 1, username: 'peter' },
    ancestors: [VOICE_ROOT, ENTRY], children: [], child_count: 0,
    in_read_thread: true, read_reply_above: false, glean_thread: true,
    thread_length: null,
  };
  const pendingGleaning = (id) => ({
    id, node_type: 'llm', llm_model: 'claude-haiku-5.5',
    user: { id: 3, username: 'claude-haiku-5.5' }, user_id: 3, parent_user_id: 1,
    content: '', llm_task_status: 'processing',
    tool_calls_meta: [{ name: '_live' }],
    ai_usage: 'chat', privacy_level: 'private',
    ancestors: [VOICE_ROOT, ENTRY, READ_PROMPT], children: [], child_count: 0,
    in_read_thread: true, read_reply_above: true, glean_thread: true,
  });
  const refused = (id, error) => ({
    node_id: id, status: 'failed', error, error_code: 'thread_too_long',
  });
  const gleanButton = () => Array.from(
    document.querySelectorAll('[data-action-group] button'),
  ).find((b) => b.textContent === 'Glean') || null;

  const serve = (statuses) => {
    mockGet.mockImplementation((url) => {
      const polled = url.match(/^\/nodes\/(\d+)\/llm-status$/);
      if (polled) return Promise.resolve({ data: statuses[polled[1]] });
      if (url.endsWith('/resolve-quotes')) {
        return Promise.resolve({ data: { quotes: {}, external_quotes: {}, has_quotes: false } });
      }
      if (url in routes) return Promise.resolve({ data: routes[url] });
      return Promise.reject(new Error(`unexpected GET ${url}`));
    });
  };

  test('a Glean says why with the limit sentence, on every press (#746)', async () => {
    mockUser = { ...ME, glean_enabled: true };
    routes['/nodes/31'] = gleanEntry;
    routes['/nodes/41'] = pendingGleaning(41);
    routes['/nodes/42'] = pendingGleaning(42);
    serve({ 41: refused(41, THREAD_LIMIT_TEXT), 42: refused(42, THREAD_LIMIT_TEXT) });
    mockPost
      .mockResolvedValueOnce({ data: { prompt_node_id: null, llm_node_id: 41 } })
      .mockResolvedValueOnce({ data: { prompt_node_id: null, llm_node_id: 42 } });
    renderAt('/node/31');

    for (const n of [1, 2]) {
      await waitFor(() => expect(gleanButton()).toBeInTheDocument());
      fireEvent.click(gleanButton());
      // The gleaning page watches the Glean, which the server refuses: a
      // toast with the server's sentence, and back to the Glean entry.
      await waitFor(() => expect(mockAddToast).toHaveBeenCalledTimes(n));
      expect(mockAddToast).toHaveBeenLastCalledWith(THREAD_LIMIT_TEXT, 8000);
      await waitFor(() => expect(gleanButton()).toBeInTheDocument());
    }
    expect(mockPost.mock.calls.map(([url]) => url))
      .toEqual(['/read/from-node/31', '/read/from-node/31']);
    // #717 stays: the refusal is not the thread's length, so the entry's
    // form shows no limit and Glean is still offered.
    expect(screen.queryByText(THREAD_LIMIT_TEXT)).not.toBeInTheDocument();
  });

  test('a Glean refused without a text gets the app\'s limit sentence', async () => {
    mockUser = { ...ME, glean_enabled: true };
    routes['/nodes/31'] = gleanEntry;
    routes['/nodes/43'] = pendingGleaning(43);
    serve({ 43: refused(43, null) });
    renderAt('/node/43');
    await waitFor(() => expect(mockAddToast).toHaveBeenCalledWith(THREAD_LIMIT_TEXT, 8000));
  });

  test('a chat reply is not toasted: its entry shows the limit (#648)', async () => {
    routes['/nodes/10'] = entry(10, 'Grew past it.', { thread_length: 'limit' });
    routes['/nodes/80'] = entry(80, '', {
      node_type: 'llm', llm_model: 'gpt-5', llm_task_status: 'processing',
      user: { id: 9, username: 'gpt-5' }, user_id: 9, parent_user_id: 1,
      ancestors: [{ id: 10, username: 'peter', user_id: 1, node_type: 'user',
        content: 'Grew past it.', ai_usage: 'chat', privacy_level: 'private' }],
    });
    serve({ 80: refused(80, THREAD_LIMIT_TEXT) });
    renderAt('/node/80');

    expect(await screen.findByText(THREAD_LIMIT_TEXT)).toBeInTheDocument();
    expect(screen.queryByText('LLM Response')).not.toBeInTheDocument();
    expect(mockAddToast).not.toHaveBeenCalled();
  });
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

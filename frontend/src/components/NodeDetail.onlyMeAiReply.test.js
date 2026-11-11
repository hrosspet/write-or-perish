// An AI reply asked for under the user's own Only-me node in a shared
// thread (Circles): the server sends what it reads of the user's data
// (thread_ai_reads), the page asks in the private wording and sends the
// key. A refusal is retried at most once, then shown.
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

const mockToast = jest.fn();
jest.mock('../contexts/UserContext', () => ({
  useUser: () => ({ user: {
    id: 2, username: 'ben', craft_mode: true, preferred_model: 'ben-model',
    default_ai_usage: 'chat', circles_enabled: true,
  } }),
}));
jest.mock('../contexts/ToastContext', () => ({
  useToast: () => ({ addToast: mockToast, removeToast: () => {} }),
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
jest.mock('./NodeFooter', () => (props) => <div>{props.children}</div>);
jest.mock('./QuotedContent', () => (props) => <div>{props.content}</div>);

import React from 'react';
import { render, screen, act, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import NodeDetailWrapper from './NodeDetailWrapper';

// ben's own AI reply in anna's shared thread, kept to Only me.
const onlyMeReply = (overrides = {}) => ({
  id: 30, node_type: 'llm', llm_model: 'ben-model',
  user: { id: 9, username: 'ben-model' }, human_owner_username: 'ben',
  parent_user_id: 2, content: 'My own AI reply.', ai_usage: 'chat',
  privacy_level: 'private', llm_task_status: 'completed',
  ancestors: [], children: [], child_count: 0,
  in_read_thread: false, read_reply_above: false, glean_thread: false,
  thread_ai_reads: { entry_id: 10, ai_reads: ['profile'], ai_reads_key: 'key-1' },
  ...overrides,
});

const refused = (key) => ({ response: { status: 409, data: {
  code: 'circle_ai_reads_changed',
  error: 'The prompt of this thread asks for more of your own data than you agreed to.',
  ai_reads: ['profile', 'memory'], ai_reads_key: key,
} } });

let routes;
beforeAll(() => { Element.prototype.scrollIntoView = () => {}; });
beforeEach(() => {
  routes = { '/nodes/30': onlyMeReply() };
  mockGet.mockReset();
  mockPost.mockReset();
  mockToast.mockReset();
  localStorage.clear();
  localStorage.setItem('loore_auto_generate', 'false');
  mockGet.mockImplementation((url) => {
    if (url.endsWith('/llm-status')) return new Promise(() => {});
    if (url.includes('/suggested-model')) {
      return Promise.resolve({ data: { suggested_model: 'ben-model' } });
    }
    if (url in routes) return Promise.resolve({ data: routes[url] });
    return Promise.reject(new Error(`unexpected GET ${url}`));
  });
  mockPost.mockResolvedValue({ data: { node_id: 80, status: 'pending' } });
  jest.spyOn(console, 'error').mockImplementation(() => {});
});
afterEach(() => { console.error.mockRestore(); });

const renderReply = async () => {
  render(
    <MemoryRouter initialEntries={['/node/30']}>
      <Routes><Route path="/node/:id" element={<NodeDetailWrapper />} /></Routes>
    </MemoryRouter>,
  );
  await screen.findByText('My own AI reply.');
};

const press = async (name) => {
  await act(async () => { fireEvent.click(screen.getByRole('button', { name })); });
};

test('the private dialog is shown and its key is sent', async () => {
  await renderReply();
  await press('LLM Response');
  expect(screen.getByRole('heading', { name: 'Generate an AI reply?' })).toBeInTheDocument();
  expect(screen.getByText(
    'Only you will see it. Your AI reads your profile for this reply, '
    + 'as the prompt shared in this thread asks.',
  )).toBeInTheDocument();
  expect(mockPost).not.toHaveBeenCalled();
  await press('Generate');
  await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
  expect(mockPost.mock.calls[0][0]).toBe('/nodes/30/llm');
  expect(mockPost.mock.calls[0][1].ai_reads_ack).toBe('private:key-1');
});

test('refused again after one retry: the message is shown and nothing more is sent', async () => {
  let n = 0;
  mockPost.mockImplementation(() => Promise.reject(refused(`key-${n += 2}`)));
  await renderReply();
  await press('LLM Response');
  await press('Generate');
  // The list changed: asked once more, then refused again.
  await screen.findByText(/Your AI reads your profile and memory/);
  await press('Generate');
  await waitFor(() => expect(mockToast).toHaveBeenCalled());
  expect(mockToast.mock.calls[0][0]).toMatch(/asks for more of your own data/);
  await new Promise((r) => setTimeout(r, 50));
  expect(mockPost).toHaveBeenCalledTimes(2);
});

test('a node the page knows nothing about stops after one retry too', async () => {
  routes['/nodes/30'] = onlyMeReply({ thread_ai_reads: undefined });
  let n = 0;
  mockPost.mockImplementation(() => Promise.reject(refused(`key-${n += 2}`)));
  await renderReply();
  await press('LLM Response');
  await waitFor(() => expect(mockToast).toHaveBeenCalled());
  await new Promise((r) => setTimeout(r, 50));
  expect(mockPost).toHaveBeenCalledTimes(2);
});

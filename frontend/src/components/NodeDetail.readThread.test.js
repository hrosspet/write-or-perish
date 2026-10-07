// The thread page in a Community Archive read thread.
// #386: a read reply fetched before its batch was submitted said
//   "Thinking…" until a refresh; the poll's batch entry now reaches it.
// #387: with auto-generate on, an upload under the picks offered only
//   "Read further"; when Loore can't know what the user wants next it
//   shows both choices.
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

// The reply's text stream: recorded, never opened (its return value is
// set in beforeEach: CRA's jest config resets mock implementations).
const mockTextStream = jest.fn();
jest.mock('../hooks/useSSE', () => ({
  useLlmTextStream: (...args) => mockTextStream(...args),
}));

// Children with their own fetching (and the markdown renderer), stubbed.
jest.mock('./MarkdownBody', () => ({ children }) => <div>{children}</div>);
jest.mock('./NodeForm', () => () => null);
jest.mock('./NodeFormModal', () => () => null);
jest.mock('./ModelSelector', () => () => null);
jest.mock('./SpeakerIcon', () => () => null);
jest.mock('./DownloadAudioIcon', () => () => null);
jest.mock('./SemanticNeighbors', () => () => null);
jest.mock('./FeedPicks', () => () => null);
jest.mock('./NodeFooter', () => ({ children }) => <div>{children}</div>);
jest.mock('./QuotedContent', () => ({ content }) => <div>{content}</div>);

import React from 'react';
import { render, screen, act, waitFor } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import NodeDetail from './NodeDetail';

const ME = { id: 1, username: 'peter', craft_mode: true, is_admin: true };

const BATCH = {
  name: '_batch', batch_id: 'msgbatch_1', custom_id: 'node-50',
  status: 'submitted', submitted_at: '2026-10-02T10:00:00',
};

// The read prompt a Home-page Read attaches, with the reply under it.
const READ_PROMPT = {
  id: 49, username: 'peter', user_id: 1, node_type: 'user',
  content: 'Read the day', is_system_prompt: true, prompt_key: 'read',
  ai_usage: 'chat', privacy_level: 'private', child_count: 1,
};

// The reply as the page fetches it right after the Read starts: the
// worker has not submitted the batch yet, so there is no "_batch" entry.
const pendingRead = () => ({
  id: 50, node_type: 'llm', llm_model: 'gpt-6-luna',
  user: { id: 2, username: 'gpt-6-luna' }, user_id: 2, parent_user_id: 1,
  content: '[LLM response generation pending...]',
  llm_task_status: 'processing', tool_calls_meta: null,
  ai_usage: 'chat', privacy_level: 'private',
  ancestors: [READ_PROMPT], children: [], child_count: 0,
  in_read_thread: true, read_reply_above: true,
});

// An audio upload saved under a finished read reply: a plain reply,
// nothing under it.
const uploadUnderPicks = (overrides = {}) => ({
  id: 60, node_type: 'user', llm_model: null,
  user: { id: 1, username: 'peter' }, user_id: 1,
  content: 'What I make of these picks (transcribed).',
  ai_usage: 'chat', privacy_level: 'private',
  ancestors: [
    READ_PROMPT,
    { id: 50, username: 'gpt-6-luna', user_id: 2, parent_user_id: 1,
      node_type: 'llm', llm_model: 'gpt-6-luna', content: 'Picks',
      ai_usage: 'chat', privacy_level: 'private', child_count: 1 },
  ],
  children: [], child_count: 0,
  in_read_thread: true, read_reply_above: true,
  ...overrides,
});

const AI_REPLY_CHILD = {
  id: 61, node_type: 'llm', llm_model: 'claude-opus-5.5', user_id: 3,
  username: 'claude-opus-5.5', content: 'About those picks…', children: [],
};

beforeAll(() => {
  // The page scrolls to the focal node; jsdom has no layout.
  Element.prototype.scrollIntoView = () => {};
});

let routes;
let pollAnswers;
beforeEach(() => {
  mockUser = ME;
  routes = {};
  pollAnswers = [];
  mockGet.mockReset();
  mockPost.mockReset();
  mockTextStream.mockReset();
  mockTextStream.mockReturnValue({ text: '', done: false });
  localStorage.clear();
  document.title = 'Loore';
  mockGet.mockImplementation((url) => {
    if (url.endsWith('/llm-status')) {
      const next = pollAnswers.shift();
      if (next) return next;
      return new Promise(() => {}); // later polls: never answered
    }
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
      <Route path="/node/:id" element={<NodeDetail />} />
    </Routes>
  </MemoryRouter>,
);

const deferred = () => {
  let resolve;
  const promise = new Promise((r) => { resolve = r; });
  return { promise, resolve };
};

describe('#386: a pending read reply', () => {
  test('says "Processing…" once the poll reports its batch, without a refresh', async () => {
    routes['/nodes/50'] = pendingRead();
    const firstPoll = deferred();
    pollAnswers.push(firstPoll.promise);

    renderAt('/node/50');
    // Fetched before the batch existed: the model is still at work.
    expect(await screen.findByText('Thinking')).toBeInTheDocument();
    expect(document.title).toBe('Thinking… — Loore');
    await waitFor(() => expect(mockGet).toHaveBeenCalledWith(
      '/nodes/50/llm-status', expect.anything()));

    // The worker submits the batch; the poll says so.
    await act(async () => {
      firstPoll.resolve({ data: {
        node_id: 50, status: 'processing', progress: 50,
        stage: 'batch', batch_submitted_at: BATCH.submitted_at,
        tool_calls_meta: [BATCH], warnings: [],
      } });
    });

    expect(await screen.findByText('Processing')).toBeInTheDocument();
    expect(screen.queryByText('Thinking')).toBeNull();
    expect(document.title).toBe('Processing… — Loore');
    // The admin's rerun controls sit with the waiting state.
    expect(screen.getByRole('button', { name: 'Rerun live' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Resubmit batch' })).toBeInTheDocument();
    // A batch is never streamed: the text stream is let go.
    const [streamNodeId, streamOpts] = mockTextStream.mock.calls.at(-1);
    expect(streamNodeId).toBeNull();
    expect(streamOpts.enabled).toBe(false);
  });

  test('a pending reply that is not a batch stays "Thinking…"', async () => {
    routes['/nodes/50'] = pendingRead();
    pollAnswers.push(Promise.resolve({ data: {
      node_id: 50, status: 'processing', progress: 10,
      tool_calls_meta: [{ name: '_read' }], warnings: [],
    } }));

    renderAt('/node/50');
    expect(await screen.findByText('Thinking')).toBeInTheDocument();
    await waitFor(() => expect(mockGet).toHaveBeenCalledWith(
      '/nodes/50/llm-status', expect.anything()));
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByText('Thinking')).toBeInTheDocument();
    expect(screen.queryByText('Processing')).toBeNull();
    // Still streamed while it is written.
    const [streamNodeId, streamOpts] = mockTextStream.mock.calls.at(-1);
    expect(streamNodeId).toBe(50);
    expect(streamOpts.enabled).toBe(true);
  });
});

describe('#387: the action row in a read thread', () => {
  // The action row under the node (the admin's top-right Read button
  // carries the same label, so look only at the row's groups).
  const buttons = () => {
    const row = Array.from(document.querySelectorAll('[data-action-group] button'));
    const named = (label) => row.find((b) => b.textContent === label) || null;
    return { llm: named('LLM Response'), read: named('Read further') };
  };

  test('with auto-generate on, an upload under the picks offers both LLM Response and Read further', async () => {
    localStorage.setItem('loore_auto_generate', 'true');
    routes['/nodes/60'] = uploadUnderPicks();

    renderAt('/node/60');
    expect(await screen.findByText(/What I make of these picks/)).toBeInTheDocument();
    const { llm, read } = buttons();
    expect(llm).toBeInTheDocument();
    expect(llm).toBeEnabled();
    expect(read).toBeInTheDocument();
    expect(read).toBeEnabled();
  });

  test('with auto-generate off it offers both, as before', async () => {
    localStorage.setItem('loore_auto_generate', 'false');
    routes['/nodes/60'] = uploadUnderPicks();

    renderAt('/node/60');
    await screen.findByText(/What I make of these picks/);
    expect(buttons().llm).toBeInTheDocument();
    expect(buttons().read).toBeInTheDocument();
  });

  test('with auto-generate on, a reply it already answered offers Read further only', async () => {
    localStorage.setItem('loore_auto_generate', 'true');
    routes['/nodes/60'] = uploadUnderPicks({
      content: 'A typed reply.', children: [AI_REPLY_CHILD], child_count: 1,
    });

    renderAt('/node/60');
    await screen.findByText('A typed reply.');
    expect(buttons().llm).toBeNull();
    expect(buttons().read).toBeInTheDocument();
  });

  test('outside a read thread, auto-generate still hides LLM Response', async () => {
    localStorage.setItem('loore_auto_generate', 'true');
    routes['/nodes/60'] = uploadUnderPicks({
      content: 'A note.', ancestors: [], in_read_thread: false, read_reply_above: false,
    });

    renderAt('/node/60');
    await screen.findByText('A note.');
    expect(buttons().llm).toBeNull();
    expect(buttons().read).toBeNull();
  });
});

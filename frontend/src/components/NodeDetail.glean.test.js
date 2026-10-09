// Glean on the thread page (#435, Peter 2026-10-09):
// - a thread started from the Glean card offers the Glean button in the
//   action row of every turn;
// - any other thread offers "Glean for this reflection" in the entry's
//   menu instead, and no Glean button;
// - a user without Glean (the gate or their own switch) sees neither;
// - a finished gleaning is headed "Today's gleanings", and an empty day
//   says so with the count of tweets read.
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

jest.mock('./MarkdownBody', () => ({ children }) => <div>{children}</div>);
jest.mock('./NodeForm', () => () => null);
jest.mock('./NodeFormModal', () => () => null);
jest.mock('./ModelSelector', () => () => <span data-testid="read-model-picker" />);
jest.mock('./SpeakerIcon', () => () => null);
jest.mock('./DownloadAudioIcon', () => () => null);
jest.mock('./SemanticNeighbors', () => () => null);
jest.mock('./FeedPicks', () => () => null);
jest.mock('./NodeFooter', () => ({ children }) => <div>{children}</div>);
jest.mock('./QuotedContent', () => ({ content }) => <div>{content}</div>);

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import NodeDetailWrapper from './NodeDetailWrapper';

const USER = { id: 1, username: 'ana', craft_mode: false, is_admin: false, glean_enabled: true };

const VOICE_ROOT = {
  id: 10, username: 'ana', user_id: 1, node_type: 'user', content: '',
  is_system_prompt: true, prompt_key: 'voice', ai_usage: 'chat',
  privacy_level: 'private', child_count: 1,
};

// A voice reply in a thread; glean_thread says where the thread started.
const reply = (overrides = {}) => ({
  id: 30, node_type: 'llm', llm_model: 'claude-opus-5.5',
  user: { id: 2, username: 'claude-opus-5.5' }, user_id: 2, parent_user_id: 1,
  content: 'That sounds like a real fork in the road.',
  llm_task_status: 'completed', tool_calls_meta: null,
  ai_usage: 'chat', privacy_level: 'private',
  ancestors: [VOICE_ROOT, {
    id: 20, username: 'ana', user_id: 1, node_type: 'user',
    content: 'I keep going back and forth on onboarding.',
    ai_usage: 'chat', privacy_level: 'private', child_count: 1,
  }],
  children: [], child_count: 0,
  in_read_thread: false, read_reply_above: false,
  glean_thread: false,
  ...overrides,
});

const READ_PROMPT = {
  id: 31, username: 'ana', user_id: 1, node_type: 'user', content: '',
  is_system_prompt: true, prompt_key: 'read_thread', ai_usage: 'chat',
  privacy_level: 'private', child_count: 1,
};

// A finished gleaning with *n* picks.
const gleaning = (n, overrides = {}) => ({
  id: 32, node_type: 'llm', llm_model: 'claude-haiku-5.5',
  user: { id: 3, username: 'claude-haiku-5.5' }, user_id: 3, parent_user_id: 1,
  content: ['A quiet day.', ...Array.from({ length: n },
    (_, i) => `Why this one.\n\n{quote_ext:${100 + i}}`)].join('\n\n'),
  llm_task_status: 'completed',
  tool_calls_meta: [{ name: '_live' }],
  read_reply: true,
  read_window: { tweets: 1240, accounts: 300, excluded: 0,
                 window_start: '2026-10-08T07:00:00Z', window_end: '2026-10-09T07:00:00Z' },
  ai_usage: 'chat', privacy_level: 'private',
  ancestors: [VOICE_ROOT, READ_PROMPT],
  children: [], child_count: 0,
  in_read_thread: true, read_reply_above: true, glean_thread: true,
  ...overrides,
});

beforeAll(() => {
  Element.prototype.scrollIntoView = () => {};
});

let routes;
beforeEach(() => {
  mockUser = USER;
  routes = {};
  mockGet.mockReset();
  mockPost.mockReset();
  mockTextStream.mockReset();
  mockTextStream.mockReturnValue({ text: '', done: false });
  localStorage.clear();
  mockGet.mockImplementation((url) => {
    if (url.endsWith('/llm-status')) return new Promise(() => {});
    if (url.endsWith('/resolve-quotes')) {
      return Promise.resolve({ data: { quotes: {}, external_quotes: {}, has_quotes: true } });
    }
    if (url in routes) return Promise.resolve({ data: routes[url] });
    return Promise.reject(new Error(`unexpected GET ${url}`));
  });
  mockPost.mockResolvedValue({ data: { prompt_node_id: 40, llm_node_id: 41 } });
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

const gleanButton = () => Array.from(
  document.querySelectorAll('[data-action-group] button'),
).find((b) => b.textContent === 'Glean') || null;

// The focal entry's menu: ancestors render above it, and these threads
// have no children.
const openMenu = () => fireEvent.click(screen.getAllByRole('button', { name: 'More actions' }).at(-1));

describe('a thread started from the Glean card', () => {
  test('offers the Glean button, and no menu entry; no model picker for a non-admin', async () => {
    routes['/nodes/30'] = reply({ glean_thread: true });
    renderAt('/node/30');
    await screen.findByText(/fork in the road/);
    const button = gleanButton();
    expect(button).toBeInTheDocument();
    expect(screen.queryByTestId('read-model-picker')).toBeNull();
    openMenu();
    expect(screen.queryByRole('button', { name: 'Glean for this reflection' })).toBeNull();

    fireEvent.click(button);
    // The server picks the model of the user's provider.
    expect(mockPost).toHaveBeenCalledWith('/read/from-node/30', { model: undefined });
  });

  test('an admin gets the model picker beside it', async () => {
    mockUser = { ...USER, is_admin: true };
    routes['/nodes/30'] = reply({ glean_thread: true });
    renderAt('/node/30');
    await screen.findByText(/fork in the road/);
    expect(gleanButton()).toBeInTheDocument();
    expect(screen.getByTestId('read-model-picker')).toBeInTheDocument();
  });
});

describe('a thread started from the Reflect card', () => {
  test('has no Glean button; the entry menu says "Glean for this reflection"', async () => {
    routes['/nodes/30'] = reply();
    renderAt('/node/30');
    await screen.findByText(/fork in the road/);
    expect(gleanButton()).toBeNull();
    openMenu();
    fireEvent.click(screen.getByRole('button', { name: 'Glean for this reflection' }));
    expect(mockPost).toHaveBeenCalledWith('/read/from-node/30', { model: undefined });
    // We land on the pending gleaning.
    await waitFor(() => expect(
      mockGet.mock.calls.some(([url]) => url === '/nodes/41')).toBe(true));
  });

  test('works days later in a thread that has gleaned already (glean again)', async () => {
    routes['/nodes/30'] = reply({ in_read_thread: true, read_reply_above: true });
    renderAt('/node/30');
    await screen.findByText(/fork in the road/);
    expect(gleanButton()).toBeNull();
    openMenu();
    expect(screen.getByRole('button', { name: 'Glean for this reflection' })).toBeInTheDocument();
  });

  test('an earlier entry of the thread has it in its own menu; a system prompt does not', async () => {
    routes['/nodes/30'] = reply();
    renderAt('/node/30');
    await screen.findByText(/fork in the road/);
    const menus = screen.getAllByRole('button', { name: 'More actions' });
    // [voice prompt, the user's entry, the focal reply]
    fireEvent.click(menus[0]);
    expect(screen.queryByRole('button', { name: 'Glean for this reflection' })).toBeNull();
    fireEvent.click(menus[0]);
    fireEvent.click(menus[1]);
    fireEvent.click(screen.getByRole('button', { name: 'Glean for this reflection' }));
    expect(mockPost).toHaveBeenCalledWith('/read/from-node/20', { model: undefined });
  });
});

describe('a user without Glean', () => {
  test.each([
    ['the Glean card thread', { glean_thread: true }],
    ['a Reflect thread', {}],
  ])('sees no Glean button and no menu entry in %s', async (_, overrides) => {
    mockUser = { ...USER, glean_enabled: false };
    routes['/nodes/30'] = reply(overrides);
    renderAt('/node/30');
    await screen.findByText(/fork in the road/);
    expect(gleanButton()).toBeNull();
    openMenu();
    expect(screen.queryByRole('button', { name: 'Glean for this reflection' })).toBeNull();
  });
});

describe('the gleaning', () => {
  test('is headed "Today\'s gleanings", with the number of picks', async () => {
    routes['/nodes/32'] = gleaning(2);
    renderAt('/node/32');
    expect(await screen.findByText("Today's gleanings")).toBeInTheDocument();
    expect(screen.getByText('2 tweets from today, chosen for what you said.')).toBeInTheDocument();
    expect(screen.queryByText('Nothing worth your time today.')).toBeNull();
  });

  test('an empty day says so, with the count of tweets read', async () => {
    routes['/nodes/32'] = gleaning(0);
    renderAt('/node/32');
    expect(await screen.findByText('Nothing worth your time today.')).toBeInTheDocument();
    expect(screen.getByText("Loore read all 1,240 of today's tweets in the archive.")).toBeInTheDocument();
    // #436: the way back to the home page's cards.
    expect(screen.getByRole('link', { name: 'Back to Home' })).toHaveAttribute('href', '/');
  });

  test('a pending gleaning says "Gleaning", not "Thinking"', async () => {
    routes['/nodes/32'] = gleaning(0, { llm_task_status: 'processing', content: '' });
    renderAt('/node/32');
    expect(await screen.findByText('Gleaning')).toBeInTheDocument();
    expect(screen.queryByText('Thinking')).toBeNull();
    expect(screen.queryByText('Nothing worth your time today.')).toBeNull();
  });
});

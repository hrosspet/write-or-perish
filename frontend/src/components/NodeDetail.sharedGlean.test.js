// The reply box under a Glean (a read reply). Its owner's reply is a
// Text-mode message about their picks (#323, POST /textmode/from-node).
// A circle reader under someone else's shared Glean replies as under any
// node of that shared thread: the plain reply path (NodeForm's own
// POST /nodes/ and save-as-node), which holds the reply to the thread's
// rules. Elsewhere the box is as before Circles.
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

let mockFormProps;
jest.mock('./NodeForm', () => (props) => { mockFormProps = props; return null; });
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
import { render, screen, act, waitFor } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import NodeDetailWrapper from './NodeDetailWrapper';

const ANNA = { id: 1, username: 'anna', craft_mode: false, preferred_model: 'anna-model', default_ai_usage: 'chat' };
const BEN = { id: 2, username: 'ben', craft_mode: false, preferred_model: 'ben-model', default_ai_usage: 'chat' };

const PICKS_PLACEHOLDER = 'Ask about these picks, or say what you make of them…';
const PLAIN_PLACEHOLDER = "Type what's on your mind…";

// anna's Glean (her read reply), shared with a circle she put ben in.
const annasGlean = (me, overrides = {}) => ({
  id: 20, node_type: 'llm', llm_model: 'claude-x', llm_task_status: 'completed',
  user: { id: 9, username: 'claude-x' }, user_id: 9,
  human_owner_username: 'anna', parent_user_id: 1,
  content: 'The picks of the day.', ai_usage: 'chat', privacy_level: 'circles',
  read_reply: true,
  ancestors: [{ id: 10, node_type: 'user', user_id: 1, username: 'anna', content: 'A note.' }],
  children: [], child_count: 0,
  in_read_thread: true, read_reply_above: true, glean_thread: false,
  audience: {
    entry_id: 20, author: 'anna', readers: ['ben', 'chidi'],
    is_author: me.username === 'anna', is_entry: true, entry_ai_usage: 'chat',
  },
  ...overrides,
});

beforeAll(() => {
  Element.prototype.scrollIntoView = () => {};
});

let routes;
beforeEach(() => {
  mockFormProps = null;
  routes = {};
  mockGet.mockReset();
  mockPost.mockReset();
  localStorage.clear();
  mockGet.mockImplementation((url) => {
    if (url.endsWith('/llm-status')) return new Promise(() => {});
    if (url in routes) return Promise.resolve({ data: routes[url] });
    return Promise.reject(new Error(`unexpected GET ${url}`));
  });
  mockPost.mockResolvedValue({ data: { user_node_id: 30, prompt_node_id: null } });
  jest.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
  console.error.mockRestore();
});

const openGlean = async (me, overrides) => {
  mockUser = me;
  routes['/nodes/20'] = annasGlean(me, overrides);
  render(
    <MemoryRouter initialEntries={['/node/20']}>
      <Routes>
        <Route path="/node/:id" element={<NodeDetailWrapper />} />
      </Routes>
    </MemoryRouter>,
  );
  await screen.findByText('The picks of the day.');
  await waitFor(() => expect(mockFormProps).not.toBeNull());
  return mockFormProps;
};

test('a circle reader replies under her shared Glean on the plain reply path', async () => {
  const form = await openGlean(BEN);
  expect(form.onSubmitOverride).toBeUndefined();
  expect(form.placeholder).toBe(PLAIN_PLACEHOLDER);
  expect(form.parentId).toBe(20);
  expect(form.parentPrivacyLevel).toBe('circles');
});

test('her own reply under her shared Glean is a Text-mode message', async () => {
  const form = await openGlean(ANNA);
  expect(form.placeholder).toBe(PICKS_PLACEHOLDER);
  expect(form.onSubmitOverride).toEqual(expect.any(Function));
  await act(async () => {
    await form.onSubmitOverride({
      content: 'why these?', privacy_level: 'circles', ai_usage: 'chat',
    });
  });
  expect(mockPost).toHaveBeenCalledTimes(1);
  const [url, body] = mockPost.mock.calls[0];
  expect(url).toBe('/textmode/from-node/20');
  expect(body).toMatchObject({ content: 'why these?', ai_usage: 'chat' });
});

test('under someone else\'s Glean that is not shared with a circle the box is as before', async () => {
  const form = await openGlean(BEN, { privacy_level: 'public', audience: null });
  expect(form.onSubmitOverride).toEqual(expect.any(Function));
  expect(form.placeholder).toBe(PICKS_PLACEHOLDER);
});

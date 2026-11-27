// A reader leaves a shared thread from its banner (Peter, 2026-11-23:
// "build leave into the current PR"). The author has no such button.
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
jest.mock('./NodeFooter', () => (props) => <div>{props.children}</div>);
jest.mock('./QuotedContent', () => (props) => <div>{props.content}</div>);

import React from 'react';
import { render, screen, act, fireEvent } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import NodeDetailWrapper from './NodeDetailWrapper';

const ANNA = { id: 1, username: 'anna', craft_mode: false, default_ai_usage: 'chat' };
const BEN = { id: 2, username: 'ben', craft_mode: false, default_ai_usage: 'chat' };

const sharedEntry = (me) => ({
  id: 10, node_type: 'user', llm_model: null,
  user: { id: 1, username: 'anna' }, user_id: 1,
  content: 'A shared entry.', ai_usage: 'chat', privacy_level: 'circles',
  ancestors: [], children: [], child_count: 0,
  in_read_thread: false, read_reply_above: false, glean_thread: false,
  audience: {
    entry_id: 10, author: 'anna', readers: ['ben', 'chidi'],
    is_author: me.username === 'anna', entry_ai_usage: 'chat',
  },
});

beforeAll(() => {
  Element.prototype.scrollIntoView = () => {};
});

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
  mockGet.mockImplementation((url) => {
    if (url.endsWith('/llm-status')) return new Promise(() => {});
    if (url === '/nodes/10') return Promise.resolve({ data: sharedEntry(mockUser) });
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
      <Route path="/shared" element={<div>Shared with you page</div>} />
    </Routes>
  </MemoryRouter>,
);

const press = async (name) => {
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name }));
  });
};

test('a reader leaves from the banner: the dialog says what closes, then the thread is left', async () => {
  mockUser = BEN;
  mockPost.mockImplementation((url) => {
    if (url === '/circles/leave/preview') {
      return Promise.resolve({ data: { author: 'anna', closes: 3 } });
    }
    if (url === '/circles/leave') {
      return Promise.resolve({ data: { left: true, author: 'anna', closes: 3 } });
    }
    return Promise.reject(new Error(`unexpected POST ${url}`));
  });
  renderAt('/node/10');
  await screen.findByText('A shared entry.');
  await press('Leave');
  expect(mockPost).toHaveBeenCalledWith('/circles/leave/preview', { entry_id: 10 });
  expect(screen.getByRole('heading', { name: 'Leave this thread?' })).toBeInTheDocument();
  expect(screen.getByText(
    'You will no longer read this thread or reply in it, and nobody sees '
    + 'your name among its readers any more, @anna included. You also stop '
    + 'reading two other entries @anna shared with you the same way as this one.',
  )).toBeInTheDocument();
  expect(screen.getByText(
    'Your replies stay where they are, with your name. @anna gets no note, '
    + 'and can add you again.',
  )).toBeInTheDocument();
  // Nothing is left before Leave in the dialog.
  expect(mockPost).not.toHaveBeenCalledWith('/circles/leave', expect.anything());
  const dialog = screen.getByRole('dialog');
  await act(async () => {
    fireEvent.click(dialog.querySelectorAll('button')[1]);
  });
  expect(mockPost).toHaveBeenCalledWith('/circles/leave', { entry_id: 10 });
  expect(await screen.findByText('Shared with you page')).toBeInTheDocument();
});

test('Cancel leaves nothing', async () => {
  mockUser = BEN;
  mockPost.mockResolvedValue({ data: { author: 'anna', closes: 1 } });
  renderAt('/node/10');
  await screen.findByText('A shared entry.');
  await press('Leave');
  await press('Cancel');
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(mockPost).toHaveBeenCalledTimes(1);
  expect(screen.getByText('A shared entry.')).toBeInTheDocument();
});

test('the author has no Leave on her own shared entry', async () => {
  mockUser = ANNA;
  renderAt('/node/10');
  await screen.findByText('A shared entry.');
  expect(screen.queryByRole('button', { name: 'Leave' })).toBeNull();
});

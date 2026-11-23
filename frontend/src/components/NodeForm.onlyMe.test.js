// A reply under the user's own node that only they read, in a thread
// someone else shared (GET /nodes/<parent> reply_only_private): the form
// offers Only me alone and sends "private", what the server accepts.
jest.mock('../contexts/UserContext', () => ({
  useUser: () => ({
    user: { id: 2, username: 'ben', default_ai_usage: 'chat', circles_enabled: true },
  }),
}));
jest.mock('../contexts/ToastContext', () => ({
  useToast: () => ({ addToast: jest.fn() }),
}));
jest.mock('./StreamingMicButton', () => ({ __esModule: true, default: () => null }));
const mockGet = jest.fn();
const mockPost = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    get: (...a) => mockGet(...a),
    post: (...a) => mockPost(...a),
    put: jest.fn(() => Promise.resolve({ data: {} })),
    delete: jest.fn(() => Promise.resolve({ data: {} })),
  },
}));

import React, { createRef } from 'react';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import NodeForm from './NodeForm';

const BOX = 'Share also the discussion leading up to this node?';

let parent;
beforeEach(() => {
  localStorage.clear();
  localStorage.setItem('loore_auto_generate', 'false');
  // A remembered "Everyone" must not reach the request either.
  localStorage.setItem('loore_last_privacy_level', 'public');
  mockGet.mockReset();
  mockPost.mockReset();
  // ben's own AI reply, kept to Only me, in anna's shared thread.
  parent = {
    id: 50, node_type: 'llm', user: { id: 9, username: 'gpt-5' },
    human_owner_username: 'ben', parent_user_id: 2,
    privacy_level: 'private', ai_usage: 'chat', audience: null,
    reply_only_private: true,
  };
  mockGet.mockImplementation((url) => {
    if (url === '/nodes/50') return Promise.resolve({ data: parent });
    if (url === '/circles') return Promise.resolve({ data: { circles: [] } });
    return Promise.reject({ response: { status: 404 } });
  });
  mockPost.mockImplementation((url) => {
    if (url === '/nodes/') return Promise.resolve({ data: { id: 51 } });
    return Promise.resolve({ data: {} });
  });
});

const renderReply = () => {
  const ref = createRef();
  render(
    <MemoryRouter>
      <NodeForm ref={ref} parentId={50} onSuccess={() => {}} />
    </MemoryRouter>,
  );
  return ref;
};

test('only Only me is offered, and the reply is sent as private', async () => {
  const ref = renderReply();
  expect(await screen.findByText(/Only you\. You are replying under a node only you can read/))
    .toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Circles' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Everyone' })).toBeNull();
  expect(screen.queryByLabelText(BOX)).toBeNull();
  fireEvent.change(screen.getAllByRole('textbox')[0], { target: { value: 'A note to myself.' } });
  await act(async () => { await ref.current.submit(); });
  await waitFor(() => expect(
    mockPost.mock.calls.filter(([url]) => url === '/nodes/')).toHaveLength(1));
  const body = mockPost.mock.calls.find(([url]) => url === '/nodes/')[1];
  expect(body).toEqual(expect.objectContaining({ parent_id: 50, privacy_level: 'private' }));
  expect(body).not.toHaveProperty('circle_ids');
});

test('without the field the three choices stay', async () => {
  parent = { ...parent, reply_only_private: undefined };
  renderReply();
  expect(await screen.findByRole('button', { name: 'Circles' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Everyone' })).toBeInTheDocument();
});

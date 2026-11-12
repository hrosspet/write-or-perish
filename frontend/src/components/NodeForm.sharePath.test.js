// Sharing an entry that has something above it in its thread shares
// only the entry. The Circles picker asks "Share also the discussion
// leading up to this node?", and the choice goes to the server as
// share_path. There is no way to share only the thread's prompt.
jest.mock('../contexts/UserContext', () => ({
  useUser: () => ({
    user: { id: 1, username: 'anna', default_ai_usage: 'chat', circles_enabled: true },
  }),
}));
jest.mock('../contexts/ToastContext', () => ({
  useToast: () => ({ addToast: jest.fn() }),
}));
jest.mock('./StreamingMicButton', () => ({ __esModule: true, default: () => null }));
const mockGet = jest.fn();
const mockPost = jest.fn();
const mockPut = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    get: (...a) => mockGet(...a),
    post: (...a) => mockPost(...a),
    put: (...a) => mockPut(...a),
    delete: jest.fn(() => Promise.resolve({ data: {} })),
  },
}));

import React, { createRef } from 'react';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import NodeForm from './NodeForm';

const BOX = 'Share also the discussion leading up to this node?';
const CIRCLES = { circles: [
  { id: 1, name: 'Close friends', members: [{ username: 'ben' }], shared_count: 1 },
] };

let audienceAnswer;
let parent;
let created;
beforeEach(() => {
  localStorage.clear();
  localStorage.setItem('loore_auto_generate', 'false');
  mockGet.mockReset();
  mockPost.mockReset();
  mockPut.mockReset();
  audienceAnswer = {
    audience: { is_entry: true, is_author: true, circle_ids: [1], usernames: [] },
    thread_path: { count: 2, shared: false },
  };
  // The node a new entry is written under: anna's own, in a private thread.
  parent = {
    id: 40, node_type: 'user', user: { id: 1, username: 'anna' }, user_id: 1,
    privacy_level: 'private', ai_usage: 'chat', audience: null,
  };
  created = { id: 41, audience_set: true };
  mockGet.mockImplementation((url) => {
    if (url === '/nodes/30/audience') return Promise.resolve({ data: audienceAnswer });
    if (url === '/nodes/40') return Promise.resolve({ data: parent });
    if (url === '/circles') return Promise.resolve({ data: CIRCLES });
    return Promise.reject({ response: { status: 404 } });
  });
  mockPost.mockImplementation((url) => {
    if (url === '/circles/audience-preview') return Promise.resolve({ data: { readers: ['ben'] } });
    if (url === '/nodes/') return Promise.resolve({ data: created });
    return Promise.resolve({ data: {} });
  });
  mockPut.mockResolvedValue({ data: { node: { id: 30 } } });
});

describe('editing a shared entry', () => {
  const renderEdit = () => {
    const ref = createRef();
    render(
      <MemoryRouter>
        <NodeForm
          ref={ref} editMode nodeId={30} initialContent="A shared entry."
          initialPrivacyLevel="circles" initialAiUsage="chat" onSuccess={() => {}}
        />
      </MemoryRouter>,
    );
    return ref;
  };

  const savedBody = async (ref) => {
    await act(async () => { await ref.current.submit(); });
    await waitFor(() => expect(mockPut).toHaveBeenCalledTimes(1));
    expect(mockPut.mock.calls[0][0]).toBe('/nodes/30');
    return mockPut.mock.calls[0][1];
  };

  test('the box is off by default, and an untouched save sends no share_path', async () => {
    const ref = renderEdit();
    expect(await screen.findByLabelText(BOX)).not.toBeChecked();
    const body = await savedBody(ref);
    expect(body).toEqual(expect.objectContaining({ circle_ids: [1], circle_usernames: [] }));
    expect(body).not.toHaveProperty('share_path');
    expect(body).not.toHaveProperty('share_prompt');
  });

  test('ticked, the save sends share_path true with the audience', async () => {
    const ref = renderEdit();
    fireEvent.click(await screen.findByLabelText(BOX));
    expect(screen.getByLabelText(BOX)).toBeChecked();
    const body = await savedBody(ref);
    expect(body).toEqual(expect.objectContaining({
      circle_ids: [1], circle_usernames: [], share_path: true,
    }));
  });

  test('a path already shared shows the box ticked; unticking sends share_path false', async () => {
    audienceAnswer.thread_path = { count: 2, shared: true };
    const ref = renderEdit();
    const box = await screen.findByLabelText(BOX);
    await waitFor(() => expect(box).toBeChecked());
    fireEvent.click(box);
    const body = await savedBody(ref);
    expect(body).toEqual(expect.objectContaining({ circle_ids: [1], share_path: false }));
  });

  test('a path already shared and left ticked sends share_path true', async () => {
    audienceAnswer.thread_path = { count: 2, shared: true };
    const ref = renderEdit();
    const box = await screen.findByLabelText(BOX);
    await waitFor(() => expect(box).toBeChecked());
    const body = await savedBody(ref);
    expect(body).toEqual(expect.objectContaining({ share_path: true }));
  });

  test('an entry with nothing above it is not asked', async () => {
    audienceAnswer.thread_path = null;
    const ref = renderEdit();
    await screen.findByLabelText('Or name people for this entry only');
    expect(screen.queryByLabelText(BOX)).toBeNull();
    const body = await savedBody(ref);
    expect(body).not.toHaveProperty('share_path');
  });

  test('the server refusing to share only a prompt: its text is the form\'s message', async () => {
    mockPut.mockRejectedValue({ response: { status: 400, data: {
      code: 'circle_prompt_node',
      error: 'A prompt is shared together with the discussion under it.',
    } } });
    const ref = renderEdit();
    await screen.findByLabelText(BOX);
    await act(async () => { await ref.current.submit(); });
    expect(await screen.findByText('A prompt is shared together with the discussion under it.'))
      .toBeInTheDocument();
  });
});

describe('a new entry under the user\'s own node', () => {
  const renderReply = () => {
    const ref = createRef();
    const onSuccess = jest.fn();
    render(
      <MemoryRouter>
        <NodeForm ref={ref} parentId={40} onSuccess={onSuccess} />
      </MemoryRouter>,
    );
    return { ref, onSuccess };
  };

  // Write the entry, choose Circles and the one circle, and wait for the
  // sentence that names the readers (nobody is shared with before it).
  const writeAndShare = async () => {
    await waitFor(() => expect(mockGet).toHaveBeenCalledWith('/nodes/40'));
    fireEvent.change(screen.getAllByRole('textbox')[0], { target: { value: 'A new entry.' } });
    fireEvent.click(await screen.findByRole('button', { name: 'Circles' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Close friends' }));
    await screen.findByText('@ben can read this entry and reply to it. Nobody else can.');
  };

  const createdBody = async (ref) => {
    await act(async () => { await ref.current.submit(); });
    await waitFor(() => expect(
      mockPost.mock.calls.filter(([url]) => url === '/nodes/')).toHaveLength(1));
    return mockPost.mock.calls.find(([url]) => url === '/nodes/')[1];
  };

  test('the box is offered, off; unticked, no share_path is sent', async () => {
    const { ref } = renderReply();
    await writeAndShare();
    expect(await screen.findByLabelText(BOX)).not.toBeChecked();
    const body = await createdBody(ref);
    expect(body).toEqual(expect.objectContaining({ parent_id: 40, circle_ids: [1] }));
    expect(body).not.toHaveProperty('share_path');
  });

  test('ticked, POST /nodes/ carries share_path true', async () => {
    const { ref } = renderReply();
    await writeAndShare();
    fireEvent.click(await screen.findByLabelText(BOX));
    const body = await createdBody(ref);
    expect(body).toEqual(expect.objectContaining({
      parent_id: 40, privacy_level: 'circles', circle_ids: [1], share_path: true,
    }));
  });

  test('when sharing is a second step, PUT /nodes/<id>/audience carries it too', async () => {
    created = { id: 41 };
    const { ref } = renderReply();
    await writeAndShare();
    fireEvent.click(await screen.findByLabelText(BOX));
    await createdBody(ref);
    await waitFor(() => expect(mockPut).toHaveBeenCalledTimes(1));
    expect(mockPut.mock.calls[0]).toEqual(['/nodes/41/audience', {
      circle_ids: [1], circle_usernames: [], share_path: true,
    }]);
  });

  test('under someone else\'s node the box is not offered', async () => {
    parent = { ...parent, user: { id: 2, username: 'ben' }, user_id: 2 };
    renderReply();
    await writeAndShare();
    await screen.findByLabelText('Or name people for this entry only');
    expect(screen.queryByLabelText(BOX)).toBeNull();
  });

  test('a reply in a shared thread is not asked: its readers are the thread\'s', async () => {
    parent = {
      ...parent, privacy_level: 'circles',
      audience: { entry_id: 40, author: 'anna', readers: ['ben'], is_author: true },
    };
    renderReply();
    await waitFor(() => expect(mockGet).toHaveBeenCalledWith('/nodes/40'));
    expect(await screen.findByText(/will see it, with your name\. Nobody else can\./))
      .toBeInTheDocument();
    expect(screen.queryByLabelText(BOX)).toBeNull();
  });
});

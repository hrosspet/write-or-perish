// Peter, 2026-11-24: a reply set to Private under one's own Public entry
// "shouldn't be refused, they should adhere to the form's selection".
// Under a public node of the user's own (one they wrote, or an AI reply
// they asked for, a Glean included) the reply form's privacy selector is
// free and starts at Public; "This reply will be public" asks only when
// Public is sent. Under someone else's public node the reply stays
// locked to Public (#228) and the question asks before every send.
jest.mock('../contexts/UserContext', () => ({
  useUser: () => ({
    user: {
      id: 1, username: 'u', default_ai_usage: 'chat',
      default_privacy_level: 'private',
    },
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
import { render, screen, fireEvent, act } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import NodeForm from './NodeForm';

const MINE = { id: 10, privacy_level: 'public', ai_usage: 'chat', node_type: 'user', user: { id: 1 } };
const MY_GLEAN = {
  id: 10, privacy_level: 'public', ai_usage: 'chat', node_type: 'llm',
  user: { id: 99 }, parent_user_id: 1,
};
const THEIRS = { id: 10, privacy_level: 'public', ai_usage: 'chat', node_type: 'user', user: { id: 2 } };

const parentIs = (parent) => mockGet.mockImplementation((url) => (
  String(url) === '/nodes/10'
    ? Promise.resolve({ data: parent })
    : Promise.reject({ response: { status: 404 } })));

beforeEach(() => {
  localStorage.clear();
  mockGet.mockReset();
  mockPost.mockReset();
  mockPost.mockResolvedValue({ data: { id: 5 } });
});

const renderReply = async (props = {}) => {
  const ref = createRef();
  const onSuccess = jest.fn();
  render(
    <MemoryRouter>
      <NodeForm ref={ref} parentId={10} parentPrivacyLevel="public"
        hideAudioUpload onSuccess={onSuccess} {...props} />
    </MemoryRouter>,
  );
  await act(async () => { await Promise.resolve(); });
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'my reply' } });
  return { ref, onSuccess };
};

const privacySelect = () => screen.getAllByRole('combobox')[0];
const send = async (ref) => { await act(async () => { await ref.current.submit(); }); };

describe.each([
  ['an entry of the user\'s own (known to the page)', MINE, { parentOwnedByViewer: true }],
  ['an entry of the user\'s own (from the form\'s request)', MINE, {}],
  ['a Glean of the user\'s own', MY_GLEAN, {}],
])('under %s that is public', (_name, parent, props) => {
  test('the selector is free and starts at Public', async () => {
    parentIs(parent);
    await renderReply(props);
    expect(screen.queryByText('Public — inherited from the thread.')).toBeNull();
    expect(privacySelect()).toHaveValue('public');
  });

  test('Private is sent as Private, without the public-reply question', async () => {
    parentIs(parent);
    const { ref, onSuccess } = await renderReply(props);
    fireEvent.change(privacySelect(), { target: { value: 'private' } });
    await send(ref);
    expect(screen.queryByText('This reply will be public')).toBeNull();
    expect(mockPost).toHaveBeenCalledTimes(1);
    expect(mockPost.mock.calls[0][0]).toBe('/nodes/');
    expect(mockPost.mock.calls[0][1]).toEqual(
      expect.objectContaining({ parent_id: 10, privacy_level: 'private' }));
    expect(onSuccess).toHaveBeenCalled();
  });

  test('Public keeps the question', async () => {
    parentIs(parent);
    const { ref } = await renderReply(props);
    await send(ref);
    expect(screen.getByText('This reply will be public')).toBeInTheDocument();
    expect(mockPost).not.toHaveBeenCalled();
  });

  test('a page-specific submit (the reply box under a Glean) gets Private too', async () => {
    parentIs(parent);
    const override = jest.fn(() => Promise.resolve({ user_node_id: 5 }));
    const { ref } = await renderReply({ ...props, onSubmitOverride: override });
    fireEvent.change(privacySelect(), { target: { value: 'private' } });
    await send(ref);
    expect(override).toHaveBeenCalledTimes(1);
    expect(override.mock.calls[0][0].privacy_level).toBe('private');
  });
});

describe('under someone else\'s public entry', () => {
  test('the level is locked to Public and the question asks first', async () => {
    parentIs(THEIRS);
    const { ref } = await renderReply();
    expect(screen.getByText('Public — inherited from the thread.')).toBeInTheDocument();
    await send(ref);
    expect(screen.getByText('This reply will be public')).toBeInTheDocument();
    expect(mockPost).not.toHaveBeenCalled();
  });

  test('a form that starts at Private still sends Public', async () => {
    parentIs(THEIRS);
    localStorage.setItem('loore_public_reply_ack', 'true');
    const { ref } = await renderReply({
      initialPrivacyLevel: 'private', initialAiUsage: 'chat',
    });
    await send(ref);
    expect(mockPost).toHaveBeenCalledTimes(1);
    expect(mockPost.mock.calls[0][1]).toEqual(
      expect.objectContaining({ privacy_level: 'public' }));
  });

  test('the request for the parent wins over the page\'s guess', async () => {
    parentIs(THEIRS);
    await renderReply({ parentOwnedByViewer: true });
    expect(screen.getByText('Public — inherited from the thread.')).toBeInTheDocument();
  });
});

describe('before the request for the parent answers', () => {
  test('a page that knows the parent is the user\'s own leaves the selector free', async () => {
    mockGet.mockImplementation(() => new Promise(() => {}));
    await renderReply({ parentOwnedByViewer: true });
    expect(screen.queryByText('Public — inherited from the thread.')).toBeNull();
  });

  test('without that, the level is locked to Public', async () => {
    mockGet.mockImplementation(() => new Promise(() => {}));
    await renderReply();
    expect(screen.getByText('Public — inherited from the thread.')).toBeInTheDocument();
  });
});

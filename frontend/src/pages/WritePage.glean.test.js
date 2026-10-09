// Text mode opened from the Glean card (#435): the new thread is marked
// as started there (entry 'glean'), for typed, recorded and uploaded
// entries alike, so every turn of it offers the Glean button. Opened from
// anywhere else, or by a user without Glean, it is an ordinary thread.
let mockUserCtx;
jest.mock('../contexts/UserContext', () => ({
  useUser: () => mockUserCtx,
}));
jest.mock('../contexts/ToastContext', () => ({
  useToast: () => ({ addToast: jest.fn(), removeToast: jest.fn() }),
}));
const mockPost = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: { get: jest.fn(() => new Promise(() => {})), post: (...args) => mockPost(...args) },
}));
let mockNodeFormProps;
jest.mock('../components/NodeForm', () => ({
  __esModule: true,
  default: (props) => {
    mockNodeFormProps = props;
    return null;
  },
}));

import React from 'react';
import { render } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import WritePage from './WritePage';

const renderAt = (path, user = {}) => {
  mockUserCtx = {
    user: { username: 'ana', has_own_entries: true, glean_enabled: true, ...user },
    markHasOwnEntries: jest.fn(),
  };
  return render(<MemoryRouter initialEntries={[path]}><WritePage /></MemoryRouter>);
};

beforeEach(() => {
  mockPost.mockReset();
  mockPost.mockResolvedValue({ data: {} });
  mockNodeFormProps = null;
  localStorage.clear();
});

const submit = (extra = {}) => mockNodeFormProps.onSubmitOverride({
  content: 'On my mind.', privacy_level: 'private', ai_usage: 'chat', ...extra,
});

test('from the Glean card a typed entry starts a Glean thread', async () => {
  renderAt('/textmode?glean=1');
  await submit();
  expect(mockPost).toHaveBeenCalledWith('/textmode/start', expect.objectContaining({
    content: 'On my mind.', entry: 'glean',
  }));
});

test('from the Glean card a recorded entry and an upload do too', async () => {
  renderAt('/textmode?glean=1');
  await submit({ streaming_session_id: 'sess-1' });
  expect(mockPost).toHaveBeenCalledWith('/drafts/streaming/sess-1/save-as-node',
    expect.objectContaining({ agentic: true, entry: 'glean' }));
  expect(mockNodeFormProps.uploadReplyOptions({ ai_usage: 'chat' }))
    .toEqual(expect.objectContaining({ agentic: true, entry: 'glean' }));
});

test.each([
  ['the Text card', '/textmode', {}],
  ['a user without Glean', '/textmode?glean=1', { glean_enabled: false }],
])('from %s the thread is an ordinary one', async (_, path, user) => {
  renderAt(path, user);
  await submit();
  const body = mockPost.mock.calls[0][1];
  expect(body).not.toHaveProperty('entry');
});

// A recorded entry in Text mode is saved with the privacy and AI usage the
// form shows when it is sent, which can differ from the ones the recording
// started with.
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

beforeEach(() => {
  mockPost.mockReset();
  mockPost.mockResolvedValue({ data: {} });
  mockNodeFormProps = null;
  localStorage.clear();
  mockUserCtx = {
    user: { username: 'ana', has_own_entries: true },
    markHasOwnEntries: jest.fn(),
  };
  render(<MemoryRouter initialEntries={['/textmode']}><WritePage /></MemoryRouter>);
});

const sendRecorded = (privacy_level, ai_usage) => mockNodeFormProps.onSubmitOverride({
  content: 'Said aloud.', privacy_level, ai_usage, streaming_session_id: 'sess-1',
});

test('a recorded entry is saved with the settings the form shows', async () => {
  await sendRecorded('private', 'train');
  expect(mockPost).toHaveBeenCalledWith('/drafts/streaming/sess-1/save-as-node',
    expect.objectContaining({ privacy_level: 'private', ai_usage: 'train', agentic: true }));
});

test('with AI usage None it is a plain entry with those settings', async () => {
  await sendRecorded('private', 'none');
  const [url, body] = mockPost.mock.calls[0];
  expect(url).toBe('/drafts/streaming/sess-1/save-as-node');
  expect(body).toEqual({ content: 'Said aloud.', privacy_level: 'private', ai_usage: 'none' });
});

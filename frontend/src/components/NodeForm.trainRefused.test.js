// #689: an entry Loore fetched through the X API is never at Train. Its
// node JSON carries `train_refused`; the editor does not offer Train and
// says why, in the words the server uses when it refuses. Other entries
// keep the Train option. An import that kept X posts at Chat says so.
jest.mock('../contexts/UserContext', () => ({
  useUser: () => ({
    user: {
      id: 1, username: 'u', default_ai_usage: 'train',
      default_privacy_level: 'private',
    },
  }),
}));
jest.mock('../contexts/ToastContext', () => ({
  useToast: () => ({ addToast: jest.fn() }),
}));
jest.mock('./StreamingMicButton', () => ({ __esModule: true, default: () => null }));
const mockGet = jest.fn();
const mockPut = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    get: (...a) => mockGet(...a),
    post: jest.fn(() => Promise.resolve({ data: {} })),
    put: (...a) => mockPut(...a),
    delete: jest.fn(() => Promise.resolve({ data: {} })),
  },
}));

import React, { createRef } from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import NodeForm from './NodeForm';
import {
  TRAIN_REFUSED_MESSAGE, keptFromTrainNote, keptFromTrainRepliesNote,
} from '../utils/aiUsage';

const MESSAGE =
  "Posts fetched from X can't be used for training, under X's rules for developers.";

beforeEach(() => {
  localStorage.clear();
  mockGet.mockReset();
  mockGet.mockRejectedValue({ response: { status: 404 } });
  mockPut.mockReset();
  mockPut.mockResolvedValue({ data: { node: {} } });
});

const renderEdit = (props = {}) => {
  const ref = createRef();
  render(
    <MemoryRouter>
      <NodeForm ref={ref} editMode nodeId={7} initialContent="a tweet"
        initialPrivacyLevel="private" initialAiUsage="chat"
        onSuccess={jest.fn()} {...props} />
    </MemoryRouter>,
  );
  return ref;
};

const aiUsageOptions = () => screen.getAllByRole('combobox')
  .find((s) => [...s.options].some((o) => o.value === 'chat'))
  .options;

test('the message matches the server', () => {
  expect(TRAIN_REFUSED_MESSAGE).toBe(MESSAGE);
});

test('an X API entry offers no Train and says why', () => {
  renderEdit({ trainRefused: true });
  expect([...aiUsageOptions()].map((o) => o.value)).toEqual(['none', 'chat']);
  expect(screen.getByText(MESSAGE)).toBeInTheDocument();
});

test('control: another entry still offers Train', () => {
  renderEdit();
  expect([...aiUsageOptions()].map((o) => o.value)).toEqual(['none', 'chat', 'train']);
  expect(screen.queryByText(MESSAGE)).not.toBeInTheDocument();
});

test('control: an X API entry saves the AI usage it shows', async () => {
  const ref = renderEdit({ trainRefused: true });
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'a tweet, edited' } });
  await act(async () => { await ref.current.submit(); });
  expect(mockPut).toHaveBeenCalledWith('/nodes/7', expect.objectContaining({
    content: 'a tweet, edited', ai_usage: 'chat',
  }));
});

test('the import result names the X posts kept at Chat', () => {
  expect(keptFromTrainNote(0)).toBeNull();
  expect(keptFromTrainNote(undefined)).toBeNull();
  expect(keptFromTrainNote(1)).toBe(`1 post that Loore fetched from X stays at Chat. ${MESSAGE}`);
  expect(keptFromTrainNote(3)).toBe(`3 posts that Loore fetched from X stay at Chat. ${MESSAGE}`);
});

test('the cascade toast names the replies kept at Chat', () => {
  expect(keptFromTrainRepliesNote(0)).toBeNull();
  expect(keptFromTrainRepliesNote(1)).toBe(`1 reply stays at Chat. ${MESSAGE}`);
  expect(keptFromTrainRepliesNote(3)).toBe(`3 replies stay at Chat. ${MESSAGE}`);
});

// Write New Entry with Auto-generate on and Agentic Reply off: the form
// saves the entry, then asks for the reply itself. An entry above the
// per-entry cap is saved as a chain of parts (tip_id is the last), and
// the reply is asked under the last part so it reads the whole entry.
jest.mock('../contexts/UserContext', () => ({
  useUser: () => ({ user: { id: 1, username: 'u', default_ai_usage: 'chat' } }),
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
import { NODE_CHAR_CAP } from './SplitContentDialog';

let created;
beforeEach(() => {
  localStorage.clear();
  localStorage.setItem('loore_agentic_reply', 'false');
  localStorage.setItem('loore_auto_generate', 'true');
  mockGet.mockReset();
  mockPost.mockReset();
  // No saved draft.
  mockGet.mockRejectedValue({ response: { status: 404 } });
  mockPost.mockImplementation((url) => {
    if (url === '/nodes/') return Promise.resolve({ data: created });
    if (url.endsWith('/llm')) return Promise.resolve({ data: { node_id: 80 } });
    return Promise.resolve({ data: {} });
  });
});

const send = async (content) => {
  const onSuccess = jest.fn();
  const ref = createRef();
  render(
    <MemoryRouter>
      <NodeForm ref={ref} parentId={null} allowAgenticPrompt onSuccess={onSuccess} />
    </MemoryRouter>,
  );
  fireEvent.change(screen.getByRole('textbox'), { target: { value: content } });
  await act(async () => { await ref.current.submit(); });
  return onSuccess;
};

test('a long entry gets its reply under the last part', async () => {
  created = { id: 20, tip_id: 22, split_into: 3 };
  const onSuccess = await send('line\n'.repeat(NODE_CHAR_CAP / 2));
  fireEvent.click(screen.getByText(/connected entries$/));

  await waitFor(() => expect(onSuccess).toHaveBeenCalled());
  const llmCalls = mockPost.mock.calls.filter(([url]) => url.endsWith('/llm'));
  expect(llmCalls.map(([url]) => url)).toEqual(['/nodes/22/llm']);
  expect(onSuccess.mock.calls[0][0]).toEqual(
    expect.objectContaining({ id: 80, awaitLlm: 80 }));
});

test('a short entry gets it under itself', async () => {
  created = { id: 20, tip_id: 20, split_into: 1 };
  const onSuccess = await send('A short note.');

  await waitFor(() => expect(onSuccess).toHaveBeenCalled());
  const llmCalls = mockPost.mock.calls.filter(([url]) => url.endsWith('/llm'));
  expect(llmCalls.map(([url]) => url)).toEqual(['/nodes/20/llm']);
});

// Write New Entry with Agentic Reply on: the entry goes through
// /textmode/start, which attaches the textmode prompt and, with
// Auto-generate on, asks for the reply. An entry above the per-entry
// cap goes there too (the server splits it into a chain of parts and
// puts the reply under the last one). It used to skip /textmode/start
// and be saved by the plain path, which leaves the reply to
// /textmode/start, so it got no reply and no textmode prompt.
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

let started;
beforeEach(() => {
  localStorage.clear();
  localStorage.setItem('loore_agentic_reply', 'true');
  mockGet.mockReset();
  mockPost.mockReset();
  // No saved draft.
  mockGet.mockRejectedValue({ response: { status: 404 } });
  mockPost.mockImplementation((url) => {
    if (url === '/textmode/start') return Promise.resolve({ data: started });
    if (url === '/nodes/') return Promise.resolve({ data: { id: 20, tip_id: 22 } });
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

const urls = () => mockPost.mock.calls.map(([url]) => url);

test('a long agentic entry with Auto-generate gets its reply', async () => {
  localStorage.setItem('loore_auto_generate', 'true');
  started = {
    conversation_id: 10, user_node_id: 20, tip_id: 22,
    llm_node_id: 80, task_id: 't',
  };
  const content = 'line\n'.repeat(NODE_CHAR_CAP / 2);
  const onSuccess = await send(content);
  fireEvent.click(screen.getByText(/connected entries$/));

  await waitFor(() => expect(onSuccess).toHaveBeenCalled());
  expect(urls()).toEqual(['/textmode/start']);
  expect(mockPost.mock.calls[0][1]).toEqual(
    expect.objectContaining({ content, auto_generate: true }));
  // Lands on the entry, which goes on to the pending reply.
  expect(onSuccess.mock.calls[0][0]).toEqual(
    expect.objectContaining({ id: 20, awaitLlm: 80 }));
});

test('a long agentic entry with Auto-generate off is saved without a reply', async () => {
  localStorage.setItem('loore_auto_generate', 'false');
  started = { conversation_id: 10, user_node_id: 20, tip_id: 22 };
  const content = 'line\n'.repeat(NODE_CHAR_CAP / 2);
  const onSuccess = await send(content);
  fireEvent.click(screen.getByText(/connected entries$/));

  await waitFor(() => expect(onSuccess).toHaveBeenCalled());
  expect(urls()).toEqual(['/textmode/start']);
  expect(mockPost.mock.calls[0][1]).toEqual(
    expect.objectContaining({ content, auto_generate: false }));
  expect(onSuccess.mock.calls[0][0].awaitLlm).toBeUndefined();
});

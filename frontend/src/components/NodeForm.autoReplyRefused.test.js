// Write New Entry with Auto-generate on (#747): when the entry is saved but
// the server refuses or fails its automatic reply, a toast says why, as on
// the thread page and on the Agentic Reply path. Two paths used to drop it:
// a typed entry with Agentic Reply off (the follow-up POST /nodes/<id>/llm)
// and a recorded entry (save-as-node's llm_error). A thread too long for a
// reply is not toasted: the entry's page shows the limit.
jest.mock('../contexts/UserContext', () => ({
  useUser: () => ({ user: { id: 1, username: 'u', default_ai_usage: 'chat' } }),
}));
const mockAddToast = jest.fn();
jest.mock('../contexts/ToastContext', () => ({
  useToast: () => ({ addToast: mockAddToast }),
}));
let mockMicProps;
jest.mock('./StreamingMicButton', () => ({
  __esModule: true,
  default: (props) => {
    mockMicProps = props;
    return null;
  },
}));
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
import { THREAD_LIMIT_TEXT } from '../utils/threadLength';

const EXPORT_REFUSAL = '{user_export} without a token limit loads your whole '
  + 'archive into every reply and is available on the Pro plan.';

let llmAnswer;
let savedAnswer;
beforeEach(() => {
  localStorage.clear();
  localStorage.setItem('loore_agentic_reply', 'false');
  localStorage.setItem('loore_auto_generate', 'true');
  mockAddToast.mockReset();
  mockGet.mockReset();
  mockPost.mockReset();
  mockMicProps = null;
  // The form logs the failed request; the toast is what this checks.
  jest.spyOn(console, 'error').mockImplementation(() => {});
  // No saved draft.
  mockGet.mockRejectedValue({ response: { status: 404 } });
  llmAnswer = () => Promise.resolve({ data: { node_id: 21 } });
  savedAnswer = { id: 30, user_node_id: 30, tip_id: 30 };
  mockPost.mockImplementation((url) => {
    if (url === '/nodes/') return Promise.resolve({ data: { id: 20 } });
    if (url === '/nodes/20/llm') return llmAnswer();
    if (url === '/drafts/streaming/sess-1/save-as-node') {
      return Promise.resolve({ data: savedAnswer });
    }
    return Promise.resolve({ data: {} });
  });
});

afterEach(() => {
  console.error.mockRestore();
});

const renderForm = () => {
  const ref = createRef();
  const onSuccess = jest.fn();
  render(
    <MemoryRouter>
      <NodeForm ref={ref} parentId={null} allowAgenticPrompt onSuccess={onSuccess} />
    </MemoryRouter>,
  );
  return { ref, onSuccess };
};

const sendTyped = async () => {
  const { ref, onSuccess } = renderForm();
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Use {user_export}' } });
  await act(async () => { await ref.current.submit(); });
  return onSuccess;
};

const sendRecorded = async () => {
  const { ref, onSuccess } = renderForm();
  act(() => { mockMicProps.onComplete({ content: 'Said aloud.', sessionId: 'sess-1' }); });
  await act(async () => { await ref.current.submit(); });
  return onSuccess;
};

const refusal = (status, data) => Promise.reject(
  Object.assign(new Error(`Request failed with status code ${status}`),
    { response: { status, data } }));

describe('a typed entry with Agentic Reply off', () => {
  test('a refused reply toasts the server\'s sentence and the entry opens', async () => {
    llmAnswer = () => refusal(400, { error: EXPORT_REFUSAL });
    const onSuccess = await sendTyped();
    expect(mockPost).toHaveBeenCalledWith('/nodes/20/llm',
      expect.objectContaining({ auto_generate: true }));
    expect(mockAddToast).toHaveBeenCalledWith(EXPORT_REFUSAL, 8000);
    expect(onSuccess).toHaveBeenCalledTimes(1);
    expect(onSuccess.mock.calls[0][0]).toEqual(expect.objectContaining({ id: 20 }));
    expect(onSuccess.mock.calls[0][0].awaitLlm).toBeUndefined();
  });

  test('a read-only model\'s refusal toasts too', async () => {
    const text = 'GPT-5.6 Luna is only for Read. Choose another model for replies.';
    llmAnswer = () => refusal(400, { error: text, code: 'model_read_only' });
    await sendTyped();
    expect(mockAddToast).toHaveBeenCalledWith(text, 8000);
  });

  test('a server error without a sentence says what the thread page says', async () => {
    llmAnswer = () => Promise.reject({ response: { status: 500, data: '<html>' } });
    await sendTyped();
    expect(mockAddToast).toHaveBeenCalledWith('Error requesting LLM response.', 8000);
  });

  test('a thread too long for a reply is not toasted: the entry\'s page shows it', async () => {
    llmAnswer = () => refusal(409, { error: THREAD_LIMIT_TEXT, code: 'thread_too_long' });
    const onSuccess = await sendTyped();
    expect(mockAddToast).not.toHaveBeenCalled();
    expect(onSuccess.mock.calls[0][0]).toEqual(expect.objectContaining({ id: 20 }));
  });

  test('the usage limit (402) is left to the banner', async () => {
    llmAnswer = () => refusal(402, { error: 'monthly_spend_limit_reached' });
    await sendTyped();
    expect(mockAddToast).not.toHaveBeenCalled();
  });

  test('a reply that starts opens it, with no toast', async () => {
    const onSuccess = await sendTyped();
    expect(mockAddToast).not.toHaveBeenCalled();
    expect(onSuccess.mock.calls[0][0]).toEqual(
      expect.objectContaining({ id: 21, awaitLlm: 21 }));
  });
});

describe('a recorded entry', () => {
  test('a refused reply toasts llm_error and the entry opens', async () => {
    savedAnswer = { id: 30, user_node_id: 30, tip_id: 30, llm_error: EXPORT_REFUSAL };
    const onSuccess = await sendRecorded();
    expect(mockPost).toHaveBeenCalledWith('/drafts/streaming/sess-1/save-as-node',
      expect.objectContaining({ auto_generate: true }));
    expect(mockAddToast).toHaveBeenCalledWith(EXPORT_REFUSAL, 10000);
    expect(onSuccess.mock.calls[0][0]).toEqual(expect.objectContaining({ id: 30 }));
    expect(onSuccess.mock.calls[0][0].awaitLlm).toBeUndefined();
  });

  test('a thread too long for a reply is not toasted', async () => {
    savedAnswer = {
      id: 30, user_node_id: 30, tip_id: 30,
      llm_error: THREAD_LIMIT_TEXT, llm_error_code: 'thread_too_long',
    };
    const onSuccess = await sendRecorded();
    expect(mockAddToast).not.toHaveBeenCalled();
    expect(onSuccess).toHaveBeenCalledTimes(1);
  });

  test('a reply that starts opens it, with no toast', async () => {
    savedAnswer = { id: 30, user_node_id: 30, tip_id: 30, llm_node_id: 31 };
    const onSuccess = await sendRecorded();
    expect(mockAddToast).not.toHaveBeenCalled();
    expect(onSuccess.mock.calls[0][0]).toEqual(
      expect.objectContaining({ id: 30, awaitLlm: 31 }));
  });
});

// #391: the page learns that the user's recording is saved as an entry when
// the server says so, not when a reply completes. A reply can fail
// (llm-status 'failed') or be skipped (the draft carries a warning and no
// reply node) after the entry exists; the welcome question must not come
// back then.
let mockStreamingOptions;
jest.mock('./useStreamingTranscription', () => ({
  useStreamingTranscription: (options) => {
    mockStreamingOptions = options;
    return {
      duration: 0, isPaused: false, isInterrupted: false,
      startStreaming: jest.fn(), stopStreaming: jest.fn(() => Promise.resolve()),
      cancelStreaming: jest.fn(), resumeStreaming: jest.fn(),
      pauseRecording: jest.fn(), resumeRecording: jest.fn(),
    };
  },
}));
jest.mock('./useLongRecordingWarning', () => {
  const armAlertContext = jest.fn();
  return { useLongRecordingWarning: () => ({ armAlertContext }) };
});
let mockPolling;
jest.mock('./useAsyncTaskPolling', () => ({
  useAsyncTaskPolling: () => mockPolling,
}));
jest.mock('./useLlmTaskWarnings', () => ({ useLlmTaskWarnings: () => {} }));
jest.mock('./useSSE', () => {
  const sse = { reset: jest.fn(), disconnect: jest.fn() };
  return { useTTSStreamSSE: () => sse };
});
jest.mock('./useMediaSession', () => ({ useMediaSession: () => {} }));
jest.mock('./useOnlineStatus', () => ({ useOnlineStatus: () => true }));
jest.mock('../contexts/AudioContext', () => {
  const audio = {
    stop: jest.fn(), setGeneratingTTS: jest.fn(), loadAudioQueue: jest.fn(),
    appendChunkToQueue: jest.fn(), renameChapter: jest.fn(),
    waitingForChunks: false,
  };
  return { useAudio: () => audio };
});
const mockAddToast = jest.fn();
jest.mock('../contexts/ToastContext', () => ({
  useToast: () => ({ addToast: mockAddToast, removeToast: jest.fn() }),
}));
const mockPost = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: { post: (...args) => mockPost(...args), get: jest.fn(), delete: jest.fn() },
}));

import { renderHook, act } from '@testing-library/react';
import { useVoiceSession } from './useVoiceSession';

const IDLE_POLLING = { data: null, status: 'idle', error: null };

let onEntrySaved;
const renderSession = () => renderHook(() => useVoiceSession({
  apiEndpoint: '/voice', onEntrySaved, aiUsage: 'chat',
}));
// What useStreamingTranscription hands the session when the recording's
// transcription is final.
const finishRecording = (data) => act(async () => {
  await mockStreamingOptions.onComplete({ sessionId: 's1', ...data });
});

beforeEach(() => {
  onEntrySaved = jest.fn();
  mockPolling = IDLE_POLLING;
  mockPost.mockReset();
  mockAddToast.mockReset();
});

test('the server creating the reply node confirms the entry is saved', async () => {
  renderSession();

  await finishRecording({ content: 'my first words', llmNodeId: 7 });

  expect(onEntrySaved).toHaveBeenCalledTimes(1);
});

test('a reply that fails afterwards leaves the entry counted and Voice ready', async () => {
  mockPolling = {
    data: { node_id: 7, content: '' }, status: 'failed', error: 'Provider error',
  };
  const { result } = renderSession();

  await finishRecording({ content: 'my first words', llmNodeId: 7 });

  expect(result.current.phase).toBe('ready');
  expect(result.current.hasError).toBe(true);
  expect(onEntrySaved).toHaveBeenCalledTimes(1);
});

test('a recording saved without a reply counts as an entry', async () => {
  const { result } = renderSession();

  await finishRecording({
    content: 'my first words', llmNodeId: null,
    warning: 'AI usage is set to None, so Loore saved this without a reply.',
  });

  expect(result.current.phase).toBe('ready');
  expect(mockAddToast).toHaveBeenCalledTimes(1);
  expect(onEntrySaved).toHaveBeenCalledTimes(1);
});

test('an entry saved by the fallback POST counts', async () => {
  mockPost.mockResolvedValue({ data: { llm_node_id: 9, user_node_id: 8 } });
  renderSession();

  await finishRecording({ content: 'my first words', llmNodeId: null });

  expect(mockPost).toHaveBeenCalledWith('/voice', expect.objectContaining({
    content: 'my first words',
  }));
  expect(onEntrySaved).toHaveBeenCalledTimes(1);
});

test('an empty transcript saves nothing and counts as nothing', async () => {
  const { result } = renderSession();

  await finishRecording({ content: '   ', llmNodeId: null });

  expect(result.current.phase).toBe('ready');
  expect(mockPost).not.toHaveBeenCalled();
  expect(onEntrySaved).not.toHaveBeenCalled();
});

test('a fallback POST the server rejects saved nothing and counts as nothing', async () => {
  const err = new Error('Request failed with status code 400');
  err.response = { status: 400, data: { error: 'That could not be saved.' } };
  mockPost.mockRejectedValue(err);
  const { result } = renderSession();

  await finishRecording({ content: 'my first words', llmNodeId: null });

  expect(result.current.phase).toBe('ready');
  expect(onEntrySaved).not.toHaveBeenCalled();
});

test('a session without the callback still completes', async () => {
  onEntrySaved = undefined;
  const { result } = renderSession();

  await finishRecording({ content: 'my first words', llmNodeId: 7 });

  expect(result.current.phase).toBe('processing');
});

// #341: a capped user's record press is refused by POST /drafts/streaming/init
// (402). The recorder must never ask for the mic, must land back in idle
// (not 'error'), and must say why with a toast.
const mockStartRecording = jest.fn();
jest.mock('./useStreamingMediaRecorder', () => ({
  useStreamingMediaRecorder: () => ({
    status: 'idle', mediaBlob: null, mediaUrl: null, duration: 0,
    chunkCount: 0, error: null, interrupted: false,
    startRecording: mockStartRecording,
    stopRecording: jest.fn(), pauseRecording: jest.fn(),
    resumeRecording: jest.fn(), resetRecording: jest.fn(),
    getTotalChunks: () => 0, getPartialBlob: () => null,
  }),
}));
jest.mock('./useSSE', () => ({
  useDraftTranscriptionSSE: () => ({
    isConnected: false, isComplete: false, finalContent: null,
    draftContent: null, disconnect: jest.fn(), reset: jest.fn(),
  }),
}));
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
import { useStreamingTranscription } from './useStreamingTranscription';

const capError = () => {
  const err = new Error('Request failed with status code 402');
  err.response = {
    status: 402,
    data: { error: 'monthly_spend_limit_reached', message: 'limit' },
  };
  return err;
};

beforeEach(() => {
  mockStartRecording.mockReset();
  mockAddToast.mockReset();
  mockPost.mockReset();
});

test('a 402 from init never opens the mic and returns to idle with a toast', async () => {
  mockPost.mockRejectedValue(capError());
  const onError = jest.fn();
  const { result } = renderHook(() => useStreamingTranscription({ onError }));

  await act(async () => { await result.current.startStreaming(); });

  expect(mockPost).toHaveBeenCalledWith('/drafts/streaming/init', expect.anything());
  expect(mockStartRecording).not.toHaveBeenCalled();
  expect(result.current.sessionState).toBe('idle');
  expect(mockAddToast).toHaveBeenCalledTimes(1);
  expect(mockAddToast.mock.calls[0][0]).toMatch(/monthly usage limit/);
  expect(onError).toHaveBeenCalledTimes(1);
  expect(onError.mock.calls[0][0].spendCapped).toBe(true);
});

test('any other init failure still ends in the error state', async () => {
  mockPost.mockRejectedValue(new Error('Network Error'));
  const onError = jest.fn();
  const { result } = renderHook(() => useStreamingTranscription({ onError }));

  await act(async () => { await result.current.startStreaming(); });

  expect(mockStartRecording).not.toHaveBeenCalled();
  expect(result.current.sessionState).toBe('error');
  expect(mockAddToast).not.toHaveBeenCalled();
  expect(onError.mock.calls[0][0].spendCapped).toBeUndefined();
});

// 2026-10-01: a Voice recording where AI may not read (AI usage 'none' on
// the account or the thread) is refused by init with 403
// {"code": "ai_usage_none", "scope"}. Like the cap: no mic, back to idle,
// and the parent learns why (it shows the explanation, so no toast here).
const aiUsageError = (scope) => {
  const err = new Error('Request failed with status code 403');
  err.response = {
    status: 403,
    data: { error: 'Voice mode needs AI', code: 'ai_usage_none', scope },
  };
  return err;
};

test.each(['account', 'thread'])(
  'an ai_usage_none refusal (%s) never opens the mic and returns to idle',
  async (scope) => {
    mockPost.mockRejectedValue(aiUsageError(scope));
    const onError = jest.fn();
    const { result } = renderHook(() => useStreamingTranscription({
      onError, label: 'Voice', aiUsage: 'none',
    }));

    await act(async () => { await result.current.startStreaming(); });

    expect(mockStartRecording).not.toHaveBeenCalled();
    expect(result.current.sessionState).toBe('idle');
    expect(mockAddToast).not.toHaveBeenCalled();
    expect(onError).toHaveBeenCalledTimes(1);
    const err = onError.mock.calls[0][0];
    expect(err.aiUsageRefused).toBe(true);
    expect(err.aiUsageScope).toBe(scope);
    expect(err.startup).toBe(true);
  },
);

// The server's limits on one recording (utils/recordingLimits): audio kept
// without its text (the monthly usage limit, or past the length limit) is
// said once, with no error sound, and the recording goes on; a chunk past
// what one recording may hold is not retried, and the recording stops.
const mockStartRecording = jest.fn(() => Promise.resolve());
const mockStopRecording = jest.fn(() => Promise.resolve());
const mockResetRecording = jest.fn();
let mockRecorderOpts;
jest.mock('./useStreamingMediaRecorder', () => ({
  useStreamingMediaRecorder: (opts) => {
    mockRecorderOpts = opts;
    return {
      status: 'recording',
      mediaBlob: null,
      mediaUrl: null,
      duration: 0,
      chunkCount: 0,
      error: null,
      interrupted: false,
      startRecording: (...a) => mockStartRecording(...a),
      stopRecording: (...a) => mockStopRecording(...a),
      pauseRecording: () => {},
      resumeRecording: () => {},
      resetRecording: (...a) => mockResetRecording(...a),
      getTotalChunks: () => 2,
      getPartialBlob: () => null,
    };
  },
}));

const mockSse = { opts: null };
jest.mock('./useSSE', () => ({
  useDraftTranscriptionSSE: (sessionId, opts) => {
    mockSse.opts = opts;
    return {
      isConnected: true, isComplete: false, finalContent: null,
      draftContent: '', disconnect: () => {}, reset: () => {},
    };
  },
}));

const mockAddToast = jest.fn(() => 1);
jest.mock('../contexts/ToastContext', () => ({
  useToast: () => ({ addToast: mockAddToast, removeToast: () => {} }),
}));

const mockPlayErrorSound = jest.fn();
jest.mock('../utils/errorSound', () => ({
  playErrorSound: (...a) => mockPlayErrorSound(...a),
}));

const mockPost = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    post: (...args) => mockPost(...args),
    get: () => new Promise(() => {}),
  },
}));

import { renderHook, act } from '@testing-library/react';
import { useStreamingTranscription } from './useStreamingTranscription';
import {
  RECORDING_LIMIT_MESSAGE, UNTRANSCRIBED_MESSAGES,
} from '../utils/recordingLimits';

const flush = () => new Promise((r) => setTimeout(r, 0));
const callsTo = (suffix) => mockPost.mock.calls.filter(([u]) => u.includes(suffix));

const keptAtLimit = (index) => ({
  chunk_index: index, status: 'failed', code: 'monthly_spend_limit_reached',
  error: "Saved as audio, not turned into text: you've reached your monthly usage limit.",
});

beforeEach(() => {
  mockStartRecording.mockClear();
  mockStopRecording.mockClear();
  mockResetRecording.mockClear();
  mockAddToast.mockClear();
  mockPlayErrorSound.mockClear();
  mockPost.mockReset();
  mockPost.mockImplementation((url) => {
    if (url === '/drafts/streaming/init') {
      return Promise.resolve({ data: { draft_id: 7, session_id: 's1' } });
    }
    return Promise.resolve({ data: {} });
  });
  jest.spyOn(console, 'warn').mockImplementation(() => {});
  jest.spyOn(console, 'error').mockImplementation(() => {});
  jest.spyOn(console, 'log').mockImplementation(() => {});
});

afterEach(() => {
  console.warn.mockRestore();
  console.error.mockRestore();
  console.log.mockRestore();
});

async function startRecording(options = {}) {
  const hook = renderHook(() => useStreamingTranscription(options));
  await act(async () => { await hook.result.current.startStreaming(); });
  expect(hook.result.current.sessionState).toBe('recording');
  return hook;
}

describe('audio kept without its text', () => {
  test('at the usage limit: said once, the banner, no error sound, the recording goes on', async () => {
    const onError = jest.fn();
    const banner = jest.fn();
    window.addEventListener('loore:spend-capped', banner);
    try {
      const { result } = await startRecording({ onError });

      act(() => {
        for (let i = 20; i < 40; i += 1) mockSse.opts.onError(keptAtLimit(i));
      });

      expect(mockAddToast).toHaveBeenCalledTimes(1);
      expect(mockAddToast.mock.calls[0][0]).toBe(
        UNTRANSCRIBED_MESSAGES.monthly_spend_limit_reached);
      expect(banner).toHaveBeenCalledTimes(1);
      expect(mockPlayErrorSound).not.toHaveBeenCalled();
      expect(onError).not.toHaveBeenCalled();
      expect(mockStopRecording).not.toHaveBeenCalled();
      expect(result.current.sessionState).toBe('recording');
    } finally {
      window.removeEventListener('loore:spend-capped', banner);
    }
  });

  test('past the length limit: said once, no banner', async () => {
    const banner = jest.fn();
    window.addEventListener('loore:spend-capped', banner);
    try {
      await startRecording();

      act(() => {
        mockSse.opts.onError({ chunk_index: 1440, status: 'failed', code: 'recording_too_long' });
        mockSse.opts.onError({ chunk_index: 1441, status: 'failed', code: 'recording_too_long' });
      });

      expect(mockAddToast).toHaveBeenCalledTimes(1);
      expect(mockAddToast.mock.calls[0][0]).toBe(UNTRANSCRIBED_MESSAGES.recording_too_long);
      expect(banner).not.toHaveBeenCalled();
      expect(mockPlayErrorSound).not.toHaveBeenCalled();
    } finally {
      window.removeEventListener('loore:spend-capped', banner);
    }
  });

  test('control: a chunk that failed still sounds the error', async () => {
    const onError = jest.fn();
    await startRecording({ onError });

    act(() => {
      mockSse.opts.onError({ chunk_index: 3, status: 'failed', error: 'Transcription failed' });
    });

    expect(mockPlayErrorSound).toHaveBeenCalledTimes(1);
    expect(onError).toHaveBeenCalledTimes(1);
    expect(mockAddToast).not.toHaveBeenCalled();
  });
});

describe('a chunk past what one recording may hold', () => {
  test.each(['recording_too_large', 'chunk_index_out_of_range', 'chunk_too_large'])(
    '%s is not retried, and the recording stops with its audio kept',
    async (code) => {
      const onError = jest.fn();
      const { result } = await startRecording({ onError });
      mockPost.mockImplementation((url) => {
        if (url.endsWith('/audio-chunk')) {
          const err = new Error('Request failed');
          err.response = { status: code === 'chunk_index_out_of_range' ? 400 : 413, data: { code } };
          return Promise.reject(err);
        }
        return Promise.resolve({ data: {} });
      });

      await act(async () => {
        mockRecorderOpts.onChunkReady(new Blob(['a']), 2880);
        await flush();
      });

      expect(callsTo('/audio-chunk')).toHaveLength(1);
      // Stopped, not reset: the audio stays for "Save audio".
      expect(mockStopRecording).toHaveBeenCalledTimes(1);
      expect(mockResetRecording).not.toHaveBeenCalled();
      expect(onError).toHaveBeenCalledTimes(1);
      expect(onError.mock.calls[0][0].fatal).toBe(true);
      expect(result.current.sessionState).toBe('error');
      expect(result.current.errorMessage).toBe(RECORDING_LIMIT_MESSAGE);
    },
  );
});

// The server's limits on one recording (utils/recordingLimits): audio kept
// without its text (past the length one recording may have transcribed) is
// said once, with no error sound, and the recording goes on; a chunk past
// what one recording may hold is not retried, and the recording stops; a
// start refused while other recordings are in progress says so plainly.
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
  test('past the length limit: said once, no error sound, the recording goes on', async () => {
    const onError = jest.fn();
    const banner = jest.fn();
    window.addEventListener('loore:spend-capped', banner);
    try {
      const { result } = await startRecording({ onError });

      act(() => {
        for (let i = 1440; i < 1460; i += 1) {
          mockSse.opts.onError({
            chunk_index: i, status: 'failed', code: 'recording_too_long',
            error: 'Saved as audio, not turned into text: the recording is longer than six hours.',
          });
        }
      });

      expect(mockAddToast).toHaveBeenCalledTimes(1);
      expect(mockAddToast.mock.calls[0][0]).toBe(UNTRANSCRIBED_MESSAGES.recording_too_long);
      expect(banner).not.toHaveBeenCalled();
      expect(mockPlayErrorSound).not.toHaveBeenCalled();
      expect(onError).not.toHaveBeenCalled();
      expect(mockStopRecording).not.toHaveBeenCalled();
      expect(result.current.sessionState).toBe('recording');
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

describe('other recordings in progress', () => {
  test('a refused start says why in plain words and opens no mic', async () => {
    const text = 'Other recordings of yours are still in progress. Finish one of them, then start this one.';
    mockPost.mockImplementation((url) => {
      if (url === '/drafts/streaming/init') {
        const err = new Error('Request failed with status code 429');
        err.response = { status: 429, data: { error: text, code: 'too_many_recordings' } };
        return Promise.reject(err);
      }
      return Promise.resolve({ data: {} });
    });
    const onError = jest.fn();
    const { result } = renderHook(() => useStreamingTranscription({ onError }));

    await act(async () => { await result.current.startStreaming(); });

    expect(mockStartRecording).not.toHaveBeenCalled();
    expect(onError).toHaveBeenCalledTimes(1);
    const err = onError.mock.calls[0][0];
    expect(err.message).toBe(text);
    expect(err.fatal).toBe(true);   // the Voice screen shows it verbatim
    expect(result.current.errorMessage).toBe(text);
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

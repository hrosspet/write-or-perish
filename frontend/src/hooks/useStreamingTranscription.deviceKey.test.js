// #388: each recording page has its own device key. "Continue recording"
// claims the session first, so the device that recorded it before is
// refused from then on; that device stops and says the recording continued
// elsewhere, instead of uploading chunks under the same indexes.
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

const mockSse = { opts: null, sessionId: null };
jest.mock('./useSSE', () => ({
  useDraftTranscriptionSSE: (sessionId, opts) => {
    mockSse.opts = opts;
    mockSse.sessionId = sessionId;
    return {
      isConnected: true, isComplete: false, finalContent: null,
      draftContent: '', disconnect: () => {}, reset: () => {},
    };
  },
}));

jest.mock('../contexts/ToastContext', () => ({
  useToast: () => ({ addToast: () => 1, removeToast: () => {} }),
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
import { RECORDING_DEVICE_KEY } from '../utils/recordingDevice';

const flush = () => new Promise((r) => setTimeout(r, 0));

const refusal = (status, code) => {
  const err = new Error(`Request failed with status code ${status}`);
  err.response = { status, data: { code } };
  return err;
};
const takenOver = () => refusal(409, 'session_taken_over');

const callsTo = (suffix) => mockPost.mock.calls.filter(([u]) => u.includes(suffix));

beforeEach(() => {
  mockStartRecording.mockClear();
  mockStopRecording.mockClear();
  mockResetRecording.mockClear();
  mockPost.mockReset();
  mockPost.mockImplementation((url) => {
    if (url === '/drafts/streaming/init') {
      return Promise.resolve({ data: { draft_id: 7, session_id: 's1' } });
    }
    if (url.endsWith('/claim')) {
      return Promise.resolve({ data: { next_chunk_index: 5 } });
    }
    return Promise.resolve({ data: {} });
  });
  navigator.sendBeacon = jest.fn(() => true);
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

describe('the device key is sent with the recording', () => {
  test('init, every chunk, finalize and the transcription stream carry it', async () => {
    expect(RECORDING_DEVICE_KEY).toMatch(/^[\w-]{16,64}$/);
    const { result } = await startRecording();
    expect(callsTo('/streaming/init')[0][1].device_key).toBe(RECORDING_DEVICE_KEY);
    expect(mockSse.opts.deviceKey).toBe(RECORDING_DEVICE_KEY);

    await act(async () => {
      mockRecorderOpts.onChunkReady(new Blob(['a']), 0);
      await flush();
    });
    expect(callsTo('/audio-chunk')[0][1].get('device_key')).toBe(RECORDING_DEVICE_KEY);

    await act(async () => { await result.current.stopStreaming(); });
    expect(callsTo('/finalize')[0][1].device_key).toBe(RECORDING_DEVICE_KEY);
  });

  test('a release carries it', async () => {
    const { result } = await startRecording();
    act(() => { result.current.cancelStreaming(); });
    expect(navigator.sendBeacon.mock.calls[0][0]).toMatch(
      `/api/drafts/streaming/s1/release?device_key=${RECORDING_DEVICE_KEY}`);
  });
});

describe('another device continued this recording', () => {
  test('a refused chunk stops the recorder once and keeps its audio', async () => {
    const onError = jest.fn();
    const { result } = await startRecording({ onError });
    mockPost.mockImplementation((url) => (
      url.endsWith('/audio-chunk') ? Promise.reject(takenOver()) : Promise.resolve({ data: {} })
    ));

    await act(async () => {
      mockRecorderOpts.onChunkReady(new Blob(['a']), 3);
      mockRecorderOpts.onChunkReady(new Blob(['b']), 4);
      await flush();
    });

    expect(callsTo('/audio-chunk')).toHaveLength(2); // one attempt each
    // Stopped, not reset: the audio stays for "Save audio".
    expect(mockStopRecording).toHaveBeenCalledTimes(1);
    expect(mockResetRecording).not.toHaveBeenCalled();
    expect(onError).toHaveBeenCalledTimes(1);
    const err = onError.mock.calls[0][0];
    expect(err.fatal).toBe(true);
    expect(err.sessionEnded).toBe(true);
    expect(err.message).toMatch(/continued in another tab or on another device/);
    expect(result.current.sessionState).toBe('error');
    expect(result.current.errorMessage).toBe(err.message);
  });

  test('the transcription stream saying so stops the recorder', async () => {
    const onError = jest.fn();
    const { result } = await startRecording({ onError });

    act(() => { mockSse.opts.onTakenOver({ code: 'session_taken_over' }); });

    expect(mockStopRecording).toHaveBeenCalledTimes(1);
    expect(onError).toHaveBeenCalledTimes(1);
    expect(onError.mock.calls[0][0].message).toMatch(/continued in another tab/);
    expect(result.current.sessionState).toBe('error');
  });

  test('Stop after a refused final chunk does not finalize', async () => {
    const onError = jest.fn();
    const { result } = await startRecording({ onError });
    mockPost.mockImplementation((url) => (
      url.endsWith('/audio-chunk') ? Promise.reject(takenOver()) : Promise.resolve({ data: {} })
    ));
    mockStopRecording.mockImplementationOnce(() => {
      mockRecorderOpts.onChunkReady(new Blob(['last']), 6);
      return Promise.resolve();
    });

    await act(async () => { await result.current.stopStreaming(); });

    expect(callsTo('/finalize')).toHaveLength(0);
    expect(onError).toHaveBeenCalledTimes(1);
    expect(onError.mock.calls[0][0].message).toMatch(/continued in another tab/);
    expect(result.current.sessionState).toBe('error');
  });

  test('a refused finalize says the recording continued elsewhere', async () => {
    const onError = jest.fn();
    const { result } = await startRecording({ onError });
    mockPost.mockImplementation((url) => (
      url.endsWith('/finalize') ? Promise.reject(takenOver()) : Promise.resolve({ data: {} })
    ));

    await act(async () => { await result.current.stopStreaming(); });

    expect(onError).toHaveBeenCalledTimes(1);
    expect(onError.mock.calls[0][0].sessionEnded).toBe(true);
    expect(result.current.errorMessage).toMatch(/continued in another tab/);
  });

  test('a chunk retried when back online stops the recorder', async () => {
    jest.useFakeTimers();
    try {
      const onError = jest.fn();
      const { result } = await startRecording({ onError });
      // Offline: every attempt fails, and the chunk waits for the network.
      mockPost.mockImplementation((url) => (
        url.endsWith('/audio-chunk')
          ? Promise.reject(new Error('Network Error'))
          : Promise.resolve({ data: {} })
      ));
      await act(async () => {
        mockRecorderOpts.onChunkReady(new Blob(['a']), 3);
        for (let i = 0; i < 5; i += 1) {
          await Promise.resolve();
          jest.advanceTimersByTime(20000);
          await Promise.resolve();
        }
      });
      expect(mockStopRecording).not.toHaveBeenCalled();

      mockPost.mockImplementation((url) => (
        url.endsWith('/audio-chunk') ? Promise.reject(takenOver()) : Promise.resolve({ data: {} })
      ));
      await act(async () => {
        window.dispatchEvent(new Event('online'));
        for (let i = 0; i < 5; i += 1) await Promise.resolve();
      });

      expect(mockStopRecording).toHaveBeenCalledTimes(1);
      expect(result.current.sessionState).toBe('error');
      expect(result.current.errorMessage).toMatch(/continued in another tab/);
    } finally {
      jest.useRealTimers();
    }
  });
});

describe('Continue recording claims the session first', () => {
  test('records from the index the claim names', async () => {
    const { result } = renderHook(() => useStreamingTranscription({}));

    await act(async () => {
      await result.current.resumeStreaming('s9', 9, 4, 'audio/webm');
    });

    const claim = callsTo('/drafts/streaming/s9/claim');
    expect(claim).toHaveLength(1);
    expect(claim[0][1]).toEqual({ device_key: RECORDING_DEVICE_KEY });
    // The claim answered 5: chunk 4 is already on the server.
    expect(mockStartRecording).toHaveBeenCalledWith(
      expect.objectContaining({ startingChunkIndex: 5, forceMimeFamily: 'audio/webm' }));
    expect(result.current.sessionState).toBe('recording');
  });

  test.each([
    ['session_live', 409, /still in progress in another tab or on another device/],
    ['session_not_active', 400, /already ended/],
    ['session_not_found', 404, /already ended/],
    [undefined, 500, /could not be continued/],
  ])('a refused claim (%s) never opens the mic and says why', async (code, status, text) => {
    mockPost.mockImplementation((url) => (
      url.endsWith('/claim') ? Promise.reject(refusal(status, code)) : Promise.resolve({ data: {} })
    ));
    const onError = jest.fn();
    const { result } = renderHook(() => useStreamingTranscription({ onError }));

    await act(async () => {
      await result.current.resumeStreaming('s9', 9, 4, 'audio/webm');
    });

    expect(mockStartRecording).not.toHaveBeenCalled();
    expect(onError).toHaveBeenCalledTimes(1);
    const err = onError.mock.calls[0][0];
    expect(err.fatal).toBe(true);
    expect(err.message).toMatch(text);
    expect(result.current.sessionState).toBe('error');
  });

  test('a page that lost the session may claim it back', async () => {
    const onError = jest.fn();
    const { result } = await startRecording({ onError });
    act(() => { mockSse.opts.onTakenOver({}); });
    expect(onError).toHaveBeenCalledTimes(1);

    await act(async () => {
      await result.current.resumeStreaming('s1', 7, 4, 'audio/webm');
    });
    expect(result.current.sessionState).toBe('recording');

    // A later refusal is reported again, not swallowed as already handled.
    act(() => { mockSse.opts.onTakenOver({}); });
    expect(onError).toHaveBeenCalledTimes(2);
  });
});

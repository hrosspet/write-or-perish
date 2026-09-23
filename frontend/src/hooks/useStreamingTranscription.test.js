// #320: a session ended elsewhere (another tab recovered or discarded it)
// must stop this tab's recorder and must not be reported as a finished
// recording — in voice mode onComplete sends the transcript to the LLM.
// A tab that leaves mid-recording releases its session.
let mockRecorderOpts;
const mockResetRecording = jest.fn();
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
      startRecording: () => Promise.resolve(),
      stopRecording: () => Promise.resolve(),
      pauseRecording: () => {},
      resumeRecording: () => {},
      resetRecording: (...a) => mockResetRecording(...a),
      getTotalChunks: () => 2,
      getPartialBlob: () => null,
    };
  },
}));

let mockSseOpts;
const mockSseHandle = {
  isConnected: true,
  isComplete: false,
  finalContent: null,
  draftContent: '',
  disconnect: () => {},
  reset: () => {},
};
jest.mock('./useSSE', () => ({
  useDraftTranscriptionSSE: (sessionId, opts) => {
    mockSseOpts = opts;
    return mockSseHandle;
  },
}));

const mockToast = { addToast: () => 1, removeToast: () => {} };
jest.mock('../contexts/ToastContext', () => ({
  useToast: () => mockToast,
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

const flush = () => new Promise((r) => setTimeout(r, 0));

const endedError = (code) => {
  const err = new Error('rejected');
  err.response = { status: code === 'session_not_found' ? 404 : 400, data: { code } };
  return err;
};

beforeEach(() => {
  mockResetRecording.mockReset();
  mockPost.mockReset();
  mockPost.mockImplementation((url) => {
    if (url === '/drafts/streaming/init') {
      return Promise.resolve({ data: { draft_id: 7, session_id: 's1' } });
    }
    return Promise.resolve({ data: {} });
  });
  navigator.sendBeacon = jest.fn(() => true);
});

async function startRecording(options = {}) {
  const hook = renderHook(() => useStreamingTranscription(options));
  await act(async () => { await hook.result.current.startStreaming(); });
  expect(hook.result.current.sessionState).toBe('recording');
  return hook;
}

describe('all_complete before this tab finalized (#320)', () => {
  test('stops the recorder and reports a fatal error, not a completion', async () => {
    const onComplete = jest.fn();
    const onError = jest.fn();
    const { result } = await startRecording({ onComplete, onError });

    act(() => { mockSseOpts.onAllComplete({ content: 'half a thought' }); });

    expect(onComplete).not.toHaveBeenCalled();
    expect(onError).toHaveBeenCalledTimes(1);
    const err = onError.mock.calls[0][0];
    expect(err.fatal).toBe(true);
    expect(err.sessionEnded).toBe(true);
    expect(mockResetRecording).toHaveBeenCalledTimes(1);
    expect(result.current.sessionState).toBe('error');
  });

  test('after stopStreaming, all_complete is the normal completion', async () => {
    const onComplete = jest.fn();
    const onError = jest.fn();
    const { result } = await startRecording({ onComplete, onError });

    await act(async () => { await result.current.stopStreaming(); });
    expect(mockPost).toHaveBeenCalledWith(
      '/drafts/streaming/s1/finalize', expect.anything(), expect.anything());

    act(() => { mockSseOpts.onAllComplete({ content: 'whole thought' }); });

    expect(onError).not.toHaveBeenCalled();
    expect(onComplete).toHaveBeenCalledTimes(1);
    expect(onComplete.mock.calls[0][0].content).toBe('whole thought');
    expect(mockResetRecording).not.toHaveBeenCalled();
  });
});

describe('uploads into a dead session (#320)', () => {
  test.each(['session_not_active', 'session_not_found'])(
    '%s stops the recorder once, without retrying', async (code) => {
      const onError = jest.fn();
      const { result } = await startRecording({ onError });
      mockPost.mockImplementation((url) => (
        url.endsWith('/audio-chunk')
          ? Promise.reject(endedError(code))
          : Promise.resolve({ data: {} })
      ));

      await act(async () => {
        mockRecorderOpts.onChunkReady(new Blob(['a']), 3);
        mockRecorderOpts.onChunkReady(new Blob(['b']), 4);
        await flush();
      });

      const chunkPosts = mockPost.mock.calls.filter(([u]) => u.endsWith('/audio-chunk'));
      expect(chunkPosts).toHaveLength(2); // one attempt each, no retries
      expect(mockResetRecording).toHaveBeenCalledTimes(1);
      expect(onError).toHaveBeenCalledTimes(1);
      expect(onError.mock.calls[0][0].sessionEnded).toBe(true);
      expect(result.current.sessionState).toBe('error');
    });
});

describe('releasing the session when the tab leaves (#320)', () => {
  const releaseUrl = '/api/drafts/streaming/s1/release';

  test('pagehide mid-recording releases the session', async () => {
    await startRecording();

    act(() => { window.dispatchEvent(new Event('pagehide')); });

    expect(navigator.sendBeacon).toHaveBeenCalledTimes(1);
    expect(navigator.sendBeacon.mock.calls[0][0]).toMatch(releaseUrl);
  });

  test('pagehide after finalize does not release', async () => {
    const { result } = await startRecording();
    await act(async () => { await result.current.stopStreaming(); });

    act(() => { window.dispatchEvent(new Event('pagehide')); });

    expect(navigator.sendBeacon).not.toHaveBeenCalled();
  });

  test('cancelStreaming mid-recording releases; after completion it does not', async () => {
    const { result } = await startRecording();
    act(() => { result.current.cancelStreaming(); });
    expect(navigator.sendBeacon).toHaveBeenCalledTimes(1);

    navigator.sendBeacon.mockClear();
    const second = await startRecording();
    await act(async () => { await second.result.current.stopStreaming(); });
    act(() => { mockSseOpts.onAllComplete({ content: 'done' }); });
    act(() => { second.result.current.cancelStreaming(); });
    expect(navigator.sendBeacon).not.toHaveBeenCalled();
  });

  test('falls back to a POST without sendBeacon', async () => {
    delete navigator.sendBeacon;
    await startRecording();

    act(() => { window.dispatchEvent(new Event('pagehide')); });

    expect(mockPost).toHaveBeenCalledWith('/drafts/streaming/s1/release');
  });
});

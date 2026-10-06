// #320: a session ended elsewhere (another tab recovered or discarded it)
// must stop this tab's recorder and must not be reported as a finished
// recording — in voice mode onComplete sends the transcript to the LLM.
// A tab that leaves mid-recording releases its session.
let mockRecorderOpts;
const mockResetRecording = jest.fn();
const mockStopRecording = jest.fn(() => Promise.resolve());
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
      stopRecording: (...a) => mockStopRecording(...a),
      pauseRecording: () => {},
      resumeRecording: () => {},
      resetRecording: (...a) => mockResetRecording(...a),
      getTotalChunks: () => 2,
      getPartialBlob: () => null,
    };
  },
}));

// A stand-in for useDraftTranscriptionSSE that keeps its own state the
// way the real hook does: on all_complete it marks itself complete (and
// sets finalContent) BEFORE it calls onAllComplete, and reset() clears
// that. The hook under test reads isComplete/finalContent back.
const mockSse = { resets: 0, isComplete: false, fireAllComplete: null };
jest.mock('./useSSE', () => {
  const React = require('react');
  return {
    useDraftTranscriptionSSE: (sessionId, opts) => {
      const [isComplete, setIsComplete] = React.useState(false);
      const [finalContent, setFinalContent] = React.useState(null);
      const optsRef = React.useRef(opts);
      React.useEffect(() => { optsRef.current = opts; });
      mockSse.isComplete = isComplete;
      mockSse.fireAllComplete = (data) => {
        setIsComplete(true);
        setFinalContent(data.content);
        if (optsRef.current.onAllComplete) optsRef.current.onAllComplete(data);
      };
      const reset = React.useCallback(() => {
        mockSse.resets += 1;
        setIsComplete(false);
        setFinalContent(null);
      }, []);
      const disconnect = React.useCallback(() => {}, []);
      return {
        isConnected: true, isComplete, finalContent, draftContent: '',
        disconnect, reset,
      };
    },
  };
});

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
  mockStopRecording.mockClear();
  mockSse.resets = 0;
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
    const resetsBefore = mockSse.resets;

    act(() => { mockSse.fireAllComplete({ content: 'half a thought' }); });

    expect(onComplete).not.toHaveBeenCalled();
    expect(onError).toHaveBeenCalledTimes(1);
    const err = onError.mock.calls[0][0];
    expect(err.fatal).toBe(true);
    expect(err.sessionEnded).toBe(true);
    // Stopped, not reset: the recorded audio stays for "Save audio".
    expect(mockStopRecording).toHaveBeenCalledTimes(1);
    expect(mockResetRecording).not.toHaveBeenCalled();
    // Stays 'error' although the SSE hook marked itself complete, and the
    // SSE state is cleared for the next recording.
    expect(result.current.sessionState).toBe('error');
    expect(mockSse.resets).toBeGreaterThan(resetsBefore);
    expect(mockSse.isComplete).toBe(false);
  });

  test('the next recording starts from fresh SSE state', async () => {
    const { result } = await startRecording({ onError: () => {} });
    act(() => { mockSse.fireAllComplete({ content: 'half a thought' }); });

    await act(async () => { await result.current.startStreaming(); });

    expect(result.current.sessionState).toBe('recording');
    expect(mockSse.isComplete).toBe(false);
  });

  test('after stopStreaming, all_complete is the normal completion', async () => {
    const onComplete = jest.fn();
    const onError = jest.fn();
    const { result } = await startRecording({ onComplete, onError });

    await act(async () => { await result.current.stopStreaming(); });
    expect(mockPost).toHaveBeenCalledWith(
      '/drafts/streaming/s1/finalize', expect.anything(), expect.anything());

    act(() => { mockSse.fireAllComplete({ content: 'whole thought' }); });

    expect(onError).not.toHaveBeenCalled();
    expect(onComplete).toHaveBeenCalledTimes(1);
    expect(onComplete.mock.calls[0][0].content).toBe('whole thought');
    expect(result.current.sessionState).toBe('complete');
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
      expect(mockStopRecording).toHaveBeenCalledTimes(1);
      expect(mockResetRecording).not.toHaveBeenCalled();
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
    act(() => { mockSse.fireAllComplete({ content: 'done' }); });
    act(() => { second.result.current.cancelStreaming(); });
    expect(navigator.sendBeacon).not.toHaveBeenCalled();
  });

  test('unmounting mid-recording releases (text mode SPA navigation)', async () => {
    const hook = await startRecording();
    hook.unmount();
    expect(navigator.sendBeacon).toHaveBeenCalledTimes(1);
    expect(navigator.sendBeacon.mock.calls[0][0]).toMatch(releaseUrl);
  });

  test('cancel then unmount (voice page) releases once', async () => {
    const hook = await startRecording();
    act(() => { hook.result.current.cancelStreaming(); });
    hook.unmount();
    expect(navigator.sendBeacon).toHaveBeenCalledTimes(1);
  });

  test('unmounting after finalize does not release', async () => {
    const hook = await startRecording();
    await act(async () => { await hook.result.current.stopStreaming(); });
    hook.unmount();
    expect(navigator.sendBeacon).not.toHaveBeenCalled();
  });

  test('pagehide then unmount releases once', async () => {
    const hook = await startRecording();
    act(() => { window.dispatchEvent(new Event('pagehide')); });
    hook.unmount();
    expect(navigator.sendBeacon).toHaveBeenCalledTimes(1);
  });

  test('falls back to a POST without sendBeacon', async () => {
    delete navigator.sendBeacon;
    await startRecording();

    act(() => { window.dispatchEvent(new Event('pagehide')); });

    expect(mockPost).toHaveBeenCalledWith('/drafts/streaming/s1/release');
  });
});

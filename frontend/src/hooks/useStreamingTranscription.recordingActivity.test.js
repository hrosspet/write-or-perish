// Payments (#438): a streaming session is a recording in progress from its
// accepted start until its transcript is in, so the app does not leave for
// the subscribe page while its last chunks and its finalize go out
// (UserContext waits for utils/recordingActivity). A refused start is not
// held: the page must show at once.
jest.mock('./useStreamingMediaRecorder', () => ({
  useStreamingMediaRecorder: () => ({
    status: 'recording', mediaBlob: null, mediaUrl: null, duration: 0,
    chunkCount: 0, error: null, interrupted: false,
    startRecording: () => Promise.resolve(),
    stopRecording: () => Promise.resolve(),
    pauseRecording: () => {}, resumeRecording: () => {},
    resetRecording: () => {}, getTotalChunks: () => 1,
    getPartialBlob: () => null,
  }),
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
import { isRecordingActive } from '../utils/recordingActivity';

beforeEach(() => {
  mockPost.mockReset();
  navigator.sendBeacon = jest.fn(() => true);
  jest.spyOn(console, 'error').mockImplementation(() => {});
  jest.spyOn(console, 'log').mockImplementation(() => {});
});

afterEach(() => {
  console.error.mockRestore();
  console.log.mockRestore();
});

test('held from the accepted start until the transcript is in', async () => {
  mockPost.mockImplementation((url) => Promise.resolve(
    url === '/drafts/streaming/init'
      ? { data: { draft_id: 7, session_id: 's1' } } : { data: {} }));
  const onComplete = jest.fn();
  const { result, unmount } = renderHook(() => useStreamingTranscription({ onComplete }));

  await act(async () => { await result.current.startStreaming(); });
  expect(result.current.sessionState).toBe('recording');
  expect(isRecordingActive()).toBe(true);

  await act(async () => { await result.current.stopStreaming(); });
  expect(result.current.sessionState).toBe('finalizing');
  expect(isRecordingActive()).toBe(true);

  act(() => { mockSse.opts.onAllComplete({ content: 'my words' }); });
  expect(onComplete).toHaveBeenCalled();
  expect(isRecordingActive()).toBe(false);
  unmount();
});

test('a start the server refused is not held', async () => {
  const refused = new Error('Request failed with status code 402');
  refused.response = { status: 402, data: { error: 'no_subscription' } };
  mockPost.mockRejectedValue(refused);
  const onError = jest.fn();
  const { result, unmount } = renderHook(() => useStreamingTranscription({ onError }));

  await act(async () => {
    await result.current.startStreaming().catch(() => {});
  });
  expect(isRecordingActive()).toBe(false);
  unmount();
});

test('leaving the page ends the hold', async () => {
  mockPost.mockImplementation((url) => Promise.resolve(
    url === '/drafts/streaming/init'
      ? { data: { draft_id: 7, session_id: 's2' } } : { data: {} }));
  const { result, unmount } = renderHook(() => useStreamingTranscription({}));
  await act(async () => { await result.current.startStreaming(); });
  expect(isRecordingActive()).toBe(true);
  unmount();
  expect(isRecordingActive()).toBe(false);
});

// Review of #682: a pause longer than the server's 45 s liveness window.
// Nothing here depends on how long the pause was: Stop finalizes the whole
// recording, and it stays a recording in progress until its transcript is
// in. The server now takes the chunks after such a pause also from an
// account that stopped being let in meanwhile (backend TestALongGap).
test('a long pause, then Stop: finalized whole, held until its transcript is in', async () => {
  mockPost.mockImplementation((url) => Promise.resolve(
    url === '/drafts/streaming/init'
      ? { data: { draft_id: 7, session_id: 's3' } } : { data: {} }));
  const { result, unmount } = renderHook(() => useStreamingTranscription({ label: 'Voice' }));
  await act(async () => { await result.current.startStreaming(); });
  act(() => { result.current.pauseRecording(); });
  await act(async () => { await new Promise((r) => setTimeout(r, 20)); });
  expect(isRecordingActive()).toBe(true);
  act(() => { result.current.resumeRecording(); });
  await act(async () => { await result.current.stopStreaming(); });
  const finalize = mockPost.mock.calls.find(([u]) => u.endsWith('/s3/finalize'));
  expect(finalize).toBeTruthy();
  expect(finalize[1].total_chunks).toBe(1);
  expect(isRecordingActive()).toBe(true);
  unmount();
});

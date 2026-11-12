// #648: in Voice mode a turn refused because the thread is too long for
// another reply is said aloud (the user may not be looking), with the
// browser's own speech, and the page is told ('too_long') so it shows the
// limit with the way to a new thread. Other refusals stay as they were.
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
const mockPlayErrorSound = jest.fn();
jest.mock('../utils/errorSound', () => ({
  playErrorSound: (...args) => mockPlayErrorSound(...args),
}));

import { renderHook, act } from '@testing-library/react';
import { useVoiceSession } from './useVoiceSession';
import {
  SENTENCE_AFTER_SOUND_MS, THREAD_LIMIT_SPOKEN, resetSpeechForTests,
} from '../utils/threadLength';

const IDLE_POLLING = { data: null, status: 'idle', error: null };
const SKIPPED = "Your recording is saved. Loore didn't reply because this "
  + 'thread is too long for another AI reply. Start a new thread to keep '
  + 'talking.';

let spoken;
let onAiUsageRefused;
const renderSession = () => renderHook(() => useVoiceSession({
  apiEndpoint: '/voice', aiUsage: 'chat', onAiUsageRefused,
  onEntrySaved: jest.fn(),
}));
const finishRecording = (data) => act(async () => {
  await mockStreamingOptions.onComplete({ sessionId: 's1', ...data });
});
// The sentence follows the error sound.
const afterSound = () => act(() => {
  jest.advanceTimersByTime(SENTENCE_AFTER_SOUND_MS);
});
const said = () => spoken.filter(Boolean);

beforeEach(() => {
  mockPolling = IDLE_POLLING;
  mockPost.mockReset();
  mockAddToast.mockReset();
  mockPlayErrorSound.mockReset();
  onAiUsageRefused = jest.fn();
  resetSpeechForTests();
  jest.useFakeTimers();
  spoken = [];
  window.speechSynthesis = {
    cancel: jest.fn(),
    speak: (u) => { spoken.push(u.text); },
  };
  window.SpeechSynthesisUtterance = function Utterance(text) { this.text = text; };
});

afterEach(() => {
  jest.useRealTimers();
  delete window.speechSynthesis;
  delete window.SpeechSynthesisUtterance;
});

test('a record press refused for the thread length: the error sound, then the sentence', () => {
  const { result } = renderSession();

  act(() => {
    mockStreamingOptions.onError({ startup: true, threadTooLong: true });
  });

  // The sound first: a browser that refuses speech outside a gesture is
  // never silent.
  expect(mockPlayErrorSound).toHaveBeenCalledTimes(1);
  expect(said()).toEqual([]);
  afterSound();
  expect(said()).toEqual([THREAD_LIMIT_SPOKEN]);
  expect(onAiUsageRefused.mock.calls[0][0]).toBe('too_long');
  expect(result.current.phase).toBe('ready');
  expect(result.current.hasError).toBe(false);
});

test('a recording whose reply was skipped for the length is saved, said and shown', async () => {
  renderSession();

  await finishRecording({
    content: 'my words', llmNodeId: null,
    warning: SKIPPED, warningCode: 'thread_too_long',
  });

  expect(mockAddToast).toHaveBeenCalledWith(SKIPPED, 8000);
  expect(mockPlayErrorSound).toHaveBeenCalledTimes(1);
  afterSound();
  expect(said()).toEqual([THREAD_LIMIT_SPOKEN]);
  expect(onAiUsageRefused.mock.calls[0][0]).toBe('too_long');
});

test('other skipped replies are not spoken (the monthly limit stays a toast)', async () => {
  renderSession();

  await finishRecording({
    content: 'my words', llmNodeId: null,
    warning: "Your recording is saved. Loore didn't reply because you've "
      + 'reached your monthly usage limit.',
  });

  expect(mockAddToast).toHaveBeenCalledTimes(1);
  afterSound();
  expect(said()).toEqual([]);
  expect(mockPlayErrorSound).not.toHaveBeenCalled();
  expect(onAiUsageRefused).not.toHaveBeenCalled();
});

test('a reply the server refused before its call: the sound, then the sentence', async () => {
  mockPolling = {
    data: { node_id: 7, content: '', error_code: 'thread_too_long' },
    status: 'failed', error: 'This thread is too long for another AI reply.',
  };
  const { result } = renderSession();

  await finishRecording({ content: 'my words', llmNodeId: 7 });

  expect(mockPlayErrorSound).toHaveBeenCalledTimes(1);
  afterSound();
  expect(said()).toEqual([THREAD_LIMIT_SPOKEN]);
  expect(mockAddToast).not.toHaveBeenCalled();
  expect(onAiUsageRefused.mock.calls[0][0]).toBe('too_long');
  expect(result.current.phase).toBe('ready');
});

test('the fallback POST refused for the length is said and shown', async () => {
  mockPost.mockRejectedValue({
    response: { status: 409, data: { code: 'thread_too_long', error: 'x', user_node_id: 8 } },
  });
  renderSession();

  await finishRecording({ content: 'my words', llmNodeId: null });

  expect(mockPlayErrorSound).toHaveBeenCalledTimes(1);
  afterSound();
  expect(said()).toEqual([THREAD_LIMIT_SPOKEN]);
  expect(onAiUsageRefused.mock.calls[0][0]).toBe('too_long');
});

test('a new recording stops the sentence, so it is never recorded', () => {
  const { result } = renderSession();
  act(() => {
    mockStreamingOptions.onError({ startup: true, threadTooLong: true });
  });

  // "Start a new thread", then Record, before the sentence was said.
  act(() => { result.current.handleStart(); });
  afterSound();

  expect(said()).toEqual([]);
  expect(window.speechSynthesis.cancel).toHaveBeenCalled();
});

test('the Record tap primes speech once (WebKit speaks later only after a gesture)', () => {
  const { result } = renderSession();
  act(() => { result.current.handleStart(); });
  act(() => { result.current.handleStart(); });
  expect(spoken).toEqual(['']);
});

// Merge train 2026-11-12 (2): a Voice turn is refused while a change the AI
// proposed waits for approval (409 `approval_pending`) and in a thread too
// long for another reply (409 `thread_too_long`, #648). The server asks the
// waiting change first, so where both apply the first press shows the change
// and, once it is decided, the next press meets the length (the error sound,
// then the sentence, then the limit). Since #651 the waiting change is said
// too, in its own sentence, never as the length.
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
jest.mock('./useAsyncTaskPolling', () => ({
  useAsyncTaskPolling: () => ({ data: null, status: 'idle', error: null }),
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
import { APPROVAL_REFUSAL, REFUSAL_SENTENCES } from '../utils/spokenRefusal';

const APPROVAL_SKIPPED = "Your recording is saved. Loore didn't reply because a "
  + 'change to your data waits for your approval. Approve or reject it, then continue.';
const APPROVAL_SPOKEN = REFUSAL_SENTENCES[APPROVAL_REFUSAL];

let spoken;
let onAiUsageRefused;
let onApprovalPending;
const renderSession = () => renderHook(() => useVoiceSession({
  apiEndpoint: '/voice', aiUsage: 'chat', onAiUsageRefused, onApprovalPending,
  onEntrySaved: jest.fn(),
}));
const afterSound = () => act(() => {
  jest.advanceTimersByTime(SENTENCE_AFTER_SOUND_MS);
});
const said = () => spoken.filter(Boolean);
const refusal = (code, extra = {}) => ({
  response: { status: 409, data: { code, error: `refused: ${code}`, ...extra } },
});

beforeEach(() => {
  mockPost.mockReset();
  mockAddToast.mockReset();
  mockPlayErrorSound.mockReset();
  onAiUsageRefused = jest.fn();
  onApprovalPending = jest.fn();
  resetSpeechForTests();
  jest.useFakeTimers();
  spoken = [];
  window.speechSynthesis = {
    cancel: jest.fn(),
    speak: (u) => { spoken.push(u.text); },
  };
  window.SpeechSynthesisUtterance = function Utterance(text) { this.text = text; };
  // A browser that plays no clip outside a tap: its own speech says it.
  window.Audio = function RefusedAudio() {
    this.addEventListener = () => {};
    this.play = () => ({ then: (ok, bad) => bad(new Error('NotAllowedError')) });
    this.pause = () => {};
    this.removeAttribute = () => {};
    this.load = () => {};
  };
});

afterEach(() => {
  jest.useRealTimers();
  delete window.speechSynthesis;
  delete window.SpeechSynthesisUtterance;
  delete window.Audio;
});

test('both: the first press shows the waiting change, the next one meets the length', () => {
  const { result } = renderSession();

  // The server answers approval_pending first: shown, and said (#651), not
  // a failure: no error sound.
  act(() => {
    mockStreamingOptions.onError({ startup: true, approvalPending: true });
  });
  afterSound();
  expect(onApprovalPending).toHaveBeenCalledTimes(1);
  expect(onAiUsageRefused).not.toHaveBeenCalled();
  expect(mockPlayErrorSound).not.toHaveBeenCalled();
  expect(said()).toEqual([APPROVAL_SPOKEN]);
  expect(result.current.phase).toBe('ready');
  expect(result.current.hasError).toBe(false);

  // Decided: the next press is refused for the length.
  act(() => {
    mockStreamingOptions.onError({ startup: true, threadTooLong: true });
  });
  expect(mockPlayErrorSound).toHaveBeenCalledTimes(1);
  afterSound();
  expect(said()).toEqual([APPROVAL_SPOKEN, THREAD_LIMIT_SPOKEN]);
  expect(onAiUsageRefused.mock.calls[0][0]).toBe('too_long');
  expect(onApprovalPending).toHaveBeenCalledTimes(1);
  expect(result.current.phase).toBe('ready');
});

test('the waiting change alone: shown and said, not as the length', () => {
  renderSession();
  act(() => {
    mockStreamingOptions.onError({ startup: true, approvalPending: true });
  });
  afterSound();
  expect(onApprovalPending).toHaveBeenCalledTimes(1);
  expect(said()).toEqual([APPROVAL_SPOKEN]);
  expect(onAiUsageRefused).not.toHaveBeenCalled();
});

test('the length alone: said and shown, no dialog asked for', () => {
  renderSession();
  act(() => {
    mockStreamingOptions.onError({ startup: true, threadTooLong: true });
  });
  afterSound();
  expect(said()).toEqual([THREAD_LIMIT_SPOKEN]);
  expect(onAiUsageRefused.mock.calls[0][0]).toBe('too_long');
  expect(onApprovalPending).not.toHaveBeenCalled();
});

test('the fallback POST: approval_pending shows the change; each is said', async () => {
  renderSession();

  mockPost.mockRejectedValueOnce(refusal('approval_pending', { pending: 1 }));
  await act(async () => {
    await mockStreamingOptions.onComplete({ sessionId: 's1', content: 'my words', llmNodeId: null });
  });
  afterSound();
  expect(onApprovalPending).toHaveBeenCalledTimes(1);
  expect(mockAddToast).toHaveBeenCalledWith('refused: approval_pending', 8000);
  expect(said()).toEqual([APPROVAL_SPOKEN]);
  expect(mockPlayErrorSound).not.toHaveBeenCalled();

  mockPost.mockRejectedValueOnce(refusal('thread_too_long', { user_node_id: 8 }));
  await act(async () => {
    await mockStreamingOptions.onComplete({ sessionId: 's2', content: 'more words', llmNodeId: null });
  });
  afterSound();
  expect(said()).toEqual([APPROVAL_SPOKEN, THREAD_LIMIT_SPOKEN]);
  expect(onAiUsageRefused.mock.calls[0][0]).toBe('too_long');
  expect(onApprovalPending).toHaveBeenCalledTimes(1);
});

test('a recording saved without a reply for a waiting change is said as the change, not the length', async () => {
  renderSession();
  await act(async () => {
    await mockStreamingOptions.onComplete({
      sessionId: 's1', content: 'my words', llmNodeId: null,
      warning: APPROVAL_SKIPPED, warningCode: 'approval_pending',
    });
  });
  afterSound();
  expect(mockAddToast).toHaveBeenCalledWith(APPROVAL_SKIPPED, 8000);
  expect(said()).toEqual([APPROVAL_SPOKEN]);
  expect(onAiUsageRefused).not.toHaveBeenCalled();
});

// #651: every refusal of a Voice turn is said aloud, as the thread's
// length is (#648): the monthly usage limit, AI usage None, a change
// waiting for approval. The refusal's sentence from its clip (Loore's
// voice); a refusal is not a failure, so no error sound (the length keeps
// the one #648 put before it). Never over the turn's reply, and a new
// recording stops it. The on-screen message stays.
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
let mockSSEOptions;
jest.mock('./useSSE', () => {
  const sse = { reset: jest.fn(), disconnect: jest.fn(), reconnect: jest.fn() };
  return {
    useTTSStreamSSE: (id, options) => {
      mockSSEOptions = options;
      return sse;
    },
  };
});
jest.mock('./useMediaSession', () => ({ useMediaSession: () => {} }));
jest.mock('./useOnlineStatus', () => ({ useOnlineStatus: () => true }));
let mockAudio;
jest.mock('../contexts/AudioContext', () => ({ useAudio: () => mockAudio }));
const mockAddToast = jest.fn();
jest.mock('../contexts/ToastContext', () => ({
  useToast: () => ({ addToast: mockAddToast, removeToast: jest.fn() }),
}));
const mockPost = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    post: (...args) => mockPost(...args),
    get: jest.fn(() => Promise.resolve({ data: {} })),
    delete: jest.fn(),
  },
}));
const mockPlayErrorSound = jest.fn();
jest.mock('../utils/errorSound', () => ({
  playErrorSound: (...args) => mockPlayErrorSound(...args),
}));

import { renderHook, act } from '@testing-library/react';
import { useVoiceSession } from './useVoiceSession';
import {
  AI_USAGE_REFUSAL, APPROVAL_REFUSAL, CLIP_START_TIMEOUT_MS, LENGTH_REFUSAL,
  REFUSAL_SENTENCES, SENTENCE_AFTER_SOUND_MS, SPEND_CAP_REFUSAL,
  refusalClipUrl, resetSpeechForTests,
} from '../utils/spokenRefusal';

const IDLE_POLLING = { data: null, status: 'idle', error: null };

// Clips the page asked to play (they play, unless *clipsHang*: still
// loading), and what the browser said.
let clips;
let spoken;
let clipsHang;
class FakeAudio {
  constructor(src) {
    this.requested = src;
    this.src = src;
    this.paused = true;
    clips.push(this);
  }

  addEventListener() {}

  play() {
    if (clipsHang) return { then: () => {} };
    this.paused = false;
    return { then: (ok) => ok() };
  }

  pause() { this.paused = true; }

  removeAttribute(name) { if (name === 'src') this.src = ''; }

  load() {}
}
const played = () => clips.map((c) => c.requested);

let onAiUsageRefused;
let onApprovalPending;
const renderSession = () => renderHook(() => useVoiceSession({
  apiEndpoint: '/voice', aiUsage: 'chat', onAiUsageRefused, onApprovalPending,
  onEntrySaved: jest.fn(),
}));
const afterSound = () => act(() => {
  jest.advanceTimersByTime(SENTENCE_AFTER_SOUND_MS);
});
const finishRecording = (data) => act(async () => {
  await mockStreamingOptions.onComplete({ sessionId: 's1', content: 'my words', ...data });
});

beforeEach(() => {
  mockPolling = IDLE_POLLING;
  mockAudio = {
    stop: jest.fn(), pause: jest.fn(), setGeneratingTTS: jest.fn(),
    loadAudioQueue: jest.fn(() => Promise.resolve()),
    appendChunkToQueue: jest.fn(() => Promise.resolve()),
    renameChapter: jest.fn(), warmup: jest.fn(),
    isPlaying: false, generatingTTS: false, waitingForChunks: false,
    queueFinishedCount: 0,
  };
  mockPost.mockReset();
  mockAddToast.mockReset();
  mockPlayErrorSound.mockReset();
  onAiUsageRefused = jest.fn();
  onApprovalPending = jest.fn();
  resetSpeechForTests();
  jest.useFakeTimers();
  clips = [];
  clipsHang = false;
  spoken = [];
  window.Audio = FakeAudio;
  window.speechSynthesis = { cancel: jest.fn(), speak: (u) => { spoken.push(u.text); } };
  window.SpeechSynthesisUtterance = function Utterance(text) { this.text = text; };
});

afterEach(() => {
  jest.useRealTimers();
  delete window.Audio;
  delete window.speechSynthesis;
  delete window.SpeechSynthesisUtterance;
});

test.each([
  ['the monthly usage limit', { spendCapped: true }, SPEND_CAP_REFUSAL],
  ['AI usage None', { aiUsageRefused: true, aiUsageScope: 'thread' }, AI_USAGE_REFUSAL],
  ['a change waiting for approval', { approvalPending: true }, APPROVAL_REFUSAL],
])('a record press refused for %s: its sentence, no error sound', (name, flags, code) => {
  const { result } = renderSession();

  act(() => { mockStreamingOptions.onError({ startup: true, ...flags }); });
  afterSound();

  expect(played()).toEqual([refusalClipUrl(code)]);
  expect(spoken).toEqual([]);
  expect(mockPlayErrorSound).not.toHaveBeenCalled();
  expect(result.current.phase).toBe('ready');
  expect(result.current.hasError).toBe(false);
});

test('the screens still learn of the refusal', () => {
  renderSession();
  act(() => {
    mockStreamingOptions.onError({ startup: true, aiUsageRefused: true, aiUsageScope: 'thread' });
  });
  expect(onAiUsageRefused).toHaveBeenCalledWith('thread');
  act(() => { mockStreamingOptions.onError({ startup: true, approvalPending: true }); });
  expect(onApprovalPending).toHaveBeenCalledTimes(1);
});

test('a start that failed for another reason has no sentence', () => {
  renderSession();
  act(() => {
    mockStreamingOptions.onError({ startup: true, name: 'NotAllowedError' });
  });
  afterSound();
  expect(played()).toEqual([]);
  expect(spoken).toEqual([]);
});

test.each([
  [SPEND_CAP_REFUSAL], [AI_USAGE_REFUSAL], [APPROVAL_REFUSAL],
])('a recording saved without a reply for %s: toasted and said', async (code) => {
  renderSession();

  await finishRecording({ llmNodeId: null, warning: 'Your recording is saved.', warningCode: code });

  expect(mockAddToast).toHaveBeenCalledWith('Your recording is saved.', 8000);
  afterSound();
  expect(played()).toEqual([refusalClipUrl(code)]);
  expect(onAiUsageRefused).not.toHaveBeenCalled();
});

test('the fallback POST refused for the usage limit: said, with the server\'s text, no failure', async () => {
  mockPost.mockRejectedValue({
    response: {
      status: 402,
      data: { error: 'monthly_spend_limit_reached', message: "You've reached your monthly usage limit." },
    },
  });
  const { result } = renderSession();

  await finishRecording({ llmNodeId: null });

  expect(mockAddToast).toHaveBeenCalledWith("You've reached your monthly usage limit.", 8000);
  expect(mockPlayErrorSound).not.toHaveBeenCalled();
  afterSound();
  expect(played()).toEqual([refusalClipUrl(SPEND_CAP_REFUSAL)]);
  expect(result.current.phase).toBe('ready');
  expect(result.current.hasError).toBe(false);
});

test('the fallback POST refused for AI usage: said, and the page explains', async () => {
  mockPost.mockRejectedValue({
    response: { status: 403, data: { code: 'ai_usage_none', scope: 'account', error: 'x' } },
  });
  renderSession();

  await finishRecording({ llmNodeId: null });
  afterSound();

  expect(played()).toEqual([refusalClipUrl(AI_USAGE_REFUSAL)]);
  expect(onAiUsageRefused).toHaveBeenCalledWith('account');
});

test('a Record press the page refused for the known limit: the reply pauses, the sentence follows', () => {
  mockAudio.isPlaying = true;
  const { result } = renderSession();

  act(() => { result.current.refuseForUsageLimit(); });

  expect(mockAudio.pause).toHaveBeenCalledTimes(1);
  expect(mockPlayErrorSound).not.toHaveBeenCalled();
  // Speech primed inside the press, for a browser that speaks only then.
  expect(spoken).toEqual(['']);
  afterSound();
  expect(played()).toEqual([refusalClipUrl(SPEND_CAP_REFUSAL)]);
});

test('leaving the Voice screen stops the sentence, and its fallback never speaks', () => {
  // Review of #675: a clip still loading when the user leaves.
  clipsHang = true;
  const { unmount } = renderSession();
  act(() => { mockStreamingOptions.onError({ startup: true, spendCapped: true }); });
  afterSound();
  expect(played()).toEqual([refusalClipUrl(SPEND_CAP_REFUSAL)]);

  unmount();

  expect(clips[0].paused).toBe(true);
  expect(clips[0].src).toBe('');
  expect(jest.getTimerCount()).toBe(0);
  act(() => { jest.advanceTimersByTime(CLIP_START_TIMEOUT_MS + 1000); });
  expect(spoken).toEqual([]);
});

test('a clip that does not start in time is said by the browser while the screen is open', () => {
  // The control for the test above.
  clipsHang = true;
  renderSession();
  act(() => { mockStreamingOptions.onError({ startup: true, spendCapped: true }); });
  afterSound();
  act(() => { jest.advanceTimersByTime(CLIP_START_TIMEOUT_MS); });
  expect(spoken).toEqual([REFUSAL_SENTENCES[SPEND_CAP_REFUSAL]]);
});

test('leaving before the sentence starts: nothing plays at all', () => {
  const { unmount } = renderSession();
  act(() => { mockStreamingOptions.onError({ startup: true, approvalPending: true }); });
  unmount();
  afterSound();
  act(() => { jest.advanceTimersByTime(CLIP_START_TIMEOUT_MS + 1000); });
  expect(played()).toEqual([]);
  expect(spoken).toEqual([]);
});

test('a new recording stops the sentence before the microphone opens', () => {
  const { result } = renderSession();
  act(() => { mockStreamingOptions.onError({ startup: true, spendCapped: true }); });
  afterSound();
  expect(clips[0].paused).toBe(false);

  act(() => { result.current.handleStart(); });

  expect(clips[0].paused).toBe(true);
});

// Never over the turn's reply: a reply refused for the thread's length
// while an earlier step of the same turn still plays waits for it to end.
const turnWithAudio = async () => {
  mockPolling = { data: { node_id: 7, status: 'processing' }, status: 'processing', error: null };
  const hook = renderSession();
  await finishRecording({ llmNodeId: 7 });
  await act(async () => {
    await mockSSEOptions.onChunkReady({ audio_url: '/media/a0.mp3', chunk_index: 0, duration: 3 });
  });
  mockAudio.generatingTTS = true;
  mockAudio.isPlaying = true;
  mockPolling = {
    data: { node_id: 7, content: '', error_code: 'thread_too_long' },
    status: 'failed', error: 'This thread is too long for another AI reply.',
  };
  hook.rerender();
  return hook;
};

test('a refusal during the turn\'s own audio waits until that audio has played', async () => {
  const { rerender } = await turnWithAudio();
  afterSound();
  expect(mockPlayErrorSound).not.toHaveBeenCalled();
  expect(played()).toEqual([]);
  expect(onAiUsageRefused.mock.calls[0][0]).toBe('too_long');

  // The queue played to its end.
  mockAudio.queueFinishedCount = 1;
  mockAudio.generatingTTS = false;
  rerender();
  expect(mockPlayErrorSound).toHaveBeenCalledTimes(1);
  afterSound();
  expect(played()).toEqual([refusalClipUrl(LENGTH_REFUSAL)]);
});

test('a new turn drops a sentence still waiting for the audio', async () => {
  const { result, rerender } = await turnWithAudio();
  act(() => { result.current.handleStart(); });
  mockAudio.queueFinishedCount = 1;
  rerender();
  afterSound();
  expect(played()).toEqual([]);
  expect(spoken.filter(Boolean)).toEqual([]);
});

test('the sentences are the server\'s', () => {
  // backend/utils/voice_refusals.py SPOKEN_REFUSALS, and the iPhone app's.
  expect(REFUSAL_SENTENCES).toEqual({
    monthly_spend_limit_reached: "You've reached your monthly usage limit, so Loore can't reply until it resets.",
    ai_usage_none: "AI usage is set to None, so Loore can't reply here.",
    approval_pending: 'A change to your data is waiting for your approval. Approve or reject it on the screen to continue.',
    thread_too_long: 'This thread is too long for another reply. Start a new thread to keep talking.',
    subscription_required_ended: "Your subscription has ended, so Loore doesn't reply. Your recording is saved.",
    subscription_required_past_due: "Your last payment didn't go through, so Loore doesn't reply. Your recording is saved.",
    subscription_required_free_month: "Your free month has ended, so Loore doesn't reply. Your recording is saved.",
    subscription_required_none: "Loore doesn't reply on an account without a subscription. Your recording is saved.",
  });
});

// ── Payments (#438): the access ends while the turn is recorded ─────────
// The recording is kept and transcribed, only the reply is skipped, and
// the reason is said aloud in the words of the screen, never as a failure.

const ENDED = 'subscription_required_ended';
const ENDED_TEXT = "Your subscription has ended, so Loore doesn't reply. Your recording is saved.";

test('a recording kept without its reply after the access ended: toasted and said', async () => {
  const onEntrySaved = jest.fn();
  const { result } = renderHook(() => useVoiceSession({
    apiEndpoint: '/voice', aiUsage: 'chat', onAiUsageRefused, onApprovalPending, onEntrySaved,
  }));

  await finishRecording({ llmNodeId: null, warning: ENDED_TEXT, warningCode: ENDED });

  expect(mockAddToast).toHaveBeenCalledWith(ENDED_TEXT, 8000);
  expect(onEntrySaved).toHaveBeenCalledTimes(1);
  afterSound();
  expect(played()).toEqual([refusalClipUrl(ENDED)]);
  expect(mockPlayErrorSound).not.toHaveBeenCalled();
  expect(result.current.phase).toBe('ready');
  expect(result.current.hasError).toBe(false);
});

test('the fallback POST answered with a kept turn: the entry is known, the sentence said, no failure', async () => {
  mockPost.mockRejectedValue({
    response: {
      status: 402,
      data: { error: 'subscription_required', message: ENDED_TEXT, code: ENDED, user_node_id: 8 },
    },
  });
  const onEntrySaved = jest.fn();
  const { result } = renderHook(() => useVoiceSession({
    apiEndpoint: '/voice', aiUsage: 'chat', onAiUsageRefused, onApprovalPending, onEntrySaved,
  }));

  await finishRecording({ llmNodeId: null });

  expect(onEntrySaved).toHaveBeenCalledTimes(1);
  afterSound();
  expect(played()).toEqual([refusalClipUrl(ENDED)]);
  expect(mockPlayErrorSound).not.toHaveBeenCalled();
  expect(result.current.phase).toBe('ready');
  expect(result.current.hasError).toBe(false);
});

test('the fallback POST refused at the start (no code): no sentence, as before', async () => {
  mockPost.mockRejectedValue({
    response: { status: 402, data: { error: 'subscription_required', message: 'x' } },
  });
  const onEntrySaved = jest.fn();
  const { result } = renderHook(() => useVoiceSession({
    apiEndpoint: '/voice', aiUsage: 'chat', onAiUsageRefused, onApprovalPending, onEntrySaved,
  }));

  await finishRecording({ llmNodeId: null });
  afterSound();

  expect(played()).toEqual([]);
  expect(onEntrySaved).not.toHaveBeenCalled();
  expect(mockPlayErrorSound).not.toHaveBeenCalled();
  expect(result.current.phase).toBe('ready');
});

test('a queued reply refused because the access ended: said instead of the error sound', async () => {
  mockPolling = {
    data: { node_id: 7, content: '', error_code: ENDED },
    status: 'failed', error: ENDED_TEXT,
  };
  const { result } = renderSession();

  await finishRecording({ llmNodeId: 7 });

  expect(mockAddToast).toHaveBeenCalledWith(ENDED_TEXT, 8000);
  afterSound();
  expect(played()).toEqual([refusalClipUrl(ENDED)]);
  expect(mockPlayErrorSound).not.toHaveBeenCalled();
  expect(result.current.phase).toBe('ready');
  expect(result.current.hasError).toBe(false);
});

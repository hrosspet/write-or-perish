// A voice turn that fails plays the error sound, so a user who isn't
// looking at the screen knows the reply stopped (Peter, 2026-10-13): the
// reply's audio fails (its TTS marked 'failed', or POST /tts refused), its
// text fails, or it can't start. With part of the reply queued, the sound
// waits until that audio has played.
const mockAudio = {
  stop: jest.fn(),
  warmup: jest.fn(),
  play: jest.fn(),
  loadAudioQueue: jest.fn(() => Promise.resolve()),
  appendChunkToQueue: jest.fn(() => Promise.resolve()),
  setGeneratingTTS: jest.fn(),
  renameChapter: jest.fn(),
  generatingTTS: false,
  waitingForChunks: false,
  queueFinishedCount: 0,
};
jest.mock('../contexts/AudioContext', () => ({ useAudio: () => mockAudio }));

const mockStreaming = {
  startStreaming: jest.fn(),
  stopStreaming: jest.fn(() => Promise.resolve()),
  cancelStreaming: jest.fn(),
  resumeStreaming: jest.fn(),
  pauseRecording: jest.fn(),
  resumeRecording: jest.fn(),
  isPaused: false,
  isInterrupted: false,
  duration: 0,
};
// What the session hands useStreamingTranscription (its onComplete runs
// when the recording's transcription is final).
let mockStreamingOptions;
jest.mock('./useStreamingTranscription', () => ({
  useStreamingTranscription: (options) => {
    mockStreamingOptions = options;
    return mockStreaming;
  },
}));
jest.mock('./useLongRecordingWarning', () => ({
  useLongRecordingWarning: () => ({ armAlertContext: () => {} }),
}));
jest.mock('./useMediaSession', () => ({ useMediaSession: () => {} }));

jest.mock('../contexts/ToastContext', () => ({
  useToast: () => ({ addToast: jest.fn(), removeToast: () => {} }),
}));
jest.mock('../utils/voiceTiming', () => ({
  startTurn: () => {},
  endTurn: () => {},
  setTurnNode: () => {},
  mark: () => {},
  markPlaying: () => {},
}));

const mockPlayErrorSound = jest.fn();
jest.mock('../utils/errorSound', () => ({
  playErrorSound: () => mockPlayErrorSound(),
}));

const mockGet = jest.fn();
const mockPost = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    get: (...args) => mockGet(...args),
    post: (...args) => mockPost(...args),
  },
}));

import { renderHook, act } from '@testing-library/react';
import { useVoiceSession } from './useVoiceSession';

class MockEventSource {
  static instances = [];

  constructor(url) {
    this.url = url;
    this.readyState = 1;
    this.listeners = {};
    this.closed = false;
    MockEventSource.instances.push(this);
  }

  addEventListener(type, fn) {
    (this.listeners[type] = this.listeners[type] || []).push(fn);
  }

  close() {
    this.closed = true;
    this.readyState = 2;
  }

  emit(type, data) {
    (this.listeners[type] || []).forEach((fn) => fn({ data: JSON.stringify(data) }));
  }
}
MockEventSource.CONNECTING = 0;
MockEventSource.OPEN = 1;
MockEventSource.CLOSED = 2;

beforeAll(() => {
  global.EventSource = MockEventSource;
  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    get: () => 'visible',
  });
});

const flush = async () => {
  await act(async () => {
    for (let i = 0; i < 10; i += 1) await Promise.resolve();
  });
};

// The reconcile loop's REST check (TTS_RECOVERY_POLL_MS).
const nextRestCheck = async () => {
  act(() => { jest.advanceTimersByTime(7000); });
  await flush();
};

// What the server says about reply node 42.
let server;
beforeEach(() => {
  jest.useFakeTimers();
  MockEventSource.instances = [];
  jest.clearAllMocks();
  mockAudio.generatingTTS = false;
  mockAudio.waitingForChunks = false;
  mockAudio.queueFinishedCount = 0;
  // CRA's jest resets mock implementations before each test.
  mockAudio.setGeneratingTTS.mockImplementation((value) => { mockAudio.generatingTTS = value; });
  window.history.replaceState({}, '', '/voice');
  server = {
    llm: { status: 'completed', content: 'The reply.' },
    tts: { status: 'processing' },
    ttsPost: () => Promise.resolve({ status: 202, data: { status: 'pending' } }),
    voicePost: () => Promise.resolve({ data: { llm_node_id: 42, user_node_id: 41 } }),
    llm43: { status: 'processing' },
    // Reply 42's continuation (#507).
    llm50: { status: 'completed', content: 'The answer.' },
    tts50: { status: 'processing' },
    ttsPost50: () => Promise.resolve({ status: 202, data: { status: 'pending' } }),
  };
  mockGet.mockImplementation((url) => {
    if (url === '/nodes/42/llm-status') {
      return Promise.resolve({ data: { node_id: 42, ...server.llm } });
    }
    if (url === '/nodes/42/tts-status') {
      return Promise.resolve({ data: { node_id: 42, ...server.tts } });
    }
    // The next turn's reply.
    if (url === '/nodes/43/llm-status') {
      return Promise.resolve({ data: { node_id: 43, ...server.llm43 } });
    }
    if (url === '/nodes/50/llm-status') {
      return Promise.resolve({ data: { node_id: 50, ...server.llm50 } });
    }
    if (url === '/nodes/50/tts-status') {
      return Promise.resolve({ data: { node_id: 50, ...server.tts50 } });
    }
    return Promise.reject(new Error(`unexpected GET ${url}`));
  });
  mockPost.mockImplementation((url) => {
    if (url === '/nodes/42/tts') return server.ttsPost();
    if (url === '/nodes/43/tts') return Promise.resolve({ status: 202, data: { status: 'pending' } });
    if (url === '/nodes/50/tts') return server.ttsPost50();
    if (url === '/voice') return server.voicePost();
    return Promise.reject(new Error(`unexpected POST ${url}`));
  });
  jest.spyOn(console, 'log').mockImplementation(() => {});
  jest.spyOn(console, 'warn').mockImplementation(() => {});
  jest.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
  jest.useRealTimers();
  console.log.mockRestore();
  console.warn.mockRestore();
  console.error.mockRestore();
});

const ttsStreams = () => MockEventSource.instances.filter(
  (es) => es.url === '/api/sse/nodes/42/tts-stream');
const restChecks = () => mockGet.mock.calls.filter(
  ([url]) => url === '/nodes/42/tts-status').length;

const renderWaiting = () => renderHook(() => useVoiceSession({
  apiEndpoint: '/voice',
  ttsTitle: 'Voice',
  initialLlmNodeId: 42,
  initialParentId: 41,
}));

// Reply 42 is spoken while it is written: its first chunk is queued and
// playing.
const renderPlaying = async () => {
  server.llm = { status: 'processing', tts_streaming: true };
  const hook = renderWaiting();
  await flush();
  act(() => ttsStreams()[0].emit('chunk_ready', {
    chunk_index: 0, audio_url: '/media/42-0.mp3', duration: 4,
  }));
  await flush();
  expect(hook.result.current.phase).toBe('playback');
  return hook;
};

// ... and then its TTS fails.
const renderSpeaking = async () => {
  const hook = await renderPlaying();
  server.tts = { status: 'failed' };
  await nextRestCheck();
  return hook;
};

// The queue played what it had and now waits for chunks that won't come.
const queueRunsOut = (rerender) => {
  mockAudio.waitingForChunks = true;
  rerender();
};

// The queue played to its end, nothing more coming (generatingTTS off).
const queueEnds = (rerender) => {
  mockAudio.queueFinishedCount += 1;
  rerender();
};

// The next llm-status poll.
const nextPoll = async () => {
  act(() => { jest.advanceTimersByTime(1500); });
  await flush();
};

// A fresh Voice page whose recording was just transcribed; the server
// made no reply node, so the page asks for the reply itself (POST /voice).
const finishRecordingWithoutReplyNode = async (options = {}) => {
  const hook = renderHook(() => useVoiceSession({
    apiEndpoint: '/voice', ttsTitle: 'Voice', aiUsage: 'chat', ...options,
  }));
  await act(async () => {
    await mockStreamingOptions.onComplete({ sessionId: 's1', content: 'my words', llmNodeId: null });
  });
  await flush();
  return hook;
};

const httpError = (status, data) => {
  const err = new Error(`Request failed with status code ${status}`);
  err.response = { status, data };
  return err;
};

test('a reply whose audio failed before any of it played sounds at once', async () => {
  const { result } = renderWaiting();
  await flush();
  expect(mockPost).toHaveBeenCalledWith('/nodes/42/tts');
  expect(ttsStreams()).toHaveLength(1);

  server.tts = { status: 'failed' };
  await nextRestCheck();

  expect(mockPlayErrorSound).toHaveBeenCalledTimes(1);
  expect(result.current.phase).toBe('ready');
  expect(result.current.hasError).toBe(true);
});

test('a refused request for the reply\'s audio sounds at once', async () => {
  server.ttsPost = () => {
    const err = new Error('Request failed with status code 500');
    err.response = { status: 500, data: { error: 'TTS failed' } };
    return Promise.reject(err);
  };
  const { result } = renderWaiting();
  await flush();

  expect(mockPlayErrorSound).toHaveBeenCalledTimes(1);
  expect(result.current.phase).toBe('ready');
  expect(result.current.hasError).toBe(true);
});

test('a request lost without an answer makes no sound (it is sent again)', async () => {
  server.ttsPost = () => Promise.reject(new Error('Network Error'));
  const { result } = renderWaiting();
  await flush();

  expect(mockPlayErrorSound).not.toHaveBeenCalled();
  expect(result.current.phase).toBe('processing');
});

test('a reply whose audio failed after some played sounds once that audio has played', async () => {
  const { result, rerender } = await renderSpeaking();
  expect(mockPlayErrorSound).not.toHaveBeenCalled();
  expect(result.current.hasError).toBe(true);

  queueRunsOut(rerender);
  expect(mockPlayErrorSound).toHaveBeenCalledTimes(1);
});

test('a failure seen by two checks at once sounds once', async () => {
  server.tts = { status: 'failed' };
  renderWaiting();
  await flush();

  // One return to the page fires more than one event; each starts a
  // REST check, and both see the failure.
  act(() => {
    document.dispatchEvent(new Event('visibilitychange'));
    window.dispatchEvent(new Event('online'));
  });
  await flush();

  expect(restChecks()).toBe(2);
  expect(mockPlayErrorSound).toHaveBeenCalledTimes(1);
});

test('cancelling before the queued audio has played drops the sound', async () => {
  const { result, rerender } = await renderSpeaking();

  act(() => result.current.handleCancelProcessing());
  queueRunsOut(rerender);

  expect(mockPlayErrorSound).not.toHaveBeenCalled();
});

test('recording again before the queued audio has played drops the sound', async () => {
  const { result, rerender } = await renderSpeaking();

  act(() => result.current.handleStart());
  queueRunsOut(rerender);

  expect(mockPlayErrorSound).not.toHaveBeenCalled();
});

// Every voice turn that fails sounds (coordinator, 2026-10-13, under
// Peter's Q1 "yes"), except a blocked AI usage and a cancelled reply.

test('a reply that cannot start with a message sounds at once', async () => {
  server.voicePost = () => Promise.reject(httpError(400, { error: 'The reply template is broken.' }));
  const { result } = await finishRecordingWithoutReplyNode();

  expect(mockPost).toHaveBeenCalledWith('/voice', expect.anything());
  expect(mockPlayErrorSound).toHaveBeenCalledTimes(1);
  expect(result.current.phase).toBe('ready');
});

test('a reply that cannot start sounds at once', async () => {
  server.voicePost = () => Promise.reject(httpError(500, {}));
  const { result } = await finishRecordingWithoutReplyNode();

  expect(mockPlayErrorSound).toHaveBeenCalledTimes(1);
  expect(result.current.phase).toBe('ready');
  expect(result.current.hasError).toBe(true);
});

test('a reply refused for AI usage None stays silent (the page explains)', async () => {
  server.voicePost = () => Promise.reject(httpError(403, { code: 'ai_usage_none', scope: 'thread' }));
  const onAiUsageRefused = jest.fn();
  await finishRecordingWithoutReplyNode({ onAiUsageRefused });

  expect(onAiUsageRefused).toHaveBeenCalledWith('thread');
  expect(mockPlayErrorSound).not.toHaveBeenCalled();
});

test('a reply whose text failed before any audio sounds at once', async () => {
  server.llm = { status: 'failed', error: 'The provider failed.' };
  const { result } = renderWaiting();
  await flush();

  expect(mockPlayErrorSound).toHaveBeenCalledTimes(1);
  expect(result.current.phase).toBe('ready');
  expect(result.current.hasError).toBe(true);
});

test('a reply whose text failed after some audio sounds once that audio has played', async () => {
  const { rerender } = await renderPlaying();
  server.llm = { status: 'failed', error: 'The provider failed.' };
  await nextPoll();
  expect(mockAudio.setGeneratingTTS).toHaveBeenLastCalledWith(false);
  expect(mockPlayErrorSound).not.toHaveBeenCalled();

  queueEnds(rerender);
  expect(mockPlayErrorSound).toHaveBeenCalledTimes(1);
});

test('a reply whose audio and then text fail sounds once', async () => {
  const { rerender } = await renderSpeaking();
  server.llm = { status: 'failed', error: 'The provider failed.' };
  await nextPoll();

  queueEnds(rerender);
  rerender();
  expect(mockPlayErrorSound).toHaveBeenCalledTimes(1);
});

// The failed-text branch ran on every render while the status stayed
// 'failed' (review of #504; the first case is also on main before it).

test('recording again after a failed reply text stays recording', async () => {
  server.llm = { status: 'failed', error: 'The provider failed.' };
  const { result, rerender } = renderWaiting();
  await flush();
  expect(result.current.phase).toBe('ready');

  act(() => result.current.handleStart());
  rerender();
  await flush();

  expect(result.current.phase).toBe('recording');
});

test('the next reply after a failed one sounds only for its own failure', async () => {
  server.llm = { status: 'failed', error: 'The provider failed.' };
  const { result } = renderWaiting();
  await flush();
  expect(mockPlayErrorSound).toHaveBeenCalledTimes(1);

  // Record again; the server makes reply 43, still being written.
  act(() => result.current.handleStart());
  await act(async () => {
    await mockStreamingOptions.onComplete({ sessionId: 's2', content: 'again', llmNodeId: 43 });
  });
  await flush();
  expect(result.current.phase).toBe('processing');
  expect(mockPlayErrorSound).toHaveBeenCalledTimes(1);

  server.llm43 = { status: 'failed', error: 'It failed again.' };
  await nextPoll();
  expect(mockPlayErrorSound).toHaveBeenCalledTimes(2);
  expect(result.current.phase).toBe('ready');
});

test('a cancelled reply stays silent', async () => {
  server.llm = { status: 'cancelled' };
  renderWaiting();
  await flush();
  await nextPoll();

  expect(mockPlayErrorSound).not.toHaveBeenCalled();
});

test('a refusal that arrives after leaving the page makes no sound', async () => {
  let refuse;
  server.ttsPost = () => new Promise((resolve, reject) => { refuse = reject; });
  const { unmount } = renderWaiting();
  await flush();
  expect(mockPost).toHaveBeenCalledWith('/nodes/42/tts');

  unmount();
  const err = new Error('Request failed with status code 500');
  err.response = { status: 500, data: {} };
  await act(async () => { refuse(err); });
  await flush();

  expect(mockPlayErrorSound).not.toHaveBeenCalled();
});

// A reply in two parts (#507): reply 42 is an interim part ("let me look
// that up") whose audio plays while the answer, node 50, is written. When
// the answer fails, the turn ends there: the first part plays to its end,
// the error sound follows once, and the page neither goes back to
// "Thinking..." nor asks for node 50's audio again.

const ttsStreamsOf = (id) => MockEventSource.instances.filter(
  (es) => es.url === `/api/sse/nodes/${id}/tts-stream`);
const ttsRequestsFor = (id) => mockPost.mock.calls.filter(
  ([url]) => url === `/nodes/${id}/tts`).length;

// Reply 42's audio is queued and playing; its stream then says the answer
// continues in node 50.
const renderContinuing = async () => {
  server.llm = {
    status: 'completed', content: 'Let me look that up.', continuation_node_id: 50,
  };
  const hook = renderWaiting();
  await flush();
  act(() => ttsStreams()[0].emit('chunk_ready', {
    chunk_index: 0, audio_url: '/media/42-0.mp3', duration: 4,
  }));
  await flush();
  act(() => ttsStreams()[0].emit('all_complete', {
    tts_url: '/media/42.mp3', continuation_node_id: 50,
  }));
  await flush();
  return hook;
};

// The queue plays what it has: then it waits for more while the turn's
// audio is still being made, and ends otherwise (AudioContext's onended).
const queuedAudioPlays = (rerender) => {
  if (mockAudio.generatingTTS) mockAudio.waitingForChunks = true;
  else mockAudio.queueFinishedCount += 1;
  rerender();
};

// Past the point where the recovery watchdog (20 s after the last
// request, checked every 7 s) would ask for the audio again.
const waitPastTheWatchdog = async () => {
  for (let i = 0; i < 5; i += 1) await nextRestCheck();
};

test("a continuation whose audio failed ends the turn after the first part's audio", async () => {
  const { result, rerender } = await renderContinuing();
  expect(ttsRequestsFor(50)).toBe(1);
  expect(result.current.phase).toBe('playback');

  server.tts50 = { status: 'failed' };
  await nextRestCheck();
  expect(result.current.phase).toBe('ready');
  expect(result.current.hasError).toBe(true);
  expect(mockPlayErrorSound).not.toHaveBeenCalled();

  queuedAudioPlays(rerender);
  await flush();
  expect(mockAudio.generatingTTS).toBe(false);
  expect(mockPlayErrorSound).toHaveBeenCalledTimes(1);
  expect(result.current.phase).toBe('ready');

  await waitPastTheWatchdog();
  expect(ttsRequestsFor(50)).toBe(1);
  expect(result.current.phase).toBe('ready');
  expect(mockPlayErrorSound).toHaveBeenCalledTimes(1);
});

test("a continuation whose audio request is refused ends the turn after the first part's audio", async () => {
  server.ttsPost50 = () => Promise.reject(httpError(500, { error: 'TTS failed' }));
  const { result, rerender } = await renderContinuing();
  expect(ttsRequestsFor(50)).toBe(1);
  expect(result.current.phase).toBe('ready');
  expect(mockPlayErrorSound).not.toHaveBeenCalled();

  queuedAudioPlays(rerender);
  await flush();
  expect(mockPlayErrorSound).toHaveBeenCalledTimes(1);
  expect(result.current.phase).toBe('ready');

  await waitPastTheWatchdog();
  expect(ttsRequestsFor(50)).toBe(1);
  expect(result.current.phase).toBe('ready');
});

test('a continuation whose audio failed during "Thinking..." sounds at once and ends the turn', async () => {
  const { result, rerender } = await renderContinuing();
  // The first part has played; the answer's audio isn't there yet.
  queuedAudioPlays(rerender);
  expect(result.current.phase).toBe('processing');

  server.tts50 = { status: 'failed' };
  await nextRestCheck();
  expect(mockPlayErrorSound).toHaveBeenCalledTimes(1);
  expect(result.current.phase).toBe('ready');

  await waitPastTheWatchdog();
  expect(ttsRequestsFor(50)).toBe(1);
  expect(result.current.phase).toBe('ready');
  expect(mockPlayErrorSound).toHaveBeenCalledTimes(1);
});

// The next recording's reply starts its own queue: its audio is not
// appended to the failed turn's.
const recordAgainAndHearTheReply = async (result) => {
  act(() => result.current.handleStart());
  server.llm43 = { status: 'completed', content: 'The next reply.' };
  await act(async () => {
    await mockStreamingOptions.onComplete({ sessionId: 's2', content: 'again', llmNodeId: 43 });
  });
  await flush();
  expect(ttsRequestsFor(43)).toBe(1);
  act(() => ttsStreamsOf(43)[0].emit('chunk_ready', {
    chunk_index: 0, audio_url: '/media/43-0.mp3', duration: 3,
  }));
  await flush();
};

test('recording again after a continuation whose audio failed starts a new queue', async () => {
  const { result, rerender } = await renderContinuing();
  server.tts50 = { status: 'failed' };
  await nextRestCheck();
  queuedAudioPlays(rerender);
  await flush();

  await recordAgainAndHearTheReply(result);

  expect(mockAudio.loadAudioQueue).toHaveBeenLastCalledWith(
    ['/media/43-0.mp3'], expect.anything(), [3], expect.anything());
  expect(mockAudio.appendChunkToQueue).not.toHaveBeenCalled();
  expect(result.current.phase).toBe('playback');
});

test('recording again after a continuation whose text failed starts a new queue', async () => {
  server.llm50 = { status: 'processing' };
  const { result, rerender } = await renderContinuing();
  server.llm50 = { status: 'failed', error: 'The provider failed.' };
  await nextPoll();
  expect(result.current.phase).toBe('ready');
  queuedAudioPlays(rerender);
  await flush();
  expect(mockPlayErrorSound).toHaveBeenCalledTimes(1);

  await recordAgainAndHearTheReply(result);

  expect(mockAudio.loadAudioQueue).toHaveBeenLastCalledWith(
    ['/media/43-0.mp3'], expect.anything(), [3], expect.anything());
  expect(mockAudio.appendChunkToQueue).not.toHaveBeenCalled();
});

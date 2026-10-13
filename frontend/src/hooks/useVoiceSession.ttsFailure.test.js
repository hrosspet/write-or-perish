// A voice reply whose audio fails (its TTS marked 'failed', or POST /tts
// refused) plays the error sound, so a user who isn't looking at the
// screen knows the reply stopped (Peter, 2026-10-13). With part of the
// reply queued, the sound waits until that audio has played.
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
jest.mock('./useStreamingTranscription', () => ({
  useStreamingTranscription: () => mockStreaming,
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
  // CRA's jest resets mock implementations before each test.
  mockAudio.setGeneratingTTS.mockImplementation((value) => { mockAudio.generatingTTS = value; });
  window.history.replaceState({}, '', '/voice');
  server = {
    llm: { status: 'completed', content: 'The reply.' },
    tts: { status: 'processing' },
    ttsPost: () => Promise.resolve({ status: 202, data: { status: 'pending' } }),
  };
  mockGet.mockImplementation((url) => {
    if (url === '/nodes/42/llm-status') {
      return Promise.resolve({ data: { node_id: 42, ...server.llm } });
    }
    if (url === '/nodes/42/tts-status') {
      return Promise.resolve({ data: { node_id: 42, ...server.tts } });
    }
    return Promise.reject(new Error(`unexpected GET ${url}`));
  });
  mockPost.mockImplementation((url) => {
    if (url === '/nodes/42/tts') return server.ttsPost();
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
// playing when its TTS fails.
const renderSpeaking = async () => {
  server.llm = { status: 'processing', tts_streaming: true };
  const hook = renderWaiting();
  await flush();
  act(() => ttsStreams()[0].emit('chunk_ready', {
    chunk_index: 0, audio_url: '/media/42-0.mp3', duration: 4,
  }));
  await flush();
  expect(hook.result.current.phase).toBe('playback');
  server.tts = { status: 'failed' };
  await nextRestCheck();
  return hook;
};

// The queue played what it had and now waits for chunks that won't come.
const queueRunsOut = (rerender) => {
  mockAudio.waitingForChunks = true;
  rerender();
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

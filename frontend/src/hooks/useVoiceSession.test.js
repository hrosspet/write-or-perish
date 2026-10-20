// #374: the voice page waits on "Thinking..." for a reply. When the user
// comes back to it (screen on again, app switched back) after the reply
// was written in the background, the page re-checks the server and shows
// what is there: the reply's audio controls, the stream picked up again,
// or the failure.
const mockAudio = {
  stop: jest.fn(),
  warmup: jest.fn(),
  play: jest.fn(),
  loadAudioQueue: jest.fn(() => Promise.resolve()),
  appendChunkToQueue: jest.fn(() => Promise.resolve()),
  setGeneratingTTS: jest.fn(),
  renameChapter: jest.fn(),
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

const mockAddToast = jest.fn();
jest.mock('../contexts/ToastContext', () => ({
  useToast: () => ({ addToast: mockAddToast, removeToast: () => {} }),
}));
jest.mock('../utils/voiceTiming', () => ({
  startTurn: () => {},
  endTurn: () => {},
  setTurnNode: () => {},
  mark: () => {},
  markPlaying: () => {},
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

let visibility = 'visible';
beforeAll(() => {
  global.EventSource = MockEventSource;
  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    get: () => visibility,
  });
});

const setVisibility = (state) => {
  act(() => {
    visibility = state;
    document.dispatchEvent(new Event('visibilitychange'));
  });
};

const flush = async () => {
  await act(async () => {
    for (let i = 0; i < 10; i += 1) await Promise.resolve();
  });
};

// What the server says about reply node 42.
let server;
beforeEach(() => {
  jest.useFakeTimers();
  visibility = 'visible';
  MockEventSource.instances = [];
  jest.clearAllMocks();
  window.history.replaceState({}, '', '/voice');
  server = {
    llm: { status: 'processing' },
    tts: { status: 'pending' },
    ttsPost: { status: 202, data: { status: 'pending' } },
    // The next turn's reply.
    llm43: { status: 'processing' },
  };
  mockGet.mockImplementation((url) => {
    if (url === '/nodes/42/llm-status') {
      return Promise.resolve({ data: { node_id: 42, ...server.llm } });
    }
    if (url === '/nodes/43/llm-status') {
      return Promise.resolve({ data: { node_id: 43, ...server.llm43 } });
    }
    if (url === '/nodes/42/tts-status') {
      return Promise.resolve({ data: { node_id: 42, ...server.tts } });
    }
    return Promise.reject(new Error(`unexpected GET ${url}`));
  });
  mockPost.mockImplementation((url) => {
    if (url === '/nodes/42/tts') return Promise.resolve(server.ttsPost);
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

const renderWaiting = () => renderHook(() => useVoiceSession({
  apiEndpoint: '/voice',
  ttsTitle: 'Voice',
  initialLlmNodeId: 42,
  initialParentId: 41,
}));

test('a reply spoken while written: its dead stream is replaced on return and the audio shows', async () => {
  // The reply is being written and spoken (#367): the page attaches to
  // its TTS stream before the user locks the phone.
  server.llm = { status: 'processing', tts_streaming: true };
  server.tts = { status: 'processing' };
  const { result } = renderWaiting();
  await flush();
  expect(result.current.phase).toBe('processing');
  expect(ttsStreams()).toHaveLength(1);

  // Screen off. iOS kills the stream without an error event; the reply
  // is written meanwhile, its speech is still being made.
  setVisibility('hidden');
  server.llm = { status: 'completed', content: 'A long reply.', tts_streaming: true };

  setVisibility('visible');
  await flush();
  expect(ttsStreams()).toHaveLength(2);
  expect(ttsStreams()[0].closed).toBe(true);

  // The fresh stream sends the chunks made so far.
  act(() => ttsStreams()[1].emit('chunk_ready', {
    chunk_index: 0, audio_url: '/media/42-0.mp3', duration: 4,
  }));
  await flush();
  expect(mockAudio.loadAudioQueue).toHaveBeenCalledWith(
    ['/media/42-0.mp3'], expect.anything(), [4], expect.anything());
  expect(result.current.phase).toBe('playback');
});

test('a reply that finished after polling ran out of time is shown on return', async () => {
  const { result } = renderWaiting();
  await flush();
  setVisibility('hidden');

  // Away for longer than the poller's 30 minutes: it gives up.
  act(() => { jest.advanceTimersByTime(31 * 60 * 1000); });
  await flush();
  expect(result.current.phase).toBe('processing');

  server.llm = { status: 'completed', content: 'The reply.' };
  server.ttsPost = { status: 200, data: { tts_url: '/media/42.mp3' } };
  setVisibility('visible');
  await flush();
  expect(mockPost).toHaveBeenCalledWith('/nodes/42/tts');
  expect(mockAudio.loadAudioQueue).toHaveBeenCalledWith(
    ['/media/42.mp3'], expect.objectContaining({ url: '/media/42.mp3' }));
  expect(result.current.phase).toBe('playback');
});

test('a reply that failed while the page was away shows the failure on return', async () => {
  const { result } = renderWaiting();
  await flush();
  setVisibility('hidden');

  server.llm = { status: 'failed', error: 'Break the request into smaller steps.' };
  setVisibility('visible');
  await flush();
  expect(mockAddToast).toHaveBeenCalledWith('Break the request into smaller steps.', 8000);
  expect(result.current.phase).toBe('ready');
  expect(result.current.hasError).toBe(true);
});

// The failed-reply branch ran on every render while the status stayed
// 'failed' (fixed on main by #504). It also tells the page that no reply
// is coming (#435: Glean stops waiting), so it must run once, for the node
// the status is about.

const nextPoll = async () => {
  act(() => { jest.advanceTimersByTime(1500); });
  await flush();
};

const renderWaitingFor = (onLLMFailed) => renderHook(() => useVoiceSession({
  apiEndpoint: '/voice',
  ttsTitle: 'Voice',
  initialLlmNodeId: 42,
  initialParentId: 41,
  onLLMFailed,
}));

test('recording again after a failed reply stays recording', async () => {
  server.llm = { status: 'failed', error: 'The provider failed.' };
  const { result, rerender } = renderWaitingFor(jest.fn());
  await flush();
  expect(result.current.phase).toBe('ready');

  act(() => result.current.handleStart());
  rerender();
  await flush();

  expect(result.current.phase).toBe('recording');
});

test('the next reply after a failed one is reported failed only for its own failure', async () => {
  server.llm = { status: 'failed', error: 'The provider failed.' };
  const onLLMFailed = jest.fn();
  const { result } = renderWaitingFor(onLLMFailed);
  await flush();
  expect(onLLMFailed).toHaveBeenCalledTimes(1);
  expect(onLLMFailed).toHaveBeenLastCalledWith(42);

  // Record again; the server makes reply 43, still being written.
  act(() => result.current.handleStart());
  await act(async () => {
    await mockStreamingOptions.onComplete({ sessionId: 's2', content: 'again', llmNodeId: 43 });
  });
  await flush();
  expect(result.current.phase).toBe('processing');
  expect(onLLMFailed).toHaveBeenCalledTimes(1);

  server.llm43 = { status: 'failed', error: 'It failed again.' };
  await nextPoll();
  expect(onLLMFailed).toHaveBeenCalledTimes(2);
  expect(onLLMFailed).toHaveBeenLastCalledWith(43);
  expect(result.current.phase).toBe('ready');
});

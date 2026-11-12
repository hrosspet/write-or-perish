// The approval announcement in Voice mode (Peter, 2026-11-12): "the
// announcement should be TTSed as a separate chunk and appended to the
// queue. So that when it's not pending, it's not voiced out anymore". When
// the turn's final node is written, the server is asked whether that
// reply's changes wait; its clip joins the same queue after the reply's
// last chunk, and only then may the queue end. A slow or failed answer
// never leaves the queue waiting for long.
const mockAudio = {
  stop: jest.fn(),
  warmup: jest.fn(),
  play: jest.fn(),
  loadAudioQueue: jest.fn(() => Promise.resolve()),
  appendChunkToQueue: jest.fn(() => Promise.resolve()),
  appendAnnouncement: jest.fn(() => Promise.resolve()),
  setGeneratingTTS: jest.fn(),
  renameChapter: jest.fn(),
  waitingForChunks: false,
};
jest.mock('../contexts/AudioContext', () => ({ useAudio: () => mockAudio }));
jest.mock('./useStreamingTranscription', () => ({
  useStreamingTranscription: () => ({
    startStreaming: jest.fn(), stopStreaming: jest.fn(() => Promise.resolve()),
    cancelStreaming: jest.fn(), resumeStreaming: jest.fn(),
    pauseRecording: jest.fn(), resumeRecording: jest.fn(),
    isPaused: false, isInterrupted: false, duration: 0,
  }),
}));
jest.mock('./useLongRecordingWarning', () => ({
  useLongRecordingWarning: () => ({ armAlertContext: () => {} }),
}));
jest.mock('./useMediaSession', () => ({ useMediaSession: () => {} }));
jest.mock('../contexts/ToastContext', () => ({
  useToast: () => ({ addToast: jest.fn(), removeToast: () => {} }),
}));
jest.mock('../utils/voiceTiming', () => ({
  startTurn: () => {}, endTurn: () => {}, setTurnNode: () => {},
  mark: () => {}, markPlaying: () => {},
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
});

const flush = async () => {
  await act(async () => {
    for (let i = 0; i < 10; i += 1) await Promise.resolve();
  });
};

const CLIP = '/api/voice/approval-announcement/one.mp3?v=abc';
// The server's answer on the announcement for reply 42: a promise.
let announcementAnswer;
const announcementAsks = () => mockGet.mock.calls.filter(
  ([url]) => url === '/voice/approval-announcement');

beforeEach(() => {
  jest.useFakeTimers();
  MockEventSource.instances = [];
  jest.clearAllMocks();
  window.history.replaceState({}, '', '/voice');
  announcementAnswer = Promise.resolve({ data: { count: 1, url: CLIP, duration: 4.5 } });
  mockGet.mockImplementation((url) => {
    if (url === '/nodes/42/llm-status') {
      return Promise.resolve({ data: { node_id: 42, status: 'completed', content: 'Noted.' } });
    }
    if (url === '/voice/approval-announcement') return announcementAnswer;
    return Promise.reject(new Error(`unexpected GET ${url}`));
  });
  mockPost.mockImplementation((url) => {
    if (url === '/nodes/42/tts') return Promise.resolve({ status: 202, data: { status: 'pending' } });
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

const stream = () => MockEventSource.instances.find(
  (es) => es.url === '/api/sse/nodes/42/tts-stream');

// Reply 42 is written; its audio arrives in one chunk, then all_complete.
const playReply = async () => {
  const hook = renderHook(() => useVoiceSession({
    apiEndpoint: '/voice', ttsTitle: 'Voice', initialLlmNodeId: 42, initialParentId: 41,
  }));
  await flush();
  act(() => stream().emit('chunk_ready', {
    chunk_index: 0, audio_url: '/media/42-0.mp3', duration: 4,
  }));
  await flush();
  return hook;
};

const lastGeneratingCall = () => mockAudio.setGeneratingTTS.mock.calls.at(-1);

test('a waiting change: the clip joins the queue after the reply, then the queue may end', async () => {
  await playReply();
  // Asked when the reply was written, before its audio was complete.
  expect(announcementAsks()).toHaveLength(1);
  expect(announcementAsks()[0][1]).toEqual({ params: { node: 42 } });
  expect(mockAudio.appendAnnouncement).not.toHaveBeenCalled();

  act(() => stream().emit('all_complete', { continuation_node_id: null, preview: 'Noted.' }));
  await flush();

  expect(mockAudio.appendAnnouncement).toHaveBeenCalledWith(CLIP, 4.5, 42, {});
  expect(lastGeneratingCall()).toEqual([false]);
  const appended = mockAudio.appendAnnouncement.mock.invocationCallOrder[0];
  const ended = mockAudio.setGeneratingTTS.mock.invocationCallOrder.at(-1);
  expect(appended).toBeLessThan(ended);
  // The clip is not a chapter of the reply.
  expect(mockAudio.appendChunkToQueue).not.toHaveBeenCalledWith(
    CLIP, expect.anything(), expect.anything());
  // Asked once per reply.
  expect(announcementAsks()).toHaveLength(1);
});

test('nothing waits: the reply plays alone', async () => {
  announcementAnswer = Promise.resolve({ data: { count: 0, url: null, duration: null } });
  await playReply();
  act(() => stream().emit('all_complete', { continuation_node_id: null }));
  await flush();

  expect(mockAudio.appendAnnouncement).not.toHaveBeenCalled();
  expect(lastGeneratingCall()).toEqual([false]);
});

test('a slow answer: the queue ends without the clip after the bound', async () => {
  announcementAnswer = new Promise(() => {});
  await playReply();
  act(() => stream().emit('all_complete', { continuation_node_id: null }));
  await flush();
  // The queue still waits (generatingTTS was never turned off).
  expect(mockAudio.setGeneratingTTS).not.toHaveBeenCalledWith(false);

  await act(async () => { jest.advanceTimersByTime(5000); });
  await flush();
  expect(mockAudio.appendAnnouncement).not.toHaveBeenCalled();
  expect(lastGeneratingCall()).toEqual([false]);
});

test('a failed answer: the reply plays alone', async () => {
  announcementAnswer = Promise.reject(new Error('Network Error'));
  announcementAnswer.catch(() => {});
  await playReply();
  act(() => stream().emit('all_complete', { continuation_node_id: null }));
  await flush();

  expect(mockAudio.appendAnnouncement).not.toHaveBeenCalled();
  expect(lastGeneratingCall()).toEqual([false]);
});

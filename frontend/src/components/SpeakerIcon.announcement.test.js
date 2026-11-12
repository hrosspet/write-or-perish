// The thread page's speaker on a Voice reply whose changes wait for
// approval (Peter, 2026-11-12: "the announcement should be TTSed as a
// separate chunk and appended to the queue. So that when it's not pending,
// it's not voiced out anymore"): the reply's audio is followed by the
// spoken announcement while they wait, asked each time it plays; its
// chapters keep their start times.
const mockAudio = {
  loadAudio: jest.fn(() => Promise.resolve()),
  loadAudioQueue: jest.fn(() => Promise.resolve()),
  appendAnnouncement: jest.fn(() => Promise.resolve()),
  updateChapters: jest.fn(),
  appendChunkToQueue: jest.fn(),
  setGeneratingTTS: jest.fn(),
  currentAudio: null,
  isPlaying: false,
  warmup: jest.fn(),
  waitingForChunks: false,
  queueFinishedCount: 0,
};
jest.mock('../contexts/AudioContext', () => ({ useAudio: () => mockAudio }));
jest.mock('../contexts/UserContext', () => ({
  useUser: () => ({ user: { username: 'ben', voice_mode_enabled: true } }),
}));
jest.mock('../contexts/ToastContext', () => ({
  useToast: () => ({ addToast: jest.fn() }),
}));
jest.mock('../hooks/useAsyncTaskPolling', () => ({
  useAsyncTaskPolling: () => ({ status: null, data: null, error: null }),
}));
jest.mock('../hooks/useSSE', () => ({
  useTTSStreamSSE: () => ({ disconnect: jest.fn(), reset: jest.fn() }),
}));
const mockGet = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: { get: (...args) => mockGet(...args), post: jest.fn() },
}));

import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import SpeakerIcon from './SpeakerIcon';

const CHAPTERS = [
  { section_index: 0, title: 'Plan', chunk_index: 0, start_time: 0 },
  { section_index: 1, title: 'Later', chunk_index: 2, start_time: 9.5 },
];
let announcement;

beforeEach(() => {
  jest.clearAllMocks();
  process.env.REACT_APP_BACKEND_URL = '';
  announcement = { count: 1, url: '/api/voice/approval-announcement/one.mp3?v=k', duration: 4.5 };
  mockGet.mockImplementation((url) => {
    if (url === '/nodes/42/audio') return Promise.resolve({ status: 200, data: { tts_url: '/media/42/tts.mp3' } });
    if (url === '/nodes/42/tts-chapters') return Promise.resolve({ status: 200, data: { chapters: CHAPTERS } });
    if (url === '/voice/approval-announcement') return Promise.resolve({ data: announcement });
    return Promise.reject(new Error(`unexpected GET ${url}`));
  });
});

const click = async () => {
  await act(async () => { fireEvent.click(screen.getByRole('button')); });
  await act(async () => { await Promise.resolve(); });
};

test('while a change waits: the reply, then the announcement; chapters by time', async () => {
  render(<SpeakerIcon nodeId={42} content="# Plan" aiUsage="chat" announce />);
  await click();

  expect(mockAudio.loadAudio).not.toHaveBeenCalled();
  const [urls, meta] = mockAudio.loadAudioQueue.mock.calls[0];
  expect(urls).toEqual(['/media/42/tts.mp3']);
  expect(meta.chapters).toEqual([
    { section_index: 0, title: 'Plan', start_time: 0 },
    { section_index: 1, title: 'Later', start_time: 9.5 },
  ]);
  expect(mockAudio.appendAnnouncement).toHaveBeenCalledWith(
    '/api/voice/approval-announcement/one.mp3?v=k', 4.5, 42);
});

test('a replay after the decision plays the reply alone', async () => {
  render(<SpeakerIcon nodeId={42} content="# Plan" aiUsage="chat" announce />);
  await click();
  announcement = { count: 0, url: null, duration: null };
  await click();

  expect(mockAudio.loadAudio).toHaveBeenCalledTimes(1);
  expect(mockAudio.loadAudio.mock.calls[0][0]).toMatchObject({
    url: '/media/42/tts.mp3', chapters: CHAPTERS });
  expect(mockAudio.appendAnnouncement).toHaveBeenCalledTimes(1);
});

test('no change waits in the thread: no question asked, as before', async () => {
  render(<SpeakerIcon nodeId={42} content="# Plan" aiUsage="chat" />);
  await click();

  expect(mockGet).not.toHaveBeenCalledWith('/voice/approval-announcement', expect.anything());
  expect(mockAudio.loadAudio.mock.calls[0][0]).toMatchObject({
    url: '/media/42/tts.mp3', chapters: CHAPTERS });
});

// A TTS stream whose generation is already complete sends every chunk at
// once (#376). The first chunk has to load the player's queue before the
// next one is appended, or the appended chunks are lost when it loads.
let mockSSEOptions = null;
jest.mock('../hooks/useSSE', () => ({
  useTTSStreamSSE: (id, options) => {
    mockSSEOptions = options;
    return { disconnect: jest.fn() };
  },
}));
jest.mock('../hooks/useAsyncTaskPolling', () => ({
  useAsyncTaskPolling: () => ({ status: null, data: null, error: null }),
}));
jest.mock('../contexts/UserContext', () => ({
  useUser: () => ({ user: { voice_mode_enabled: true } }),
}));
const mockAddToast = jest.fn();
jest.mock('../contexts/ToastContext', () => ({
  useToast: () => ({ addToast: mockAddToast }),
}));
jest.mock('../utils/errorSound', () => ({ playErrorSound: jest.fn() }));
const mockAudio = {
  loadAudio: jest.fn(),
  loadAudioQueue: jest.fn(),
  updateChapters: jest.fn(),
  appendChunkToQueue: jest.fn(),
  setGeneratingTTS: jest.fn(),
  warmup: jest.fn(),
  currentAudio: null,
  isPlaying: false,
};
jest.mock('../contexts/AudioContext', () => ({
  useAudio: () => mockAudio,
}));
// Plain functions: CRA's resetMocks would clear jest.fn implementations.
let mockResolveChapters;
jest.mock('../api', () => ({
  get: (url) => {
    if (url.endsWith('/tts-chapters')) {
      // The chapters request is still in flight while the chunks arrive.
      return new Promise((resolve) => { mockResolveChapters = resolve; });
    }
    return Promise.resolve({ status: 404, data: {} });
  },
  post: () => Promise.resolve({ status: 202, data: {} }),
}));

import React from 'react';
import { render, fireEvent, act } from '@testing-library/react';
import SpeakerIcon from './SpeakerIcon';
import { playErrorSound } from '../utils/errorSound';

const chunk = (i) => ({
  chunk_index: i,
  audio_url: `https://loore.test/media/n/tts_chunk_${i}.mp3?v=1`,
  duration: 2.0,
  section_index: 0,
});

test('chunks that arrive at once all reach the queue, in order', async () => {
  const { getByRole } = render(<SpeakerIcon nodeId={5} content="# Title" />);
  await act(async () => { fireEvent.click(getByRole('button')); });

  act(() => {
    mockSSEOptions.onChunkReady(chunk(0));
    mockSSEOptions.onChunkReady(chunk(1));
    mockSSEOptions.onAllComplete({ tts_url: '/media/n/tts.mp3?v=1' });
  });

  expect(mockAudio.loadAudioQueue).toHaveBeenCalledTimes(1);
  expect(mockAudio.loadAudioQueue.mock.calls[0][0]).toEqual([chunk(0).audio_url]);
  expect(mockAudio.appendChunkToQueue).toHaveBeenCalledWith(chunk(1).audio_url, 2.0);
  expect(mockAudio.loadAudioQueue.mock.invocationCallOrder[0])
    .toBeLessThan(mockAudio.appendChunkToQueue.mock.invocationCallOrder[0]);

  // The chapters are applied to the playing queue once they arrive.
  const chapters = [{ title: 'Title', start_time: 0, chunk_index: 0 }];
  await act(async () => { mockResolveChapters({ status: 200, data: { chapters } }); });
  expect(mockAudio.updateChapters).toHaveBeenCalledWith(5, 'node', chapters);
});

test('a profile streams with chapters from its own route', async () => {
  mockAudio.updateChapters.mockClear();
  const { getByRole } = render(<SpeakerIcon profileId={9} content="# Title" aiUsage="chat" />);
  await act(async () => { fireEvent.click(getByRole('button')); });

  act(() => { mockSSEOptions.onChunkReady(chunk(0)); });
  const chapters = [{ title: 'Title', start_time: 0, chunk_index: 0 }];
  await act(async () => { mockResolveChapters({ status: 200, data: { chapters } }); });
  expect(mockAudio.updateChapters).toHaveBeenCalledWith(9, 'profile', chapters);
});

// AI usage 'none': no new speech for an entry, a model's reply or a profile
// version (the server refuses it as well, speech_allowed).
test.each([
  [{ nodeId: 5 }],
  [{ profileId: 9 }],
])('the icon is off where AI usage is none (%o)', (ids) => {
  const { getByRole } = render(
    <SpeakerIcon {...ids} content="text" aiUsage="none" />);
  expect(getByRole('button')).toBeDisabled();
  expect(getByRole('button')).toHaveAttribute('title', 'TTS disabled — No AI access');
});

test('the icon is on for a profile version AI may read', () => {
  const { getByRole } = render(
    <SpeakerIcon profileId={9} content="text" aiUsage="chat" />);
  expect(getByRole('button')).not.toBeDisabled();
});

// The server's `error` event: generation failed (every attempt at a chunk
// gave no playable audio). The stream is turned off, so it doesn't
// reconnect every 3 s, and the spinner stops.
test('a failed generation stops the stream and the spinner, and says so', async () => {
  const { getByRole } = render(
    <SpeakerIcon profileId={9} content="# Title" aiUsage="chat" />);
  await act(async () => { fireEvent.click(getByRole('button')); });
  expect(mockSSEOptions.enabled).toBe(true);
  expect(getByRole('button')).toHaveAttribute('title', 'Generating audio...');

  act(() => { mockSSEOptions.onError({ message: 'TTS generation failed' }); });

  expect(mockSSEOptions.enabled).toBe(false);
  expect(getByRole('button')).toHaveAttribute('title', 'Play audio');
  expect(mockAudio.setGeneratingTTS).toHaveBeenLastCalledWith(false);
  expect(playErrorSound).toHaveBeenCalledTimes(1);
  expect(mockAddToast).toHaveBeenCalledWith(
    expect.stringContaining("couldn't read this aloud"), 6000);
});

test('a failure mid-stream lets the audio that arrived play, then says so', async () => {
  const { getByRole, rerender } = render(<SpeakerIcon nodeId={5} content="# Title" />);
  await act(async () => { fireEvent.click(getByRole('button')); });
  act(() => { mockSSEOptions.onChunkReady(chunk(0)); });
  // The player now plays this entry's queue.
  mockAudio.currentAudio = { id: 5, type: 'node' };
  mockAudio.queueFinishedCount = 0;
  mockAudio.waitingForChunks = false;
  rerender(<SpeakerIcon nodeId={5} content="# Title" />);

  act(() => { mockSSEOptions.onError({ message: 'TTS generation failed' }); });

  expect(mockSSEOptions.enabled).toBe(false);
  expect(getByRole('button')).toHaveAttribute('title', 'Play audio');
  expect(mockAudio.loadAudioQueue).toHaveBeenCalledTimes(1);
  expect(playErrorSound).not.toHaveBeenCalled();

  // The queued audio plays to its end: now the failure is announced.
  mockAudio.queueFinishedCount = 1;
  rerender(<SpeakerIcon nodeId={5} content="# Title" />);
  expect(playErrorSound).toHaveBeenCalledTimes(1);
  expect(mockAddToast).toHaveBeenCalledWith(
    expect.stringContaining("couldn't read the rest aloud"), 6000);

  mockAudio.currentAudio = null;
  delete mockAudio.queueFinishedCount;
  delete mockAudio.waitingForChunks;
});

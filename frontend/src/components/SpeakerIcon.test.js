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

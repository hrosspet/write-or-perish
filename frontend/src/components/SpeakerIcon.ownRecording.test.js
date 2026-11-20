// Payments (#438): an account without Voice mode (free, or its
// subscription ended) still plays its own recordings; NodeDetail passes
// `ownRecording` only while the server has payments on. Without the flag
// the icons stay hidden, as before.
jest.mock('../hooks/useSSE', () => ({
  useTTSStreamSSE: () => ({ disconnect: jest.fn() }),
}));
jest.mock('../hooks/useAsyncTaskPolling', () => ({
  useAsyncTaskPolling: () => ({ status: null, data: null, error: null }),
}));
jest.mock('../contexts/UserContext', () => ({
  useUser: () => ({ user: { voice_mode_enabled: false } }),
}));
jest.mock('../contexts/ToastContext', () => ({
  useToast: () => ({ addToast: () => {} }),
}));
jest.mock('../utils/errorSound', () => ({ playErrorSound: () => {} }));
jest.mock('../contexts/AudioContext', () => ({
  useAudio: () => ({
    loadAudio: () => {}, loadAudioQueue: () => {}, updateChapters: () => {},
    appendChunkToQueue: () => {}, setGeneratingTTS: () => {}, warmup: () => {},
    currentAudio: null, isPlaying: false,
  }),
}));
jest.mock('../api', () => ({
  get: () => Promise.resolve({ status: 404, data: {} }),
  post: () => Promise.resolve({ status: 202, data: {} }),
}));

import React from 'react';
import { render } from '@testing-library/react';
import SpeakerIcon from './SpeakerIcon';
import DownloadAudioIcon from './DownloadAudioIcon';

test('without Voice mode the icons stay hidden', () => {
  const { container } = render(
    <>
      <SpeakerIcon nodeId={1} content="x" isPublic={false} aiUsage="chat" />
      <DownloadAudioIcon nodeId={1} isPublic={false} aiUsage="chat" />
    </>,
  );
  expect(container.querySelectorAll('button')).toHaveLength(0);
});

test('the owner of a recording sees both icons', () => {
  const { container } = render(
    <>
      <SpeakerIcon nodeId={1} content="x" isPublic={false} aiUsage="chat" ownRecording />
      <DownloadAudioIcon nodeId={1} isPublic={false} aiUsage="chat" ownRecording />
    </>,
  );
  expect(container.querySelectorAll('button').length).toBeGreaterThanOrEqual(2);
});

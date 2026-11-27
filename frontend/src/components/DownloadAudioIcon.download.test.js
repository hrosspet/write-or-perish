// The download button saves a file only from a 2xx answer. A refusal with
// its own words (audio-download's 429 download_busy) is shown as a toast and
// nothing is saved; another failed merge falls back to the spoken version,
// and when nothing can be saved the server's words (or a plain sentence)
// are shown.
const mockAddToast = jest.fn();
const mockGet = jest.fn();
jest.mock('../contexts/UserContext', () => ({
  useUser: () => ({ user: { voice_mode_enabled: true } }),
}));
jest.mock('../contexts/ToastContext', () => ({
  useToast: () => ({ addToast: mockAddToast }),
}));
jest.mock('../api', () => ({
  get: (...args) => mockGet(...args),
}));

import React from 'react';
import { render, fireEvent, waitFor } from '@testing-library/react';
import DownloadAudioIcon, { DOWNLOAD_FAILED_TEXT } from './DownloadAudioIcon';

const BUSY = 'Another download of a recording is being prepared. '
  + 'Please try again in a few minutes.';

let saved;
let fetched;

function answer(status, body, contentType = 'application/json') {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => {
      if (!contentType.includes('json')) throw new Error('not JSON');
      return body;
    },
    blob: async () => new Blob([JSON.stringify(body)]),
  };
}

function serve(routes) {
  global.fetch = jest.fn(async (url) => {
    fetched.push(url);
    const key = Object.keys(routes).find((k) => url.includes(k));
    return routes[key];
  });
}

beforeEach(() => {
  saved = [];
  fetched = [];
  mockAddToast.mockReset();
  mockGet.mockReset();
  global.URL.createObjectURL = jest.fn(() => 'blob:x');
  global.URL.revokeObjectURL = jest.fn();
  jest.spyOn(HTMLAnchorElement.prototype, 'click')
    .mockImplementation(function click() { saved.push(this.download); });
  jest.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
  jest.restoreAllMocks();
  delete global.fetch;
});

function clickDownload() {
  const { container } = render(
    <DownloadAudioIcon nodeId={7} isPublic={false} aiUsage="chat" />,
  );
  fireEvent.click(container.querySelector('button'));
}

test('a busy merged download shows the server words and saves nothing', async () => {
  mockGet.mockResolvedValue({
    status: 200,
    data: { has_audio_chunks: true, tts_url: '/media/user/1/node/7/tts.mp3' },
  });
  serve({
    'audio-download': answer(429, { error: BUSY, code: 'download_busy' }),
    'tts.mp3': answer(200, {}),
  });
  clickDownload();
  await waitFor(() => expect(mockAddToast).toHaveBeenCalledWith(BUSY, 6000));
  expect(saved).toEqual([]);
  expect(fetched.some((u) => u.includes('tts.mp3'))).toBe(false);
});

test('a merged download is saved as the recording', async () => {
  mockGet.mockResolvedValue({ status: 200, data: { has_audio_chunks: true } });
  serve({ 'audio-download': answer(200, {}) });
  clickDownload();
  await waitFor(() => expect(saved).toEqual(['node-7-recording.mp3']));
  expect(mockAddToast).not.toHaveBeenCalled();
});

test('a failed merge without its own words falls back to the spoken version', async () => {
  mockGet.mockResolvedValue({
    status: 200,
    data: { has_audio_chunks: true, tts_url: '/media/user/1/node/7/tts.mp3' },
  });
  serve({
    'audio-download': answer(504, null, 'text/html'),
    'tts.mp3': answer(200, {}),
  });
  clickDownload();
  await waitFor(() => expect(saved).toEqual(['node-7-tts.mp3']));
  expect(mockAddToast).not.toHaveBeenCalled();
});

test('a failed merge with nothing to fall back on says so', async () => {
  mockGet.mockResolvedValue({ status: 200, data: { has_audio_chunks: true } });
  serve({ 'audio-download': answer(504, null, 'text/html') });
  clickDownload();
  await waitFor(() => expect(mockAddToast).toHaveBeenCalledWith(
    DOWNLOAD_FAILED_TEXT, 6000));
  expect(saved).toEqual([]);
});

test('a refused recording file is not saved and its words are shown', async () => {
  mockGet.mockResolvedValue({
    status: 200,
    data: { original_url: '/media/user/1/node/7/original.m4a' },
  });
  serve({ 'original.m4a': answer(404, { error: 'File not found' }) });
  clickDownload();
  await waitFor(() => expect(mockAddToast).toHaveBeenCalledWith(
    'File not found', 6000));
  expect(saved).toEqual([]);
});

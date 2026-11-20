// #651: a Record or Continue press refused for the monthly usage limit the
// page already knows of (#341) keeps its toast and is said aloud too
// (useVoiceSession.refuseForUsageLimit pauses a reply and says it). No
// recording starts.
jest.mock('../contexts/UserContext', () => ({
  useUser: () => ({
    user: { username: 'ben', default_ai_usage: 'chat', has_own_entries: true },
    markHasOwnEntries: jest.fn(),
  }),
}));
const mockAddToast = jest.fn();
jest.mock('../contexts/ToastContext', () => ({
  useToast: () => ({ addToast: mockAddToast, removeToast: jest.fn() }),
}));
const mockGet = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    get: (...args) => mockGet(...args),
    post: () => Promise.reject(new Error('unexpected POST')),
    delete: () => Promise.resolve({ data: {} }),
  },
}));
let mockPhase;
const mockSession = {};
jest.mock('../hooks/useVoiceSession', () => ({
  useVoiceSession: () => ({
    phase: mockPhase, isStopping: false, hasError: false, isOnline: true,
    streaming: { isPaused: false, isInterrupted: false, duration: 0 },
    audio: {
      isPlaying: mockPhase === 'playback', generatingTTS: false, cumulativeTime: 0,
      totalDuration: 0, queueFinishedCount: 0, pause: jest.fn(), play: jest.fn(),
      stop: jest.fn(), skipBackward: jest.fn(), skipForward: jest.fn(),
      seekToCumulativeTime: jest.fn(), chapterStartTime: () => 0,
      currentAudio: mockPhase === 'playback' ? { title: 'Voice', chapters: [] } : null,
    },
    ...mockSession,
    handleStop: jest.fn(), handleResumeSession: jest.fn(),
    handleCancelProcessing: jest.fn(), setThreadParentId: jest.fn(),
    handleResumeRecording: jest.fn(), getThreadParentId: () => null,
  }),
}));
jest.mock('../hooks/useAsyncTaskPolling', () => ({
  useAsyncTaskPolling: () => ({ status: null, data: null, error: null }),
}));
jest.mock('../components/OfflineBanner', () => () => null);
jest.mock('../components/ProposalInline', () => () => null);

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import VoicePage from './VoicePage';
import { markSpendBlocked } from '../utils/spendCap';

const renderAt = (path) => render(
  <MemoryRouter initialEntries={[path]}>
    <Routes><Route path="/voice" element={<VoicePage />} /></Routes>
  </MemoryRouter>,
);

beforeEach(() => {
  mockGet.mockImplementation((url) => Promise.resolve({
    data: url === '/drafts/interrupted' ? []
      : url === '/tool-approvals' ? { pending: [] } : { allowed: true },
  }));
  mockAddToast.mockReset();
  mockSession.handleStart = jest.fn();
  mockSession.handleContinue = jest.fn();
  mockSession.refuseForUsageLimit = jest.fn();
});

test('Record while capped: the toast, the sentence, no recording', async () => {
  markSpendBlocked();
  mockPhase = 'ready';
  renderAt('/voice');

  fireEvent.click(await screen.findByLabelText('Record'));

  expect(mockAddToast).toHaveBeenCalledTimes(1);
  expect(mockSession.refuseForUsageLimit).toHaveBeenCalledTimes(1);
  expect(mockSession.handleStart).not.toHaveBeenCalled();
});

test('Continue while capped: the toast, the sentence, no new turn', async () => {
  markSpendBlocked();
  mockPhase = 'playback';
  renderAt('/voice?parent=41');

  await waitFor(() => expect(screen.getByLabelText('Continue')).toBeInTheDocument());
  fireEvent.click(screen.getByLabelText('Continue'));

  expect(mockAddToast).toHaveBeenCalledTimes(1);
  expect(mockSession.refuseForUsageLimit).toHaveBeenCalledTimes(1);
  expect(mockSession.handleContinue).not.toHaveBeenCalled();
});

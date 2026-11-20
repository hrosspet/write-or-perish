// Glean again on the Voice screen after "all of today's tweets" (#717),
// with the real useAsyncTaskPolling: the refusal clears the screen's wait
// (its poll address goes away), and the next Glean waits for its own poll.
// The hook kept the old failure as its error when the address changed, so
// the next gleaning's first render read it as "the poll gave up" and the
// screen opened the thread page while that gleaning was still running.
let mockUserCtx;
jest.mock('../contexts/UserContext', () => ({
  useUser: () => mockUserCtx,
}));
const mockAddToast = jest.fn();
jest.mock('../contexts/ToastContext', () => ({
  useToast: () => ({ addToast: mockAddToast, removeToast: jest.fn() }),
}));
const mockSayRefusal = jest.fn();
jest.mock('../utils/spokenRefusal', () => ({
  ...jest.requireActual('../utils/spokenRefusal'),
  sayRefusal: (...args) => mockSayRefusal(...args),
}));
const mockGet = jest.fn();
const mockPost = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    get: (...args) => mockGet(...args),
    post: (...args) => mockPost(...args),
    delete: jest.fn(() => Promise.resolve({ data: {} })),
  },
}));
let mockSessionOptions;
let mockPhase;
const mockStop = jest.fn();
jest.mock('../hooks/useVoiceSession', () => ({
  useVoiceSession: (options) => {
    mockSessionOptions = options;
    return {
      phase: mockPhase, isStopping: false, hasError: false, isOnline: true,
      streaming: { isPaused: false, isInterrupted: false, duration: 0 },
      audio: {
        isPlaying: true, pause: jest.fn(), stop: mockStop, play: jest.fn(),
        skipBackward: jest.fn(), skipForward: jest.fn(),
        cumulativeTime: 0, totalDuration: 0, chapterStartTime: () => 0,
        currentAudio: null,
      },
      handleStart: jest.fn(), handleStop: jest.fn(),
      handleContinue: jest.fn(), handleResumeSession: jest.fn(),
      handleCancelProcessing: jest.fn(), setThreadParentId: jest.fn(),
      handleResumeRecording: jest.fn(),
      getThreadParentId: () => mockThreadParent,
    };
  },
}));
let mockThreadParent = null;
jest.mock('../components/OfflineBanner', () => () => null);
jest.mock('../components/ProposalInline', () => () => null);

import React from 'react';
import { render, screen, act, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter, Routes, Route, useParams } from 'react-router-dom';
import VoicePage from './VoicePage';

function NodeStub() {
  const { id } = useParams();
  return <div>{`Thread page ${id}`}</div>;
}

const renderAt = (path, user = {}) => {
  mockUserCtx = {
    user: { username: 'ana', default_ai_usage: 'chat', glean_enabled: true, ...user },
    markHasOwnEntries: jest.fn(),
  };
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/voice" element={<VoicePage />} />
        <Route path="/node/:id" element={<NodeStub />} />
      </Routes>
    </MemoryRouter>,
  );
};

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
  mockStop.mockReset();
  mockSessionOptions = null;
  mockThreadParent = null;
  mockAddToast.mockReset();
  mockSayRefusal.mockReset();
  mockPhase = 'ready';
  mockGet.mockImplementation((url) => {
    if (url === '/drafts/interrupted') return Promise.resolve({ data: [] });
    if (url === '/voice/availability') return Promise.resolve({ data: { allowed: true } });
    return Promise.reject(new Error(`unexpected GET ${url}`));
  });
});

const glean = () => screen.queryByRole('button', { name: /^(Glean|Gleaning)$/ });


test('a second Glean after the day-done refusal waits for its own poll', async () => {
  const text = "You've gleaned through all of today's tweets. New ones come in once a day.";
  mockGet.mockImplementation((url) => {
    if (url === '/drafts/interrupted') return Promise.resolve({ data: [] });
    if (url === '/voice/availability') return Promise.resolve({ data: { allowed: true } });
    if (url === '/nodes/57/llm-status') return Promise.resolve({ data: { status: 'failed', error: text, error_code: 'glean_day_done' } });
    if (url === '/nodes/58/llm-status') return Promise.resolve({ data: { status: 'processing', progress: 10 } });
    return Promise.reject(new Error(`unexpected GET ${url}`));
  });
  renderAt('/voice?parent=42&glean=1');
  await screen.findByText("What's on your mind?");
  mockPost.mockResolvedValueOnce({ data: { llm_node_id: 57 } });
  await act(async () => { fireEvent.click(glean()); });
  await waitFor(() => expect(mockSayRefusal).toHaveBeenCalledWith('glean_day_done'));
  await waitFor(() => expect(glean()).toHaveTextContent(/^Glean$/));
  expect(screen.queryByText('Thread page 57')).toBeNull();

  // The user taps Glean again; the new gleaning (58) is still processing.
  mockPost.mockResolvedValueOnce({ data: { llm_node_id: 58 } });
  await act(async () => { fireEvent.click(glean()); });
  await act(async () => { await new Promise((r) => setTimeout(r, 50)); });
  expect(screen.queryByText('Thread page 58')).toBeNull();
});

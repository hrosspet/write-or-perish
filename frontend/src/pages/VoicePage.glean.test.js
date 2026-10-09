// Glean on the Voice screen (#435, Peter 2026-10-09): in a session opened
// from the Glean card (or continuing a thread started there) a labeled
// Glean button sits under the record button, shown only once the thread
// has a recorded message. Pressing it starts a live glean; the screen
// waits ("Gleaning") and then opens the gleaning in text mode, never
// reading it aloud. A Reflect session has no Glean button at any turn.
let mockUserCtx;
jest.mock('../contexts/UserContext', () => ({
  useUser: () => mockUserCtx,
}));
jest.mock('../contexts/ToastContext', () => ({
  useToast: () => ({ addToast: jest.fn(), removeToast: jest.fn() }),
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
// The glean's own poll: what the provider has done so far.
let mockGleanStatus = null;
let mockGleanError = null;
const mockPoll = jest.fn();
jest.mock('../hooks/useAsyncTaskPolling', () => ({
  useAsyncTaskPolling: (endpoint, options) => {
    mockPoll(endpoint, options);
    return { status: endpoint ? mockGleanStatus : null, data: null, error: endpoint ? mockGleanError : null };
  },
}));
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
  mockPoll.mockReset();
  mockSessionOptions = null;
  mockThreadParent = null;
  mockGleanStatus = null;
  mockGleanError = null;
  mockPhase = 'ready';
  mockGet.mockImplementation((url) => {
    if (url === '/drafts/interrupted') return Promise.resolve({ data: [] });
    if (url === '/voice/availability') return Promise.resolve({ data: { allowed: true } });
    return Promise.reject(new Error(`unexpected GET ${url}`));
  });
});

const glean = () => screen.queryByRole('button', { name: /^(Glean|Gleaning)$/ });

test('a fresh Glean session: no Glean before the first recording, then under the record button', async () => {
  renderAt('/voice?glean=1');
  expect(await screen.findByText("What's on your mind?")).toBeInTheDocument();
  // The new thread is marked as started from the Glean card.
  expect(mockSessionOptions.entry).toBe('glean');
  expect(glean()).toBeNull();

  // The first recording is saved; its reply is still coming.
  act(() => { mockSessionOptions.onEntrySaved(); });
  expect(glean()).toBeDisabled();

  // The reply is in: Glean starts under it.
  mockThreadParent = 55;
  mockPhase = 'playback';
  act(() => { mockSessionOptions.onLLMComplete(55, 'A reply.'); });
  expect(glean()).toBeEnabled();

  mockPost.mockResolvedValue({ data: { prompt_node_id: 56, llm_node_id: 57 } });
  await act(async () => { fireEvent.click(glean()); });
  expect(mockPost).toHaveBeenCalledWith('/read/from-node/55', {});
  // The voice reply stops; the gleaning is never handed to the voice
  // session, so it is never spoken.
  expect(mockStop).toHaveBeenCalled();
  expect(screen.getByRole('button', { name: 'Gleaning' })).toBeDisabled();
  expect(mockPoll).toHaveBeenLastCalledWith('/nodes/57/llm-status', expect.objectContaining({ enabled: true }));
});

test('when the gleaning is ready the view switches to text mode', async () => {
  renderAt('/voice?parent=42&glean=1');
  await screen.findByText("What's on your mind?");
  mockPost.mockResolvedValue({ data: { prompt_node_id: 56, llm_node_id: 57 } });
  await act(async () => { fireEvent.click(glean()); });
  expect(mockPost).toHaveBeenCalledWith('/read/from-node/42', {});

  mockGleanStatus = 'completed';
  // The next render picks the finished poll up (here: a state change).
  act(() => { mockSessionOptions.onLLMComplete(42, ''); });
  await waitFor(() => expect(screen.getByText('Thread page 57')).toBeInTheDocument());
});

test('a poll that gives up opens the thread page instead of staying on "Gleaning"', async () => {
  renderAt('/voice?parent=42&glean=1');
  await screen.findByText("What's on your mind?");
  mockPost.mockResolvedValue({ data: { prompt_node_id: 56, llm_node_id: 57 } });
  await act(async () => { fireEvent.click(glean()); });
  mockGleanError = 'Polling timeout - task took too long';
  act(() => { mockSessionOptions.onLLMComplete(42, ''); });
  await waitFor(() => expect(screen.getByText('Thread page 57')).toBeInTheDocument());
});

test('a session continuing a Glean-card thread shows Glean at once', async () => {
  renderAt('/voice?parent=42&glean=1');
  await screen.findByText("What's on your mind?");
  expect(glean()).toBeEnabled();
});

test('a Reflect session has no Glean button at any turn', async () => {
  renderAt('/voice');
  await screen.findByText("What's on your mind?");
  expect(mockSessionOptions.entry).toBeNull();
  act(() => { mockSessionOptions.onEntrySaved(); });
  mockThreadParent = 55;
  mockPhase = 'playback';
  act(() => { mockSessionOptions.onLLMComplete(55, 'A reply.'); });
  expect(glean()).toBeNull();
});

test('a user with Glean off gets no Glean button, even from a Glean link', async () => {
  renderAt('/voice?parent=42&glean=1', { glean_enabled: false });
  await screen.findByText("What's on your mind?");
  expect(mockSessionOptions.entry).toBeNull();
  expect(glean()).toBeNull();
});

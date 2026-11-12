// Merge train 2026-11-12 (2): a Voice turn can be refused for two reasons,
// a change the AI proposed that waits for the user's approval (409
// `approval_pending`) and a thread too long for another reply (409
// `thread_too_long`, #648). When both apply, the waiting change comes
// first: it needs a decision before anything else; the length follows.
//
// Here: a reply that both proposes a waiting change and takes the thread to
// its limit keeps its session (its audio, the announcement and the dialog
// play out), Continue gives way to the limit, and after the decision the
// screen shows the limit. Each refusal alone keeps its own screen.
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
let mockAudio;
let mockThreadParent = null;
const mockMounts = jest.fn();
const mockUnmounts = jest.fn();
jest.mock('../hooks/useVoiceSession', () => {
  const { useEffect } = jest.requireActual('react');
  return {
    useVoiceSession: (options) => {
      mockSessionOptions = options;
      // The real hook stops the player when it unmounts: a session that
      // unmounts mid-reply cuts the reply off.
      useEffect(() => {
        mockMounts();
        return () => mockUnmounts();
      }, []);
      return {
        phase: mockPhase, isStopping: false, hasError: false, isOnline: true,
        streaming: { isPaused: false, isInterrupted: false, duration: 0 },
        audio: mockAudio,
        handleStart: jest.fn(), handleStop: jest.fn(),
        handleContinue: jest.fn(), handleResumeSession: jest.fn(),
        handleCancelProcessing: jest.fn(), setThreadParentId: jest.fn(),
        handleResumeRecording: jest.fn(),
        getThreadParentId: () => mockThreadParent,
      };
    },
  };
});
jest.mock('../hooks/useAsyncTaskPolling', () => ({
  useAsyncTaskPolling: () => ({ status: null, data: null, error: null }),
}));
jest.mock('../components/OfflineBanner', () => () => null);
jest.mock('../components/ProposalInline', () => () => null);

import React from 'react';
import { render, screen, act, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import VoicePage from './VoicePage';
import { THREAD_LIMIT_TEXT } from '../utils/threadLength';

const tree = (path) => (
  <MemoryRouter initialEntries={[path]}>
    <Routes>
      <Route path="/voice" element={<VoicePage />} />
      <Route path="/node/:id" element={<div>Thread page</div>} />
    </Routes>
  </MemoryRouter>
);

const renderAt = (path) => {
  mockUserCtx = {
    user: { username: 'ben', default_ai_usage: 'chat', has_own_entries: true },
    markHasOwnEntries: jest.fn(),
  };
  return render(tree(path));
};

const call = (id, turn = 41) => ({
  id, node_id: turn, turn_node_id: turn, tool: 'update_artifact',
  kinds: ['other_user'], kind: 'memory', artifact_title: 'Memory',
  created: false, base_version: 1, current_version: 1, stale: false,
  lines: [{ type: 'add', text: `- note ${id}` }],
  title: 'Change your Memory',
});

const audioState = (extra = {}) => ({
  isPlaying: false, generatingTTS: false, cumulativeTime: 0, totalDuration: 0,
  queueFinishedCount: 0,
  pause: jest.fn(), play: jest.fn(), stop: jest.fn(),
  skipBackward: jest.fn(), skipForward: jest.fn(),
  seekToCumulativeTime: jest.fn(), chapterStartTime: () => 0,
  currentAudio: null, ...extra,
});
const reply = { title: 'Voice', chapters: [] };
const playing = () => audioState({
  currentAudio: reply, isPlaying: true, cumulativeTime: 3, totalDuration: 10,
});
const heard = () => audioState({
  currentAudio: reply, cumulativeTime: 10, totalDuration: 10, queueFinishedCount: 1,
});

let mockPending;
let mockAvailability;
let mockLlmStatus;

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
  mockMounts.mockReset();
  mockUnmounts.mockReset();
  mockSessionOptions = null;
  mockThreadParent = null;
  mockPhase = 'ready';
  mockAudio = audioState();
  mockPending = [];
  mockAvailability = { allowed: true };
  mockLlmStatus = { status: 'completed' };
  mockGet.mockImplementation((url) => {
    if (url === '/drafts/interrupted') return Promise.resolve({ data: [] });
    if (url === '/voice/availability') return Promise.resolve({ data: mockAvailability });
    if (url === '/tool-approvals') return Promise.resolve({ data: { pending: mockPending } });
    if (url.endsWith('/llm-status')) return Promise.resolve({ data: mockLlmStatus });
    return Promise.resolve({ data: {} });
  });
  mockPost.mockImplementation((url) => {
    const [, id, verb] = url.match(/\/tool-approvals\/(\d+)\/(approve|reject)/) || [];
    if (!id) return Promise.reject(new Error(`unexpected POST ${url}`));
    mockPending = mockPending.filter((p) => p.id !== Number(id));
    return Promise.resolve({
      data: { outcome: verb === 'approve' ? 'approved' : 'rejected', pending: mockPending },
    });
  });
});

test('both: a reply that waits and reaches the limit plays out, then the dialog, then the limit', async () => {
  mockPhase = 'playback';
  mockAudio = playing();
  mockThreadParent = 41;
  const { rerender } = renderAt('/voice?parent=40');
  await screen.findByRole('button', { name: 'Continue' });
  expect(mockMounts).toHaveBeenCalledTimes(1);

  // The reply is written: one change waits, and the thread is at its limit.
  mockPending = [call(7)];
  mockLlmStatus = { status: 'completed', thread_length: 'limit' };
  await act(async () => { mockSessionOptions.onLLMComplete(41, 'Noted.'); });

  // The session stays: the reply (and the announcement after it) keeps playing.
  expect(mockUnmounts).not.toHaveBeenCalled();
  expect(screen.queryByText('This thread is too long')).toBeNull();
  // Continue gives way to the limit; the waiting change keeps its line.
  await screen.findByText(THREAD_LIMIT_TEXT);
  expect(screen.queryByRole('button', { name: 'Continue' })).toBeNull();
  expect(screen.getByRole('button', { name: 'Start a new thread' })).toBeInTheDocument();
  expect(screen.getByText(/A change to your data waits for your approval/)).toBeInTheDocument();
  expect(screen.queryByRole('dialog')).toBeNull();

  // Heard to its end: the change is decided first.
  mockAudio = heard();
  rerender(tree('/voice?parent=40'));
  const dialog = await screen.findByRole('dialog');
  expect(dialog).toHaveTextContent('Change your Memory');
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: /Approve/ }));
  });
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());

  // Then the limit stays, with the way on; nothing was cut off.
  expect(screen.queryByText(/A change to your data waits/)).toBeNull();
  expect(screen.getByText(THREAD_LIMIT_TEXT)).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Back to the thread' }))
    .toHaveAttribute('href', '/node/41');
  expect(mockUnmounts).not.toHaveBeenCalled();
});

test('both, back at ready: the block with the waiting line and the dialog', async () => {
  mockPhase = 'playback';
  mockAudio = playing();
  mockThreadParent = 41;
  const { rerender } = renderAt('/voice?parent=40');
  await screen.findByRole('button', { name: 'Continue' });
  mockPending = [call(7)];
  mockLlmStatus = { status: 'completed', thread_length: 'limit' };
  await act(async () => { mockSessionOptions.onLLMComplete(41, 'Noted.'); });
  await screen.findByText(THREAD_LIMIT_TEXT);

  // The turn ends in the ready phase (e.g. its audio failed): the limit's
  // screen, and the change still to decide on it.
  mockPhase = 'ready';
  mockAudio = audioState();
  rerender(tree('/voice?parent=40'));
  expect(screen.getByText('This thread is too long')).toBeInTheDocument();
  expect(screen.getByText(/A change to your data waits for your approval/)).toBeInTheDocument();
  expect(await screen.findByRole('dialog')).toHaveTextContent('Change your Memory');
  expect(screen.queryByRole('button', { name: 'Record' })).toBeNull();
});

test('the limit alone: a reply that reaches it plays out with the limit in place of Continue', async () => {
  mockPhase = 'playback';
  mockAudio = playing();
  mockThreadParent = 41;
  renderAt('/voice?parent=40');
  await screen.findByRole('button', { name: 'Continue' });
  mockLlmStatus = { status: 'completed', thread_length: 'limit' };
  await act(async () => { mockSessionOptions.onLLMComplete(41, 'Noted.'); });

  await screen.findByText(THREAD_LIMIT_TEXT);
  expect(mockUnmounts).not.toHaveBeenCalled();
  expect(screen.queryByRole('button', { name: 'Continue' })).toBeNull();
  expect(screen.queryByText(/waits for your approval/)).toBeNull();
  expect(screen.queryByRole('dialog')).toBeNull();
});

test('the waiting change alone: Continue stays, off, with its line', async () => {
  mockPhase = 'playback';
  mockAudio = playing();
  mockThreadParent = 41;
  renderAt('/voice?parent=40');
  await screen.findByRole('button', { name: 'Continue' });
  mockPending = [call(7)];
  mockLlmStatus = { status: 'completed', thread_length: 'notice' };
  await act(async () => { mockSessionOptions.onLLMComplete(41, 'Noted.'); });

  await screen.findByText(/A change to your data waits for your approval/);
  expect(screen.getByRole('button', { name: 'Continue' })).toBeDisabled();
  expect(screen.queryByText(THREAD_LIMIT_TEXT)).toBeNull();
  expect(screen.queryByRole('button', { name: 'Start a new thread' })).toBeNull();
});

test('a refused press, change first: approval_pending opens the dialog and no limit shows', async () => {
  mockThreadParent = 41;
  renderAt('/voice?parent=41');
  await screen.findByRole('button', { name: 'Record' });

  // The server answers approval_pending (asked before the length).
  mockPending = [call(7)];
  await act(async () => { mockSessionOptions.onApprovalPending(); });
  expect(await screen.findByRole('dialog')).toHaveTextContent('Change your Memory');
  expect(screen.queryByText('This thread is too long')).toBeNull();
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: /Reject/ }));
  });
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  expect(screen.getByRole('button', { name: 'Record' })).not.toBeDisabled();

  // The next press meets the length: the limit's screen.
  act(() => { mockSessionOptions.onAiUsageRefused('too_long'); });
  expect(screen.getByText('This thread is too long')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Start a new thread' })).toBeInTheDocument();
});

test('opened on a thread already at the limit: the limit screen, the change is decided on the thread', async () => {
  mockPending = [call(7)];
  mockAvailability = { allowed: false, code: 'thread_too_long', error: THREAD_LIMIT_TEXT };
  renderAt('/voice?parent=41');

  expect(await screen.findByText('This thread is too long')).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Back to the thread' }))
    .toHaveAttribute('href', '/node/41');
  expect(mockMounts).not.toHaveBeenCalled();
});

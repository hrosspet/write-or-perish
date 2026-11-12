// Voice mode when the AI's changes to the user's data wait for approval
// (Peter, 2026-11-12): "The dialog could appear on the Voice screen
// itself, right after the reply" - "yes, I'd like that. [...] Also, the
// lockscreen controls shouldn't allow next recording". The dialog opens on
// the Voice screen once the reply has been heard (its audio ends saying a
// change waits), it has no "Later", and no new turn starts until every
// waiting call of the thread is approved or rejected.
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
const mockHandleContinue = jest.fn();
const mockHandleStart = jest.fn();
jest.mock('../hooks/useVoiceSession', () => ({
  useVoiceSession: (options) => {
    mockSessionOptions = options;
    return {
      phase: mockPhase, isStopping: false, hasError: false, isOnline: true,
      streaming: { isPaused: false, isInterrupted: false, duration: 0 },
      audio: mockAudio,
      handleStart: mockHandleStart, handleStop: jest.fn(),
      handleContinue: mockHandleContinue, handleResumeSession: jest.fn(),
      handleCancelProcessing: jest.fn(), setThreadParentId: jest.fn(),
      handleResumeRecording: jest.fn(),
      getThreadParentId: () => mockThreadParent,
    };
  },
}));
jest.mock('../hooks/useAsyncTaskPolling', () => ({
  useAsyncTaskPolling: () => ({ status: null, data: null, error: null }),
}));
jest.mock('../components/OfflineBanner', () => () => null);
jest.mock('../components/ProposalInline', () => () => null);

import React from 'react';
import { render, screen, act, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter, Routes, Route, useParams } from 'react-router-dom';
import VoicePage from './VoicePage';
import { VOICE_WAITS_TEXT } from '../components/ToolApprovalDialog';

function NodeStub() {
  const { id } = useParams();
  return <div>{`Thread page ${id}`}</div>;
}

const tree = (path) => (
  <MemoryRouter initialEntries={[path]}>
    <Routes>
      <Route path="/voice" element={<VoicePage />} />
      <Route path="/node/:id" element={<NodeStub />} />
    </Routes>
  </MemoryRouter>
);

const renderAt = (path) => {
  mockUserCtx = {
    user: { username: 'ben', default_ai_usage: 'chat' },
    markHasOwnEntries: jest.fn(),
  };
  return render(tree(path));
};

// One waiting call as GET /tool-approvals lists it.
const call = (id, turn = 41) => ({
  id, node_id: turn, turn_node_id: turn, tool: 'update_artifact',
  kinds: ['other_user'], kind: 'memory', artifact_title: 'Memory',
  created: false, base_version: 1, current_version: 1, stale: false,
  lines: [{ type: 'add', text: `- note ${id}` }],
  title: 'Change your Memory',
});

let mockPending;
const audioState = (extra = {}) => ({
  isPlaying: false, generatingTTS: false, cumulativeTime: 0, totalDuration: 0,
  pause: jest.fn(), play: jest.fn(), stop: jest.fn(),
  skipBackward: jest.fn(), skipForward: jest.fn(),
  seekToCumulativeTime: jest.fn(), chapterStartTime: () => 0,
  currentAudio: null, ...extra,
});
const playing = () => audioState({ isPlaying: true, cumulativeTime: 3, totalDuration: 10 });
const heard = () => audioState({ cumulativeTime: 10, totalDuration: 10 });
const pausedPartway = () => audioState({ cumulativeTime: 4, totalDuration: 10 });

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
  mockHandleContinue.mockReset();
  mockHandleStart.mockReset();
  mockSessionOptions = null;
  mockThreadParent = null;
  mockPhase = 'ready';
  mockAudio = audioState();
  mockPending = [];
  mockGet.mockImplementation((url) => {
    if (url === '/drafts/interrupted') return Promise.resolve({ data: [] });
    if (url === '/voice/availability') return Promise.resolve({ data: { allowed: true } });
    if (url === '/tool-approvals') return Promise.resolve({ data: { pending: mockPending } });
    return Promise.resolve({ data: {} });
  });
  mockPost.mockImplementation((url) => {
    const [, id, verb] = url.match(/\/tool-approvals\/(\d+)\/(approve|reject)/) || [];
    if (!id) return Promise.reject(new Error(`unexpected POST ${url}`));
    const decided = mockPending.find((p) => p.id === Number(id));
    mockPending = mockPending.filter((p) => p.id !== Number(id));
    return Promise.resolve({
      data: {
        outcome: verb === 'approve' ? 'approved' : 'rejected',
        pending: mockPending.filter((p) => p.turn_node_id === decided.turn_node_id),
      },
    });
  });
});

const approvalsAsked = () => mockGet.mock.calls.filter(([url]) => url === '/tool-approvals');

test('opened on a thread where a change waits: the dialog, no Later, recording off', async () => {
  mockPending = [call(7)];
  renderAt('/voice?parent=41');

  const dialog = await screen.findByRole('dialog');
  expect(approvalsAsked()[0][1]).toEqual({ params: { node: 41 } });
  expect(dialog).toHaveTextContent('Change your Memory');
  expect(dialog).toHaveTextContent(VOICE_WAITS_TEXT);
  expect(screen.queryByRole('button', { name: 'Later' })).toBeNull();
  expect(screen.getByRole('button', { name: 'Open the thread' })).toBeInTheDocument();
  // Escape decides nothing and doesn't close it here.
  fireEvent.keyDown(window, { key: 'Escape' });
  expect(screen.getByRole('dialog')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Record' })).toBeDisabled();
});

test('after a reply: the dialog waits until the reply has been heard', async () => {
  mockPhase = 'playback';
  mockAudio = playing();
  mockThreadParent = 41;
  const { rerender } = renderAt('/voice?parent=40');
  await waitFor(() => expect(approvalsAsked()).toHaveLength(1));

  mockPending = [call(7)];
  await act(async () => { mockSessionOptions.onLLMComplete(41, 'I asked first.'); });

  // The reply (and its spoken sentence) still plays: no dialog yet, but
  // the next turn can't start, and the line says why.
  expect(approvalsAsked()[1][1]).toEqual({ params: { node: 41 } });
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(screen.getByRole('button', { name: 'Continue' })).toBeDisabled();
  expect(screen.getByText(/A change to your data waits for your approval/)).toBeInTheDocument();

  mockAudio = heard();
  rerender(tree('/voice?parent=40'));
  expect(await screen.findByRole('dialog')).toHaveTextContent('Change your Memory');
});

test('a reply paused partway: no dialog until the line is pressed', async () => {
  mockPhase = 'playback';
  mockAudio = pausedPartway();
  mockPending = [call(7)];
  renderAt('/voice?parent=41');
  const line = await screen.findByText(/A change to your data waits for your approval/);
  expect(screen.queryByRole('dialog')).toBeNull();

  fireEvent.click(line);
  expect(screen.getByRole('dialog')).toBeInTheDocument();
});

test('recording works again once every waiting call is decided', async () => {
  mockPending = [call(7), call(8)];
  renderAt('/voice?parent=41');
  await screen.findByRole('dialog');
  expect(screen.getByText(/2 changes to your data wait for your approval/)).toBeInTheDocument();

  const approve = screen.getAllByRole('button', { name: 'Approve' })[0];
  await act(async () => { fireEvent.click(approve); });
  expect(mockPost).toHaveBeenCalledWith('/tool-approvals/7/approve');
  // One still waits: still in the dialog, recording still off.
  expect(screen.getByRole('dialog')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Record' })).toBeDisabled();

  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Reject' }));
  });
  expect(mockPost).toHaveBeenCalledWith('/tool-approvals/8/reject');
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  const record = screen.getByRole('button', { name: 'Record' });
  expect(record).toBeEnabled();
  expect(screen.queryByText(/wait for your approval/)).toBeNull();
  fireEvent.click(record);
  expect(mockHandleStart).toHaveBeenCalled();
});

test('the next reply in a thread with only your text: nothing shows', async () => {
  mockPhase = 'playback';
  mockAudio = heard();
  renderAt('/voice?parent=40');
  await waitFor(() => expect(approvalsAsked()).toHaveLength(1));
  await act(async () => { mockSessionOptions.onLLMComplete(41, 'Noted.'); });

  expect(approvalsAsked()).toHaveLength(2);
  expect(screen.queryByRole('dialog')).toBeNull();
  const cont = screen.getByRole('button', { name: 'Continue' });
  expect(cont).toBeEnabled();
  fireEvent.click(cont);
  expect(mockHandleContinue).toHaveBeenCalled();
});

test('a recording the server refused shows the waiting change at once', async () => {
  mockThreadParent = 41;
  renderAt('/voice?parent=41');
  await waitFor(() => expect(approvalsAsked()).toHaveLength(1));
  expect(screen.queryByRole('dialog')).toBeNull();

  mockPending = [call(9)];
  await act(async () => { mockSessionOptions.onApprovalPending(); });

  expect(approvalsAsked()[1][1]).toEqual({ params: { node: 41 } });
  expect(await screen.findByRole('dialog')).toHaveTextContent('Change your Memory');
});

test('"Open the thread" leaves for the thread page', async () => {
  mockThreadParent = 41;
  mockPending = [call(7)];
  renderAt('/voice?parent=41');
  await screen.findByRole('dialog');

  fireEvent.click(screen.getByRole('button', { name: 'Open the thread' }));
  expect(screen.getByText('Thread page 41')).toBeInTheDocument();
});

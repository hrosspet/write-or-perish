// Voice mode records only where a reply may follow (2026-10-01). When AI
// usage keeps the account (a fresh thread) or the thread Voice continues
// away from AI, the Voice screen explains instead of offering the record
// button, and says where to change it.
let mockUserCtx;
jest.mock('../contexts/UserContext', () => ({
  useUser: () => mockUserCtx,
}));
jest.mock('../contexts/ToastContext', () => ({
  useToast: () => ({ addToast: jest.fn(), removeToast: jest.fn() }),
}));
const mockGet = jest.fn();
const mockDelete = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    get: (...args) => mockGet(...args),
    post: jest.fn(),
    delete: (...args) => mockDelete(...args),
  },
}));
let mockSessionOptions;
let mockPhase;
const mockUseVoiceSession = jest.fn();
const mockHandleStart = jest.fn();
const mockHandleResumeSession = jest.fn();
jest.mock('../hooks/useVoiceSession', () => ({
  useVoiceSession: (options) => {
    mockSessionOptions = options;
    mockUseVoiceSession(options);
    return {
      phase: mockPhase, isStopping: false, hasError: false, isOnline: true,
      streaming: { isPaused: false, isInterrupted: false, duration: 0 },
      audio: { isPlaying: false, pause: jest.fn(), play: jest.fn() },
      handleStart: mockHandleStart, handleStop: jest.fn(),
      handleContinue: jest.fn(), handleResumeSession: mockHandleResumeSession,
      handleCancelProcessing: jest.fn(), setThreadParentId: jest.fn(),
      handleResumeRecording: jest.fn(),
    };
  },
}));
jest.mock('../components/OfflineBanner', () => () => null);
jest.mock('../components/ProposalInline', () => () => null);

import React from 'react';
import { render, screen, act, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import VoicePage, { VOICE_NEEDS_AI_TEXT, FINISH_WITHOUT_REPLY_TEXT } from './VoicePage';

// GET answers by URL: the interrupted-recording check (useInterruptedRecovery)
// and /voice/availability (keyed by its parent param, '' for none).
const answerGets = ({ interrupted = [], availability = {} } = {}) => {
  mockGet.mockImplementation((url, config) => {
    if (url === '/drafts/interrupted') {
      return Promise.resolve({ data: interrupted });
    }
    if (url === '/voice/availability') {
      const parent = config?.params?.parent;
      const key = parent == null ? '' : String(parent);
      return Promise.resolve({ data: availability[key] || { allowed: true } });
    }
    return Promise.reject(new Error(`unexpected GET ${url}`));
  });
};

const renderAt = (path, user) => {
  mockUserCtx = { user: { username: 'alice', ...user } };
  return render(
    <MemoryRouter initialEntries={[path]}>
      <VoicePage />
    </MemoryRouter>,
  );
};

beforeEach(() => {
  mockGet.mockReset();
  mockDelete.mockReset();
  mockDelete.mockResolvedValue({ data: {} });
  mockUseVoiceSession.mockReset();
  mockHandleStart.mockReset();
  mockHandleResumeSession.mockReset();
  mockSessionOptions = null;
  mockPhase = 'ready';
  answerGets();
});

test('a fresh thread on an account set to None explains instead of recording', async () => {
  renderAt('/voice', { default_ai_usage: 'none' });

  expect(await screen.findByText('Voice mode needs AI')).toBeInTheDocument();
  expect(screen.getByText(VOICE_NEEDS_AI_TEXT.account)).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Account settings' }))
    .toHaveAttribute('href', '/account#ai-usage');
  expect(screen.queryByText("What's on your mind?")).not.toBeInTheDocument();
  expect(mockUseVoiceSession).not.toHaveBeenCalled();
  // Only the interrupted-recording check: the account decides here.
  expect(mockGet).toHaveBeenCalledTimes(1);
  expect(mockGet).toHaveBeenCalledWith('/drafts/interrupted');
});

test('a fresh thread on a chat account records', async () => {
  renderAt('/voice', { default_ai_usage: 'chat' });

  expect(await screen.findByText("What's on your mind?")).toBeInTheDocument();
  expect(screen.queryByText('Voice mode needs AI')).not.toBeInTheDocument();
});

test('a thread set to None explains, with the way back to it', async () => {
  answerGets({ availability: { 42: {
    allowed: false, code: 'ai_usage_none', scope: 'thread', error: 'x',
  } } });
  renderAt('/voice?parent=42', { default_ai_usage: 'chat' });

  expect(await screen.findByText(VOICE_NEEDS_AI_TEXT.thread)).toBeInTheDocument();
  expect(mockGet).toHaveBeenCalledWith('/voice/availability',
    { params: { parent: '42' } });
  expect(screen.getByRole('link', { name: 'Back to the thread' }))
    .toHaveAttribute('href', '/node/42');
  expect(screen.getByRole('link', { name: 'Account settings' }))
    .toHaveAttribute('href', '/account#ai-usage');
  expect(mockUseVoiceSession).not.toHaveBeenCalled();
});

test('the thread decides when continuing, not the account', async () => {
  answerGets({ availability: { 7: { allowed: true } } });
  renderAt('/voice?resume=7&parent=7', { default_ai_usage: 'none' });

  expect(await screen.findByText("What's on your mind?")).toBeInTheDocument();
  expect(mockGet).toHaveBeenCalledWith('/voice/availability',
    { params: { parent: '7' } });
});

test('nothing is recorded or offered while the server is asked', () => {
  mockGet.mockReturnValue(new Promise(() => {}));
  renderAt('/voice?parent=42', { default_ai_usage: 'chat' });

  expect(screen.queryByText("What's on your mind?")).not.toBeInTheDocument();
  expect(screen.queryByText('Voice mode needs AI')).not.toBeInTheDocument();
  expect(mockUseVoiceSession).not.toHaveBeenCalled();
});

test('when the server cannot be asked, recording is offered', async () => {
  // The server still refuses a turn it may not reply to (init's 403).
  mockGet.mockRejectedValue(new Error('Network Error'));
  renderAt('/voice?parent=42', { default_ai_usage: 'chat' });

  expect(await screen.findByText("What's on your mind?")).toBeInTheDocument();
});

test("a refusal when recording starts brings the explanation", async () => {
  renderAt('/voice', { default_ai_usage: 'chat' });
  expect(await screen.findByText("What's on your mind?")).toBeInTheDocument();

  act(() => { mockSessionOptions.onAiUsageRefused('account'); });

  expect(screen.getByText(VOICE_NEEDS_AI_TEXT.account)).toBeInTheDocument();
  expect(screen.queryByText("What's on your mind?")).not.toBeInTheDocument();
});

// An interrupted recording (a reload or a closed tab mid-recording, in Voice
// or Write-page dictation) can be finished or discarded only on this page,
// so it is offered where Voice mode is blocked too, before the block.
const INTERRUPTED = {
  id: 5, session_id: 'sess-1', parent_id: null, label: 'Voice',
  content: '', chunk_count: 3, has_stored_chunks: true,
  streaming_mime_type: 'audio/webm',
};

test('where Voice mode is blocked, an interrupted recording is offered first', async () => {
  answerGets({
    interrupted: [INTERRUPTED],
    availability: { '': { allowed: false, scope: 'account', code: 'ai_usage_none' } },
  });
  renderAt('/voice', { default_ai_usage: 'none' });

  expect(await screen.findByText('Unfinished Voice recording')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Continue recording' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Discard' })).toBeInTheDocument();
  // What Continue leads to, asked for the thread the recording continues
  // (none here: the account decides).
  expect(await screen.findByText(FINISH_WITHOUT_REPLY_TEXT)).toBeInTheDocument();
  expect(mockGet).toHaveBeenCalledWith('/voice/availability', {});
  expect(screen.queryByText('Voice mode needs AI')).not.toBeInTheDocument();
  // No ?resume= reply is played, nothing records.
  expect(mockSessionOptions.initialLlmNodeId).toBeNull();
  expect(mockHandleStart).not.toHaveBeenCalled();
  expect(mockHandleResumeSession).not.toHaveBeenCalled();
});

test('discarding it deletes the recording and leaves the explanation', async () => {
  answerGets({
    interrupted: [INTERRUPTED],
    availability: { '': { allowed: false, scope: 'account', code: 'ai_usage_none' } },
  });
  renderAt('/voice', { default_ai_usage: 'none' });

  fireEvent.click(await screen.findByRole('button', { name: 'Discard' }));

  expect(await screen.findByText(VOICE_NEEDS_AI_TEXT.account)).toBeInTheDocument();
  expect(mockDelete).toHaveBeenCalledWith('/drafts/streaming/sess-1/discard');
  expect(screen.queryByText('Unfinished Voice recording')).not.toBeInTheDocument();
  expect(mockHandleResumeSession).not.toHaveBeenCalled();
  expect(mockHandleStart).not.toHaveBeenCalled();
});

test('continuing finishes that recording; then the page explains, with no record button', async () => {
  answerGets({
    interrupted: [{ ...INTERRUPTED, parent_id: 42 }],
    availability: { 42: { allowed: false, scope: 'thread', code: 'ai_usage_none' } },
  });
  mockHandleResumeSession.mockImplementation(() => { mockPhase = 'recording'; });
  const { rerender } = renderAt('/voice?parent=42', { default_ai_usage: 'chat' });

  expect(await screen.findByText(FINISH_WITHOUT_REPLY_TEXT)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Continue recording' }));

  // The interrupted session is resumed: no fresh turn is started.
  expect(mockHandleResumeSession).toHaveBeenCalledWith({
    sessionId: 'sess-1', draftId: 5, chunkCount: 3, parentId: 42,
    mimeType: 'audio/webm',
  });
  expect(mockHandleStart).not.toHaveBeenCalled();
  expect(await screen.findByText("What's on your mind?")).toBeInTheDocument();

  // Finalize saved it as an entry without a reply: back to ready, where
  // the block stands instead of the record button.
  mockPhase = 'ready';
  rerender(
    <MemoryRouter initialEntries={['/voice?parent=42']}>
      <VoicePage />
    </MemoryRouter>,
  );
  expect(await screen.findByText(VOICE_NEEDS_AI_TEXT.thread)).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Back to the thread' }))
    .toHaveAttribute('href', '/node/42');
  expect(screen.queryByText("What's on your mind?")).not.toBeInTheDocument();
  expect(mockHandleStart).not.toHaveBeenCalled();
});

test('where the recording continues a thread that lets AI reply, no note is added', async () => {
  answerGets({
    interrupted: [{ ...INTERRUPTED, parent_id: 9 }],
    availability: { 9: { allowed: true } },
  });
  renderAt('/voice', { default_ai_usage: 'none' });

  expect(await screen.findByText('Unfinished Voice recording')).toBeInTheDocument();
  await waitFor(() => expect(mockGet).toHaveBeenCalledWith(
    '/voice/availability', { params: { parent: 9 } }));
  expect(screen.queryByText(FINISH_WITHOUT_REPLY_TEXT)).not.toBeInTheDocument();
});

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
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    get: (...args) => mockGet(...args),
    post: jest.fn(),
    delete: jest.fn(),
  },
}));
jest.mock('../hooks/useInterruptedRecovery', () => ({
  useInterruptedRecovery: () => ({
    interruptedDraft: null, checked: true,
    handleDiscard: jest.fn(), clearInterrupted: jest.fn(),
  }),
}));
let mockSessionOptions;
const mockUseVoiceSession = jest.fn();
jest.mock('../hooks/useVoiceSession', () => ({
  useVoiceSession: (options) => {
    mockSessionOptions = options;
    mockUseVoiceSession(options);
    return {
      phase: 'ready', isStopping: false, hasError: false, isOnline: true,
      streaming: { isPaused: false, isInterrupted: false, duration: 0 },
      audio: { isPlaying: false, pause: jest.fn(), play: jest.fn() },
      handleStart: jest.fn(), handleStop: jest.fn(),
      handleContinue: jest.fn(), handleResumeSession: jest.fn(),
      handleCancelProcessing: jest.fn(), setThreadParentId: jest.fn(),
      handleResumeRecording: jest.fn(),
    };
  },
}));
jest.mock('../components/OfflineBanner', () => () => null);
jest.mock('../components/RecoveryBanner', () => () => null);
jest.mock('../components/ProposalInline', () => () => null);

import React from 'react';
import { render, screen, act } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import VoicePage, { VOICE_NEEDS_AI_TEXT } from './VoicePage';

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
  mockUseVoiceSession.mockReset();
  mockSessionOptions = null;
});

test('a fresh thread on an account set to None explains instead of recording', () => {
  renderAt('/voice', { default_ai_usage: 'none' });

  expect(screen.getByText('Voice mode needs AI')).toBeInTheDocument();
  expect(screen.getByText(VOICE_NEEDS_AI_TEXT.account)).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Account settings' }))
    .toHaveAttribute('href', '/account#ai-usage');
  expect(screen.queryByText("What's on your mind?")).not.toBeInTheDocument();
  expect(mockUseVoiceSession).not.toHaveBeenCalled();
  expect(mockGet).not.toHaveBeenCalled();
});

test('a fresh thread on a chat account records', () => {
  renderAt('/voice', { default_ai_usage: 'chat' });

  expect(screen.getByText("What's on your mind?")).toBeInTheDocument();
  expect(screen.queryByText('Voice mode needs AI')).not.toBeInTheDocument();
});

test('a thread set to None explains, with the way back to it', async () => {
  mockGet.mockResolvedValue({ data: {
    allowed: false, code: 'ai_usage_none', scope: 'thread', error: 'x',
  } });
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
  mockGet.mockResolvedValue({ data: { allowed: true } });
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

test("a refusal when recording starts brings the explanation", () => {
  renderAt('/voice', { default_ai_usage: 'chat' });
  expect(screen.getByText("What's on your mind?")).toBeInTheDocument();

  act(() => { mockSessionOptions.onAiUsageRefused('account'); });

  expect(screen.getByText(VOICE_NEEDS_AI_TEXT.account)).toBeInTheDocument();
  expect(screen.queryByText("What's on your mind?")).not.toBeInTheDocument();
});

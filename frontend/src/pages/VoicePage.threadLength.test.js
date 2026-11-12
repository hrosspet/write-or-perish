// #648: Voice mode on a thread too long for another reply shows the limit
// in place of the record button, with a new thread as the way on; near the
// limit a calm line says the thread is getting long.
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
    delete: jest.fn(() => Promise.resolve({ data: {} })),
  },
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
      handleResumeRecording: jest.fn(), getThreadParentId: () => 42,
    };
  },
}));
jest.mock('../components/OfflineBanner', () => () => null);
jest.mock('../components/ProposalInline', () => () => null);

import React from 'react';
import { render, screen, act, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import VoicePage from './VoicePage';
import { THREAD_LIMIT_TEXT, THREAD_NOTICE_TEXT } from '../utils/threadLength';

const answerGets = (availability = {}) => {
  mockGet.mockImplementation((url, config) => {
    if (url === '/drafts/interrupted') return Promise.resolve({ data: [] });
    if (url === '/voice/availability') {
      const parent = config?.params?.parent;
      const key = parent == null ? '' : String(parent);
      return Promise.resolve({ data: availability[key] || { allowed: true } });
    }
    return Promise.reject(new Error(`unexpected GET ${url}`));
  });
};

const renderAt = (path) => {
  mockUserCtx = {
    user: { username: 'alice', default_ai_usage: 'chat', has_own_entries: true },
    markHasOwnEntries: jest.fn(),
  };
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
  answerGets();
});

test('a thread at the limit shows why, a new thread, and the way back', async () => {
  answerGets({ 42: { allowed: false, code: 'thread_too_long', error: 'x' } });
  renderAt('/voice?parent=42');

  expect(await screen.findByText('This thread is too long')).toBeInTheDocument();
  expect(screen.getByText(THREAD_LIMIT_TEXT)).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Back to the thread' }))
    .toHaveAttribute('href', '/node/42');
  expect(mockUseVoiceSession).not.toHaveBeenCalled();

  // A new thread: a fresh Voice session, without the old parent.
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Start a new thread' }));
  });
  expect(screen.queryByText('This thread is too long')).not.toBeInTheDocument();
  expect(mockUseVoiceSession).toHaveBeenCalled();
  const last = mockUseVoiceSession.mock.calls.at(-1)[0];
  expect(last.initialParentId).toBeNull();
});

test('near the limit the screen records and says the thread is getting long', async () => {
  answerGets({ 42: { allowed: true, thread_length: 'notice' } });
  renderAt('/voice?parent=42');

  expect(await screen.findByText(THREAD_NOTICE_TEXT)).toBeInTheDocument();
  expect(mockUseVoiceSession).toHaveBeenCalled();
});

test('a turn refused for the length in a session shows the limit', async () => {
  renderAt('/voice?parent=42');
  await screen.findByText("What's on your mind?");

  act(() => { mockSessionOptions.onAiUsageRefused('too_long'); });

  expect(screen.getByText('This thread is too long')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Start a new thread' })).toBeInTheDocument();
});

// The first cohort: /cohort opened the interview in Voice on its pending
// reply (?resume=), and that reply failed. The page goes back to /cohort,
// which says what happened and offers Try again. A failed reply anywhere
// else stays on Voice.
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
jest.mock('../hooks/useVoiceSession', () => ({
  useVoiceSession: (options) => {
    mockSessionOptions = options;
    return {
      phase: 'processing', isStopping: false, hasError: false, isOnline: true,
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
jest.mock('../components/ProposalInline', () => () => null);

import React from 'react';
import { render, screen, act } from '@testing-library/react';
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom';
import VoicePage from './VoicePage';
import { markInterviewOpen } from '../utils/cohort';

function Where() {
  const loc = useLocation();
  return <div data-testid="where">{loc.pathname}</div>;
}

beforeEach(() => {
  window.sessionStorage.clear();
  mockSessionOptions = null;
  mockUserCtx = { user: { username: 'mara', default_ai_usage: 'chat' }, markHasOwnEntries: jest.fn() };
  mockGet.mockImplementation((url) => {
    if (url === '/drafts/interrupted') return Promise.resolve({ data: [] });
    if (url === '/voice/availability') return Promise.resolve({ data: { allowed: true } });
    return Promise.resolve({ data: {} });
  });
});

const renderAt = async (path) => {
  await act(async () => {
    render(
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/voice" element={<><VoicePage /><Where /></>} />
          <Route path="/cohort" element={<Where />} />
        </Routes>
      </MemoryRouter>,
    );
  });
};

test('the interview\'s opening failed in Voice: back to /cohort', async () => {
  markInterviewOpen('First Eight');
  await renderAt('/voice?resume=15');
  expect(mockSessionOptions).not.toBeNull();
  act(() => { mockSessionOptions.onLLMFailed(15); });
  expect(screen.getByTestId('where')).toHaveTextContent('/cohort');
});

test('a failed reply in Voice that /cohort did not open stays on Voice', async () => {
  await renderAt('/voice?resume=15');
  act(() => { mockSessionOptions.onLLMFailed(15); });
  expect(screen.getByTestId('where')).toHaveTextContent('/voice');
});

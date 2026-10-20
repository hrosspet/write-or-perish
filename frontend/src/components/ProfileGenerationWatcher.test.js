// The hook's real implementation pulls in api/axios; control its output
// directly so each render can set exactly the {data, error} pair under test.
let mockPolling = { data: null, error: null };
jest.mock('../hooks/useAsyncTaskPolling', () => ({
  useAsyncTaskPolling: () => mockPolling,
}));
const mockAddToast = jest.fn();
jest.mock('../contexts/ToastContext', () => ({
  useToast: () => ({ addToast: mockAddToast }),
}));
const mockSetUser = jest.fn();
jest.mock('../contexts/UserContext', () => ({
  useUser: () => ({ user: { profile_batch_pending: true }, setUser: mockSetUser }),
}));

import React from 'react';
import { render, act } from '@testing-library/react';
import ProfileGenerationWatcher from './ProfileGenerationWatcher';

const running = {
  running: true, status: 'running', source: 'batch',
  message: 'Chunk 2 of ~5', latest_profile: { id: 1 },
};

beforeEach(() => {
  mockPolling = { data: null, error: null };
  mockAddToast.mockClear();
  mockSetUser.mockClear();
});

// #258 re-review: the hook keeps the last good `data` when a request
// fails, so a poller that answered once and then gave up after N errors
// still holds `data`. The clean-up must run anyway, or the label pulses
// "Chunk n of ~N" forever with nothing updating it.
test('clears the indicator when polling gives up while stale data is held', () => {
  const done = jest.fn();
  window.addEventListener('loore_profile_done', done);
  mockPolling = { data: running, error: null };
  const { rerender } = render(<ProfileGenerationWatcher />);
  expect(done).not.toHaveBeenCalled();

  mockPolling = { data: running, error: 'Polling stopped after 10 consecutive errors' };
  act(() => { rerender(<ProfileGenerationWatcher />); });

  expect(done).toHaveBeenCalledTimes(1);
  expect(mockAddToast).not.toHaveBeenCalled(); // quiet stop
  // The cached running flags are dropped so the build is not re-adopted.
  const update = mockSetUser.mock.calls[0][0]({ profile_batch_pending: true });
  expect(update.profile_batch_pending).toBe(false);
  window.removeEventListener('loore_profile_done', done);
});

// A `failed` answer also sets the hook's `error`; that outcome belongs to
// the data effect alone (one toast, one done event).
test('a failed answer is handled once, by the data effect', () => {
  const done = jest.fn();
  window.addEventListener('loore_profile_done', done);
  mockPolling = { data: running, error: null };
  const { rerender } = render(<ProfileGenerationWatcher />);

  mockPolling = {
    data: { running: false, status: 'failed', latest_profile: { id: 1 } },
    error: 'Task failed',
  };
  act(() => { rerender(<ProfileGenerationWatcher />); });

  expect(done).toHaveBeenCalledTimes(1);
  expect(mockAddToast).toHaveBeenCalledTimes(1);
  expect(mockAddToast).toHaveBeenCalledWith('Profile generation failed');
  window.removeEventListener('loore_profile_done', done);
});

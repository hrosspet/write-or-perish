// The hook's real implementation pulls in api/axios; control its output
// directly so each render can set exactly the {data, error} pair under test.
let mockPolling = { data: null, error: null };
// The endpoint the watcher asked the hook to poll, per render (null: none).
let mockEndpoints = [];
jest.mock('../hooks/useAsyncTaskPolling', () => ({
  useAsyncTaskPolling: (endpoint) => {
    mockEndpoints.push(endpoint);
    return mockPolling;
  },
}));
const mockAddToast = jest.fn();
jest.mock('../contexts/ToastContext', () => ({
  useToast: () => ({ addToast: mockAddToast }),
}));
const mockSetUser = jest.fn();
let mockUser = { profile_batch_pending: true };
jest.mock('../contexts/UserContext', () => ({
  useUser: () => ({ user: mockUser, setUser: mockSetUser }),
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
  mockEndpoints = [];
  mockUser = { profile_batch_pending: true };
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

// #731: a pre-fill load's final merge that goes on after the person was
// let in is followed like a build, and its version gets the usual notice.
const mergeRunning = {
  running: true, status: 'progress', source: 'merge', merge_id: 7,
  message: 'Integrating profile versions', latest_profile: { id: 1 },
};

// Rendered with the payload's flag; the first answer is a running merge.
const followMerge = () => {
  mockUser = { profile_merge_pending: true };
  const view = render(<ProfileGenerationWatcher />);
  mockPolling = { data: { ...mergeRunning }, error: null };
  act(() => { view.rerender(<ProfileGenerationWatcher />); });
  return view;
};

test('a late merge that saves its version gives the usual notice', () => {
  const progress = [];
  const onProgress = (e) => progress.push(e.detail);
  window.addEventListener('loore_profile_progress', onProgress);
  const { rerender } = followMerge();
  // Started by the payload's flag; once it saw the merge, it asks for it.
  expect(mockEndpoints).toContain('/export/profile-progress');
  expect(mockEndpoints[mockEndpoints.length - 1])
    .toBe('/export/profile-progress?merge_id=7');
  expect(progress[0]).toMatchObject({
    running: true, source: 'merge', message: 'Integrating profile versions',
  });

  mockPolling = {
    data: { running: false, status: 'completed', latest_profile: { id: 2 } },
    error: null,
  };
  act(() => { rerender(<ProfileGenerationWatcher />); });

  expect(mockAddToast).toHaveBeenCalledTimes(1);
  expect(mockAddToast).toHaveBeenCalledWith('Your profile has been updated ✓', 6000);
  const update = mockSetUser.mock.calls[0][0]({ profile_merge_pending: true });
  expect(update.profile_merge_pending).toBe(false);
  window.removeEventListener('loore_profile_progress', onProgress);
});

test('a late merge that ends without a version says nothing', () => {
  const done = jest.fn();
  window.addEventListener('loore_profile_done', done);
  const { rerender } = followMerge();

  // Even when another version landed meanwhile (the person's own edit
  // won over the merge): the endpoint's `unchanged` decides.
  mockPolling = {
    data: { running: false, status: 'unchanged', latest_profile: { id: 2 } },
    error: null,
  };
  act(() => { rerender(<ProfileGenerationWatcher />); });

  expect(done).toHaveBeenCalledTimes(1);
  expect(mockAddToast).not.toHaveBeenCalled();
  window.removeEventListener('loore_profile_done', done);
});

// A batch build saves its version while a late merge still runs, and the
// merge then ends without one: the build's notice still comes, as it does
// when no merge runs.
test('a batch build seen before an unchanged merge keeps its notice', () => {
  mockUser = { profile_batch_pending: true };
  const { rerender } = render(<ProfileGenerationWatcher />);
  mockPolling = {
    data: { running: true, status: 'progress', source: 'batch',
      message: 'Integrating profile versions', latest_profile: { id: 1 } },
    error: null,
  };
  act(() => { rerender(<ProfileGenerationWatcher />); });
  mockPolling = { data: { ...mergeRunning, latest_profile: { id: 2 } }, error: null };
  act(() => { rerender(<ProfileGenerationWatcher />); });
  mockPolling = {
    data: { running: false, status: 'unchanged', latest_profile: { id: 2 } },
    error: null,
  };
  act(() => { rerender(<ProfileGenerationWatcher />); });

  expect(mockAddToast).toHaveBeenCalledTimes(1);
  expect(mockAddToast).toHaveBeenCalledWith('Your profile has been updated ✓', 6000);
});

// As after a usual build: an app opened once the merge has ended gets a
// payload with no running flag, so nothing is polled and no notice comes.
test('no running flag in the payload: nothing is followed', () => {
  mockUser = {
    profile_generation_task_id: null,
    profile_batch_pending: false,
    profile_merge_pending: false,
  };
  render(<ProfileGenerationWatcher />);
  expect(mockEndpoints.every((e) => e === null)).toBe(true);
  expect(mockAddToast).not.toHaveBeenCalled();
});

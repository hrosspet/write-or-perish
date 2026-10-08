// useAsyncTaskPolling's reportVisible option (Read tracking, 2026-10-02):
// the server counts a finished Read reply as opened only for a poll sent
// while the tab is visible, so a page left in a background tab never
// counts. A reply that finished while the tab was hidden is counted by one
// extra poll when the tab is shown.
const mockGet = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: { get: (...args) => mockGet(...args) },
}));

import { renderHook, act } from '@testing-library/react';
import { useAsyncTaskPolling } from './useAsyncTaskPolling';

const ENDPOINT = '/nodes/7/llm-status';

let visibility;
const setVisibility = (state) => {
  visibility = state;
  document.dispatchEvent(new Event('visibilitychange'));
};

// Resolve queued promises (the poll awaits the request) inside act().
const flush = () => act(async () => { await Promise.resolve(); });

const optionsOf = (call) => call[1];

beforeEach(() => {
  jest.useFakeTimers();
  mockGet.mockReset();
  visibility = 'visible';
  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    get: () => visibility,
  });
});

afterEach(() => {
  jest.useRealTimers();
});

const render = (options) => renderHook(
  () => useAsyncTaskPolling(ENDPOINT, {
    enabled: true, interval: 1000, ...options,
  }),
);

test('polls carry no visibility parameter unless asked', async () => {
  mockGet.mockResolvedValue({ data: { status: 'processing' } });
  render({});
  await flush();

  expect(mockGet).toHaveBeenCalledTimes(1);
  expect(optionsOf(mockGet.mock.calls[0])).not.toHaveProperty('params');
});

test('a visible tab sends visible=1 with every poll', async () => {
  mockGet.mockResolvedValue({ data: { status: 'processing' } });
  render({ reportVisible: true });
  await flush();
  await act(async () => { jest.advanceTimersByTime(1000); });

  expect(mockGet).toHaveBeenCalledTimes(2);
  for (const call of mockGet.mock.calls) {
    expect(call[0]).toBe(ENDPOINT);
    expect(optionsOf(call).params).toEqual({ visible: 1 });
  }
});

test('a hidden tab polls without the flag', async () => {
  visibility = 'hidden';
  mockGet.mockResolvedValue({ data: { status: 'processing' } });
  render({ reportVisible: true });
  await flush();
  await act(async () => { jest.advanceTimersByTime(1000); });

  expect(mockGet).toHaveBeenCalledTimes(2);
  for (const call of mockGet.mock.calls) {
    expect(optionsOf(call)).not.toHaveProperty('params');
  }
});

test('a task that finishes in a hidden tab is reported once the tab is shown', async () => {
  visibility = 'hidden';
  mockGet.mockResolvedValue({ data: { status: 'completed', content: 'x' } });
  const { result } = render({ reportVisible: true });
  await flush();

  expect(result.current.status).toBe('completed');
  expect(mockGet).toHaveBeenCalledTimes(1);
  expect(optionsOf(mockGet.mock.calls[0])).not.toHaveProperty('params');

  // Still hidden: nothing more is sent.
  act(() => { setVisibility('hidden'); });
  expect(mockGet).toHaveBeenCalledTimes(1);

  act(() => { setVisibility('visible'); });
  await flush();
  expect(mockGet).toHaveBeenCalledTimes(2);
  expect(mockGet.mock.calls[1][0]).toBe(ENDPOINT);
  expect(optionsOf(mockGet.mock.calls[1]).params).toEqual({ visible: 1 });

  // Once: later changes of visibility send nothing.
  act(() => { setVisibility('hidden'); });
  act(() => { setVisibility('visible'); });
  await flush();
  expect(mockGet).toHaveBeenCalledTimes(2);
});

test('a task that finishes in a visible tab needs no extra poll', async () => {
  mockGet.mockResolvedValue({ data: { status: 'completed', content: 'x' } });
  render({ reportVisible: true });
  await flush();
  expect(optionsOf(mockGet.mock.calls[0]).params).toEqual({ visible: 1 });

  act(() => { setVisibility('hidden'); });
  act(() => { setVisibility('visible'); });
  await flush();
  expect(mockGet).toHaveBeenCalledTimes(1);
});

test('a failed task is not reported', async () => {
  visibility = 'hidden';
  mockGet.mockResolvedValue({ data: { status: 'failed', error: 'boom' } });
  render({ reportVisible: true });
  await flush();

  act(() => { setVisibility('visible'); });
  await flush();
  expect(mockGet).toHaveBeenCalledTimes(1);
});

test('leaving the page before the tab is shown sends nothing', async () => {
  visibility = 'hidden';
  mockGet.mockResolvedValue({ data: { status: 'completed', content: 'x' } });
  const { unmount } = render({ reportVisible: true });
  await flush();
  unmount();

  act(() => { setVisibility('visible'); });
  await flush();
  expect(mockGet).toHaveBeenCalledTimes(1);
});

test('without reportVisible a hidden finish sends nothing more', async () => {
  visibility = 'hidden';
  mockGet.mockResolvedValue({ data: { status: 'completed', content: 'x' } });
  render({});
  await flush();

  act(() => { setVisibility('visible'); });
  await flush();
  expect(mockGet).toHaveBeenCalledTimes(1);
});

// #374: a phone suspends a page whose screen is off. When the user comes
// back (tab shown again, back/forward-cache restore, back online), a page
// waiting on a reply asks the server at once, picks polling up again if it
// ran out of time while the page was away, and never lets an answer sent
// before the suspension replace a newer one.
const mockGet = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: { get: (...args) => mockGet(...args) },
}));

import { renderHook, act } from '@testing-library/react';
import { useAsyncTaskPolling } from './useAsyncTaskPolling';

let visibility = 'visible';
beforeAll(() => {
  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    get: () => visibility,
  });
});

const setVisibility = (state) => {
  visibility = state;
  document.dispatchEvent(new Event('visibilitychange'));
};

const pageShow = (persisted) => {
  const event = new Event('pageshow');
  Object.defineProperty(event, 'persisted', { value: persisted });
  window.dispatchEvent(event);
};

// Let pending promise callbacks (the awaited api.get) run.
const flush = async () => {
  await act(async () => {
    for (let i = 0; i < 5; i += 1) await Promise.resolve();
  });
};

const answer = (status, extra = {}) => Promise.resolve({ data: { status, ...extra } });

beforeEach(() => {
  jest.useFakeTimers();
  visibility = 'visible';
  mockGet.mockReset();
  jest.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
  jest.useRealTimers();
  console.error.mockRestore();
});

test('a return to the page polls at once instead of at the next tick', async () => {
  mockGet.mockImplementation(() => answer('processing'));
  const { result } = renderHook(() => useAsyncTaskPolling('/nodes/7/llm-status', {
    enabled: true, interval: 60000,
  }));
  await flush();
  expect(mockGet).toHaveBeenCalledTimes(1);
  expect(result.current.status).toBe('processing');

  act(() => setVisibility('hidden'));
  expect(mockGet).toHaveBeenCalledTimes(1);

  // The reply finished while the screen was off.
  mockGet.mockImplementation(() => answer('completed', { content: 'Hello' }));
  act(() => setVisibility('visible'));
  await flush();
  expect(mockGet).toHaveBeenCalledTimes(2);
  expect(result.current.status).toBe('completed');
  expect(result.current.data.content).toBe('Hello');
});

test('a back/forward-cache restore and a reconnect count as a return; a first load does not', async () => {
  mockGet.mockImplementation(() => answer('processing'));
  renderHook(() => useAsyncTaskPolling('/nodes/7/llm-status', {
    enabled: true, interval: 60000,
  }));
  await flush();
  expect(mockGet).toHaveBeenCalledTimes(1);

  act(() => pageShow(false));
  await flush();
  expect(mockGet).toHaveBeenCalledTimes(1);

  act(() => pageShow(true));
  await flush();
  expect(mockGet).toHaveBeenCalledTimes(2);

  act(() => { window.dispatchEvent(new Event('online')); });
  await flush();
  expect(mockGet).toHaveBeenCalledTimes(3);
});

test('polling that ran out of time while the page was away starts again on return', async () => {
  mockGet.mockImplementation(() => answer('processing'));
  const { result } = renderHook(() => useAsyncTaskPolling('/nodes/7/llm-status', {
    enabled: true, interval: 1000, maxDuration: 10000,
  }));
  await flush();
  act(() => setVisibility('hidden'));

  act(() => { jest.advanceTimersByTime(10001); });
  await flush();
  expect(result.current.isPolling).toBe(false);
  expect(result.current.error).toMatch(/timeout/);
  const callsWhenStopped = mockGet.mock.calls.length;

  act(() => { jest.advanceTimersByTime(5000); });
  expect(mockGet).toHaveBeenCalledTimes(callsWhenStopped);

  mockGet.mockImplementation(() => answer('completed', { content: 'Done' }));
  act(() => setVisibility('visible'));
  await flush();
  expect(mockGet).toHaveBeenCalledTimes(callsWhenStopped + 1);
  expect(result.current.status).toBe('completed');
  expect(result.current.error).toBe(null);
});

test('a poller stopped after too many errors stays stopped on return', async () => {
  mockGet.mockImplementation(() => Promise.reject(new Error('network')));
  const { result } = renderHook(() => useAsyncTaskPolling('/export/profile-progress', {
    enabled: true, interval: 1000, maxDuration: 0, maxConsecutiveErrors: 2,
  }));
  await flush();
  act(() => { jest.advanceTimersByTime(1000); });
  await flush();
  expect(result.current.error).toMatch(/consecutive errors/);
  const calls = mockGet.mock.calls.length;

  act(() => setVisibility('hidden'));
  act(() => setVisibility('visible'));
  await flush();
  expect(mockGet).toHaveBeenCalledTimes(calls);
});

test('an answer sent before the suspension does not replace a newer one', async () => {
  let answerStale;
  mockGet.mockImplementationOnce(() => new Promise((resolve) => { answerStale = resolve; }));
  mockGet.mockImplementation(() => answer('completed', { content: 'Fresh' }));
  const { result } = renderHook(() => useAsyncTaskPolling('/nodes/7/llm-status', {
    enabled: true, interval: 60000,
  }));
  await flush();
  expect(mockGet).toHaveBeenCalledTimes(1);

  act(() => setVisibility('hidden'));
  act(() => setVisibility('visible'));
  await flush();
  expect(result.current.status).toBe('completed');

  // The request from before the suspension is answered last.
  await act(async () => { answerStale({ data: { status: 'processing' } }); });
  await flush();
  expect(result.current.status).toBe('completed');
  expect(result.current.data.content).toBe('Fresh');
});

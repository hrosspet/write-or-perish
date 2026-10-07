// Opening another node of a thread: the current page stays (with a spinner)
// until the node is fetched, then its page opens with the data in hand.
jest.mock('../api', () => ({ get: jest.fn() }));

import { renderHook, act } from '@testing-library/react';
import api from '../api';
import useNodePrefetch, {
  peekPrefetchedNode, takePrefetchedNode, MAX_WAIT_MS, FRESH_FOR_MS,
} from './useNodePrefetch';

// A request that answers when the test says so, and rejects when aborted
// (as axios does).
const deferredGet = () => {
  const calls = [];
  api.get.mockImplementation((url, { signal }) => new Promise((resolve, reject) => {
    calls.push({ url, resolve, reject, signal });
    signal.addEventListener('abort', () => reject(new Error('canceled')));
  }));
  return calls;
};

beforeEach(() => {
  jest.useFakeTimers();
  api.get.mockReset();
});

afterEach(() => {
  jest.useRealTimers();
});

test('pending until the node arrives, then opens with the data handed out once', async () => {
  const calls = deferredGet();
  const open = jest.fn();
  const { result } = renderHook(() => useNodePrefetch());

  act(() => { result.current.openNode(7, open); });
  expect(result.current.pending).toBe(true);
  expect(calls[0].url).toBe('/nodes/7');
  expect(open).not.toHaveBeenCalled();

  await act(async () => { calls[0].resolve({ data: { id: 7, content: 'seven' } }); });
  expect(open).toHaveBeenCalledTimes(1);
  expect(result.current.pending).toBe(false);
  expect(peekPrefetchedNode('7')).toEqual({ id: 7, content: 'seven' });
  await expect(takePrefetchedNode(7)).resolves.toEqual({ data: { id: 7, content: 'seven' } });
  expect(takePrefetchedNode(7)).toBeNull();
});

test('a click on another node replaces the first; only the newest opens', async () => {
  const calls = deferredGet();
  const openFirst = jest.fn();
  const openSecond = jest.fn();
  const { result } = renderHook(() => useNodePrefetch());

  act(() => { result.current.openNode(1, openFirst); });
  act(() => { result.current.openNode(2, openSecond); });
  expect(calls[0].signal.aborted).toBe(true);

  await act(async () => { calls[1].resolve({ data: { id: 2 } }); });
  expect(openFirst).not.toHaveBeenCalled();
  expect(openSecond).toHaveBeenCalledTimes(1);
  expect(takePrefetchedNode(1)).toBeNull();
  await expect(takePrefetchedNode(2)).resolves.toEqual({ data: { id: 2 } });
});

test('the same node clicked again (a double click) keeps the first request', async () => {
  const calls = deferredGet();
  const open = jest.fn();
  const { result } = renderHook(() => useNodePrefetch());

  act(() => { result.current.openNode(8, open); });
  act(() => { jest.advanceTimersByTime(MAX_WAIT_MS - 500); });
  act(() => { result.current.openNode(8, open); });
  expect(calls).toHaveLength(1);
  expect(calls[0].signal.aborted).toBe(false);

  // The wait still ends MAX_WAIT_MS after the first click.
  await act(async () => { jest.advanceTimersByTime(500); });
  expect(open).toHaveBeenCalledTimes(1);
});

test('a slow answer opens the page anyway, which waits for the same request', async () => {
  const calls = deferredGet();
  const open = jest.fn();
  const { result } = renderHook(() => useNodePrefetch());

  act(() => { result.current.openNode(3, open); });
  await act(async () => { jest.advanceTimersByTime(MAX_WAIT_MS); });
  expect(open).toHaveBeenCalledTimes(1);
  expect(result.current.pending).toBe(false);
  expect(calls[0].signal.aborted).toBe(false);
  expect(peekPrefetchedNode(3)).toBeNull();

  const handed = takePrefetchedNode(3);
  calls[0].resolve({ data: { id: 3 } });
  await expect(handed).resolves.toEqual({ data: { id: 3 } });
  expect(api.get).toHaveBeenCalledTimes(1);
});

test('a failed fetch opens the page with the error to show', async () => {
  const notFound = { response: { status: 404 } };
  api.get.mockRejectedValue(notFound);
  const open = jest.fn();
  const { result } = renderHook(() => useNodePrefetch());

  await act(async () => { result.current.openNode(4, open); });
  expect(open).toHaveBeenCalledTimes(1);
  expect(peekPrefetchedNode(4)).toBeNull();
  await expect(takePrefetchedNode(4)).rejects.toBe(notFound);
});

test('leaving the page drops a click in flight', async () => {
  const calls = deferredGet();
  const open = jest.fn();
  const { result, unmount } = renderHook(() => useNodePrefetch());

  act(() => { result.current.openNode(5, open); });
  unmount();
  expect(calls[0].signal.aborted).toBe(true);
  await act(async () => { jest.advanceTimersByTime(MAX_WAIT_MS); });
  expect(open).not.toHaveBeenCalled();
  expect(takePrefetchedNode(5)).toBeNull();
});

test('a fetched node goes stale after FRESH_FOR_MS', async () => {
  const calls = deferredGet();
  const { result } = renderHook(() => useNodePrefetch());

  act(() => { result.current.openNode(6, () => {}); });
  await act(async () => { calls[0].resolve({ data: { id: 6 } }); });
  expect(peekPrefetchedNode(6)).toEqual({ id: 6 });
  jest.advanceTimersByTime(FRESH_FOR_MS);
  expect(peekPrefetchedNode(6)).toBeNull();
  expect(takePrefetchedNode(6)).toBeNull();
});

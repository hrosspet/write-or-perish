// #426: a send that clears the draft must not be undone by an autosave
// that was already on the wire.
jest.mock('../api', () => ({
  __esModule: true,
  default: { get: jest.fn(), post: jest.fn(), delete: jest.fn() },
}));

import { renderHook, act } from '@testing-library/react';
import api from '../api';
import { useDraft } from './useDraft';

const flush = async () => { await act(async () => { await Promise.resolve(); }); };

let calls;
beforeEach(() => {
  jest.useFakeTimers();
  calls = [];
  jest.clearAllMocks();
  api.get.mockRejectedValue({ response: { status: 404 } });
  api.delete.mockImplementation(async () => { calls.push('delete'); return {}; });
});
afterEach(() => jest.useRealTimers());

const deferredPost = () => {
  let resolve, reject;
  api.post.mockImplementation(() => {
    calls.push('post-start');
    return new Promise((res, rej) => { resolve = res; reject = rej; });
  });
  return {
    resolve: () => { calls.push('post-done'); resolve({ data: { updated_at: '2026-01-01T00:00:00' } }); },
    reject: () => { calls.push('post-done'); reject(new Error('boom')); },
  };
};

test('delete waits for the save in flight, then sends the DELETE', async () => {
  const post = deferredPost();
  const { result } = renderHook(() => useDraft({ parentId: 1 }));
  await flush();

  act(() => { result.current.saveDraft('sent text'); });
  act(() => { jest.advanceTimersByTime(1000); });
  expect(calls).toEqual(['post-start']);

  let deleted;
  act(() => { deleted = result.current.deleteDraft(); });
  await flush();
  expect(calls).toEqual(['post-start']); // DELETE held back

  post.resolve();
  await act(async () => { await deleted; });
  expect(calls).toEqual(['post-start', 'post-done', 'delete']);
});

test('no new autosave starts while the delete is waiting', async () => {
  const post = deferredPost();
  const { result } = renderHook(() => useDraft({ parentId: 1 }));
  await flush();

  act(() => { result.current.saveDraft('sent text'); });
  act(() => { jest.advanceTimersByTime(1000); });
  let deleted;
  act(() => { deleted = result.current.deleteDraft(); });
  // A late keystroke while the DELETE waits
  act(() => { result.current.saveDraft('late'); });
  act(() => { jest.advanceTimersByTime(5000); });
  post.resolve();
  await act(async () => { await deleted; });

  expect(api.post).toHaveBeenCalledTimes(1);
  expect(calls[calls.length - 1]).toBe('delete');
});

test('a failed save in flight does not skip the DELETE', async () => {
  const post = deferredPost();
  jest.spyOn(console, 'error').mockImplementation(() => {});
  const { result } = renderHook(() => useDraft({ parentId: 1 }));
  await flush();

  act(() => { result.current.saveDraft('sent text'); });
  act(() => { jest.advanceTimersByTime(1000); });
  let deleted;
  act(() => { deleted = result.current.deleteDraft(); });
  post.reject();
  await act(async () => { await deleted; });

  expect(calls).toEqual(['post-start', 'post-done', 'delete']);
  console.error.mockRestore();
});

test('delete with nothing in flight sends the DELETE at once', async () => {
  const { result } = renderHook(() => useDraft({ parentId: 1 }));
  await flush();
  await act(async () => { await result.current.deleteDraft(); });
  expect(calls).toEqual(['delete']);
});

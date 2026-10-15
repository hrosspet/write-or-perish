// #392: the updates modal never opens on /welcome. A session that starts
// there doesn't ask for updates at all; any other session asks once.
const mockGet = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: { get: (...args) => mockGet(...args) },
}));

import { renderHook, waitFor, act } from '@testing-library/react';
import { useSessionUpdates } from './useSessionUpdates';

const READY = { id: 1, approved: true, terms_up_to_date: true };
const UNREAD = {
  changelog: [], notifications: [],
  polls: [{ id: 3, question: "What's one thing you wish Loore did?" }],
};

const renderAt = (pathname, user = READY) => renderHook(
  ({ u, path }) => useSessionUpdates(u, path),
  { initialProps: { u: user, path: pathname } },
);

beforeEach(() => {
  window.sessionStorage.clear();
  mockGet.mockReset();
  mockGet.mockResolvedValue({ data: UNREAD });
});

test('a session that starts on /welcome never asks', async () => {
  const { result, rerender } = renderAt('/welcome');
  expect(mockGet).not.toHaveBeenCalled();
  expect(result.current[0]).toBeNull();

  // "Reflect" leads to the homepage: still nothing this session.
  rerender({ u: READY, path: '/' });
  rerender({ u: { ...READY }, path: '/voice' });
  await act(async () => {});
  expect(mockGet).not.toHaveBeenCalled();
  expect(result.current[0]).toBeNull();
});

test('accepting the terms on /welcome does not ask either', async () => {
  const { rerender } = renderAt('/welcome', { ...READY, terms_up_to_date: false });
  rerender({ u: READY, path: '/welcome' });
  await act(async () => {});
  expect(mockGet).not.toHaveBeenCalled();
});

test('any other first page asks once and opens on something unread', async () => {
  const { result, rerender } = renderAt('/');
  expect(mockGet).toHaveBeenCalledTimes(1);
  expect(mockGet).toHaveBeenCalledWith('/updates');
  await waitFor(() => expect(result.current[0]).toEqual(UNREAD));

  rerender({ u: { ...READY }, path: '/log' });
  expect(mockGet).toHaveBeenCalledTimes(1);

  act(() => { result.current[1](); });
  expect(result.current[0]).toBeNull();
});

test('nothing unread opens nothing', async () => {
  mockGet.mockResolvedValue({
    data: { changelog: [], notifications: [], polls: [] },
  });
  const { result } = renderAt('/');
  await act(async () => {});
  expect(mockGet).toHaveBeenCalledTimes(1);
  expect(result.current[0]).toBeNull();
});

test('waits for an approved user with current terms', async () => {
  const { rerender } = renderAt('/', null);
  rerender({ u: { ...READY, terms_up_to_date: false }, path: '/' });
  expect(mockGet).not.toHaveBeenCalled();
  rerender({ u: READY, path: '/' });
  expect(mockGet).toHaveBeenCalledTimes(1);
});

test.each(['/confirm-account-deletion', '/account-restore'])(
  'a session that starts on %s never asks', async (path) => {
    const { result, rerender } = renderAt(path);
    rerender({ u: READY, path: '/' });
    await act(async () => {});
    expect(mockGet).not.toHaveBeenCalled();
    expect(result.current[0]).toBeNull();
  });

test('restore: the full reload onto the landing page stays silent, then asks again', async () => {
  // On /account-restore nobody is signed in yet.
  const first = renderAt('/account-restore', null);
  first.unmount();
  // window.location.assign: a new session, now signed in, on the next page.
  const landing = renderAt('/');
  await act(async () => {});
  expect(mockGet).not.toHaveBeenCalled();
  expect(landing.result.current[0]).toBeNull();
  landing.unmount();
  // The next visit asks as usual.
  renderAt('/');
  expect(mockGet).toHaveBeenCalledTimes(1);
});

// #599: the both-providers question on opening Loore. Once per visit when
// the server says to ask, on the first page that suits it; never on a
// Voice or Text page, during a recording, or over another dialog; a close
// without an answer holds it for the rest of the visit.
import { renderHook, act } from '@testing-library/react';
import { useBothProvidersQuestion, LATER_FLAG } from './useBothProvidersQuestion';
import { setRecordingActive } from '../utils/recordingActivity';

const ASK = {
  id: 1, approved: true, terms_up_to_date: true,
  ask_both_providers: { model: true, visit: true, import: true },
};

const renderAt = (path, user = ASK, blocked = false) => renderHook(
  ({ u, p, b }) => useBothProvidersQuestion(u, p, b),
  { initialProps: { u: user, p: path, b: blocked } },
);

beforeEach(() => window.sessionStorage.clear());

test('opens on the homepage when the server says to ask', () => {
  const { result } = renderAt('/');
  expect(result.current[0]).toBe(true);
});

test('does not open when the server says not to', () => {
  const { result } = renderAt('/', {
    ...ASK, ask_both_providers: { model: false, visit: false, import: false } });
  expect(result.current[0]).toBe(false);
});

test('waits through a Voice or Text session for the next page', () => {
  const { result, rerender } = renderAt('/voice');
  expect(result.current[0]).toBe(false);
  rerender({ u: ASK, p: '/textmode', b: false });
  expect(result.current[0]).toBe(false);
  rerender({ u: ASK, p: '/log', b: false });
  expect(result.current[0]).toBe(true);
});

test('waits while a recording is in progress', () => {
  const rec = {};
  act(() => setRecordingActive(rec, true));
  const { result } = renderAt('/');
  expect(result.current[0]).toBe(false);
  act(() => setRecordingActive(rec, false));
  expect(result.current[0]).toBe(true);
});

test('waits while another dialog is open', () => {
  const { result, rerender } = renderAt('/', ASK, true);
  expect(result.current[0]).toBe(false);
  rerender({ u: ASK, p: '/', b: false });
  expect(result.current[0]).toBe(true);
});

test('a visit that starts on /welcome does not ask', () => {
  const { result, rerender } = renderAt('/welcome');
  rerender({ u: ASK, p: '/', b: false });
  expect(result.current[0]).toBe(false);
});

test('a close without an answer holds it for the visit', () => {
  const { result } = renderAt('/');
  act(() => result.current[1](false));
  expect(result.current[0]).toBe(false);
  expect(window.sessionStorage.getItem(LATER_FLAG)).toBe('1');
  // The reload after an import's result: not asked again.
  const again = renderAt('/');
  expect(again.result.current[0]).toBe(false);
});

test('an answer leaves nothing held', () => {
  const { result } = renderAt('/');
  act(() => result.current[1](true));
  expect(result.current[0]).toBe(false);
  expect(window.sessionStorage.getItem(LATER_FLAG)).toBeNull();
});

test('asks once per visit', () => {
  const { result, rerender } = renderAt('/');
  act(() => result.current[1](true));
  rerender({ u: { ...ASK }, p: '/log', b: false });
  expect(result.current[0]).toBe(false);
});

test('leaving the page closes it without an answer', () => {
  const { result, rerender } = renderAt('/');
  expect(result.current[0]).toBe(true);
  rerender({ u: ASK, p: '/account', b: false });
  expect(result.current[0]).toBe(false);
  expect(window.sessionStorage.getItem(LATER_FLAG)).toBe('1');
});

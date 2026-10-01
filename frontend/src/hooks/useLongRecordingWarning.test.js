// The 59-minute warning shared by text mode and voice mode (#320, #243):
// once per recording, chime on the context armed in the record gesture,
// and the context it created is closed when the recording ends.
const mockAddToast = jest.fn();
jest.mock('../contexts/ToastContext', () => ({
  useToast: () => ({ addToast: (...a) => mockAddToast(...a) }),
}));

import { renderHook, act } from '@testing-library/react';
import {
  useLongRecordingWarning, LONG_RECORDING_WARNING_SEC,
} from './useLongRecordingWarning';

const fakeContext = () => ({
  state: 'running',
  currentTime: 0,
  destination: {},
  resume: jest.fn(),
  close: jest.fn(),
  createOscillator: jest.fn(() => ({
    connect: jest.fn(), frequency: {}, start: jest.fn(), stop: jest.fn(),
  })),
  createGain: jest.fn(() => ({
    connect: jest.fn(),
    gain: { setValueAtTime: jest.fn(), exponentialRampToValueAtTime: jest.fn() },
  })),
});

let created;
beforeEach(() => {
  mockAddToast.mockReset();
  created = [];
  window.AudioContext = jest.fn(() => {
    const ctx = fakeContext();
    created.push(ctx);
    return ctx;
  });
});

const render = () => renderHook(
  ({ duration, active }) => useLongRecordingWarning(duration, active),
  { initialProps: { duration: 0, active: false } },
);

test('warns once at 59 minutes, on the context armed in the gesture', () => {
  const { result, rerender } = render();
  act(() => { result.current.armAlertContext(); });
  expect(created).toHaveLength(1);
  const armed = created[0];

  rerender({ duration: 120, active: true });
  expect(mockAddToast).not.toHaveBeenCalled();

  rerender({ duration: LONG_RECORDING_WARNING_SEC, active: true });
  rerender({ duration: LONG_RECORDING_WARNING_SEC + 30, active: true });

  expect(mockAddToast).toHaveBeenCalledTimes(1);
  expect(mockAddToast.mock.calls[0][0]).toMatch(/59 minutes/);
  expect(armed.createOscillator).toHaveBeenCalled();
  expect(created).toHaveLength(1); // no fresh context for the chime
});

test('a new recording (duration back to 0) can warn again', () => {
  const { rerender } = render();
  rerender({ duration: LONG_RECORDING_WARNING_SEC, active: true });
  rerender({ duration: 0, active: true });
  rerender({ duration: LONG_RECORDING_WARNING_SEC, active: true });
  expect(mockAddToast).toHaveBeenCalledTimes(2);
});

test('closes the context it created when the recording ends', () => {
  const { result, rerender } = render();
  act(() => { result.current.armAlertContext(); });
  rerender({ duration: 5, active: true });
  expect(created[0].close).not.toHaveBeenCalled();

  rerender({ duration: 5, active: false });
  expect(created[0].close).toHaveBeenCalledTimes(1);
});

test('uses and never closes a context the caller shares', () => {
  const shared = fakeContext();
  const { result, rerender, unmount } = render();
  act(() => { result.current.armAlertContext(shared); });
  expect(created).toHaveLength(0);

  rerender({ duration: LONG_RECORDING_WARNING_SEC, active: true });
  expect(shared.createOscillator).toHaveBeenCalled();

  rerender({ duration: 0, active: false });
  unmount();
  expect(shared.close).not.toHaveBeenCalled();
});

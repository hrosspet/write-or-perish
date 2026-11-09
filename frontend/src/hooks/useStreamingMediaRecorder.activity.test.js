// #599: the both-providers question never opens while a recording is in
// progress, also on pages other than Voice and Text (a reply recorded on
// a thread page). The signal comes from useStreamingMediaRecorder itself:
// recording and paused count, a stopped recording or an unmounted
// recorder does not.
import { renderHook, act } from '@testing-library/react';
import { useStreamingMediaRecorder } from './useStreamingMediaRecorder';
import { useBothProvidersQuestion } from './useBothProvidersQuestion';
import { isRecordingActive } from '../utils/recordingActivity';

class FakeRecorder {
  static isTypeSupported(m) { return m.startsWith('audio/webm'); }
  constructor(stream, opts) { this.state = 'inactive'; this.mimeType = opts.mimeType; }
  start() { this.state = 'recording'; }
  pause() { this.state = 'paused'; }
  resume() { this.state = 'recording'; }
  requestData() {}
  stop() {
    this.state = 'inactive';
    if (this.ondataavailable) this.ondataavailable({ data: new Blob(['x'], { type: this.mimeType }) });
    if (this.onstop) this.onstop();
  }
}

// Plain functions, not jest.fn(): the test setup resets mocks before each test.
const track = {
  readyState: 'live', stop() {}, addEventListener() {}, removeEventListener() {},
};
const stream = { getTracks: () => [track], getAudioTracks: () => [track] };

const ASK = {
  id: 1, approved: true, terms_up_to_date: true,
  ask_both_providers: { model: true, visit: true, import: true },
};

let savedRecorder;
let savedMediaDevices;
beforeAll(() => {
  savedRecorder = global.MediaRecorder;
  savedMediaDevices = navigator.mediaDevices;
  global.MediaRecorder = FakeRecorder;
  Object.defineProperty(navigator, 'mediaDevices', {
    configurable: true, value: { getUserMedia: async () => stream },
  });
  if (!URL.createObjectURL) URL.createObjectURL = () => 'blob:fake';
});
afterAll(() => {
  global.MediaRecorder = savedRecorder;
  Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: savedMediaDevices });
});
beforeEach(() => window.sessionStorage.clear());

// A thread page with the recorder of its reply form; ``blocked`` stands
// for another dialog that keeps the question back at first.
const renderThreadPage = () => renderHook(
  ({ path, blocked }) => ({
    rec: useStreamingMediaRecorder(),
    ask: useBothProvidersQuestion(ASK, path, blocked),
  }),
  { initialProps: { path: '/node/5', blocked: true } },
);

test('a recording on a thread page holds the question until it stops', async () => {
  const { result, rerender } = renderThreadPage();
  await act(async () => { await result.current.rec.startRecording(); });
  expect(result.current.rec.status).toBe('recording');
  expect(isRecordingActive()).toBe(true);

  rerender({ path: '/node/5', blocked: false });
  expect(result.current.ask[0]).toBe(false);

  act(() => result.current.rec.pauseRecording());
  expect(result.current.rec.status).toBe('paused');
  expect(isRecordingActive()).toBe(true);
  rerender({ path: '/log', blocked: false });
  expect(result.current.ask[0]).toBe(false);

  await act(async () => { await result.current.rec.stopRecording(); });
  expect(result.current.rec.status).toBe('recorded');
  expect(isRecordingActive()).toBe(false);
  expect(result.current.ask[0]).toBe(true);
});

test('an unmounted recorder no longer counts as recording', async () => {
  const { result, unmount } = renderHook(() => useStreamingMediaRecorder());
  await act(async () => { await result.current.startRecording(); });
  expect(isRecordingActive()).toBe(true);
  unmount();
  expect(isRecordingActive()).toBe(false);
});

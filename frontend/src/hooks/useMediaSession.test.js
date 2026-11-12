// The web's lock-screen controls (Media Session, iPhone Safari). Peter,
// 2026-11-12: "the lockscreen controls shouldn't allow next recording
// (replay of the reply should be still possible ...)" while a change the
// AI proposed waits for approval. On the web no lock-screen control starts
// a recording at all outside one: after a reply (playback) and between
// turns (ready) every handler is cleared, so the lock screen only plays,
// pauses and seeks the reply's audio element (the browser's own controls).
// Only a recording already under way has handlers (resume, pause, stop).
import { renderHook } from '@testing-library/react';

// The hook reads the user agent when it is loaded: an iPhone's, before
// the require below.
Object.defineProperty(window.navigator, 'userAgent', {
  value: 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)',
  configurable: true,
});
global.MediaMetadata = function MediaMetadata(init) { Object.assign(this, init); };
const { useMediaSession } = require('./useMediaSession');

let handlers;

beforeEach(() => {
  handlers = {};
  Object.defineProperty(window.navigator, 'mediaSession', {
    value: {
      setActionHandler: (action, handler) => { handlers[action] = handler; },
      playbackState: 'none',
      metadata: null,
    },
    configurable: true,
  });
});

const props = (phase) => ({
  phase,
  isPaused: false,
  duration: 0,
  handlePauseRecording: jest.fn(),
  handleResumeRecording: jest.fn(),
  handleStop: jest.fn(),
  handleCancelProcessing: jest.fn(),
  ttsTitle: 'Voice',
});

test.each(['playback', 'ready'])('%s: no lock-screen control can start a recording', (phase) => {
  const p = props(phase);
  renderHook(() => useMediaSession(p));

  for (const action of ['play', 'pause', 'nexttrack', 'previoustrack', 'stop']) {
    expect(handlers[action]).toBeNull();
  }
});

test('only a recording under way has handlers', () => {
  const p = props('recording');
  renderHook(() => useMediaSession(p));

  handlers.nexttrack();
  handlers.play();
  expect(p.handleStop).toHaveBeenCalled();
  expect(p.handleResumeRecording).toHaveBeenCalled();
});

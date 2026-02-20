import { useEffect, useRef } from 'react';

const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) ||
  (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);

function formatTime(seconds) {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${s.toString().padStart(2, '0')}`;
}

/**
 * useMediaSession — exposes lock-screen controls via the Media Session API.
 *
 * Only activates on iOS (desktop browsers don't need it — playback controls
 * are auto-provided by the browser for <audio> elements).
 *
 * During recording: shows pause/resume + stop (nexttrack) on the lock screen.
 *   Title updates every second with elapsed time (e.g. "Recording 1:23").
 * During processing: shows cancel (nexttrack) on the lock screen.
 * During playback/ready: clears handlers so the browser manages audio natively.
 */
export function useMediaSession({
  phase,
  isPaused,
  handlePauseRecording,
  handleResumeRecording,
  handleStop,
  handleCancelProcessing,
  ttsTitle,
}) {
  const intervalRef = useRef(null);
  const elapsedRef = useRef(0);

  // Store callbacks in refs so the effect doesn't re-run when they change.
  // (The parent recreates these on every render due to unstable `streaming` object.)
  const handlersRef = useRef({});
  handlersRef.current = {
    handlePauseRecording,
    handleResumeRecording,
    handleStop,
    handleCancelProcessing,
  };

  useEffect(() => {
    if (!isIOS || !('mediaSession' in navigator)) return;

    const ms = navigator.mediaSession;
    const artwork = [{ src: '/apple-touch-icon.png', sizes: '180x180', type: 'image/png' }];

    const clear = (action) => {
      try { ms.setActionHandler(action, null); } catch (_) { /* unsupported */ }
    };
    const allActions = ['play', 'pause', 'stop', 'nexttrack', 'previoustrack',
      'seekbackward', 'seekforward', 'seekto'];

    if (phase === 'recording') {
      ms.setActionHandler('play', () => handlersRef.current.handleResumeRecording());
      ms.setActionHandler('pause', () => handlersRef.current.handlePauseRecording());
      ms.setActionHandler('nexttrack', () => handlersRef.current.handleStop());
      clear('stop');
      clear('previoustrack');
      clear('seekbackward');
      clear('seekforward');
      clear('seekto');

      ms.playbackState = isPaused ? 'paused' : 'playing';

      // Update title with elapsed duration every second
      const updateTitle = () => {
        const time = formatTime(elapsedRef.current);
        const label = isPaused ? `Paused ${time}` : `Recording ${time}`;
        ms.metadata = new MediaMetadata({ title: label, artist: 'Loore', artwork });
      };
      updateTitle();
      intervalRef.current = setInterval(() => {
        if (!isPaused) {
          elapsedRef.current += 1;
        }
        updateTitle();
      }, 1000);
    } else if (phase === 'processing') {
      ms.metadata = new MediaMetadata({
        title: (ttsTitle || 'Thinking') + '…',
        artist: 'Loore',
        artwork,
      });
      clear('play');
      clear('pause');
      ms.setActionHandler('nexttrack', () => handlersRef.current.handleCancelProcessing());
      clear('stop');
      clear('previoustrack');
      clear('seekbackward');
      clear('seekforward');
      clear('seekto');
      ms.playbackState = 'playing';
      elapsedRef.current = 0;
    } else if (phase === 'playback') {
      ms.metadata = new MediaMetadata({
        title: ttsTitle || 'Audio',
        artist: 'Loore',
        artwork,
      });
      allActions.forEach(clear);
      elapsedRef.current = 0;
    } else {
      ms.metadata = null;
      allActions.forEach(clear);
      elapsedRef.current = 0;
    }

    return () => {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
    };
  }, [phase, isPaused, ttsTitle]);
}

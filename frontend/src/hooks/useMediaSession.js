import { useEffect, useRef } from 'react';

const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) ||
  (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);

/**
 * useMediaSession — exposes lock-screen controls via the Media Session API.
 *
 * Only activates on iOS (desktop browsers don't need it — playback controls
 * are auto-provided by the browser for <audio> elements).
 *
 * During recording: shows pause/resume + stop on the lock screen.
 * During processing: shows stop (cancel) on the lock screen.
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
  const positionIntervalRef = useRef(null);
  const elapsedRef = useRef(0);

  useEffect(() => {
    if (!isIOS || !('mediaSession' in navigator)) return;

    const ms = navigator.mediaSession;

    // -- Metadata --
    const artwork = [{ src: '/apple-touch-icon.png', sizes: '180x180', type: 'image/png' }];

    if (phase === 'recording') {
      ms.metadata = new MediaMetadata({
        title: isPaused ? 'Paused' : 'Recording…',
        artist: 'Loore',
        artwork,
      });
    } else if (phase === 'processing') {
      ms.metadata = new MediaMetadata({
        title: (ttsTitle || 'Thinking') + '…',
        artist: 'Loore',
        artwork,
      });
    } else if (phase === 'playback') {
      ms.metadata = new MediaMetadata({
        title: ttsTitle || 'Audio',
        artist: 'Loore',
        artwork,
      });
    } else {
      ms.metadata = null;
    }

    // -- Action handlers --
    const clear = (action) => {
      try { ms.setActionHandler(action, null); } catch (_) { /* unsupported */ }
    };
    const allActions = ['play', 'pause', 'stop', 'nexttrack', 'previoustrack',
      'seekbackward', 'seekforward', 'seekto'];

    if (phase === 'recording') {
      ms.setActionHandler('play', () => handleResumeRecording());
      ms.setActionHandler('pause', () => handlePauseRecording());
      ms.setActionHandler('stop', () => handleStop());
      clear('nexttrack');
      clear('previoustrack');
      clear('seekbackward');
      clear('seekforward');
      clear('seekto');

      ms.playbackState = isPaused ? 'paused' : 'playing';
    } else if (phase === 'processing') {
      clear('play');
      clear('pause');
      ms.setActionHandler('stop', () => handleCancelProcessing());
      clear('nexttrack');
      clear('previoustrack');
      clear('seekbackward');
      clear('seekforward');
      clear('seekto');
      ms.playbackState = 'playing';
    } else {
      // playback / ready — let browser defaults work
      allActions.forEach(clear);
    }

    // -- Position state (recording only) --
    if (phase === 'recording') {
      const updatePosition = () => {
        try {
          ms.setPositionState({
            duration: elapsedRef.current + 1,
            position: elapsedRef.current,
            playbackRate: 1,
          });
        } catch (_) { /* some browsers don't support setPositionState */ }
      };
      updatePosition();
      positionIntervalRef.current = setInterval(() => {
        if (!isPaused) {
          elapsedRef.current += 1;
          updatePosition();
        }
      }, 1000);
    } else {
      elapsedRef.current = 0;
    }

    return () => {
      if (positionIntervalRef.current) {
        clearInterval(positionIntervalRef.current);
        positionIntervalRef.current = null;
      }
    };
  }, [phase, isPaused, handlePauseRecording, handleResumeRecording, handleStop, handleCancelProcessing, ttsTitle]);
}

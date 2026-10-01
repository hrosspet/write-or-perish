import { useCallback, useEffect, useRef } from 'react';
import { useToast } from '../contexts/ToastContext';

// #127: recordings run into a cliff at ~60 minutes. Warn a minute before.
export const LONG_RECORDING_WARNING_SEC = 59 * 60;

/**
 * Play a rising two-note "wrap up soon" chime via the Web Audio API.
 * Rising + repeated so it reads as an attention cue, not a failure (cf.
 * the descending playErrorSound in useStreamingTranscription — same idiom).
 * The sound matters more than the toast here: long recordings often run
 * with the screen off / phone pocketed via headphones, where a visual
 * toast is never seen. Best-effort — silently ignored if audio is
 * unavailable or blocked.
 */
export function playWarningSound(sessionCtx) {
  try {
    // Prefer the long-lived context created inside the record-press user
    // gesture: iOS may refuse to START a fresh AudioContext while the page
    // is backgrounded (screen off / pocketed) — the exact situation the
    // 59-minute chime exists for (#243). A gesture-activated running
    // context only needs resume(), which is permitted.
    const ownCtx = !sessionCtx || sessionCtx.state === 'closed';
    const ctx = ownCtx
      ? new (window.AudioContext || window.webkitAudioContext)()
      : sessionCtx;
    if (ctx.state === 'suspended') ctx.resume();
    const playTone = (freq, startTime, duration) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.frequency.value = freq;
      osc.type = 'triangle';
      gain.gain.setValueAtTime(0.2, startTime);
      gain.gain.exponentialRampToValueAtTime(0.01, startTime + duration);
      osc.start(startTime);
      osc.stop(startTime + duration);
    };
    const now = ctx.currentTime;
    // Two rising G5 -> C6 chimes, repeated for salience in a pocket.
    playTone(784, now, 0.18);
    playTone(1047, now + 0.20, 0.24);
    playTone(784, now + 0.52, 0.18);
    playTone(1047, now + 0.72, 0.30);
    // Only close a context we created ourselves — the session context
    // stays open for the rest of the recording.
    if (ownCtx) setTimeout(() => ctx.close(), 1200);
  } catch (e) {
    // Audio not available - silently ignore
    console.warn('[LongRecordingWarning] Could not play warning sound:', e);
  }
}

/**
 * The 59-minute warning, shared by text mode (StreamingMicButton) and
 * voice mode (useVoiceSession) — voice mode had none (#320, #243).
 * Fires once per recording, with a sound as well as a toast.
 *
 * @param {number} duration - seconds recorded so far (a resumed session
 *   continues from its offset, so this is the session's total)
 * @param {boolean} active - a recording session is under way; when it
 *   turns false the alert context armed for it is closed
 * @returns {{ armAlertContext: Function }} call armAlertContext() INSIDE
 *   the record-press user gesture: that activation is what lets the chime
 *   start while the page is backgrounded. Pass an AudioContext the caller
 *   already keeps running for the recording to reuse it (not closed here).
 */
export function useLongRecordingWarning(duration, active) {
  const { addToast } = useToast();
  const warnedRef = useRef(false);
  const alertRef = useRef(null); // { ctx, owned }

  useEffect(() => {
    if (duration < 1) warnedRef.current = false;
    if (duration >= LONG_RECORDING_WARNING_SEC && !warnedRef.current) {
      warnedRef.current = true;
      playWarningSound(alertRef.current?.ctx);
      addToast(
        'You’ve been recording for 59 minutes — consider stopping '
        + 'soon and continuing in a new recording.', 10000);
    }
  }, [duration, addToast]);

  const releaseAlertContext = useCallback(() => {
    const alert = alertRef.current;
    alertRef.current = null;
    if (alert?.owned) {
      try { alert.ctx.close(); } catch (_) { /* already closed */ }
    }
  }, []);

  const armAlertContext = useCallback((sharedCtx) => {
    releaseAlertContext();
    if (sharedCtx) {
      alertRef.current = { ctx: sharedCtx, owned: false };
      return;
    }
    try {
      alertRef.current = {
        ctx: new (window.AudioContext || window.webkitAudioContext)(),
        owned: true,
      };
    } catch (_) { alertRef.current = null; }
  }, [releaseAlertContext]);

  // Close the alert context once the recording session ends, and on unmount.
  useEffect(() => {
    if (!active) releaseAlertContext();
  }, [active, releaseAlertContext]);
  useEffect(() => releaseAlertContext, [releaseAlertContext]);

  return { armAlertContext };
}

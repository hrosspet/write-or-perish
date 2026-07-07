import { useState, useRef, useCallback, useEffect } from 'react';

// Preference-ordered MIME list. WebM/Opus first (existing browsers stay
// on the optimized raw-byte-concat path), then MP4/AAC for iOS Safari
// and other WebKit-based browsers that don't ship WebM in MediaRecorder.
// Order matters: the first supported entry wins.
const PREFERRED_MIMES = [
  'audio/webm;codecs=opus',
  'audio/webm',
  'audio/mp4;codecs=mp4a.40.2',
  'audio/mp4',
];

// Return 'audio/webm' or 'audio/mp4' (family prefix only) given any MIME
// string — matches the backend's `_mime_family` so resume passes round-trip.
const mimeFamily = (m) => (m || '').split(';', 1)[0].trim().toLowerCase();

/**
 * useStreamingMediaRecorder hook for recording audio with real-time chunk emission.
 *
 * This hook uses MediaRecorder with timeslice to emit audio chunks at regular intervals
 * (default 5 minutes), enabling real-time transcription while still recording.
 *
 * Returns status, mediaBlob (final), chunks array, duration, and control functions.
 */
export function useStreamingMediaRecorder({
  chunkIntervalMs = 15 * 1000, // 15 seconds default — frequent uploads for safety
  onChunkReady = null, // Callback when a chunk is ready: (blob, chunkIndex) => void
} = {}) {
  const [status, setStatus] = useState('idle'); // 'idle' | 'recording' | 'paused' | 'recorded'
  const [mediaBlob, setMediaBlob] = useState(null); // Final combined blob
  const [mediaUrl, setMediaUrl] = useState('');
  const [duration, setDuration] = useState(0);
  const [chunkCount, setChunkCount] = useState(0);
  const [error, setError] = useState(null);

  const recorderRef = useRef(null);
  const streamRef = useRef(null);
  const startTimeRef = useRef(null);
  const chunksRef = useRef([]); // All chunks for final assembly
  const chunkIndexRef = useRef(0);
  const durationIntervalRef = useRef(null);
  const stopResolveRef = useRef(null); // Resolve fn for the stop promise
  const dataAvailableFiredRef = useRef(false); // Track if ondataavailable ran during stop
  const lifecycleCleanupRef = useRef(null); // #88 visibility/pagehide listeners
  const trackCleanupRef = useRef(null); // #88 mic-track mute/ended listeners
  const interruptedRef = useRef(false); // #88 mic taken by the OS (phone call / lock screen)
  const userStopRef = useRef(false); // A user-initiated stop is in flight
  const durationOffsetMsRef = useRef(0); // ms of audio recorded before this session (resume)
  const pausedAtRef = useRef(null); // Timestamp when paused
  const totalPausedMsRef = useRef(0); // Accumulated paused duration
  const mimeTypeRef = useRef(null); // Active recorder's mimeType (so non-state callbacks like getPartialBlob can read it)

  // Clean up on unmount
  useEffect(() => {
    return () => {
      if (durationIntervalRef.current) {
        clearInterval(durationIntervalRef.current);
      }
      if (streamRef.current) {
        streamRef.current.getTracks().forEach(track => track.stop());
      }
    };
  }, []);

  const resetRecording = useCallback(() => {
    // Clean up previous recording
    if (lifecycleCleanupRef.current) {
      lifecycleCleanupRef.current();
      lifecycleCleanupRef.current = null;
    }
    if (trackCleanupRef.current) {
      trackCleanupRef.current();
      trackCleanupRef.current = null;
    }
    if (mediaUrl) {
      URL.revokeObjectURL(mediaUrl);
    }
    if (recorderRef.current && recorderRef.current.state !== 'inactive') {
      recorderRef.current.stop();
    }
    if (streamRef.current) {
      streamRef.current.getTracks().forEach(track => track.stop());
    }
    if (durationIntervalRef.current) {
      clearInterval(durationIntervalRef.current);
      durationIntervalRef.current = null;
    }

    recorderRef.current = null;
    streamRef.current = null;
    chunksRef.current = [];
    chunkIndexRef.current = 0;
    pausedAtRef.current = null;
    totalPausedMsRef.current = 0;
    mimeTypeRef.current = null;
    interruptedRef.current = false;
    userStopRef.current = false;
    durationOffsetMsRef.current = 0;
    setMediaBlob(null);
    setMediaUrl('');
    setDuration(0);
    setChunkCount(0);
    setError(null);
    setStatus('idle');
  }, [mediaUrl]);

  // Wire ondataavailable/onstop/onerror on a recorder instance. Shared by the
  // initial start and the interruption re-acquire path (#88) so both
  // recorders behave identically. Every handler guards on
  // recorderRef.current so a stale recorder (replaced by reset or
  // re-acquire) can't emit chunks into the wrong session or clobber UI
  // state after teardown.
  const wireRecorder = useCallback((mediaRecorder, stream) => {
    // MediaRecorder with a timeslice emits format-specific fragments
    // of one continuous stream — only chunk 0 has the init prefix
    // (Matroska EBML/Segment/Tracks for WebM, ftyp+moov for fMP4);
    // chunks 1+ are header-less fragment bodies whose timestamps are
    // absolute to the original recording. Per the respective byte-stream
    // formats, those fragments concatenated in order as raw bytes form
    // exactly one valid file. So we upload each blob verbatim and let
    // the backend do the binary concat + a single remux pass to rewrite
    // duration metadata.
    mediaRecorder.ondataavailable = (e) => {
      if (recorderRef.current !== mediaRecorder) {
        // Stale recorder — drop the data, but never strand a pending stop.
        if (stopResolveRef.current) {
          stopResolveRef.current();
          stopResolveRef.current = null;
        }
        return;
      }
      const recorderState = recorderRef.current?.state || 'unknown';
      console.log(`[StreamingRecorder] ondataavailable fired: size=${e.data?.size || 0}, recorderState=${recorderState}, timeSinceStart=${Date.now() - startTimeRef.current}ms`);

      dataAvailableFiredRef.current = true;

      if (e.data && e.data.size > 0) {
        chunksRef.current.push(e.data);

        if (onChunkReady) {
          const chunkIndex = chunkIndexRef.current;
          chunkIndexRef.current += 1;
          setChunkCount(prev => prev + 1);

          console.log(`[StreamingRecorder] Chunk ${chunkIndex} ready: size=${e.data.size}, totalChunks=${chunksRef.current.length}`);
          onChunkReady(e.data, chunkIndex);
        }
      } else {
        console.warn(`[StreamingRecorder] ondataavailable with empty data: size=${e.data?.size}, recorderState=${recorderState}`);
      }

      if (stopResolveRef.current) {
        stopResolveRef.current();
        stopResolveRef.current = null;
      }
    };

    mediaRecorder.onstop = () => {
      if (recorderRef.current !== mediaRecorder) return;

      // #88: the recorder can stop on its own when the OS takes the mic
      // for good (track 'ended' — phone call / lock screen, esp. iOS).
      // That is an interruption, not a user stop (user stops set
      // userStopRef first): hold the session paused so the user can
      // resume with a re-acquired mic instead of finalizing half-dead.
      const track = stream.getAudioTracks()[0];
      const trackDied = interruptedRef.current
        || (track && track.readyState === 'ended');
      if (trackDied && !userStopRef.current) {
        console.log('[StreamingRecorder] onstop from mic interruption — holding paused for resume');
        interruptedRef.current = true;
        stream.getTracks().forEach(t => t.stop());
        if (!pausedAtRef.current) pausedAtRef.current = Date.now();
        setStatus('paused');
        return;
      }

      if (lifecycleCleanupRef.current) {
        lifecycleCleanupRef.current();
        lifecycleCleanupRef.current = null;
      }
      if (trackCleanupRef.current) {
        trackCleanupRef.current();
        trackCleanupRef.current = null;
      }
      console.log(`[StreamingRecorder] onstop fired: totalChunks=${chunksRef.current.length}, chunkIndex=${chunkIndexRef.current}`);
      // Combine all chunks into final blob
      const blob = new Blob(chunksRef.current, { type: mediaRecorder.mimeType });
      const url = URL.createObjectURL(blob);
      setMediaBlob(blob);
      setMediaUrl(url);

      const ms = Date.now() - startTimeRef.current - totalPausedMsRef.current + durationOffsetMsRef.current;
      setDuration(ms / 1000);
      setStatus('recorded');
      userStopRef.current = false;

      // Stop duration tracking
      if (durationIntervalRef.current) {
        clearInterval(durationIntervalRef.current);
        durationIntervalRef.current = null;
      }

      // Stop all tracks
      stream.getTracks().forEach(track => track.stop());

      // Fallback: if ondataavailable didn't fire (no data to emit),
      // resolve the stop promise here so stopStreaming doesn't hang.
      // When ondataavailable DID fire, it resolves the promise itself
      // after its async work completes — so we skip this.
      if (stopResolveRef.current && !dataAvailableFiredRef.current) {
        console.log('[StreamingRecorder] onstop resolving stop promise (no ondataavailable)');
        stopResolveRef.current();
        stopResolveRef.current = null;
      }
    };

    mediaRecorder.onerror = (e) => {
      console.error('MediaRecorder error:', e);
      setError(e.error?.message || 'Recording error');
      setStatus('idle');
    };
  }, [onChunkReady]);

  // #88: watch the mic track for OS-level interruption (phone call, lock
  // screen). 'mute' = capture suspended (may or may not come back);
  // 'ended' = gone for good. Either way the recorder would keep
  // "recording" silence with the timer climbing, so flush what's buffered
  // and auto-pause. Deliberately NO auto-resume on 'unmute' — the user
  // resumes explicitly (lock-screen play button), and resumeRecording
  // re-acquires the mic if the track died.
  const watchTrack = useCallback((mediaRecorder, stream) => {
    const track = stream.getAudioTracks()[0];
    if (!track) return;
    const onInterruption = (e) => {
      if (recorderRef.current !== mediaRecorder) return;
      if (mediaRecorder.state === 'recording') {
        console.log(`[StreamingRecorder] Mic track ${e.type} (phone call / lock screen) — auto-pausing`);
        interruptedRef.current = true;
        // Flush buffered audio through the normal upload path first —
        // same mechanism as a user pause.
        try { mediaRecorder.requestData(); } catch (err) { /* no-op */ }
        try { mediaRecorder.pause(); } catch (err) { /* no-op */ }
        if (!pausedAtRef.current) pausedAtRef.current = Date.now();
        setStatus('paused');
      } else if (e.type === 'ended') {
        // Already paused (or auto-stopped): just mark the track dead so
        // resume knows to re-acquire rather than resume into silence.
        interruptedRef.current = true;
      }
    };
    track.addEventListener('mute', onInterruption);
    track.addEventListener('ended', onInterruption);
    trackCleanupRef.current = () => {
      track.removeEventListener('mute', onInterruption);
      track.removeEventListener('ended', onInterruption);
    };
  }, []);

  // startingChunkIndex: offset for chunk numbering when resuming an interrupted session
  // durationOffset: seconds of audio already recorded before this session
  // forceMimeFamily: 'audio/webm' or 'audio/mp4' — required for resume; the
  //   recorder must match the family chunk 0 was uploaded with, otherwise
  //   the server rejects subsequent chunks with mime_mismatch.
  const startRecording = useCallback(async ({
    startingChunkIndex = 0,
    durationOffset = 0,
    forceMimeFamily = null,
  } = {}) => {
    resetRecording();

    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      const err = new Error('MediaDevices API not supported');
      setError(err.message);
      throw err;
    }

    if (typeof MediaRecorder === 'undefined') {
      const err = new Error('MediaRecorder API is not available in this browser. Please update your browser or operating system.');
      err.name = 'NotSupportedError';
      setError(err.message);
      throw err;
    }

    // Capture the codec capability matrix + UA for diagnostics — same log
    // that confirmed user 50's iOS-18.0-Safari case in the field.
    const codecSupport = {
      webm: MediaRecorder.isTypeSupported('audio/webm'),
      webmOpus: MediaRecorder.isTypeSupported('audio/webm;codecs=opus'),
      mp4: MediaRecorder.isTypeSupported('audio/mp4'),
      mp4Aac: MediaRecorder.isTypeSupported('audio/mp4;codecs=mp4a.40.2'),
    };

    // Permanent debug knob: ?force_mime=mp4 or ?force_mime=webm constrains
    // the negotiation to a single family. Useful for asking any user to
    // reproduce a specific codec path without code changes (e.g. exercising
    // the MP4 path on a WebM-supporting browser to validate the server-side
    // pipeline). No effect unless the param is present, so it can't be
    // triggered by accident in production. Resume paths (forceMimeFamily
    // branch below) ignore this since the session's family is already
    // locked by chunk 0.
    const forced = new URLSearchParams(window.location.search).get('force_mime');

    let chosenMime;
    if (forceMimeFamily) {
      // Resume path: the session's family is fixed by chunk 0. Refuse if
      // this browser can't record it — same behavior as a fresh start
      // hitting an unsupported environment.
      if (!MediaRecorder.isTypeSupported(forceMimeFamily)) {
        console.log('[StreamingRecorder] Codec support:', codecSupport, 'forceMimeFamily:', forceMimeFamily, 'forced:', forced, 'UA:', navigator.userAgent);
        const err = new Error(
          `Resumed session uses ${forceMimeFamily}, which this browser cannot record. (Detected codecs: ${JSON.stringify(codecSupport)})`
        );
        err.name = 'NotSupportedError';
        setError(err.message);
        throw err;
      }
      // Prefer a codec-qualified entry that shares the family for explicit
      // codec selection; fall back to family-only if none qualified.
      chosenMime =
        PREFERRED_MIMES.find(
          (m) => m.startsWith(forceMimeFamily + ';') && MediaRecorder.isTypeSupported(m),
        ) || forceMimeFamily;
    } else {
      let preferred = PREFERRED_MIMES;
      if (forced === 'mp4') {
        preferred = PREFERRED_MIMES.filter((m) => m.startsWith('audio/mp4'));
      } else if (forced === 'webm') {
        preferred = PREFERRED_MIMES.filter((m) => m.startsWith('audio/webm'));
      }
      chosenMime = preferred.find((m) => MediaRecorder.isTypeSupported(m));
      if (!chosenMime) {
        console.log('[StreamingRecorder] Codec support:', codecSupport, 'forced:', forced, 'UA:', navigator.userAgent);
        const err = new Error(
          forced
            ? `Forced mime '${forced}' is not supported by this browser. (Detected codecs: ${JSON.stringify(codecSupport)})`
            : `This browser cannot record in any supported audio format. (Detected codecs: ${JSON.stringify(codecSupport)})`
        );
        err.name = 'NotSupportedError';
        setError(err.message);
        throw err;
      }
    }

    console.log('[StreamingRecorder] Codec support:', codecSupport, 'chosen:', chosenMime, 'forced:', forced, 'UA:', navigator.userAgent);

    // Pre-check the microphone permission where the Permissions API is
    // available. On some platforms (notably Android Chrome) a previously
    // denied permission makes getUserMedia reject silently/inconsistently or
    // hang; querying first lets us fail fast with an unambiguous
    // NotAllowedError so the caller surfaces a clear "permission denied"
    // toast instead of a stuck "recording" state. Wrapped in try/catch so
    // browsers that don't support permissions.query (or the 'microphone'
    // name) simply fall through to getUserMedia as before.
    try {
      if (navigator.permissions?.query) {
        const permStatus = await navigator.permissions.query({ name: 'microphone' });
        if (permStatus?.state === 'denied') {
          const permErr = new Error('Microphone permission is denied.');
          permErr.name = 'NotAllowedError';
          throw permErr;
        }
      }
    } catch (permCheckErr) {
      // Re-throw our own denial; swallow unsupported-query errors so we still
      // attempt getUserMedia (the source of truth for the prompt/grant flow).
      if (permCheckErr?.name === 'NotAllowedError') {
        console.error('Microphone permission denied (permissions.query):', permCheckErr);
        setError(permCheckErr.message);
        throw permCheckErr;
      }
      console.log('[StreamingRecorder] permissions.query unavailable or failed; falling through to getUserMedia:', permCheckErr?.message);
    }

    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;

      const options = { mimeType: chosenMime };
      const mediaRecorder = new MediaRecorder(stream, options);
      console.log('[StreamingRecorder] MediaRecorder constructed:', { requested: chosenMime, actual: mediaRecorder.mimeType, family: mimeFamily(mediaRecorder.mimeType) });
      recorderRef.current = mediaRecorder;
      mimeTypeRef.current = mediaRecorder.mimeType;
      chunksRef.current = [];
      chunkIndexRef.current = startingChunkIndex;

      wireRecorder(mediaRecorder, stream);
      watchTrack(mediaRecorder, stream);

      startTimeRef.current = Date.now();
      totalPausedMsRef.current = 0;
      pausedAtRef.current = null;
      interruptedRef.current = false;
      userStopRef.current = false;
      durationOffsetMsRef.current = durationOffset * 1000;

      // Start recording with timeslice for chunked output
      // #88: flush the in-flight timeslice when the page is about to be
      // hidden or unloaded (phone call, lock screen, tab switch, kill).
      // requestData() emits the buffered audio through the normal
      // ondataavailable → upload path while the page can still run JS;
      // the upload layer adds a sendBeacon fallback for dying pages.
      const lifecycleFlush = () => {
        if (recorderRef.current
            && recorderRef.current.state === 'recording') {
          console.log('[StreamingRecorder] Lifecycle flush (visibility/pagehide)');
          try { recorderRef.current.requestData(); } catch (e) { /* no-op */ }
        }
      };
      const onVisibility = () => {
        if (document.visibilityState === 'hidden') lifecycleFlush();
      };
      window.addEventListener('pagehide', lifecycleFlush);
      document.addEventListener('visibilitychange', onVisibility);
      lifecycleCleanupRef.current = () => {
        window.removeEventListener('pagehide', lifecycleFlush);
        document.removeEventListener('visibilitychange', onVisibility);
      };

      // The timeslice parameter makes ondataavailable fire at the specified interval
      mediaRecorder.start(chunkIntervalMs);
      setStatus('recording');
      setDuration(durationOffset);

      // Start duration tracking (subtracts paused time from elapsed)
      durationIntervalRef.current = setInterval(() => {
        if (startTimeRef.current && !pausedAtRef.current) {
          const elapsed = Date.now() - startTimeRef.current - totalPausedMsRef.current + durationOffsetMsRef.current;
          setDuration(elapsed / 1000);
        }
      }, 1000);

    } catch (err) {
      console.error('Error accessing microphone:', err);
      setError(err.message);
      // Preserve the permission-denial identity when rethrowing so the
      // parent's onError handler maps it to the right toast. Some browsers
      // use the legacy 'PermissionDeniedError' name; normalize both to a
      // rethrown error whose .name survives (a plain `throw err` already
      // preserves it, but guard against any environment that strips it).
      if (err && (err.name === 'NotAllowedError' || err.name === 'PermissionDeniedError')) {
        const permErr = new Error(err.message || 'Microphone permission is denied.');
        permErr.name = err.name;
        throw permErr;
      }
      throw err;
    }
  }, [resetRecording, chunkIntervalMs, wireRecorder, watchTrack]);

  const pauseRecording = useCallback(() => {
    if (recorderRef.current && recorderRef.current.state === 'recording') {
      // Flush buffered audio as a chunk before pausing, so it gets uploaded
      // and transcribed into the draft. Protects against tab kills during
      // long pauses — the audio is safe on the server even if the tab dies.
      recorderRef.current.requestData();
      recorderRef.current.pause();
      pausedAtRef.current = Date.now();
      setStatus('paused');
    }
  }, []);

  const resumeRecording = useCallback(async () => {
    const rec = recorderRef.current;
    if (!rec) return;

    const track = streamRef.current ? streamRef.current.getAudioTracks()[0] : null;
    const trackAlive = !!(track && track.readyState === 'live' && !track.muted);

    if (rec.state === 'paused' && trackAlive) {
      // Healthy mic: a plain user pause, or the OS gave the track back
      // after an interruption (Android often unmutes once the call ends).
      if (pausedAtRef.current) {
        totalPausedMsRef.current += Date.now() - pausedAtRef.current;
        pausedAtRef.current = null;
      }
      interruptedRef.current = false;
      rec.resume();
      setStatus('recording');
      return;
    }

    // Only an interruption leaves us here: paused on a muted/dead track,
    // or the recorder auto-stopped when the track ended. Anything else
    // (e.g. resume while actively recording) is a no-op.
    if (rec.state !== 'paused' && !interruptedRef.current) return;

    // #88: re-acquire the mic with a fresh getUserMedia + MediaRecorder —
    // after a phone call the old track never delivers audio again. The new
    // recorder's first chunk carries its own init segment; the server
    // detects init-bearing chunks at N>0 and splits transcription into
    // subsessions (#124), so chunk numbering simply continues. (chunksRef
    // then spans two container streams — the local preview blob may only
    // play up to the boundary; the server-side merge is canonical.)
    console.log('[StreamingRecorder] Resuming after interruption — re-acquiring microphone');
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      // Detach + release the dead recorder/stream only once the new mic
      // is granted, so a failed re-acquire leaves the paused state intact.
      if (trackCleanupRef.current) {
        trackCleanupRef.current();
        trackCleanupRef.current = null;
      }
      const oldRecorder = rec;
      const oldStream = streamRef.current;
      oldRecorder.ondataavailable = null;
      oldRecorder.onstop = null;
      oldRecorder.onerror = null;
      try {
        if (oldRecorder.state !== 'inactive') oldRecorder.stop();
      } catch (e) { /* already dead */ }
      if (oldStream) oldStream.getTracks().forEach(t => t.stop());

      streamRef.current = stream;
      const mediaRecorder = new MediaRecorder(
        stream,
        mimeTypeRef.current ? { mimeType: mimeTypeRef.current } : undefined
      );
      recorderRef.current = mediaRecorder;
      wireRecorder(mediaRecorder, stream);
      watchTrack(mediaRecorder, stream);
      mediaRecorder.start(chunkIntervalMs);

      if (pausedAtRef.current) {
        totalPausedMsRef.current += Date.now() - pausedAtRef.current;
        pausedAtRef.current = null;
      }
      interruptedRef.current = false;
      setError(null);
      setStatus('recording');
    } catch (err) {
      // Stay paused — the user can retry resume, or stop and keep
      // everything recorded up to the interruption (already uploaded).
      console.error('[StreamingRecorder] Mic re-acquire failed:', err);
      setError(err.message || 'Could not re-acquire microphone');
    }
  }, [wireRecorder, watchTrack, chunkIntervalMs]);

  const stopRecording = useCallback(() => {
    const state = recorderRef.current?.state;
    if (recorderRef.current && (state === 'recording' || state === 'paused')) {
      // If paused, accumulate final pause duration before stopping
      if (state === 'paused' && pausedAtRef.current) {
        totalPausedMsRef.current += Date.now() - pausedAtRef.current;
        pausedAtRef.current = null;
      }
      console.log(`[StreamingRecorder] stopRecording called: chunksRef.length=${chunksRef.current.length}, chunkIndex=${chunkIndexRef.current}`);
      // Returns a promise that resolves when the final ondataavailable handler
      // has completed (including onChunkReady which enqueues the upload).
      // This lets stopStreaming await it instead of guessing with a timeout.
      return new Promise((resolve) => {
        dataAvailableFiredRef.current = false;
        stopResolveRef.current = resolve;
        // Mark this as a USER stop so onstop finalizes normally even when
        // the mic track died mid-session (#88 interruption handling).
        userStopRef.current = true;
        // stop() fires a final ondataavailable with all remaining data, then onstop.
        // Do NOT call requestData() before stop() — it creates a race condition
        // where the final chunk's ondataavailable may not fire reliably.
        recorderRef.current.stop();
      });
    }
    return Promise.resolve();
  }, []);

  // Get the total chunk count including any offset from resumed sessions
  const getTotalChunks = useCallback(() => {
    return chunkIndexRef.current;
  }, []);

  // Get a partial blob from chunks recorded so far (for download during recording)
  const getPartialBlob = useCallback(() => {
    if (chunksRef.current.length === 0) return null;
    return new Blob(chunksRef.current, { type: mimeTypeRef.current || 'audio/webm' });
  }, []);

  return {
    status,
    mediaBlob,
    mediaUrl,
    duration,
    chunkCount,
    error,
    startRecording,
    stopRecording,
    pauseRecording,
    resumeRecording,
    resetRecording,
    getTotalChunks,
    getPartialBlob,
    chunkIntervalMs,
  };
}

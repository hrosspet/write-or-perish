import { useState, useRef, useCallback, useEffect } from 'react';

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
  const pausedAtRef = useRef(null); // Timestamp when paused
  const totalPausedMsRef = useRef(0); // Accumulated paused duration

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
    setMediaBlob(null);
    setMediaUrl('');
    setDuration(0);
    setChunkCount(0);
    setError(null);
    setStatus('idle');
  }, [mediaUrl]);

  // startingChunkIndex: offset for chunk numbering when resuming an interrupted session
  // durationOffset: seconds of audio already recorded before this session
  const startRecording = useCallback(async ({ startingChunkIndex = 0, durationOffset = 0 } = {}) => {
    resetRecording();

    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      const err = new Error('MediaDevices API not supported');
      setError(err.message);
      throw err;
    }

    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;

      const options = { mimeType: 'audio/webm' };
      const mediaRecorder = new MediaRecorder(stream, options);
      recorderRef.current = mediaRecorder;
      chunksRef.current = [];
      chunkIndexRef.current = startingChunkIndex;

      // MediaRecorder with a timeslice emits Matroska *fragments* of one
      // continuous stream — only chunk 0 has an EBML header; chunks 1+ are
      // header-less cluster data whose timestamps are absolute to the
      // original recording. These fragments, concatenated in order as raw
      // bytes on the server, form exactly one valid WebM. So we upload each
      // blob verbatim and let the backend do the binary concat + a single
      // remux pass to rewrite duration metadata.
      mediaRecorder.ondataavailable = (e) => {
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
        console.log(`[StreamingRecorder] onstop fired: totalChunks=${chunksRef.current.length}, chunkIndex=${chunkIndexRef.current}`);
        // Combine all chunks into final blob
        const blob = new Blob(chunksRef.current, { type: mediaRecorder.mimeType });
        const url = URL.createObjectURL(blob);
        setMediaBlob(blob);
        setMediaUrl(url);

        const ms = Date.now() - startTimeRef.current - totalPausedMsRef.current + durationOffsetMs;
        setDuration(ms / 1000);
        setStatus('recorded');

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

      startTimeRef.current = Date.now();
      totalPausedMsRef.current = 0;
      pausedAtRef.current = null;
      const durationOffsetMs = durationOffset * 1000;

      // Start recording with timeslice for chunked output
      // The timeslice parameter makes ondataavailable fire at the specified interval
      mediaRecorder.start(chunkIntervalMs);
      setStatus('recording');
      setDuration(durationOffset);

      // Start duration tracking (subtracts paused time from elapsed)
      durationIntervalRef.current = setInterval(() => {
        if (startTimeRef.current && !pausedAtRef.current) {
          const elapsed = Date.now() - startTimeRef.current - totalPausedMsRef.current + durationOffsetMs;
          setDuration(elapsed / 1000);
        }
      }, 1000);

    } catch (err) {
      console.error('Error accessing microphone:', err);
      setError(err.message);
      throw err;
    }
  }, [resetRecording, chunkIntervalMs, onChunkReady]);

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

  const resumeRecording = useCallback(() => {
    if (recorderRef.current && recorderRef.current.state === 'paused') {
      // Accumulate the time spent paused
      if (pausedAtRef.current) {
        totalPausedMsRef.current += Date.now() - pausedAtRef.current;
        pausedAtRef.current = null;
      }
      recorderRef.current.resume();
      setStatus('recording');
    }
  }, []);

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
    return new Blob(chunksRef.current, { type: 'audio/webm' });
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

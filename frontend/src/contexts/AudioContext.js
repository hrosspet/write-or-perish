import React, { createContext, useContext, useState, useRef, useCallback } from 'react';
import { useToast } from './ToastContext';

const AudioContext = createContext();

export const useAudio = () => {
  const context = useContext(AudioContext);
  if (!context) {
    throw new Error('useAudio must be used within an AudioProvider');
  }
  return context;
};

// Map an HTMLMediaElement.error to a user-facing toast message. The
// most common cause of code 4 (SRC_NOT_SUPPORTED) here is older iOS
// Safari being asked to play a WebM/Opus recording made in
// Chrome/Firefox; we surface that hypothesis since the browser doesn't
// reveal which codec it choked on. Other codes are rare and stay
// generic — network / decode errors mid-playback.
const playbackErrorMessage = (audioEl) => {
  const err = audioEl && audioEl.error;
  if (!err) return 'Audio playback failed.';
  if (err.code === 4) {
    const src = (audioEl.currentSrc || '').toLowerCase();
    if (src.includes('.webm')) {
      return "This recording is in WebM/Opus, which this browser can't play. Try Chrome or update iOS to 17.4+.";
    }
    return "This audio format isn't supported by your browser.";
  }
  if (err.code === 2) return 'Network error loading audio. Try again.';
  if (err.code === 3) return 'Audio decoding error. The recording may be corrupted.';
  return 'Audio playback failed.';
};

export const AudioProvider = ({ children }) => {
  const { addToast } = useToast();
  const [currentAudio, setCurrentAudio] = useState(null);
  const [isPlaying, setIsPlaying] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [loading, setLoading] = useState(false);
  const [playbackRate, setPlaybackRate] = useState(1);
  const [currentChunkIndex, setCurrentChunkIndex] = useState(0);
  const [totalChunks, setTotalChunks] = useState(0);
  // For chunked playback: track durations and cumulative time
  const [chunkDurations, setChunkDurations] = useState([]);
  const [totalDuration, setTotalDuration] = useState(0);
  const [cumulativeTime, setCumulativeTime] = useState(0);
  const [generatingTTS, _setGeneratingTTS] = useState(false);
  const generatingTTSRef = useRef(false);
  const setGeneratingTTS = useCallback((value) => {
    generatingTTSRef.current = value;
    _setGeneratingTTS(value);
  }, []);
  // Web Audio API context — unlocked during user gesture to allow later autoplay on Safari
  const webAudioCtxRef = useRef(null);
  // Gesture-activated Audio element for Safari autoplay
  const warmedAudioRef = useRef(null);
  // Track whether playback ended while waiting for more chunks.
  // Mirrored as state so consumers (voice mode) can react to the drain —
  // e.g. switch back to a "Thinking..." UI while the next chain node's
  // TTS is still being generated.
  const waitingForChunksRef = useRef(false);
  const [waitingForChunks, _setWaitingForChunks] = useState(false);
  const setWaitingForChunks = useCallback((value) => {
    waitingForChunksRef.current = value;
    _setWaitingForChunks(value);
  }, []);
  const audioRef = useRef(null);
  const intervalRef = useRef(null);
  const audioQueueRef = useRef([]);
  const allChunkUrlsRef = useRef([]);
  const chunkDurationsRef = useRef([]);
  const currentChunkIndexRef = useRef(0);
  const queueMetadataRef = useRef(null);
  // Playback ID to prevent stale event handlers from updating state
  const playbackIdRef = useRef(0);

  // Calculate cumulative time based on chunk index and current position
  const calculateCumulativeTime = useCallback((chunkIndex, timeInChunk) => {
    const durations = chunkDurationsRef.current;
    if (!durations.length) return timeInChunk;
    const completedChunksDuration = durations.slice(0, chunkIndex).reduce((a, b) => a + b, 0);
    const result = completedChunksDuration + timeInChunk;
    return result;
  }, []);

  // Update current time periodically
  const startTimeTracking = useCallback(() => {
    if (intervalRef.current) {
      clearInterval(intervalRef.current);
    }
    // Capture the current playback ID to check if interval is still valid
    const currentPlaybackId = playbackIdRef.current;

    intervalRef.current = setInterval(() => {
      // Check if this interval is still valid (playback ID hasn't changed)
      if (playbackIdRef.current !== currentPlaybackId) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
        return;
      }
      if (audioRef.current && isFinite(audioRef.current.currentTime)) {
        const timeInChunk = audioRef.current.currentTime;
        const currentChunkIdx = currentChunkIndexRef.current;
        setCurrentTime(timeInChunk);
        setCumulativeTime(calculateCumulativeTime(currentChunkIdx, timeInChunk));
      }
    }, 100);
  }, [calculateCumulativeTime]);

  const stopTimeTracking = useCallback(() => {
    if (intervalRef.current) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
  }, []);

  // Preload metadata for all chunks to get their durations
  // Note: MediaRecorder-produced files often lack proper duration metadata.
  // We try multiple strategies but ultimately rely on actual playback to correct durations.
  const preloadChunkDurations = useCallback(async (urls) => {
    const durations = await Promise.all(
      urls.map(url => new Promise((resolve) => {
        const audio = new Audio();
        audio.crossOrigin = 'use-credentials';
        audio.preload = 'metadata';
        let resolved = false;

        const resolveOnce = (duration) => {
          if (resolved) return;
          resolved = true;
          audio.onloadedmetadata = null;
          audio.ondurationchange = null;
          audio.onerror = null;
          audio.src = ''; // Release the resource
          resolve(duration);
        };

        // Try to get duration from metadata
        audio.onloadedmetadata = () => {
          const dur = audio.duration;
          if (isFinite(dur) && dur > 0 && dur < 36000) { // Valid and less than 10 hours
            resolveOnce(dur);
          }
          // If not valid, wait for durationchange or timeout
        };

        // Some browsers update duration after more data loads
        audio.ondurationchange = () => {
          const dur = audio.duration;
          if (isFinite(dur) && dur > 0 && dur < 36000) {
            resolveOnce(dur);
          }
        };

        audio.onerror = () => {
          // Fallback: estimate 5 minutes per chunk (typical recording chunk size)
          resolveOnce(300);
        };

        // Timeout: if we can't get duration in 3 seconds, use fallback
        setTimeout(() => {
          if (!resolved) {
            console.warn(`[AudioDebug] Could not determine duration for chunk, using fallback 300s: ${url}`);
            resolveOnce(300);
          }
        }, 3000);

        audio.src = url;
      }))
    );
    return durations;
  }, []);

  // Find which chunk contains a given cumulative time
  const findChunkForTime = useCallback((targetTime, durations) => {
    let accumulatedTime = 0;
    for (let i = 0; i < durations.length; i++) {
      if (targetTime < accumulatedTime + durations[i]) {
        return { chunkIndex: i, timeInChunk: targetTime - accumulatedTime };
      }
      accumulatedTime += durations[i];
    }
    // If we're past the end, return the last chunk at its end
    const lastIndex = durations.length - 1;
    return { chunkIndex: lastIndex, timeInChunk: durations[lastIndex] || 0 };
  }, []);

  // Helper to recalculate total duration from chunk durations
  const recalculateTotalDuration = useCallback((durations) => {
    const total = durations.reduce((a, b) => a + b, 0);
    setTotalDuration(total);
    return total;
  }, []);

  // Clean up an audio element by removing handlers and pausing
  const cleanupAudio = useCallback((audio) => {
    if (!audio) return;
    audio.onloadedmetadata = null;
    audio.oncanplay = null;
    audio.ontimeupdate = null;
    audio.onended = null;
    audio.onpause = null;
    audio.onplay = null;
    audio.onerror = null;
    try {
      audio.pause();
    } catch (e) {
      // Ignore errors when pausing
    }
  }, []);

  // Create and play a chunk at a specific index and time.
  // preloadedElement: optional Audio element that already started loading (for instant playback)
  const playChunkAtTime = useCallback((chunkIndex, timeInChunk, shouldAutoPlay = true, preloadedElement = null) => {
    const urls = allChunkUrlsRef.current;
    const durations = chunkDurationsRef.current;

    if (!urls.length || chunkIndex >= urls.length) return;

    // IMPORTANT: Stop the interval FIRST to prevent race conditions
    stopTimeTracking();

    // Increment playback ID to invalidate any stale event handlers
    playbackIdRef.current += 1;
    const thisPlaybackId = playbackIdRef.current;

    // Clean up old audio element before creating new one
    if (audioRef.current) {
      cleanupAudio(audioRef.current);
    }

    // Ensure timeInChunk is valid
    const safeTimeInChunk = isFinite(timeInChunk) ? timeInChunk : 0;

    // Update current chunk index
    currentChunkIndexRef.current = chunkIndex;
    setCurrentChunkIndex(chunkIndex);

    // Immediately update cumulative time for UI feedback
    const newCumulativeTime = calculateCumulativeTime(chunkIndex, safeTimeInChunk);
    setCumulativeTime(newCumulativeTime);
    setCurrentTime(safeTimeInChunk);

    // Update queue to contain chunks after current one
    audioQueueRef.current = urls.slice(chunkIndex + 1);

    // Reuse preloaded element (already fetching), gesture-activated warmup
    // element (Safari autoplay), or — for chunks chained from `onended` —
    // the existing element so user-gesture autoplay permission carries
    // over. iOS Safari ties autoplay to a specific HTMLMediaElement; a
    // fresh Audio() created mid-playback has no gesture context and
    // play() rejects with NotAllowedError, so the next batch never
    // starts and playback appears to pause at chunk boundaries.
    const chunkUrl = urls[chunkIndex];
    let audio;
    if (preloadedElement) {
      audio = preloadedElement;
    } else if (warmedAudioRef.current) {
      audio = warmedAudioRef.current;
      warmedAudioRef.current = null;
      audio.src = chunkUrl;
    } else if (audioRef.current) {
      audio = audioRef.current;
      audio.src = chunkUrl;
      audio.load();
    } else {
      audio = new Audio(chunkUrl);
    }
    audioRef.current = audio;
    audio.playbackRate = playbackRate;

    // Track if we've already seeked (to avoid multiple seeks)
    let hasSeeked = false;

    audio.onloadedmetadata = () => {
      if (playbackIdRef.current !== thisPlaybackId) return;

      const actualDuration = audio.duration;
      if (isFinite(actualDuration) && Math.abs(durations[chunkIndex] - actualDuration) > 1) {
        const updatedDurations = [...chunkDurationsRef.current];
        updatedDurations[chunkIndex] = actualDuration;
        chunkDurationsRef.current = updatedDurations;
        setChunkDurations(updatedDurations);
        recalculateTotalDuration(updatedDurations);
      }
      setDuration(actualDuration);
      setLoading(false);
    };

    // Use canplay event for seeking - this fires when the browser can actually seek
    audio.oncanplay = () => {
      if (playbackIdRef.current !== thisPlaybackId) return;
      if (hasSeeked) return;
      hasSeeked = true;

      if (safeTimeInChunk > 0 && isFinite(safeTimeInChunk)) {
        const clampedTime = Math.min(safeTimeInChunk, audio.duration || 300);
        audio.currentTime = clampedTime;
      }
    };

    audio.ontimeupdate = () => {
      if (playbackIdRef.current !== thisPlaybackId) return;

      const timeInCurrentChunk = audio.currentTime;
      setCurrentTime(timeInCurrentChunk);
      setCumulativeTime(calculateCumulativeTime(chunkIndex, timeInCurrentChunk));
    };

    audio.onended = () => {
      if (playbackIdRef.current !== thisPlaybackId) return;

      // Correct stored duration with the actual playback length — preloaded
      // metadata is unreliable for MediaRecorder-produced files.
      const actualDuration = audio.currentTime;
      if (isFinite(actualDuration) && actualDuration > 0) {
        const currentDurations = chunkDurationsRef.current;
        const existingDuration = currentDurations[chunkIndex];
        // Update if significantly different (more than 0.5 sec)
        if (!existingDuration || Math.abs(existingDuration - actualDuration) > 0.5) {
          const updatedDurations = [...currentDurations];
          updatedDurations[chunkIndex] = actualDuration;
          chunkDurationsRef.current = updatedDurations;
          setChunkDurations(updatedDurations);
          recalculateTotalDuration(updatedDurations);
        }
      }

      const queue = audioQueueRef.current;
      if (queue.length > 0) {
        const nextIndex = chunkIndex + 1;
        playChunkAtTime(nextIndex, 0, true);
      } else if (generatingTTSRef.current) {
        // Queue empty but TTS still generating - wait for more chunks
        setWaitingForChunks(true);
        setIsPlaying(false);
        stopTimeTracking();
      } else {
        // Playback finished - keep metadata so user can seek back or replay
        // Only stop() clears everything
        setWaitingForChunks(false);
        setIsPlaying(false);
        stopTimeTracking();
        // Show position at end of total duration
        const totalDur = chunkDurationsRef.current.reduce((a, b) => a + b, 0);
        setCurrentTime(chunkDurationsRef.current[chunkIndex] || 0);
        setCumulativeTime(totalDur);
      }
    };

    audio.onpause = () => {
      if (playbackIdRef.current !== thisPlaybackId) return;
      setIsPlaying(false);
      stopTimeTracking();
    };

    audio.onplay = () => {
      if (playbackIdRef.current !== thisPlaybackId) return;
      setIsPlaying(true);
      startTimeTracking();
    };

    audio.onerror = (e) => {
      if (playbackIdRef.current !== thisPlaybackId) return;
      console.error('Error loading audio chunk:', audio.error || e);
      addToast(playbackErrorMessage(audio), 6000);
      setLoading(false);
      setIsPlaying(false);
      stopTimeTracking();
    };

    if (shouldAutoPlay) {
      audio.play().catch(err => console.error('Error playing chunk:', err));
    }
  }, [playbackRate, calculateCumulativeTime, startTimeTracking, stopTimeTracking, recalculateTotalDuration, cleanupAudio, addToast, setWaitingForChunks]);

  const loadAudio = useCallback(async (audioData) => {
    // If there's already audio playing, pause it first
    if (audioRef.current) {
      audioRef.current.pause();
      stopTimeTracking();
    }

    // Reset chunk-related state for single audio
    setTotalChunks(0);
    setCurrentChunkIndex(0);
    currentChunkIndexRef.current = 0;
    setChunkDurations([]);
    chunkDurationsRef.current = [];
    setTotalDuration(0);
    setCumulativeTime(0);
    allChunkUrlsRef.current = [];
    audioQueueRef.current = [];

    setLoading(true);
    setCurrentAudio(audioData);

    // Create or reuse audio element (gesture-activated for Safari autoplay)
    let audio;
    if (warmedAudioRef.current) {
      audio = warmedAudioRef.current;
      warmedAudioRef.current = null;
      audio.src = audioData.url;
    } else {
      audio = new Audio(audioData.url);
    }
    audioRef.current = audio;

    // Set playback rate
    audio.playbackRate = playbackRate;

    // Set up event listeners
    audio.onloadedmetadata = () => {
      setDuration(audio.duration);
      setTotalDuration(audio.duration);
      setLoading(false);
    };

    audio.ontimeupdate = () => {
      setCurrentTime(audio.currentTime);
      setCumulativeTime(audio.currentTime);
    };

    audio.onended = () => {
      setIsPlaying(false);
      stopTimeTracking();
      setCurrentTime(0);
      setCumulativeTime(0);
    };

    audio.onpause = () => {
      setIsPlaying(false);
      stopTimeTracking();
    };

    audio.onplay = () => {
      setIsPlaying(true);
      startTimeTracking();
    };

    audio.onerror = (e) => {
      console.error('Error loading audio:', audio.error || e);
      addToast(playbackErrorMessage(audio), 6000);
      setLoading(false);
      setIsPlaying(false);
      stopTimeTracking();
    };

    // Auto-play the audio
    try {
      await audio.play();
    } catch (err) {
      console.error('Error playing audio:', err);
      setLoading(false);
    }
  }, [startTimeTracking, stopTimeTracking, playbackRate, addToast]);

  // Load and play a queue of audio URLs (for chunked playback)
  // serverDurations: optional array of durations from backend (accurate via ffprobe)
  // When provided, these are used instead of browser metadata detection
  const loadAudioQueue = useCallback(async (urls, audioData, serverDurations = null) => {
    if (!urls || urls.length === 0) return;

    // If there's already audio playing, pause it first
    if (audioRef.current) {
      audioRef.current.pause();
      stopTimeTracking();
    }

    // Start preloading the first chunk IMMEDIATELY so audio is already
    // buffering while we compute durations below.  Reuse the gesture-
    // activated element on Safari (needed for autoplay permission).
    let preloadedElement;
    if (warmedAudioRef.current) {
      preloadedElement = warmedAudioRef.current;
      warmedAudioRef.current = null;
      preloadedElement.src = urls[0];
    } else {
      preloadedElement = new Audio(urls[0]);
      preloadedElement.preload = 'auto';
    }

    setLoading(true);
    setCurrentAudio(audioData);
    queueMetadataRef.current = audioData;
    setWaitingForChunks(false);

    // Store all URLs for seeking
    allChunkUrlsRef.current = urls;
    setTotalChunks(urls.length);
    setCurrentChunkIndex(0);
    currentChunkIndexRef.current = 0;

    // Use server-provided durations if available (accurate via ffprobe)
    // Otherwise fall back to browser metadata detection
    let durations;
    if (serverDurations && serverDurations.length === urls.length && serverDurations.every(d => d != null)) {
      durations = serverDurations;
    } else {
      durations = await preloadChunkDurations(urls);
    }
    chunkDurationsRef.current = durations;
    setChunkDurations(durations);

    const total = durations.reduce((a, b) => a + b, 0);
    setTotalDuration(total);
    setDuration(durations[0] || 0);
    setCumulativeTime(0);

    // Set up queue (remaining URLs after first)
    audioQueueRef.current = urls.slice(1);

    // Play the first chunk — pass the preloaded element so it doesn't
    // need to create and fetch a new one from scratch.
    playChunkAtTime(0, 0, true, preloadedElement);
  }, [stopTimeTracking, preloadChunkDurations, playChunkAtTime, setWaitingForChunks]);

  // Append a single chunk URL to the active audio queue (for streaming TTS)
  const appendChunkToQueue = useCallback(async (url, serverDuration = null) => {
    const urls = allChunkUrlsRef.current;

    // Queue invariant: one turn's audio never contains the same file
    // twice. Chunk URLs carry a per-generation cache-bust (?v=), and for
    // single-chunk nodes the chunk URL *is* the node's full tts URL — so
    // any double-delivery (SSE replay, chunk_ready + already-generated
    // POST racing on the same node) funnels into an identical URL here.
    // Warn loudly: if this fires we want to know which path delivered it.
    if (urls.includes(url)) {
      console.warn('[Audio] Skipped duplicate queue append:', url);
      return;
    }

    // Append URL
    urls.push(url);
    const newIndex = urls.length - 1;

    // Get duration: use server-provided or preload from browser
    let dur;
    if (serverDuration != null && isFinite(serverDuration) && serverDuration > 0) {
      dur = serverDuration;
    } else {
      const durations = await preloadChunkDurations([url]);
      dur = durations[0];
    }

    // Append duration
    chunkDurationsRef.current.push(dur);
    setChunkDurations([...chunkDurationsRef.current]);

    // Update totals
    setTotalChunks(urls.length);
    const newTotal = chunkDurationsRef.current.reduce((a, b) => a + b, 0);
    setTotalDuration(newTotal);

    // Also add to queue if playback hasn't reached this chunk yet
    audioQueueRef.current.push(url);

    // If playback ended waiting for more chunks, auto-play the new chunk
    if (waitingForChunksRef.current) {
      setWaitingForChunks(false);
      playChunkAtTime(newIndex, 0, true);
    }
  }, [preloadChunkDurations, playChunkAtTime, setWaitingForChunks]);

  const play = useCallback(async () => {
    if (audioRef.current && !isPlaying) {
      try {
        await audioRef.current.play();
      } catch (err) {
        console.error('Error playing audio:', err);
      }
      return;
    }
    // After stop() (#161) the element is released but currentAudio + (for
    // chunked playback) chunk URLs are retained. Re-init from the beginning.
    if (!audioRef.current) {
      if (allChunkUrlsRef.current.length > 0) {
        // Chunked playback: playChunkAtTime creates a fresh Audio() when
        // audioRef.current is null, using the retained chunk URLs.
        playChunkAtTime(0, 0, true);
      } else if (currentAudio && currentAudio.url) {
        // Single-clip playback: rebuild the element and replay from 0.
        loadAudio(currentAudio);
      }
    }
  }, [isPlaying, playChunkAtTime, currentAudio, loadAudio]);

  const pause = useCallback(() => {
    if (audioRef.current && isPlaying) {
      audioRef.current.pause();
    }
  }, [isPlaying]);

  // Stop (#161): reset playback to the beginning but KEEP the player mounted
  // and replayable. We invalidate any in-flight handlers, reset position to 0,
  // and — for Bluetooth safety — still fully release the HTMLAudioElement
  // (removeAttribute('src') + load() + null the ref) so the browser drops the
  // audio session and can cleanly switch A2DP->HFP when the mic is next
  // requested. We deliberately RETAIN currentAudio plus all chunk/queue
  // metadata (allChunkUrlsRef, chunkDurationsRef, queueMetadataRef,
  // totalChunks, chunkDurations, totalDuration) so the player stays visible;
  // play() re-inits a fresh element from chunk 0 using the retained URLs.
  const stop = useCallback(() => {
    if (audioRef.current) {
      // Invalidate any pending event handlers / interval callbacks bound to
      // the element we're about to release.
      playbackIdRef.current += 1;
      audioRef.current.pause();
      // Fully release the audio element so the browser drops the audio session.
      // A paused element with loaded media can hold the Bluetooth A2DP profile,
      // preventing a clean switch to HFP when the mic is requested next.
      audioRef.current.removeAttribute('src');
      audioRef.current.load();
      audioRef.current = null;
    }
    stopTimeTracking();
    setIsPlaying(false);
    // Reset playback position to the start (UI + tracking refs).
    setCurrentTime(0);
    setCumulativeTime(0);
    setCurrentChunkIndex(0);
    currentChunkIndexRef.current = 0;
    // Rebuild the queue so a subsequent play() restarts from chunk 0.
    audioQueueRef.current = allChunkUrlsRef.current.slice(1);
    setWaitingForChunks(false);
    // NOTE: intentionally NOT clearing currentAudio / allChunkUrlsRef /
    // chunkDurationsRef / queueMetadataRef / totalChunks / chunkDurations /
    // totalDuration — the player stays mounted and replayable. Use
    // closePlayer() for full teardown + hide.
  }, [stopTimeTracking, setWaitingForChunks]);

  // Close the player (#161): full teardown + hide. Releases the element AND
  // clears currentAudio plus all chunk/queue metadata so the player unmounts.
  // Wired to the X (close) affordance and used on refresh-equivalent dismissal.
  const closePlayer = useCallback(() => {
    if (audioRef.current) {
      playbackIdRef.current += 1;
      audioRef.current.pause();
      // Fully release the audio element so the browser drops the audio session.
      // A paused element with loaded media can hold the Bluetooth A2DP profile,
      // preventing a clean switch to HFP when the mic is requested next.
      audioRef.current.removeAttribute('src');
      audioRef.current.load();
      audioRef.current = null;
    }
    stopTimeTracking();
    setCurrentTime(0);
    setCumulativeTime(0);
    setIsPlaying(false);

    // Reset chunk state
    setCurrentChunkIndex(0);
    currentChunkIndexRef.current = 0;
    setTotalChunks(0);
    setChunkDurations([]);
    chunkDurationsRef.current = [];
    setTotalDuration(0);
    allChunkUrlsRef.current = [];
    audioQueueRef.current = [];
    queueMetadataRef.current = null;
    setWaitingForChunks(false);
    setCurrentAudio(null);
  }, [stopTimeTracking, setWaitingForChunks]);

  // Seek to a cumulative time position (works across chunks)
  const seekToCumulativeTime = useCallback((targetCumulativeTime) => {
    // Guard against invalid input
    if (!isFinite(targetCumulativeTime)) {
      console.warn('seekToCumulativeTime: invalid target time', targetCumulativeTime);
      return;
    }

    const durations = chunkDurationsRef.current;
    const urls = allChunkUrlsRef.current;

    // For single audio (no chunks)
    if (!durations.length || !urls.length) {
      if (audioRef.current && isFinite(audioRef.current.duration)) {
        const clampedTime = Math.max(0, Math.min(targetCumulativeTime, audioRef.current.duration));
        audioRef.current.currentTime = clampedTime;
        setCurrentTime(clampedTime);
        setCumulativeTime(clampedTime);
      }
      return;
    }

    // Clamp to valid range
    const totalDur = durations.reduce((a, b) => a + b, 0);
    if (!isFinite(totalDur) || totalDur <= 0) {
      console.warn('seekToCumulativeTime: invalid total duration', totalDur);
      return;
    }
    const clampedTime = Math.max(0, Math.min(targetCumulativeTime, totalDur));

    // If seeking to the exact end, just show end position without playing
    // (Playing at the end would immediately trigger onended)
    if (clampedTime >= totalDur - 0.1) {
      // Increment playback ID to invalidate any pending interval callbacks
      playbackIdRef.current += 1;
      if (audioRef.current) {
        audioRef.current.pause();
      }
      setIsPlaying(false);
      stopTimeTracking();
      const lastChunkIndex = durations.length - 1;
      setCurrentChunkIndex(lastChunkIndex);
      currentChunkIndexRef.current = lastChunkIndex;
      setCurrentTime(durations[lastChunkIndex] || 0);
      setCumulativeTime(totalDur);
      return;
    }

    // Find which chunk and position
    const { chunkIndex, timeInChunk } = findChunkForTime(clampedTime, durations);

    // If same chunk, just seek within it
    if (chunkIndex === currentChunkIndexRef.current && audioRef.current) {
      const safeTime = isFinite(timeInChunk) ? timeInChunk : 0;
      audioRef.current.currentTime = safeTime;
      setCurrentTime(safeTime);
      setCumulativeTime(clampedTime);
    } else {
      // Different chunk - need to load it
      const wasPlaying = isPlaying;
      if (audioRef.current) {
        audioRef.current.pause();
      }
      playChunkAtTime(chunkIndex, timeInChunk, wasPlaying);
    }
  }, [findChunkForTime, isPlaying, playChunkAtTime, stopTimeTracking]);

  const skipForward = useCallback(() => {
    const durations = chunkDurationsRef.current;

    // For single audio (no chunks)
    if (!durations.length) {
      if (audioRef.current) {
        audioRef.current.currentTime = Math.min(
          audioRef.current.currentTime + 10,
          audioRef.current.duration
        );
      }
      return;
    }

    const timeInChunk = audioRef.current?.currentTime || 0;
    const currentCumulative = calculateCumulativeTime(currentChunkIndexRef.current, timeInChunk);
    seekToCumulativeTime(currentCumulative + 10);
  }, [calculateCumulativeTime, seekToCumulativeTime]);

  const skipBackward = useCallback(() => {
    const durations = chunkDurationsRef.current;

    // For single audio (no chunks)
    if (!durations.length) {
      if (audioRef.current) {
        audioRef.current.currentTime = Math.max(
          audioRef.current.currentTime - 10,
          0
        );
      }
      return;
    }

    const timeInChunk = audioRef.current?.currentTime || 0;
    const currentCumulative = calculateCumulativeTime(currentChunkIndexRef.current, timeInChunk);
    seekToCumulativeTime(currentCumulative - 10);
  }, [calculateCumulativeTime, seekToCumulativeTime]);

  // Legacy seek (for single audio compatibility) - now delegates to cumulative seek
  const seek = useCallback((time) => {
    seekToCumulativeTime(time);
  }, [seekToCumulativeTime]);

  const changePlaybackRate = useCallback(() => {
    const rates = [1, 1.25, 1.5, 2];
    const currentIndex = rates.indexOf(playbackRate);
    const nextIndex = (currentIndex + 1) % rates.length;
    const newRate = rates[nextIndex];

    setPlaybackRate(newRate);
    if (audioRef.current) {
      const wasPlaying = !audioRef.current.paused;
      const currentTimeStamp = audioRef.current.currentTime;

      audioRef.current.playbackRate = newRate;

      // Ensure audio continues playing if it was playing before
      if (wasPlaying && audioRef.current.paused) {
        audioRef.current.currentTime = currentTimeStamp;
        audioRef.current.play().catch(err => console.error('Error resuming playback:', err));
      }
    }
  }, [playbackRate]);

  // Call during a user gesture (e.g. stop-recording click) to unlock audio on Safari.
  // Safari requires AudioContext to be created/resumed during a gesture; once unlocked,
  // subsequent HTMLAudioElement.play() calls are allowed even without a gesture.
  const warmup = useCallback(() => {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return;
    if (!webAudioCtxRef.current) {
      webAudioCtxRef.current = new AC();
    }
    const ctx = webAudioCtxRef.current;
    if (ctx.state === 'suspended') {
      ctx.resume();
    }
    // Play a silent buffer to fully activate the Web Audio pipeline
    const buffer = ctx.createBuffer(1, 1, 22050);
    const source = ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(ctx.destination);
    source.start(0);

    // Also pre-activate an HTMLAudioElement during this gesture.
    // Safari tracks per-element activation — reusing this element later bypasses autoplay.
    const silentWav = 'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQAAAAA=';
    const el = new Audio(silentWav);
    el.play().then(() => {
      el.pause();
      warmedAudioRef.current = el;
    }).catch(() => {});
  }, []);

  // Replace the chapter list on the currently-loaded audio (#145). Used
  // after TTS generation completes to swap the chapters fetched mid-stream
  // (their start times were computed from not-yet-generated chunk durations,
  // so later chapters clustered together) for the final, correctly-spaced
  // ones. Guarded on id/type so a different audio that started meanwhile is
  // not clobbered.
  const updateChapters = useCallback((id, type, chapters) => {
    setCurrentAudio((prev) =>
      prev && prev.id === id && prev.type === type
        ? { ...prev, chapters }
        : prev
    );
  }, []);

  const value = {
    currentAudio,
    isPlaying,
    currentTime,
    duration,
    loading,
    playbackRate,
    currentChunkIndex,
    totalChunks,
    // Chunked playback cumulative tracking
    cumulativeTime,
    totalDuration,
    chunkDurations,
    generatingTTS,
    setGeneratingTTS,
    waitingForChunks,
    loadAudio,
    loadAudioQueue,
    updateChapters,
    appendChunkToQueue,
    play,
    pause,
    stop,
    closePlayer,
    skipForward,
    skipBackward,
    seek,
    seekToCumulativeTime,
    changePlaybackRate,
    warmup,
  };

  return <AudioContext.Provider value={value}>{children}</AudioContext.Provider>;
};

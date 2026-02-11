import React, { createContext, useContext, useState, useRef, useCallback } from 'react';

const AudioContext = createContext();

export const useAudio = () => {
  const context = useContext(AudioContext);
  if (!context) {
    throw new Error('useAudio must be used within an AudioProvider');
  }
  return context;
};

export const AudioProvider = ({ children }) => {
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
  // Track whether playback ended while waiting for more chunks
  const waitingForChunksRef = useRef(false);
  const audioRef = useRef(null);
  const intervalRef = useRef(null);
  const audioQueueRef = useRef([]);
  const allChunkUrlsRef = useRef([]);
  const chunkDurationsRef = useRef([]);
  const currentChunkIndexRef = useRef(0);
  const queueMetadataRef = useRef(null);
  // Playback ID to prevent stale event handlers from updating state
  const playbackIdRef = useRef(0);
  // For WebM files with continuous timestamps: track the starting timestamp offset of current chunk
  // (MediaRecorder with timeslice produces chunks where timestamps continue across chunks)
  const chunkTimestampOffsetRef = useRef(0);
  // Pre-unlocked audio element for autoplay after async events (e.g. SSE chunk arrival)
  const warmedUpAudioRef = useRef(null);

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
        const rawTime = audioRef.current.currentTime;
        // Subtract timestamp offset for WebM files with continuous timestamps
        const timeInChunk = rawTime - chunkTimestampOffsetRef.current;
        // Read chunk index from ref directly for most up-to-date value
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

  // Create and play a chunk at a specific index and time
  const playChunkAtTime = useCallback((chunkIndex, timeInChunk, shouldAutoPlay = true) => {
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

    // Create new audio element (reuse warmed-up element if available to bypass autoplay restrictions)
    const chunkUrl = urls[chunkIndex];
    let audio;
    if (warmedUpAudioRef.current) {
      audio = warmedUpAudioRef.current;
      warmedUpAudioRef.current = null;
      audio.src = chunkUrl;
    } else {
      audio = new Audio(chunkUrl);
    }
    audioRef.current = audio;
    audio.playbackRate = playbackRate;

    // Detect if this is a WebM file with continuous timestamps
    // WebM files from MediaRecorder with timeslice have timestamps that continue
    // across chunks (chunk 0: 0-300s, chunk 1: 300-600s, etc.)
    // MP3 and other formats start at timestamp 0 in each file
    const isWebM = chunkUrl.toLowerCase().includes('.webm');

    // Track if we've already seeked (to avoid multiple seeks)
    let hasSeeked = false;
    // Track if we've detected the timestamp offset for this chunk
    let hasDetectedOffset = false;

    audio.onloadedmetadata = () => {
      // Check if this handler is still valid
      if (playbackIdRef.current !== thisPlaybackId) return;

      const actualDuration = audio.duration;

      if (isWebM) {
        // WebM duration includes all prior chunks, so don't compare directly
        // The ontimeupdate handler will detect the offset and correct durations
      } else {
        // For MP3 and other formats, update stored duration if significantly different
        if (isFinite(actualDuration) && Math.abs(durations[chunkIndex] - actualDuration) > 1) {
          const updatedDurations = [...chunkDurationsRef.current];
          updatedDurations[chunkIndex] = actualDuration;
          chunkDurationsRef.current = updatedDurations;
          setChunkDurations(updatedDurations);
          recalculateTotalDuration(updatedDurations);
        }
      }
      setDuration(actualDuration);
      setLoading(false);
    };

    // Use canplay event for seeking - this fires when the browser can actually seek
    audio.oncanplay = () => {
      if (playbackIdRef.current !== thisPlaybackId) return;
      if (hasSeeked) return; // Only seek once
      hasSeeked = true;

      if (isWebM) {
        // For WebM with continuous timestamps, add the expected offset
        const expectedOffset = durations.slice(0, chunkIndex).reduce((a, b) => a + b, 0);
        const rawSeekTime = safeTimeInChunk + expectedOffset;
        const clampedTime = Math.max(expectedOffset, Math.min(rawSeekTime, (audio.duration || expectedOffset + 300)));
        if (isFinite(clampedTime)) {
          audio.currentTime = clampedTime;
        }
      } else {
        // For MP3 and other formats, seek directly within the chunk
        if (safeTimeInChunk > 0 && isFinite(safeTimeInChunk)) {
          const clampedTime = Math.min(safeTimeInChunk, audio.duration || 300);
          audio.currentTime = clampedTime;
        }
      }
    };

    // Set the timestamp offset immediately based on format detection
    // (don't wait for ontimeupdate which fires too late for initial seek)
    if (isWebM) {
      // Will be refined in ontimeupdate, but set a reasonable initial value
      chunkTimestampOffsetRef.current = durations.slice(0, chunkIndex).reduce((a, b) => a + b, 0);
    } else {
      // MP3 and other formats always start at 0
      chunkTimestampOffsetRef.current = 0;
    }

    audio.ontimeupdate = () => {
      // Check if this handler is still valid
      if (playbackIdRef.current !== thisPlaybackId) return;

      const rawTime = audio.currentTime;

      // Refine timestamp offset detection on first time update (WebM only)
      if (!hasDetectedOffset) {
        hasDetectedOffset = true;
        if (isWebM) {
          const expectedOffset = durations.slice(0, chunkIndex).reduce((a, b) => a + b, 0);
          if (rawTime > expectedOffset - 5 && rawTime < expectedOffset + durations[chunkIndex] + 5) {
            chunkTimestampOffsetRef.current = expectedOffset;
          } else if (rawTime < 5) {
            chunkTimestampOffsetRef.current = 0;
          } else {
            chunkTimestampOffsetRef.current = rawTime;
          }
        }
        // For non-WebM, offset stays 0 (set above)
      }

      // Calculate time within chunk by subtracting the offset
      const timeInCurrentChunk = rawTime - chunkTimestampOffsetRef.current;
      setCurrentTime(timeInCurrentChunk);
      setCumulativeTime(calculateCumulativeTime(chunkIndex, timeInCurrentChunk));
    };

    audio.onended = () => {
      // Check if this handler is still valid
      if (playbackIdRef.current !== thisPlaybackId) return;

      // CRITICAL: Update this chunk's duration based on actual playback time
      // This fixes the issue where preloaded metadata durations are incorrect
      // (common with MediaRecorder-produced files that lack proper duration headers)
      // Subtract the timestamp offset to get the actual duration within this chunk
      const rawEndTime = audio.currentTime;
      const actualDuration = rawEndTime - chunkTimestampOffsetRef.current;
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
        waitingForChunksRef.current = true;
        setIsPlaying(false);
        stopTimeTracking();
      } else {
        // Playback finished - keep metadata so user can seek back or replay
        // Only stop() clears everything
        waitingForChunksRef.current = false;
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
      console.error('Error loading audio chunk:', e);
      setLoading(false);
      setIsPlaying(false);
      stopTimeTracking();
    };

    if (shouldAutoPlay) {
      audio.play().catch(err => console.error('Error playing chunk:', err));
    }
  }, [playbackRate, calculateCumulativeTime, startTimeTracking, stopTimeTracking, recalculateTotalDuration, cleanupAudio]);

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
    chunkTimestampOffsetRef.current = 0;

    setLoading(true);
    setCurrentAudio(audioData);

    // Create new audio element
    const audio = new Audio(audioData.url);
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
      console.error('Error loading audio:', e);
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
  }, [startTimeTracking, stopTimeTracking, playbackRate]);

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

    setLoading(true);
    setCurrentAudio(audioData);
    queueMetadataRef.current = audioData;

    // Store all URLs for seeking
    allChunkUrlsRef.current = urls;
    setTotalChunks(urls.length);
    setCurrentChunkIndex(0);
    currentChunkIndexRef.current = 0;
    chunkTimestampOffsetRef.current = 0;

    // Use server-provided durations if available (accurate via ffprobe)
    // Otherwise fall back to browser metadata detection (unreliable for WebM with continuous timestamps)
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

    // Play the first chunk
    playChunkAtTime(0, 0, true);
  }, [stopTimeTracking, preloadChunkDurations, playChunkAtTime]);

  // Append a single chunk URL to the active audio queue (for streaming TTS)
  const appendChunkToQueue = useCallback(async (url, serverDuration = null) => {
    const urls = allChunkUrlsRef.current;

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
      waitingForChunksRef.current = false;
      playChunkAtTime(newIndex, 0, true);
    }
  }, [preloadChunkDurations, playChunkAtTime]);

  // Call during a user gesture to unlock audio playback for later async use (e.g. SSE chunks)
  const warmUpAudio = useCallback(() => {
    const audio = new Audio();
    // Play a tiny silent WAV to establish the gesture-unlocked audio element
    audio.src = 'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQAAAAA=';
    audio.play().then(() => {
      audio.pause();
      warmedUpAudioRef.current = audio;
    }).catch(() => {});
  }, []);

  const play = useCallback(async () => {
    if (audioRef.current && !isPlaying) {
      try {
        await audioRef.current.play();
      } catch (err) {
        console.error('Error playing audio:', err);
      }
    }
  }, [isPlaying]);

  const pause = useCallback(() => {
    if (audioRef.current && isPlaying) {
      audioRef.current.pause();
    }
  }, [isPlaying]);

  const stop = useCallback(() => {
    if (audioRef.current) {
      audioRef.current.pause();
      audioRef.current.currentTime = 0;
      setCurrentTime(0);
      setCumulativeTime(0);
      setIsPlaying(false);
      stopTimeTracking();

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
      chunkTimestampOffsetRef.current = 0;
      setCurrentAudio(null);
    }
  }, [stopTimeTracking]);

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
      // Add timestamp offset for WebM with continuous timestamps
      const rawSeekTime = safeTime + chunkTimestampOffsetRef.current;
      audioRef.current.currentTime = rawSeekTime;
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

    // Calculate current cumulative time and add 10 seconds
    // Subtract the timestamp offset to get the actual position within the chunk
    const rawTime = audioRef.current?.currentTime || 0;
    const timeInChunk = rawTime - chunkTimestampOffsetRef.current;
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

    // Calculate current cumulative time and subtract 10 seconds
    // Subtract the timestamp offset to get the actual position within the chunk
    const rawTime = audioRef.current?.currentTime || 0;
    const timeInChunk = rawTime - chunkTimestampOffsetRef.current;
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
    loadAudio,
    loadAudioQueue,
    appendChunkToQueue,
    warmUpAudio,
    play,
    pause,
    stop,
    skipForward,
    skipBackward,
    seek,
    seekToCumulativeTime,
    changePlaybackRate,
  };

  return <AudioContext.Provider value={value}>{children}</AudioContext.Provider>;
};

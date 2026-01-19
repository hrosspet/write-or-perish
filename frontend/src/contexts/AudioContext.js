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
    return completedChunksDuration + timeInChunk;
  }, []);

  // Update current time periodically
  const startTimeTracking = useCallback(() => {
    if (intervalRef.current) {
      clearInterval(intervalRef.current);
    }
    // Capture the current playback ID to check if interval is still valid
    const currentPlaybackId = playbackIdRef.current;
    const currentChunkIdx = currentChunkIndexRef.current;

    intervalRef.current = setInterval(() => {
      // Check if this interval is still valid (playback ID hasn't changed)
      if (playbackIdRef.current !== currentPlaybackId) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
        return;
      }
      if (audioRef.current && isFinite(audioRef.current.currentTime)) {
        const timeInChunk = audioRef.current.currentTime;
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
  const preloadChunkDurations = useCallback(async (urls) => {
    const durations = await Promise.all(
      urls.map(url => new Promise((resolve) => {
        const audio = new Audio();
        audio.crossOrigin = 'use-credentials';
        audio.preload = 'metadata';
        audio.onloadedmetadata = () => {
          const dur = audio.duration;
          audio.src = ''; // Release the resource
          resolve(isFinite(dur) ? dur : 300);
        };
        audio.onerror = () => {
          // Fallback: estimate 5 minutes per chunk (typical recording chunk size)
          resolve(300);
        };
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

    // Create new audio element
    const audio = new Audio(urls[chunkIndex]);
    audioRef.current = audio;
    audio.playbackRate = playbackRate;

    // Track if we've already seeked (to avoid multiple seeks)
    let hasSeeked = false;

    audio.onloadedmetadata = () => {
      // Check if this handler is still valid
      if (playbackIdRef.current !== thisPlaybackId) return;

      const actualDuration = audio.duration;
      // Update the stored duration with actual value if it differs significantly
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
      if (hasSeeked) return; // Only seek once
      hasSeeked = true;

      const clampedTime = Math.max(0, Math.min(safeTimeInChunk, audio.duration || 0));
      if (clampedTime > 0 && isFinite(clampedTime)) {
        audio.currentTime = clampedTime;
      }
    };

    audio.ontimeupdate = () => {
      // Check if this handler is still valid
      if (playbackIdRef.current !== thisPlaybackId) return;

      const timeInCurrentChunk = audio.currentTime;
      setCurrentTime(timeInCurrentChunk);
      setCumulativeTime(calculateCumulativeTime(chunkIndex, timeInCurrentChunk));
    };

    audio.onended = () => {
      // Check if this handler is still valid
      if (playbackIdRef.current !== thisPlaybackId) return;

      const queue = audioQueueRef.current;
      if (queue.length > 0) {
        const nextIndex = chunkIndex + 1;
        playChunkAtTime(nextIndex, 0, true);
      } else {
        // Playback finished
        setIsPlaying(false);
        stopTimeTracking();
        setCurrentTime(0);
        setCumulativeTime(0);
        setCurrentChunkIndex(0);
        currentChunkIndexRef.current = 0;
        setTotalChunks(0);
        audioQueueRef.current = [];
        allChunkUrlsRef.current = [];
        chunkDurationsRef.current = [];
        setChunkDurations([]);
        setTotalDuration(0);
        queueMetadataRef.current = null;
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
  const loadAudioQueue = useCallback(async (urls, audioData) => {
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

    // Preload all chunk durations
    const durations = await preloadChunkDurations(urls);
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
  }, [findChunkForTime, isPlaying, playChunkAtTime]);

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
    const currentCumulative = calculateCumulativeTime(currentChunkIndexRef.current, audioRef.current?.currentTime || 0);
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
    const currentCumulative = calculateCumulativeTime(currentChunkIndexRef.current, audioRef.current?.currentTime || 0);
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
    loadAudio,
    loadAudioQueue,
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

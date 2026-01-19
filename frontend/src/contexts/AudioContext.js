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
    intervalRef.current = setInterval(() => {
      if (audioRef.current) {
        const timeInChunk = audioRef.current.currentTime;
        setCurrentTime(timeInChunk);
        setCumulativeTime(calculateCumulativeTime(currentChunkIndexRef.current, timeInChunk));
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
        audio.preload = 'metadata';
        audio.onloadedmetadata = () => {
          resolve(audio.duration);
          audio.src = ''; // Release the resource
        };
        audio.onerror = () => {
          console.error('Error loading chunk metadata:', url);
          resolve(0); // Fallback to 0 on error
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

  // Create and play a chunk at a specific index and time
  const playChunkAtTime = useCallback((chunkIndex, timeInChunk, shouldAutoPlay = true) => {
    const urls = allChunkUrlsRef.current;
    const durations = chunkDurationsRef.current;

    if (!urls.length || chunkIndex >= urls.length) return;

    // Update current chunk index
    currentChunkIndexRef.current = chunkIndex;
    setCurrentChunkIndex(chunkIndex);

    // Update queue to contain chunks after current one
    audioQueueRef.current = urls.slice(chunkIndex + 1);

    // Create new audio element
    const audio = new Audio(urls[chunkIndex]);
    audioRef.current = audio;
    audio.playbackRate = playbackRate;

    audio.onloadedmetadata = () => {
      setDuration(durations[chunkIndex]);
      audio.currentTime = timeInChunk;
      setLoading(false);
    };

    audio.ontimeupdate = () => {
      const timeInCurrentChunk = audio.currentTime;
      setCurrentTime(timeInCurrentChunk);
      setCumulativeTime(calculateCumulativeTime(currentChunkIndexRef.current, timeInCurrentChunk));
    };

    audio.onended = () => {
      const queue = audioQueueRef.current;
      if (queue.length > 0) {
        const nextIndex = currentChunkIndexRef.current + 1;
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
      setIsPlaying(false);
      stopTimeTracking();
    };

    audio.onplay = () => {
      setIsPlaying(true);
      startTimeTracking();
    };

    audio.onerror = (e) => {
      console.error('Error loading audio chunk:', e);
      setLoading(false);
      setIsPlaying(false);
      stopTimeTracking();
    };

    if (shouldAutoPlay) {
      audio.play().catch(err => console.error('Error playing chunk:', err));
    }
  }, [playbackRate, calculateCumulativeTime, startTimeTracking, stopTimeTracking]);

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
    const durations = chunkDurationsRef.current;
    const urls = allChunkUrlsRef.current;

    // For single audio (no chunks)
    if (!durations.length || !urls.length) {
      if (audioRef.current) {
        const clampedTime = Math.max(0, Math.min(targetCumulativeTime, audioRef.current.duration || 0));
        audioRef.current.currentTime = clampedTime;
        setCurrentTime(clampedTime);
        setCumulativeTime(clampedTime);
      }
      return;
    }

    // Clamp to valid range
    const totalDur = durations.reduce((a, b) => a + b, 0);
    const clampedTime = Math.max(0, Math.min(targetCumulativeTime, totalDur));

    // Find which chunk and position
    const { chunkIndex, timeInChunk } = findChunkForTime(clampedTime, durations);

    // If same chunk, just seek within it
    if (chunkIndex === currentChunkIndexRef.current && audioRef.current) {
      audioRef.current.currentTime = timeInChunk;
      setCurrentTime(timeInChunk);
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

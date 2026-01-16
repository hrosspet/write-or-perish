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
  const audioRef = useRef(null);
  const intervalRef = useRef(null);
  const audioQueueRef = useRef([]);
  const queueMetadataRef = useRef(null);

  // Update current time periodically
  const startTimeTracking = useCallback(() => {
    if (intervalRef.current) {
      clearInterval(intervalRef.current);
    }
    intervalRef.current = setInterval(() => {
      if (audioRef.current) {
        setCurrentTime(audioRef.current.currentTime);
      }
    }, 100);
  }, []);

  const stopTimeTracking = useCallback(() => {
    if (intervalRef.current) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
  }, []);

  const loadAudio = useCallback(async (audioData) => {
    // If there's already audio playing, pause it first
    if (audioRef.current) {
      audioRef.current.pause();
      stopTimeTracking();
    }

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
      setLoading(false);
    };

    audio.ontimeupdate = () => {
      setCurrentTime(audio.currentTime);
    };

    audio.onended = () => {
      // Check if there are more chunks in the queue
      const queue = audioQueueRef.current;

      if (queue.length > 0) {
        // Play next chunk
        const nextUrl = queue.shift();
        setCurrentChunkIndex(prev => prev + 1);

        const nextAudio = new Audio(nextUrl);
        audioRef.current = nextAudio;
        nextAudio.playbackRate = playbackRate;

        nextAudio.onloadedmetadata = () => {
          setDuration(prev => prev); // Keep accumulated duration
        };
        nextAudio.ontimeupdate = () => setCurrentTime(nextAudio.currentTime);
        nextAudio.onended = audio.onended; // Reuse same handler
        nextAudio.onpause = () => { setIsPlaying(false); stopTimeTracking(); };
        nextAudio.onplay = () => { setIsPlaying(true); startTimeTracking(); };
        nextAudio.onerror = audio.onerror;

        nextAudio.play().catch(err => console.error('Error playing next chunk:', err));
      } else {
        // Queue finished
        setIsPlaying(false);
        stopTimeTracking();
        setCurrentTime(0);
        setCurrentChunkIndex(0);
        setTotalChunks(0);
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

    // Store remaining URLs in queue (skip first one, we'll play it directly)
    audioQueueRef.current = urls.slice(1);
    queueMetadataRef.current = audioData;
    setTotalChunks(urls.length);
    setCurrentChunkIndex(0);

    setLoading(true);
    setCurrentAudio(audioData);

    // Create audio element for first chunk
    const audio = new Audio(urls[0]);
    audioRef.current = audio;
    audio.playbackRate = playbackRate;

    audio.onloadedmetadata = () => {
      setDuration(audio.duration);
      setLoading(false);
    };

    audio.ontimeupdate = () => {
      setCurrentTime(audio.currentTime);
    };

    audio.onended = () => {
      // Check if there are more chunks in the queue
      const queue = audioQueueRef.current;

      if (queue.length > 0) {
        // Play next chunk
        const nextUrl = queue.shift();
        setCurrentChunkIndex(prev => prev + 1);

        const nextAudio = new Audio(nextUrl);
        audioRef.current = nextAudio;
        nextAudio.playbackRate = playbackRate;

        nextAudio.onloadedmetadata = () => {
          setDuration(prev => prev);
        };
        nextAudio.ontimeupdate = () => setCurrentTime(nextAudio.currentTime);
        nextAudio.onended = audio.onended;
        nextAudio.onpause = () => { setIsPlaying(false); stopTimeTracking(); };
        nextAudio.onplay = () => { setIsPlaying(true); startTimeTracking(); };
        nextAudio.onerror = audio.onerror;

        nextAudio.play().catch(err => console.error('Error playing next chunk:', err));
      } else {
        // Queue finished
        setIsPlaying(false);
        stopTimeTracking();
        setCurrentTime(0);
        setCurrentChunkIndex(0);
        setTotalChunks(0);
        audioQueueRef.current = [];
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
      audioQueueRef.current = [];
      queueMetadataRef.current = null;
    };

    // Auto-play the first chunk
    try {
      await audio.play();
    } catch (err) {
      console.error('Error playing audio:', err);
      setLoading(false);
    }
  }, [startTimeTracking, stopTimeTracking, playbackRate]);

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
      setIsPlaying(false);
      stopTimeTracking();
    }
  }, [stopTimeTracking]);

  const skipForward = useCallback(() => {
    if (audioRef.current) {
      audioRef.current.currentTime = Math.min(
        audioRef.current.currentTime + 10,
        audioRef.current.duration
      );
    }
  }, []);

  const skipBackward = useCallback(() => {
    if (audioRef.current) {
      audioRef.current.currentTime = Math.max(
        audioRef.current.currentTime - 10,
        0
      );
    }
  }, []);

  const seek = useCallback((time) => {
    if (audioRef.current) {
      audioRef.current.currentTime = time;
      setCurrentTime(time);
    }
  }, []);

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
    loadAudio,
    loadAudioQueue,
    play,
    pause,
    stop,
    skipForward,
    skipBackward,
    seek,
    changePlaybackRate,
  };

  return <AudioContext.Provider value={value}>{children}</AudioContext.Provider>;
};

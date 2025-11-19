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
  const audioRef = useRef(null);
  const intervalRef = useRef(null);

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
      setIsPlaying(false);
      stopTimeTracking();
      setCurrentTime(0);
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
      audioRef.current.playbackRate = newRate;
    }
  }, [playbackRate]);

  const value = {
    currentAudio,
    isPlaying,
    currentTime,
    duration,
    loading,
    playbackRate,
    loadAudio,
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

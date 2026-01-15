import { useState, useCallback, useRef, useEffect } from 'react';
import { useTTSStreamSSE } from './useSSE';
import api from '../api';

/**
 * useStreamingTTS - Hook for streaming TTS playback.
 *
 * Initiates TTS generation and starts playing audio chunks as soon as
 * they're ready, without waiting for the entire generation to complete.
 *
 * @param {number} nodeId - Node ID to generate TTS for
 * @param {Object} options
 * @param {boolean} options.autoPlay - Automatically start playing when first chunk is ready
 * @param {number} options.playbackRate - Playback speed (default: 1)
 * @param {Function} options.onComplete - Called when playback finishes
 * @param {Function} options.onError - Called on errors
 */
export function useStreamingTTS(nodeId, options = {}) {
  const {
    autoPlay = true,
    playbackRate = 1,
    onComplete = null,
    onError = null,
  } = options;

  // State
  const [state, setState] = useState('idle'); // idle, generating, playing, paused, complete, error
  const [progress, setProgress] = useState(0);
  const [currentChunkIndex, setCurrentChunkIndex] = useState(0);
  const [totalChunks, setTotalChunks] = useState(0);
  const [isGenerating, setIsGenerating] = useState(false);
  const [errorMessage, setErrorMessage] = useState(null);

  // Audio refs
  const audioRef = useRef(null);
  const audioQueueRef = useRef([]); // Queue of audio URLs to play
  const isPlayingRef = useRef(false);

  // SSE subscription for TTS chunk updates
  const {
    isConnected: sseConnected,
    audioChunks,
    isComplete: generationComplete,
    finalUrl,
    getAudioQueue,
    disconnect: disconnectSSE,
    reset: resetSSE,
  } = useTTSStreamSSE(nodeId, {
    enabled: isGenerating,
    onChunkReady: (data) => {
      // Add new chunk to queue
      audioQueueRef.current.push(data.audio_url);
      setTotalChunks(audioQueueRef.current.length);

      // If autoPlay and not already playing, start playback
      if (autoPlay && !isPlayingRef.current && state !== 'playing') {
        playNextChunk();
      }
    },
    onAllComplete: (data) => {
      setIsGenerating(false);
      // If not currently playing anything, we're done
      if (!isPlayingRef.current && currentChunkIndex >= audioQueueRef.current.length) {
        setState('complete');
        if (onComplete) {
          onComplete({ finalUrl: data.tts_url });
        }
      }
    },
  });

  // Play the next chunk in the queue
  const playNextChunk = useCallback(() => {
    if (currentChunkIndex >= audioQueueRef.current.length) {
      // No more chunks to play
      if (generationComplete) {
        // All done
        setState('complete');
        isPlayingRef.current = false;
        if (onComplete) {
          onComplete({ finalUrl });
        }
      }
      return;
    }

    const url = audioQueueRef.current[currentChunkIndex];

    if (audioRef.current) {
      audioRef.current.pause();
    }

    const audio = new Audio(url);
    audioRef.current = audio;
    audio.playbackRate = playbackRate;

    audio.onended = () => {
      setCurrentChunkIndex(prev => prev + 1);
      // Play next chunk
      setTimeout(() => {
        if (isPlayingRef.current) {
          playNextChunk();
        }
      }, 50); // Small delay between chunks for smoother transition
    };

    audio.onerror = (e) => {
      console.error('Audio playback error:', e);
      // Try to continue with next chunk
      setCurrentChunkIndex(prev => prev + 1);
      if (isPlayingRef.current) {
        playNextChunk();
      }
    };

    audio.play().then(() => {
      setState('playing');
      isPlayingRef.current = true;
    }).catch(err => {
      console.error('Failed to play audio:', err);
      setErrorMessage(err.message);
      if (onError) {
        onError(err);
      }
    });
  }, [currentChunkIndex, generationComplete, finalUrl, playbackRate, onComplete, onError]);

  // Update playback rate when it changes
  useEffect(() => {
    if (audioRef.current) {
      audioRef.current.playbackRate = playbackRate;
    }
  }, [playbackRate]);

  // Start TTS generation and playback
  const startTTS = useCallback(async () => {
    if (!nodeId) {
      console.error('Cannot start TTS: no nodeId');
      return;
    }

    // Reset state
    audioQueueRef.current = [];
    setCurrentChunkIndex(0);
    setTotalChunks(0);
    setProgress(0);
    setErrorMessage(null);
    resetSSE();

    setState('generating');
    setIsGenerating(true);

    try {
      // Trigger TTS generation
      await api.post(`/nodes/${nodeId}/tts`);

      // SSE will handle receiving chunks and triggering playback

    } catch (err) {
      console.error('Failed to start TTS:', err);
      setState('error');
      setIsGenerating(false);
      setErrorMessage(err.response?.data?.error || err.message);
      if (onError) {
        onError(err);
      }
    }
  }, [nodeId, resetSSE, onError]);

  // Pause playback
  const pause = useCallback(() => {
    if (audioRef.current && state === 'playing') {
      audioRef.current.pause();
      setState('paused');
      isPlayingRef.current = false;
    }
  }, [state]);

  // Resume playback
  const resume = useCallback(() => {
    if (audioRef.current && state === 'paused') {
      audioRef.current.play();
      setState('playing');
      isPlayingRef.current = true;
    } else if (state === 'paused' && currentChunkIndex < audioQueueRef.current.length) {
      // Resume from queue
      isPlayingRef.current = true;
      playNextChunk();
    }
  }, [state, currentChunkIndex, playNextChunk]);

  // Stop playback completely
  const stop = useCallback(() => {
    if (audioRef.current) {
      audioRef.current.pause();
      audioRef.current.currentTime = 0;
    }
    disconnectSSE();
    setState('idle');
    isPlayingRef.current = false;
    setIsGenerating(false);
  }, [disconnectSSE]);

  // Clean up on unmount
  useEffect(() => {
    return () => {
      if (audioRef.current) {
        audioRef.current.pause();
      }
      disconnectSSE();
    };
  }, [disconnectSSE]);

  return {
    // State
    state,
    isPlaying: state === 'playing',
    isPaused: state === 'paused',
    isGenerating,
    currentChunkIndex,
    totalChunks,
    errorMessage,

    // Connection
    isSSEConnected: sseConnected,

    // Audio chunks info
    audioChunks,
    generationComplete,
    finalUrl,

    // Actions
    startTTS,
    pause,
    resume,
    stop,
  };
}

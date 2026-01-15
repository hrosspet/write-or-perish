import React, { useState, useCallback } from 'react';
import { useStreamingTTS } from '../hooks/useStreamingTTS';

/**
 * StreamingAudioPlayer - Audio player that supports streaming TTS playback.
 *
 * Starts playing audio as soon as the first chunk is ready, without waiting
 * for the entire TTS generation to complete.
 *
 * Props:
 * @param {number} nodeId - Node ID to play TTS for
 * @param {Function} onComplete - Called when playback finishes
 * @param {Function} onError - Called on errors
 */
export default function StreamingAudioPlayer({
  nodeId,
  onComplete = null,
  onError = null,
}) {
  const [playbackRate, setPlaybackRate] = useState(1);

  const {
    state,
    isPlaying,
    isPaused,
    isGenerating,
    currentChunkIndex,
    totalChunks,
    errorMessage,
    isSSEConnected,
    generationComplete,
    startTTS,
    pause,
    resume,
    stop,
  } = useStreamingTTS(nodeId, {
    autoPlay: true,
    playbackRate,
    onComplete,
    onError,
  });

  const handlePlayPause = useCallback(() => {
    if (state === 'idle' || state === 'complete' || state === 'error') {
      startTTS();
    } else if (isPlaying) {
      pause();
    } else if (isPaused) {
      resume();
    }
  }, [state, isPlaying, isPaused, startTTS, pause, resume]);

  const handleStop = useCallback(() => {
    stop();
  }, [stop]);

  const cyclePlaybackRate = useCallback(() => {
    const rates = [1, 1.25, 1.5, 2];
    const currentIndex = rates.indexOf(playbackRate);
    const nextIndex = (currentIndex + 1) % rates.length;
    setPlaybackRate(rates[nextIndex]);
  }, [playbackRate]);

  // Get status text
  const getStatusText = () => {
    switch (state) {
      case 'idle':
        return 'Ready';
      case 'generating':
        return isSSEConnected
          ? `Generating... ${totalChunks > 0 ? `(${totalChunks} chunks ready)` : ''}`
          : 'Connecting...';
      case 'playing':
        return `Playing chunk ${currentChunkIndex + 1}/${totalChunks}${!generationComplete ? ' (generating...)' : ''}`;
      case 'paused':
        return 'Paused';
      case 'complete':
        return 'Complete';
      case 'error':
        return errorMessage || 'Error';
      default:
        return '';
    }
  };

  const containerStyle = {
    display: 'flex',
    flexDirection: 'column',
    gap: '8px',
    padding: '12px',
    backgroundColor: '#f8f9fa',
    borderRadius: '8px',
    border: '1px solid #e9ecef',
  };

  const controlsStyle = {
    display: 'flex',
    alignItems: 'center',
    gap: '8px',
  };

  const buttonStyle = (isActive = false) => ({
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    width: '36px',
    height: '36px',
    border: 'none',
    borderRadius: '50%',
    backgroundColor: isActive ? '#007bff' : '#e9ecef',
    color: isActive ? '#fff' : '#495057',
    cursor: 'pointer',
    transition: 'all 0.2s ease',
  });

  const statusStyle = {
    fontSize: '0.85em',
    color: state === 'error' ? '#dc3545' : '#6c757d',
  };

  const progressStyle = {
    display: 'flex',
    alignItems: 'center',
    gap: '8px',
    fontSize: '0.8em',
    color: '#6c757d',
  };

  return (
    <div style={containerStyle}>
      <div style={controlsStyle}>
        {/* Play/Pause Button */}
        <button
          type="button"
          onClick={handlePlayPause}
          style={buttonStyle(isPlaying)}
          title={isPlaying ? 'Pause' : 'Play'}
        >
          {isPlaying ? <PauseIcon /> : <PlayIcon />}
        </button>

        {/* Stop Button */}
        <button
          type="button"
          onClick={handleStop}
          style={buttonStyle()}
          title="Stop"
          disabled={state === 'idle'}
        >
          <StopIcon />
        </button>

        {/* Playback Rate */}
        <button
          type="button"
          onClick={cyclePlaybackRate}
          style={{
            ...buttonStyle(),
            width: 'auto',
            borderRadius: '4px',
            padding: '4px 8px',
            fontSize: '0.8em',
          }}
          title="Change playback speed"
        >
          {playbackRate}x
        </button>

        {/* Streaming Indicator */}
        {(isGenerating || !generationComplete) && state !== 'idle' && (
          <span style={{ color: isSSEConnected ? '#28a745' : '#ffc107', fontSize: '0.8em' }}>
            {isSSEConnected ? 'Streaming' : 'Connecting...'}
          </span>
        )}
      </div>

      {/* Status */}
      <div style={statusStyle}>{getStatusText()}</div>

      {/* Progress indicator */}
      {totalChunks > 0 && (
        <div style={progressStyle}>
          <div style={{
            flex: 1,
            height: '4px',
            backgroundColor: '#e9ecef',
            borderRadius: '2px',
            overflow: 'hidden',
          }}>
            <div style={{
              width: `${((currentChunkIndex + 1) / totalChunks) * 100}%`,
              height: '100%',
              backgroundColor: '#007bff',
              transition: 'width 0.3s ease',
            }} />
          </div>
          <span>{currentChunkIndex + 1}/{totalChunks}</span>
        </div>
      )}
    </div>
  );
}

// Icon components
const PlayIcon = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
    <path d="M8 5v14l11-7z" />
  </svg>
);

const PauseIcon = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
    <path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z" />
  </svg>
);

const StopIcon = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
    <rect x="6" y="6" width="12" height="12" />
  </svg>
);

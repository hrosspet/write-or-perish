import React from 'react';
import { FaPlay, FaPause, FaStop, FaStepBackward, FaStepForward } from 'react-icons/fa';
import { useAudio } from '../contexts/AudioContext';

const GlobalAudioPlayer = () => {
  const {
    currentAudio,
    isPlaying,
    currentTime,
    duration,
    loading,
    play,
    pause,
    stop,
    skipBackward,
    skipForward,
    seek,
  } = useAudio();

  // Don't show the player if there's no audio loaded
  if (!currentAudio) {
    return null;
  }

  const formatTime = (seconds) => {
    if (isNaN(seconds) || !isFinite(seconds)) return '0:00';
    const mins = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);
    return `${mins}:${secs.toString().padStart(2, '0')}`;
  };

  const handleSeek = (e) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const percentage = x / rect.width;
    const newTime = percentage * duration;
    seek(newTime);
  };

  return (
    <div style={{
      position: 'fixed',
      top: 0,
      left: 0,
      right: 0,
      backgroundColor: '#2a2a2a',
      borderBottom: '1px solid #444',
      padding: '8px 16px',
      display: 'flex',
      alignItems: 'center',
      gap: '12px',
      zIndex: 999,
      boxShadow: '0 2px 8px rgba(0,0,0,0.3)',
    }}>
      {/* Title */}
      <div style={{
        color: '#e0e0e0',
        fontSize: '14px',
        fontWeight: '500',
        minWidth: '150px',
        overflow: 'hidden',
        textOverflow: 'ellipsis',
        whiteSpace: 'nowrap',
      }}>
        {currentAudio.title || 'Audio Player'}
      </div>

      {/* Controls */}
      <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
        <button
          onClick={skipBackward}
          disabled={loading}
          title="Skip back 10 seconds"
          style={{
            background: 'none',
            border: 'none',
            color: loading ? '#666' : '#e0e0e0',
            cursor: loading ? 'not-allowed' : 'pointer',
            fontSize: '16px',
            padding: '4px',
            display: 'flex',
            alignItems: 'center',
          }}
        >
          <FaStepBackward />
        </button>

        {isPlaying ? (
          <button
            onClick={pause}
            disabled={loading}
            title="Pause"
            style={{
              background: 'none',
              border: 'none',
              color: loading ? '#666' : '#61dafb',
              cursor: loading ? 'not-allowed' : 'pointer',
              fontSize: '18px',
              padding: '4px',
              display: 'flex',
              alignItems: 'center',
            }}
          >
            <FaPause />
          </button>
        ) : (
          <button
            onClick={play}
            disabled={loading}
            title="Play"
            style={{
              background: 'none',
              border: 'none',
              color: loading ? '#666' : '#61dafb',
              cursor: loading ? 'not-allowed' : 'pointer',
              fontSize: '18px',
              padding: '4px',
              display: 'flex',
              alignItems: 'center',
            }}
          >
            <FaPlay />
          </button>
        )}

        <button
          onClick={stop}
          disabled={loading}
          title="Stop"
          style={{
            background: 'none',
            border: 'none',
            color: loading ? '#666' : '#e0e0e0',
            cursor: loading ? 'not-allowed' : 'pointer',
            fontSize: '16px',
            padding: '4px',
            display: 'flex',
            alignItems: 'center',
          }}
        >
          <FaStop />
        </button>

        <button
          onClick={skipForward}
          disabled={loading}
          title="Skip forward 10 seconds"
          style={{
            background: 'none',
            border: 'none',
            color: loading ? '#666' : '#e0e0e0',
            cursor: loading ? 'not-allowed' : 'pointer',
            fontSize: '16px',
            padding: '4px',
            display: 'flex',
            alignItems: 'center',
          }}
        >
          <FaStepForward />
        </button>
      </div>

      {/* Time display */}
      <div style={{
        color: '#b0b0b0',
        fontSize: '12px',
        minWidth: '80px',
      }}>
        {formatTime(currentTime)} / {formatTime(duration)}
      </div>

      {/* Progress bar */}
      <div
        onClick={handleSeek}
        style={{
          flex: 1,
          height: '6px',
          backgroundColor: '#444',
          borderRadius: '3px',
          cursor: 'pointer',
          position: 'relative',
          overflow: 'hidden',
        }}
      >
        <div
          style={{
            height: '100%',
            width: `${duration > 0 ? (currentTime / duration) * 100 : 0}%`,
            backgroundColor: '#61dafb',
            borderRadius: '3px',
            transition: 'width 0.1s linear',
          }}
        />
      </div>
    </div>
  );
};

export default GlobalAudioPlayer;

import React from 'react';
import { FaPlay, FaPause, FaStop, FaUndo, FaRedo } from 'react-icons/fa';
import { useAudio } from '../contexts/AudioContext';

const GlobalAudioPlayer = () => {
  const {
    currentAudio,
    isPlaying,
    currentTime,
    duration,
    loading,
    playbackRate,
    play,
    pause,
    stop,
    skipBackward,
    skipForward,
    seek,
    changePlaybackRate,
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
      display: 'flex',
      alignItems: 'center',
      gap: '12px',
      marginLeft: 'auto',
    }}>
      {/* Title */}
      <div style={{
        color: '#e0e0e0',
        fontSize: '13px',
        fontWeight: '400',
        maxWidth: '200px',
        overflow: 'hidden',
        textOverflow: 'ellipsis',
        whiteSpace: 'nowrap',
      }}>
        {currentAudio.title || 'Audio Player'}
      </div>

      {/* Controls */}
      <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
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
              fontSize: '16px',
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
              fontSize: '16px',
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
            fontSize: '14px',
            padding: '4px',
            display: 'flex',
            alignItems: 'center',
          }}
        >
          <FaStop />
        </button>

        <button
          onClick={skipBackward}
          disabled={loading}
          title="Skip back 10 seconds"
          style={{
            background: 'none',
            border: 'none',
            color: loading ? '#666' : '#e0e0e0',
            cursor: loading ? 'not-allowed' : 'pointer',
            fontSize: '14px',
            padding: '4px',
            display: 'flex',
            alignItems: 'center',
          }}
        >
          <FaUndo />
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
            fontSize: '14px',
            padding: '4px',
            display: 'flex',
            alignItems: 'center',
          }}
        >
          <FaRedo />
        </button>

        <button
          onClick={changePlaybackRate}
          disabled={loading}
          title="Change playback speed"
          style={{
            background: 'none',
            border: '1px solid #666',
            borderRadius: '4px',
            color: loading ? '#666' : '#e0e0e0',
            cursor: loading ? 'not-allowed' : 'pointer',
            fontSize: '11px',
            padding: '2px 6px',
            display: 'flex',
            alignItems: 'center',
            fontWeight: '500',
            minWidth: '38px',
            justifyContent: 'center',
          }}
        >
          {playbackRate}x
        </button>
      </div>

      {/* Time display */}
      <div style={{
        color: '#b0b0b0',
        fontSize: '11px',
        minWidth: '70px',
      }}>
        {formatTime(currentTime)} / {formatTime(duration)}
      </div>

      {/* Progress bar */}
      <div
        onClick={handleSeek}
        style={{
          width: '150px',
          height: '5px',
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

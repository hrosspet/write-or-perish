import React from 'react';
import { FaPlay, FaPause, FaStop, FaUndo, FaRedo } from 'react-icons/fa';
import { useAudio } from '../contexts/AudioContext';

const GlobalAudioPlayer = () => {
  const {
    currentAudio,
    isPlaying,
    loading,
    playbackRate,
    // Use cumulative values for display (falls back gracefully for single audio)
    cumulativeTime,
    totalDuration,
    generatingTTS,
    play,
    pause,
    stop,
    skipBackward,
    skipForward,
    seekToCumulativeTime,
    changePlaybackRate,
  } = useAudio();

  // Use cumulative time/duration for display with fallbacks
  const displayTime = cumulativeTime || 0;
  const displayDuration = totalDuration || 0;

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
    if (!displayDuration || displayDuration <= 0) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const percentage = Math.max(0, Math.min(1, x / rect.width));
    const newTime = percentage * displayDuration;
    if (isFinite(newTime)) {
      seekToCumulativeTime(newTime);
    }
  };

  return (
    <div style={{
      display: 'flex',
      alignItems: 'center',
      gap: '12px',
    }}>
      {/* Title */}
      <div style={{
        color: 'var(--text-primary)',
        fontSize: '13px',
        fontWeight: '300',
        fontFamily: 'var(--sans)',
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
              color: loading ? 'var(--text-muted)' : 'var(--accent)',
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
              color: loading ? 'var(--text-muted)' : 'var(--accent)',
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
            color: loading ? 'var(--text-muted)' : 'var(--text-primary)',
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
            color: loading ? 'var(--text-muted)' : 'var(--text-primary)',
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
            color: loading ? 'var(--text-muted)' : 'var(--text-primary)',
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
            border: '1px solid var(--border)',
            borderRadius: '4px',
            color: loading ? 'var(--text-muted)' : 'var(--text-primary)',
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
        color: 'var(--text-muted)',
        fontSize: '11px',
        minWidth: '70px',
        display: 'flex',
        alignItems: 'center',
        gap: '4px',
      }}>
        {formatTime(displayTime)} / {formatTime(displayDuration)}
        {generatingTTS && (
          <span
            style={{
              fontSize: '9px',
              color: 'var(--accent)',
              animation: 'pulse 1.5s ease-in-out infinite',
            }}
            title="Generating more audio..."
          >
            ●
          </span>
        )}
      </div>

      {/* Progress bar */}
      <div
        onClick={handleSeek}
        style={{
          width: '150px',
          height: '5px',
          backgroundColor: 'var(--bg-card)',
          borderRadius: '3px',
          cursor: 'pointer',
          position: 'relative',
          overflow: 'hidden',
        }}
      >
        <div
          style={{
            height: '100%',
            width: `${displayDuration > 0 ? (displayTime / displayDuration) * 100 : 0}%`,
            backgroundColor: 'var(--accent)',
            borderRadius: '3px',
            transition: 'width 0.1s linear',
          }}
        />
      </div>
    </div>
  );
};

export default GlobalAudioPlayer;

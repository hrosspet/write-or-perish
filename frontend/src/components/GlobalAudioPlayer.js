import React, { useEffect, useRef } from 'react';
import { FaPlay, FaPause, FaStop, FaUndo, FaRedo } from 'react-icons/fa';
import { useAudio } from '../contexts/AudioContext';
import { useIsMobile } from '../hooks/useIsMobile';

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
    closePlayer,
    skipBackward,
    skipForward,
    seekToCumulativeTime,
    changePlaybackRate,
  } = useAudio();

  // Below 640px the player can't fit inline in the 56px NavBar row, so it
  // pops out into a fixed bottom floating card (#28).
  const isMobile = useIsMobile(640);
  const cardRef = useRef(null);

  // Publish the floating card's occupied height (from its top to the
  // viewport bottom) as --floating-player-offset so the toast stack can
  // sit ABOVE the player instead of covering it. ToastProvider is an
  // ancestor of AudioProvider, so it can't read audio state directly — a
  // CSS variable on :root is the decoupled hand-off. Cleared whenever the
  // floating card isn't shown.
  useEffect(() => {
    const root = document.documentElement;
    const clear = () => root.style.setProperty('--floating-player-offset', '0px');

    if (!isMobile || !currentAudio) {
      clear();
      return undefined;
    }
    const el = cardRef.current;
    if (!el) {
      clear();
      return undefined;
    }
    const update = () => {
      const top = el.getBoundingClientRect().top;
      // +8px breathing room between a toast and the player.
      const offset = Math.max(0, window.innerHeight - top + 8);
      root.style.setProperty('--floating-player-offset', `${offset}px`);
    };
    update();
    let ro;
    if (typeof ResizeObserver !== 'undefined') {
      ro = new ResizeObserver(update);
      ro.observe(el);
    }
    window.addEventListener('resize', update);
    return () => {
      if (ro) ro.disconnect();
      window.removeEventListener('resize', update);
      clear();
    };
  }, [isMobile, currentAudio]);

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

  const inner = (
    <>
      {/* Title */}
      <div style={{
        color: 'var(--text-primary)',
        fontSize: '13px',
        fontWeight: '300',
        fontFamily: 'var(--sans)',
        maxWidth: isMobile ? 'none' : '200px',
        flex: isMobile ? '1 1 auto' : '0 0 auto',
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
          width: isMobile ? 'auto' : '150px',
          flex: isMobile ? '1 1 auto' : '0 0 auto',
          minWidth: isMobile ? '60px' : undefined,
          height: '5px',
          backgroundColor: isMobile ? 'var(--bg-surface)' : 'var(--bg-card)',
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
    </>
  );

  // Desktop: render inline inside the NavBar row. Stop (the square button in
  // `inner`) resets to 0 and keeps the player visible (#161); the X below
  // fully dismisses it.
  if (!isMobile) {
    return (
      <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
        {inner}
        {/* Close — full teardown + hide the player (#161 closePlayer). */}
        <button
          onClick={closePlayer}
          title="Close player"
          aria-label="Close player"
          style={{
            background: 'none',
            border: 'none',
            color: 'var(--text-muted)',
            cursor: 'pointer',
            fontSize: '1rem',
            lineHeight: 1,
            flexShrink: 0,
            padding: '4px',
            display: 'flex',
            alignItems: 'center',
          }}
        >
          ✕
        </button>
      </div>
    );
  }

  // Mobile: pop out of the NavBar into a persistent floating card pinned to
  // the bottom, styled like the toasts (#28). Stays until the user closes
  // it with ✕ or refreshes the tab. zIndex sits just below the toast stack
  // (9999) so toasts render above it; the toast container offsets itself by
  // --floating-player-offset (set in the effect above) to avoid overlap.
  return (
    <div
      ref={cardRef}
      style={{
        position: 'fixed',
        bottom: 'max(16px, env(safe-area-inset-bottom))',
        left: '50%',
        transform: 'translateX(-50%)',
        width: 'calc(100vw - 24px)',
        maxWidth: '480px',
        boxSizing: 'border-box',
        zIndex: 9998,
        display: 'flex',
        alignItems: 'center',
        gap: '10px',
        background: 'var(--bg-card)',
        border: '1px solid var(--border)',
        borderRadius: '8px',
        padding: '10px 14px',
        boxShadow: '0 4px 20px rgba(0,0,0,0.3)',
      }}
    >
      {inner}
      {/* Close — full teardown + hide the floating player (#161
          closePlayer). The Stop button inside `inner` resets playback to 0
          and keeps the player visible; only this X (or a refresh) dismisses
          it. */}
      <button
        onClick={closePlayer}
        title="Close player"
        aria-label="Close player"
        style={{
          background: 'none',
          border: 'none',
          color: 'var(--text-muted)',
          cursor: 'pointer',
          fontSize: '1.1rem',
          lineHeight: 1,
          flexShrink: 0,
          padding: '4px',
          display: 'flex',
          alignItems: 'center',
        }}
      >
        ✕
      </button>
    </div>
  );
};

export default GlobalAudioPlayer;

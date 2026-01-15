import React, { useCallback } from 'react';
import { useStreamingTranscription } from '../hooks/useStreamingTranscription';

/**
 * StreamingMicButton - A microphone button that supports real-time streaming transcription.
 *
 * When recording, audio chunks are sent to the server every 5 minutes (configurable)
 * and transcribed in real-time. Transcript text appears in the draft while still recording.
 *
 * Props:
 * @param {number} parentId - Parent node ID (optional)
 * @param {string} privacyLevel - Privacy level for the node
 * @param {string} aiUsage - AI usage setting
 * @param {Function} onRecordingStart - Called when recording starts (before streaming init)
 * @param {Function} onTranscriptUpdate - Called with updated transcript text
 * @param {Function} onComplete - Called when recording and transcription is complete
 * @param {Function} onError - Called on errors
 * @param {boolean} disabled - Whether the button is disabled
 */
export default function StreamingMicButton({
  parentId = null,
  privacyLevel = 'private',
  aiUsage = 'none',
  onRecordingStart = null,
  onTranscriptUpdate = null,
  onComplete = null,
  onError = null,
  disabled = false,
}) {
  const {
    sessionState,
    duration,
    chunkCount,
    transcribedChunks,
    errorMessage,
    isSSEConnected,
    startStreaming,
    stopStreaming,
    cancelStreaming,
  } = useStreamingTranscription({
    parentId,
    privacyLevel,
    aiUsage,
    chunkIntervalMs: 5 * 60 * 1000, // 5 minutes
    onTranscriptUpdate,
    onComplete,
    onError,
  });

  const handleClick = useCallback(() => {
    if (sessionState === 'idle') {
      // Call onRecordingStart before starting to capture pre-existing content
      if (onRecordingStart) {
        onRecordingStart();
      }
      startStreaming();
    } else if (sessionState === 'recording') {
      stopStreaming();
    } else if (sessionState === 'complete' || sessionState === 'error') {
      cancelStreaming();
    }
  }, [sessionState, startStreaming, stopStreaming, cancelStreaming, onRecordingStart]);

  // Format duration as MM:SS
  const formatDuration = (seconds) => {
    const mins = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);
    return `${mins}:${secs.toString().padStart(2, '0')}`;
  };

  // Determine button content based on state
  const getButtonContent = () => {
    switch (sessionState) {
      case 'idle':
        return (
          <>
            <MicIcon />
            <span>Stream Record</span>
          </>
        );
      case 'initializing':
        return (
          <>
            <LoadingIcon />
            <span>Starting...</span>
          </>
        );
      case 'recording':
        return (
          <>
            <StopIcon />
            <span>{formatDuration(duration)}</span>
            {chunkCount > 0 && (
              <span style={{ fontSize: '0.8em', marginLeft: '4px' }}>
                ({transcribedChunks}/{chunkCount} chunks)
              </span>
            )}
          </>
        );
      case 'finalizing':
        return (
          <>
            <LoadingIcon />
            <span>Finalizing...</span>
          </>
        );
      case 'complete':
        return (
          <>
            <CheckIcon />
            <span>Done - Click to reset</span>
          </>
        );
      case 'error':
        return (
          <>
            <ErrorIcon />
            <span>Error - Click to retry</span>
          </>
        );
      default:
        return <MicIcon />;
    }
  };

  const buttonStyle = {
    display: 'flex',
    alignItems: 'center',
    gap: '6px',
    padding: '8px 16px',
    border: sessionState === 'recording' ? '2px solid #dc3545' : '1px solid #ccc',
    borderRadius: '4px',
    backgroundColor: sessionState === 'recording' ? '#fff0f0' : '#fff',
    cursor: disabled ? 'not-allowed' : 'pointer',
    opacity: disabled ? 0.5 : 1,
    transition: 'all 0.2s ease',
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
      <button
        type="button"
        onClick={handleClick}
        disabled={disabled || sessionState === 'initializing' || sessionState === 'finalizing'}
        style={buttonStyle}
      >
        {getButtonContent()}
      </button>

      {/* SSE connection indicator */}
      {(sessionState === 'recording' || sessionState === 'finalizing') && (
        <div style={{ fontSize: '0.75em', color: isSSEConnected ? '#28a745' : '#ffc107' }}>
          {isSSEConnected ? 'Live transcription active' : 'Connecting...'}
        </div>
      )}

      {/* Error message */}
      {errorMessage && (
        <div style={{ fontSize: '0.8em', color: '#dc3545' }}>
          {errorMessage}
        </div>
      )}
    </div>
  );
}

// Icon components
const MicIcon = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
    <path d="M12 14c1.66 0 3-1.34 3-3V5c0-1.66-1.34-3-3-3S9 3.34 9 5v6c0 1.66 1.34 3 3 3z" />
    <path d="M17 11c0 2.76-2.24 5-5 5s-5-2.24-5-5H5c0 3.53 2.61 6.43 6 6.92V21h2v-3.08c3.39-.49 6-3.39 6-6.92h-2z" />
  </svg>
);

const StopIcon = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="#dc3545">
    <rect x="6" y="6" width="12" height="12" />
  </svg>
);

const LoadingIcon = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" style={{ animation: 'spin 1s linear infinite' }}>
    <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 18c-4.42 0-8-3.58-8-8s3.58-8 8-8 8 3.58 8 8-3.58 8-8 8z" opacity="0.3" />
    <path d="M12 2v4c3.31 0 6 2.69 6 6h4c0-5.52-4.48-10-10-10z" />
    <style>{`@keyframes spin { 100% { transform: rotate(360deg); } }`}</style>
  </svg>
);

const CheckIcon = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="#28a745">
    <path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z" />
  </svg>
);

const ErrorIcon = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="#dc3545">
    <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-2h2v2zm0-4h-2V7h2v6z" />
  </svg>
);

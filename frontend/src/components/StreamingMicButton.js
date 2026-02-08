import React, { useCallback, useEffect, useMemo, useState } from 'react';
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
    mediaBlob,
    isOffline,
    startStreaming,
    stopStreaming,
    cancelStreaming,
    getPartialBlob,
  } = useStreamingTranscription({
    parentId,
    privacyLevel,
    aiUsage,
    chunkIntervalMs: 5 * 60 * 1000, // 5 minutes
    onTranscriptUpdate,
    onComplete,
    onError,
  });

  // Preserve the recorded blob so the download button stays visible after auto-reset
  const [lastMediaBlob, setLastMediaBlob] = useState(null);

  // Capture blob when recording completes, before auto-reset clears it
  useEffect(() => {
    if (sessionState === 'complete' && mediaBlob) {
      setLastMediaBlob(mediaBlob);
    }
  }, [sessionState, mediaBlob]);

  // Clear preserved blob when starting a new recording
  useEffect(() => {
    if (sessionState === 'initializing') {
      setLastMediaBlob(null);
    }
  }, [sessionState]);

  // Auto-reset to idle after completion (no need for "Done - Click to reset" step)
  useEffect(() => {
    if (sessionState === 'complete') {
      // Small delay to ensure onComplete has finished
      const timer = setTimeout(() => {
        cancelStreaming();
      }, 100);
      return () => clearTimeout(timer);
    }
  }, [sessionState, cancelStreaming]);

  const handleDownload = useCallback(() => {
    const blob = mediaBlob || getPartialBlob() || lastMediaBlob;
    if (!blob) return;
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `recording-${new Date().toISOString().slice(0, 19).replace(/:/g, '-')}.webm`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }, [mediaBlob, getPartialBlob, lastMediaBlob]);

  const showDownload = useMemo(() => {
    return ['recording', 'finalizing', 'complete', 'error'].includes(sessionState) || lastMediaBlob != null;
  }, [sessionState, lastMediaBlob]);

  const handleClick = useCallback(() => {
    if (sessionState === 'idle') {
      // Call onRecordingStart before starting to capture pre-existing content
      if (onRecordingStart) {
        onRecordingStart();
      }
      startStreaming();
    } else if (sessionState === 'recording') {
      stopStreaming();
    } else if (sessionState === 'error') {
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
            <span>Record</span>
          </>
        );
      case 'initializing':
        return (
          <>
            <MicIcon />
            <span>Record</span>
          </>
        );
      case 'recording':
        return (
          <>
            <StopIcon />
            <span>{formatDuration(duration)}</span>
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
        // Auto-reset will kick in, but just in case show idle state
        return (
          <>
            <MicIcon />
            <span>Record</span>
          </>
        );
      case 'error':
        return (
          <>
            <ErrorIcon />
            <span>Error - Retry</span>
          </>
        );
      default:
        return (
          <>
            <MicIcon />
            <span>Record</span>
          </>
        );
    }
  };

  return (
    <div style={{ display: 'inline-flex', flexDirection: 'column', alignItems: 'flex-start', gap: '4px' }}>
      {isOffline && sessionState === 'recording' && (
        <div style={{
          padding: '4px 10px',
          backgroundColor: 'var(--bg-card)',
          color: 'var(--accent)',
          border: '1px solid var(--accent)',
          borderRadius: '6px',
          fontSize: '12px',
          fontFamily: 'var(--sans)',
          fontWeight: 300,
          lineHeight: '1.4',
        }}>
          Offline — recording continues, uploads will retry when connection returns
        </div>
      )}
      <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
        <button
          type="button"
          onClick={handleClick}
          disabled={disabled || sessionState === 'initializing' || sessionState === 'finalizing'}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: '6px',
            padding: '8px 16px',
            cursor: disabled || sessionState === 'initializing' || sessionState === 'finalizing' ? 'not-allowed' : 'pointer',
          }}
        >
          {getButtonContent()}
        </button>
        {showDownload && (
          <button
            type="button"
            onClick={handleDownload}
            title="Download recording"
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: '4px',
              padding: '8px 12px',
              cursor: 'pointer',
              fontSize: '12px',
            }}
          >
            <DownloadIcon />
            <span>Save audio</span>
          </button>
        )}
      </div>
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

const ErrorIcon = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="#dc3545">
    <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-2h2v2zm0-4h-2V7h2v6z" />
  </svg>
);

const DownloadIcon = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
    <path d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z" />
  </svg>
);

import { useState, useCallback, useRef, useEffect } from 'react';
import { useStreamingMediaRecorder } from './useStreamingMediaRecorder';
import { useDraftTranscriptionSSE } from './useSSE';
import api from '../api';

/**
 * Play an error sound using the Web Audio API.
 * Uses two descending tones to create an unmistakable "error" alert.
 */
function playErrorSound() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const playTone = (freq, startTime, duration) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.frequency.value = freq;
      osc.type = 'square';
      gain.gain.setValueAtTime(0.15, startTime);
      gain.gain.exponentialRampToValueAtTime(0.01, startTime + duration);
      osc.start(startTime);
      osc.stop(startTime + duration);
    };
    const now = ctx.currentTime;
    playTone(660, now, 0.15);
    playTone(440, now + 0.18, 0.15);
    playTone(330, now + 0.36, 0.25);
    // Close context after sounds finish
    setTimeout(() => ctx.close(), 1000);
  } catch (e) {
    // Audio not available - silently ignore
    console.warn('[StreamingTranscription] Could not play error sound:', e);
  }
}

/**
 * useStreamingTranscription - Complete streaming transcription workflow (Draft-based).
 *
 * This hook orchestrates the entire streaming transcription process using drafts:
 * 1. Initialize a streaming session (creates a Draft, NOT a node)
 * 2. Start recording with chunked output
 * 3. Upload chunks as they're captured
 * 4. Receive transcription results via SSE (text appears in draft in real-time)
 * 5. Finalize when recording stops (transcript stays in draft)
 * 6. User can then edit the draft and save it as a node when ready
 *
 * No node is created until the user explicitly saves the draft.
 *
 * @param {Object} options
 * @param {number} options.parentId - Parent node ID (optional)
 * @param {string} options.privacyLevel - Privacy level for the eventual node
 * @param {string} options.aiUsage - AI usage setting for the eventual node
 * @param {number} options.chunkIntervalMs - Chunk interval (default: 5 minutes)
 * @param {Function} options.onTranscriptUpdate - Called with updated transcript text
 * @param {Function} options.onComplete - Called when transcription is complete
 * @param {Function} options.onError - Called on errors
 */
export function useStreamingTranscription(options = {}) {
  const {
    parentId = null,
    privacyLevel = 'private',
    aiUsage = 'none',
    chunkIntervalMs = 5 * 60 * 1000, // 5 minutes
    onTranscriptUpdate = null,
    onComplete = null,
    onError = null,
  } = options;

  // State
  const [sessionState, setSessionState] = useState('idle'); // idle, initializing, recording, finalizing, complete, error
  const [draftId, setDraftId] = useState(null);
  const [sessionId, setSessionId] = useState(null);
  const [transcript, setTranscript] = useState('');
  const [uploadedChunks, setUploadedChunks] = useState(0);
  const [transcribedChunks, setTranscribedChunks] = useState(0);
  const [errorMessage, setErrorMessage] = useState(null);

  // Refs for tracking state across callbacks
  const sessionIdRef = useRef(null);
  const draftIdRef = useRef(null);
  const totalChunksRef = useRef(0);
  const pendingUploadsRef = useRef([]); // Track in-flight upload promises
  const failedChunksRef = useRef([]); // Track chunks that failed all retries

  // Network status
  const [isOffline, setIsOffline] = useState(!navigator.onLine);

  /**
   * Upload a single chunk with exponential backoff retry.
   * Returns true on success, false if all retries exhausted.
   */
  const uploadChunkWithRetry = useCallback(async (blob, chunkIndex, sessionId) => {
    const maxRetries = 4;
    const baseDelay = 2000; // 2 seconds

    for (let attempt = 0; attempt <= maxRetries; attempt++) {
      try {
        const formData = new FormData();
        formData.append('chunk', blob, `chunk_${chunkIndex}.webm`);
        formData.append('chunk_index', chunkIndex.toString());

        await api.post(`/drafts/streaming/${sessionId}/audio-chunk`, formData, {
          headers: { 'Content-Type': 'multipart/form-data' },
          timeout: 600000,
        });

        console.log(`[StreamingTranscription] Upload complete: chunk=${chunkIndex}` + (attempt > 0 ? ` (after ${attempt} retries)` : ''));
        return true;
      } catch (err) {
        if (attempt < maxRetries) {
          const delay = baseDelay * Math.pow(2, attempt); // 2s, 4s, 8s, 16s
          console.warn(`[StreamingTranscription] Upload failed (attempt ${attempt + 1}/${maxRetries + 1}): chunk=${chunkIndex}, retrying in ${delay}ms...`, err.message);
          await new Promise(resolve => setTimeout(resolve, delay));
        } else {
          console.error(`[StreamingTranscription] Upload FAILED after ${maxRetries + 1} attempts: chunk=${chunkIndex}`, err);
          return false;
        }
      }
    }
    return false;
  }, []);

  // Handle chunk upload
  const uploadChunk = useCallback(async (blob, chunkIndex) => {
    if (!sessionIdRef.current) {
      console.error('[StreamingTranscription] Cannot upload chunk: session not initialized');
      return;
    }

    const sessionId = sessionIdRef.current;
    console.log(`[StreamingTranscription] Starting upload: chunk=${chunkIndex}, blobSize=${blob.size}, session=${sessionId}`);

    const uploadPromise = (async () => {
      const success = await uploadChunkWithRetry(blob, chunkIndex, sessionId);

      if (success) {
        setUploadedChunks(prev => prev + 1);
        totalChunksRef.current = Math.max(totalChunksRef.current, chunkIndex + 1);
      } else {
        // Store failed chunk for retry when network returns
        failedChunksRef.current.push({ blob, chunkIndex, sessionId });
        playErrorSound();
        if (onError) {
          onError(new Error(`Failed to upload chunk ${chunkIndex} after retries`));
        }
      }
    })();

    pendingUploadsRef.current.push(uploadPromise);
  }, [onError, uploadChunkWithRetry]);

  // Streaming media recorder
  const {
    status: recorderStatus,
    mediaBlob,
    mediaUrl,
    duration,
    chunkCount,
    error: recorderError,
    startRecording: startMediaRecorder,
    stopRecording: stopMediaRecorder,
    resetRecording: resetMediaRecorder,
    getTotalChunks,
    getPartialBlob,
  } = useStreamingMediaRecorder({
    chunkIntervalMs,
    onChunkReady: uploadChunk,
  });

  // SSE subscription for transcription updates (draft-based)
  const {
    isConnected: sseConnected,
    isComplete: transcriptionComplete,
    finalContent,
    draftContent,
    disconnect: disconnectSSE,
    reset: resetSSE,
  } = useDraftTranscriptionSSE(sessionId, {
    enabled: sessionState === 'recording' || sessionState === 'finalizing',
    onChunkComplete: (data) => {
      setTranscribedChunks(prev => prev + 1);
    },
    onContentUpdate: (data) => {
      // Update transcript with content from server
      setTranscript(data.content);
      if (onTranscriptUpdate) {
        onTranscriptUpdate(data.content);
      }
    },
    onAllComplete: (data) => {
      setSessionState('complete');
      setTranscript(data.content);
      if (onComplete) {
        onComplete({
          draftId: draftIdRef.current,
          sessionId: sessionIdRef.current,
          content: data.content,
        });
      }
    },
    onError: (data) => {
      console.error('Transcription error:', data);
      playErrorSound();
      if (onError) {
        onError(new Error(data.error || 'Transcription failed'));
      }
    },
  });

  // Update internal transcript when draft content changes from SSE
  // Note: Don't call onTranscriptUpdate here - it's already called in onContentUpdate callback.
  // Calling it here too causes duplicate updates, and the effect can run AFTER onComplete
  // due to React's effect timing, overwriting the combined content with just the new transcript.
  useEffect(() => {
    if (draftContent) {
      setTranscript(draftContent);
    }
  }, [draftContent]);

  // Handle transcription completion
  useEffect(() => {
    if (transcriptionComplete && finalContent) {
      setSessionState('complete');
      setTranscript(finalContent);
    }
  }, [transcriptionComplete, finalContent]);

  // Online/offline detection and retry failed chunks
  useEffect(() => {
    const handleOnline = async () => {
      setIsOffline(false);
      console.log('[StreamingTranscription] Network back online');

      // Retry failed chunks
      if (failedChunksRef.current.length > 0) {
        console.log(`[StreamingTranscription] Retrying ${failedChunksRef.current.length} failed chunks...`);
        const chunksToRetry = [...failedChunksRef.current];
        failedChunksRef.current = [];

        for (const { blob, chunkIndex, sessionId } of chunksToRetry) {
          const success = await uploadChunkWithRetry(blob, chunkIndex, sessionId);
          if (success) {
            setUploadedChunks(prev => prev + 1);
            totalChunksRef.current = Math.max(totalChunksRef.current, chunkIndex + 1);
          } else {
            // Still failing — put it back
            failedChunksRef.current.push({ blob, chunkIndex, sessionId });
          }
        }

        if (failedChunksRef.current.length > 0) {
          console.warn(`[StreamingTranscription] ${failedChunksRef.current.length} chunks still failing after online retry`);
        }
      }
    };

    const handleOffline = () => {
      setIsOffline(true);
      console.log('[StreamingTranscription] Network went offline');
    };

    window.addEventListener('online', handleOnline);
    window.addEventListener('offline', handleOffline);
    return () => {
      window.removeEventListener('online', handleOnline);
      window.removeEventListener('offline', handleOffline);
    };
  }, [uploadChunkWithRetry]);

  // Initialize streaming session (creates draft, NOT node)
  const initSession = useCallback(async () => {
    setSessionState('initializing');
    setErrorMessage(null);

    try {
      const response = await api.post('/drafts/streaming/init', {
        parent_id: parentId,
        privacy_level: privacyLevel,
        ai_usage: aiUsage,
      });

      const { draft_id, session_id } = response.data;

      setDraftId(draft_id);
      setSessionId(session_id);
      draftIdRef.current = draft_id;
      sessionIdRef.current = session_id;

      return { draftId: draft_id, sessionId: session_id };

    } catch (err) {
      console.error('Failed to initialize streaming session:', err);
      setSessionState('error');
      setErrorMessage(err.message);
      if (onError) {
        onError(err);
      }
      throw err;
    }
  }, [parentId, privacyLevel, aiUsage, onError]);

  // Start streaming transcription
  const startStreaming = useCallback(async () => {
    try {
      // Initialize session
      await initSession();

      // Start recording
      await startMediaRecorder();
      setSessionState('recording');

    } catch (err) {
      console.error('Failed to start streaming:', err);
      setSessionState('error');
      setErrorMessage(err.message);
    }
  }, [initSession, startMediaRecorder]);

  // Stop streaming and finalize
  const stopStreaming = useCallback(async () => {
    console.log(`[StreamingTranscription] stopStreaming called: session=${sessionIdRef.current}, pendingUploads=${pendingUploadsRef.current.length}`);

    // Stop recording first — this triggers a final ondataavailable with remaining audio
    stopMediaRecorder();
    setSessionState('finalizing');

    // Wait for the final ondataavailable event to fire and push to chunksRef.
    // The event is async (queued in the event loop after stop()), so we need
    // to yield control back to the event loop before reading the chunk count.
    await new Promise(resolve => setTimeout(resolve, 100));

    // Now wait for ALL pending uploads to complete (including the final chunk's upload)
    // This replaces the old blind 500ms timeout with an actual guarantee.
    const uploadsToWait = [...pendingUploadsRef.current];
    console.log(`[StreamingTranscription] Waiting for ${uploadsToWait.length} pending uploads to complete...`);
    await Promise.allSettled(uploadsToWait);

    // Get total chunks — should now include the final chunk
    const totalChunks = getTotalChunks();
    console.log(`[StreamingTranscription] All uploads settled. totalChunks=${totalChunks}, uploadedChunks=${totalChunksRef.current}`);

    try {
      // Call finalize endpoint
      await api.post(`/drafts/streaming/${sessionIdRef.current}/finalize`, {
        total_chunks: totalChunks,
      }, {
        timeout: 120000, // 2 minutes for finalize (just queues the task)
      });

      console.log(`[StreamingTranscription] Finalize request sent: total_chunks=${totalChunks}`);
      // SSE will notify when complete
      // Keep finalizing state until SSE signals completion

    } catch (err) {
      console.error(`[StreamingTranscription] Finalize FAILED:`, err);
      playErrorSound();
      setErrorMessage(err.message);
      if (onError) {
        onError(err);
      }
    }
  }, [stopMediaRecorder, getTotalChunks, onError]);

  // Save the streaming draft as a node
  const saveAsNode = useCallback(async (editedContent = null) => {
    if (!sessionIdRef.current) {
      throw new Error('No streaming session to save');
    }

    try {
      const response = await api.post(`/drafts/streaming/${sessionIdRef.current}/save-as-node`, {
        content: editedContent || transcript,
      });

      return response.data;
    } catch (err) {
      console.error('Failed to save streaming draft as node:', err);
      if (onError) {
        onError(err);
      }
      throw err;
    }
  }, [transcript, onError]);

  // Cancel/reset everything
  const cancelStreaming = useCallback(() => {
    disconnectSSE();
    resetMediaRecorder();
    resetSSE();

    setSessionState('idle');
    setDraftId(null);
    setSessionId(null);
    setTranscript('');
    setUploadedChunks(0);
    setTranscribedChunks(0);
    setErrorMessage(null);

    draftIdRef.current = null;
    sessionIdRef.current = null;
    totalChunksRef.current = 0;
    pendingUploadsRef.current = [];
    failedChunksRef.current = [];
  }, [disconnectSSE, resetMediaRecorder, resetSSE]);

  return {
    // State
    sessionState,
    draftId,
    sessionId,
    transcript,
    duration,
    chunkCount,
    uploadedChunks,
    transcribedChunks,
    errorMessage,
    recorderError,

    // Connection status
    isRecording: recorderStatus === 'recording',
    isSSEConnected: sseConnected,
    isOffline,

    // Media
    mediaBlob,
    mediaUrl,

    // Actions
    startStreaming,
    stopStreaming,
    saveAsNode,
    cancelStreaming,
    getPartialBlob,
  };
}

import { useState, useCallback, useRef, useEffect } from 'react';
import { useStreamingMediaRecorder } from './useStreamingMediaRecorder';
import { useDraftTranscriptionSSE } from './useSSE';
import api from '../api';

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

  // Handle chunk upload
  const uploadChunk = useCallback(async (blob, chunkIndex) => {
    if (!sessionIdRef.current) {
      console.error('Cannot upload chunk: session not initialized');
      return;
    }

    try {
      const formData = new FormData();
      formData.append('chunk', blob, `chunk_${chunkIndex}.webm`);
      formData.append('chunk_index', chunkIndex.toString());

      await api.post(`/drafts/streaming/${sessionIdRef.current}/audio-chunk`, formData, {
        headers: { 'Content-Type': 'multipart/form-data' },
      });

      setUploadedChunks(prev => prev + 1);
      totalChunksRef.current = Math.max(totalChunksRef.current, chunkIndex + 1);

    } catch (err) {
      console.error('Failed to upload chunk:', err);
      if (onError) {
        onError(err);
      }
    }
  }, [onError]);

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
      if (onError) {
        onError(new Error(data.error || 'Transcription failed'));
      }
    },
  });

  // Update transcript when draft content changes from SSE
  useEffect(() => {
    if (draftContent) {
      setTranscript(draftContent);
      if (onTranscriptUpdate) {
        onTranscriptUpdate(draftContent);
      }
    }
  }, [draftContent, onTranscriptUpdate]);

  // Handle transcription completion
  useEffect(() => {
    if (transcriptionComplete && finalContent) {
      setSessionState('complete');
      setTranscript(finalContent);
    }
  }, [transcriptionComplete, finalContent]);

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
    // Stop recording first
    stopMediaRecorder();
    setSessionState('finalizing');

    // Wait a moment for final chunk to be captured
    await new Promise(resolve => setTimeout(resolve, 500));

    // Get total chunks
    const totalChunks = getTotalChunks();

    try {
      // Call finalize endpoint
      await api.post(`/drafts/streaming/${sessionIdRef.current}/finalize`, {
        total_chunks: totalChunks,
      });

      // SSE will notify when complete
      // Keep finalizing state until SSE signals completion

    } catch (err) {
      console.error('Failed to finalize streaming:', err);
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

    // Media
    mediaBlob,
    mediaUrl,

    // Actions
    startStreaming,
    stopStreaming,
    saveAsNode,
    cancelStreaming,
  };
}

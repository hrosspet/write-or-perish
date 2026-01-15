import { useState, useCallback, useRef, useEffect } from 'react';
import { useStreamingMediaRecorder } from './useStreamingMediaRecorder';
import { useTranscriptionSSE } from './useSSE';
import api from '../api';

/**
 * useStreamingTranscription - Complete streaming transcription workflow.
 *
 * This hook orchestrates the entire streaming transcription process:
 * 1. Initialize a streaming session on the server
 * 2. Start recording with chunked output
 * 3. Upload chunks as they're captured
 * 4. Receive transcription results via SSE
 * 5. Finalize when recording stops
 *
 * @param {Object} options
 * @param {number} options.parentId - Parent node ID (optional)
 * @param {string} options.privacyLevel - Privacy level for the node
 * @param {string} options.aiUsage - AI usage setting for the node
 * @param {number} options.chunkIntervalMs - Chunk interval (default: 5 minutes)
 * @param {Function} options.onTranscriptUpdate - Called with assembled transcript
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
  const [nodeId, setNodeId] = useState(null);
  const [sessionId, setSessionId] = useState(null);
  const [transcript, setTranscript] = useState('');
  const [uploadedChunks, setUploadedChunks] = useState(0);
  const [transcribedChunks, setTranscribedChunks] = useState(0);
  const [errorMessage, setErrorMessage] = useState(null);

  // Refs for tracking state across callbacks
  const sessionIdRef = useRef(null);
  const nodeIdRef = useRef(null);
  const totalChunksRef = useRef(0);

  // Handle chunk upload
  const uploadChunk = useCallback(async (blob, chunkIndex) => {
    if (!sessionIdRef.current || !nodeIdRef.current) {
      console.error('Cannot upload chunk: session not initialized');
      return;
    }

    try {
      const formData = new FormData();
      formData.append('chunk', blob, `chunk_${chunkIndex}.webm`);
      formData.append('chunk_index', chunkIndex.toString());
      formData.append('session_id', sessionIdRef.current);

      await api.post(`/nodes/${nodeIdRef.current}/audio-chunk`, formData, {
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

  // SSE subscription for transcription updates
  const {
    isConnected: sseConnected,
    chunks: transcriptChunks,
    isComplete: transcriptionComplete,
    finalContent,
    getAssembledTranscript,
    disconnect: disconnectSSE,
    reset: resetSSE,
  } = useTranscriptionSSE(nodeId, {
    enabled: sessionState === 'recording' || sessionState === 'finalizing',
    onChunkComplete: (data) => {
      setTranscribedChunks(prev => prev + 1);
      // Update transcript with new chunk
      setTranscript(prev => {
        const chunks = [...transcriptChunks, { index: data.chunk_index, text: data.text }]
          .sort((a, b) => a.index - b.index);
        const newTranscript = chunks.map(c => c.text).join('\n\n');
        if (onTranscriptUpdate) {
          onTranscriptUpdate(newTranscript);
        }
        return newTranscript;
      });
    },
    onAllComplete: (data) => {
      setSessionState('complete');
      setTranscript(data.content);
      if (onComplete) {
        onComplete({
          nodeId: nodeIdRef.current,
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

  // Update transcript when chunks change
  useEffect(() => {
    if (transcriptChunks.length > 0) {
      const assembled = getAssembledTranscript();
      setTranscript(assembled);
      if (onTranscriptUpdate) {
        onTranscriptUpdate(assembled);
      }
    }
  }, [transcriptChunks, getAssembledTranscript, onTranscriptUpdate]);

  // Handle transcription completion
  useEffect(() => {
    if (transcriptionComplete && finalContent) {
      setSessionState('complete');
      setTranscript(finalContent);
    }
  }, [transcriptionComplete, finalContent]);

  // Initialize streaming session
  const initSession = useCallback(async () => {
    setSessionState('initializing');
    setErrorMessage(null);

    try {
      const response = await api.post('/nodes/streaming/init', {
        parent_id: parentId,
        node_type: 'user',
        privacy_level: privacyLevel,
        ai_usage: aiUsage,
        chunk_interval_ms: chunkIntervalMs,
      });

      const { node_id, session_id } = response.data;

      setNodeId(node_id);
      setSessionId(session_id);
      nodeIdRef.current = node_id;
      sessionIdRef.current = session_id;

      return { nodeId: node_id, sessionId: session_id };

    } catch (err) {
      console.error('Failed to initialize streaming session:', err);
      setSessionState('error');
      setErrorMessage(err.message);
      if (onError) {
        onError(err);
      }
      throw err;
    }
  }, [parentId, privacyLevel, aiUsage, chunkIntervalMs, onError]);

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
      await api.post(`/nodes/${nodeIdRef.current}/finalize-streaming`, {
        session_id: sessionIdRef.current,
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

  // Cancel/reset everything
  const cancelStreaming = useCallback(() => {
    disconnectSSE();
    resetMediaRecorder();
    resetSSE();

    setSessionState('idle');
    setNodeId(null);
    setSessionId(null);
    setTranscript('');
    setUploadedChunks(0);
    setTranscribedChunks(0);
    setErrorMessage(null);

    nodeIdRef.current = null;
    sessionIdRef.current = null;
    totalChunksRef.current = 0;
  }, [disconnectSSE, resetMediaRecorder, resetSSE]);

  return {
    // State
    sessionState,
    nodeId,
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
    cancelStreaming,
  };
}

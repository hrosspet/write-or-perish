import { useState, useEffect, useCallback, useRef } from 'react';

/**
 * useSSE hook for subscribing to Server-Sent Events.
 *
 * This hook manages an EventSource connection to receive real-time updates
 * from the server for streaming transcription and TTS playback.
 *
 * @param {string} url - SSE endpoint URL (e.g., '/api/sse/nodes/123/transcription-stream')
 * @param {Object} options - Configuration options
 * @param {boolean} options.enabled - Whether to connect (default: false)
 * @param {Function} options.onMessage - Callback for generic messages
 * @param {Object} options.eventHandlers - Map of event names to handlers
 * @param {number} options.reconnectDelay - Delay between reconnection attempts (default: 3000ms)
 */
export function useSSE(url, options = {}) {
  const {
    enabled = false,
    onMessage = null,
    eventHandlers = {},
    reconnectDelay = 3000,
  } = options;

  const [isConnected, setIsConnected] = useState(false);
  const [error, setError] = useState(null);
  const [lastEvent, setLastEvent] = useState(null);

  const eventSourceRef = useRef(null);
  const reconnectTimeoutRef = useRef(null);

  const disconnect = useCallback(() => {
    if (reconnectTimeoutRef.current) {
      clearTimeout(reconnectTimeoutRef.current);
      reconnectTimeoutRef.current = null;
    }
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
      eventSourceRef.current = null;
    }
    setIsConnected(false);
  }, []);

  const connect = useCallback(() => {
    if (!url || !enabled) {
      return;
    }

    // Close existing connection
    disconnect();

    try {
      // Create EventSource with credentials for auth
      const eventSource = new EventSource(url, { withCredentials: true });
      eventSourceRef.current = eventSource;

      eventSource.onopen = () => {
        setIsConnected(true);
        setError(null);
      };

      eventSource.onerror = (e) => {
        console.error('SSE connection error:', e);
        setIsConnected(false);

        // EventSource will automatically try to reconnect for transient errors
        // But if the connection is closed, we might need to handle it
        if (eventSource.readyState === EventSource.CLOSED) {
          setError('Connection closed');

          // Attempt to reconnect after delay
          reconnectTimeoutRef.current = setTimeout(() => {
            if (enabled) {
              connect();
            }
          }, reconnectDelay);
        }
      };

      // Handle generic message events (data-only, no event type)
      eventSource.onmessage = (e) => {
        try {
          const data = JSON.parse(e.data);
          setLastEvent({ type: 'message', data });
          if (onMessage) {
            onMessage(data);
          }
        } catch (err) {
          console.error('Failed to parse SSE message:', err);
        }
      };

      // Register handlers for specific event types
      Object.entries(eventHandlers).forEach(([eventType, handler]) => {
        eventSource.addEventListener(eventType, (e) => {
          try {
            // Skip if data is undefined or empty (can happen on connection errors)
            if (!e.data) {
              console.warn(`SSE event ${eventType} received with no data`);
              return;
            }
            const data = JSON.parse(e.data);
            setLastEvent({ type: eventType, data });
            handler(data);
          } catch (err) {
            console.error(`Failed to parse SSE event ${eventType}:`, err);
          }
        });
      });

    } catch (err) {
      console.error('Failed to create EventSource:', err);
      setError(err.message);
    }
  }, [url, enabled, onMessage, eventHandlers, reconnectDelay, disconnect]);

  // Connect/disconnect based on enabled state
  useEffect(() => {
    if (enabled && url) {
      connect();
    } else {
      disconnect();
    }

    return () => {
      disconnect();
    };
  }, [enabled, url, connect, disconnect]);

  return {
    isConnected,
    error,
    lastEvent,
    connect,
    disconnect,
  };
}

/**
 * useTranscriptionSSE - Specialized hook for streaming transcription updates.
 *
 * @param {number} nodeId - Node ID to subscribe to
 * @param {Object} options
 * @param {boolean} options.enabled - Whether to connect
 * @param {Function} options.onChunkComplete - Called when a chunk is transcribed
 * @param {Function} options.onAllComplete - Called when all chunks are done
 * @param {Function} options.onError - Called on transcription error
 */
export function useTranscriptionSSE(nodeId, options = {}) {
  const {
    enabled = false,
    onChunkComplete = null,
    onAllComplete = null,
    onError = null,
  } = options;

  const [chunks, setChunks] = useState([]); // Array of { index, text }
  const [isComplete, setIsComplete] = useState(false);
  const [finalContent, setFinalContent] = useState(null);

  // Use REACT_APP_BACKEND_URL for SSE since EventSource doesn't go through CRA proxy
  const backendUrl = process.env.REACT_APP_BACKEND_URL || '';
  const url = nodeId ? `${backendUrl}/api/sse/nodes/${nodeId}/transcription-stream` : null;

  const eventHandlers = {
    chunk_complete: (data) => {
      setChunks(prev => {
        // Add or update chunk
        const existing = prev.find(c => c.index === data.chunk_index);
        if (existing) {
          return prev.map(c =>
            c.index === data.chunk_index ? { ...c, text: data.text } : c
          );
        }
        return [...prev, { index: data.chunk_index, text: data.text }].sort(
          (a, b) => a.index - b.index
        );
      });
      if (onChunkComplete) {
        onChunkComplete(data);
      }
    },
    chunk_error: (data) => {
      if (onError) {
        onError(data);
      }
    },
    all_complete: (data) => {
      setIsComplete(true);
      setFinalContent(data.content);
      if (onAllComplete) {
        onAllComplete(data);
      }
    },
    error: (data) => {
      if (onError) {
        onError(data);
      }
    },
    heartbeat: () => {
      // Keep-alive, no action needed
    },
  };

  const { isConnected, error, disconnect } = useSSE(url, {
    enabled,
    eventHandlers,
  });

  // Get assembled transcript from chunks
  const getAssembledTranscript = useCallback(() => {
    return chunks.map(c => c.text).join('\n\n');
  }, [chunks]);

  // Reset state
  const reset = useCallback(() => {
    setChunks([]);
    setIsComplete(false);
    setFinalContent(null);
  }, []);

  return {
    isConnected,
    error,
    chunks,
    isComplete,
    finalContent,
    getAssembledTranscript,
    disconnect,
    reset,
  };
}

/**
 * useDraftTranscriptionSSE - Specialized hook for draft-based streaming transcription.
 *
 * This is similar to useTranscriptionSSE but works with session_id instead of node_id,
 * and includes content_update events as the draft is updated in real-time.
 *
 * @param {string} sessionId - Session ID to subscribe to
 * @param {Object} options
 * @param {boolean} options.enabled - Whether to connect
 * @param {Function} options.onChunkComplete - Called when a chunk is transcribed
 * @param {Function} options.onContentUpdate - Called when draft content is updated
 * @param {Function} options.onAllComplete - Called when all chunks are done
 * @param {Function} options.onError - Called on transcription error
 */
export function useDraftTranscriptionSSE(sessionId, options = {}) {
  const {
    enabled = false,
    onChunkComplete = null,
    onContentUpdate = null,
    onAllComplete = null,
    onError = null,
  } = options;

  const [chunks, setChunks] = useState([]); // Array of { index, text }
  const [isComplete, setIsComplete] = useState(false);
  const [finalContent, setFinalContent] = useState(null);
  const [draftContent, setDraftContent] = useState('');

  // Track last received chunk for reconnection
  // Use ref for tracking (no re-renders) and state for URL (triggers reconnect when needed)
  const lastChunkIndexRef = useRef(-1);
  const [reconnectChunkIndex, setReconnectChunkIndex] = useState(-1);

  // Use REACT_APP_BACKEND_URL for SSE since EventSource doesn't go through CRA proxy
  const backendUrl = process.env.REACT_APP_BACKEND_URL || '';
  // Include last_chunk param for reconnection support - only add if we've received chunks
  const lastChunkParam = reconnectChunkIndex >= 0 ? `?last_chunk=${reconnectChunkIndex}` : '';
  const url = sessionId ? `${backendUrl}/api/sse/drafts/${sessionId}/transcription-stream${lastChunkParam}` : null;

  const eventHandlers = {
    chunk_complete: (data) => {
      // Track last received chunk for reconnection (ref doesn't trigger re-renders)
      lastChunkIndexRef.current = Math.max(lastChunkIndexRef.current, data.chunk_index);

      setChunks(prev => {
        // Add or update chunk
        const existing = prev.find(c => c.index === data.chunk_index);
        if (existing) {
          return prev.map(c =>
            c.index === data.chunk_index ? { ...c, text: data.text } : c
          );
        }
        return [...prev, { index: data.chunk_index, text: data.text }].sort(
          (a, b) => a.index - b.index
        );
      });
      if (onChunkComplete) {
        onChunkComplete(data);
      }
    },
    chunk_error: (data) => {
      if (onError) {
        onError(data);
      }
    },
    content_update: (data) => {
      setDraftContent(data.content);
      if (onContentUpdate) {
        onContentUpdate(data);
      }
    },
    all_complete: (data) => {
      setIsComplete(true);
      setFinalContent(data.content);
      setDraftContent(data.content);
      if (onAllComplete) {
        onAllComplete(data);
      }
    },
    error: (data) => {
      if (onError) {
        onError(data);
      }
    },
    heartbeat: () => {
      // Keep-alive, no action needed
    },
  };

  const { isConnected, error, disconnect } = useSSE(url, {
    enabled,
    eventHandlers,
  });

  // When connection drops, update reconnectChunkIndex to trigger reconnect with last_chunk param
  const wasConnectedRef = useRef(false);
  useEffect(() => {
    if (wasConnectedRef.current && !isConnected && enabled && !isComplete) {
      // Connection was lost - update state to trigger reconnect with proper URL
      setReconnectChunkIndex(lastChunkIndexRef.current);
    }
    wasConnectedRef.current = isConnected;
  }, [isConnected, enabled, isComplete]);

  // Get assembled transcript from chunks
  const getAssembledTranscript = useCallback(() => {
    return chunks.map(c => c.text).join('\n\n');
  }, [chunks]);

  // Reset state
  const reset = useCallback(() => {
    setChunks([]);
    setIsComplete(false);
    setFinalContent(null);
    setDraftContent('');
    lastChunkIndexRef.current = -1;
    setReconnectChunkIndex(-1);
  }, []);

  return {
    isConnected,
    error,
    chunks,
    isComplete,
    finalContent,
    draftContent,
    getAssembledTranscript,
    disconnect,
    reset,
  };
}

/**
 * useTTSStreamSSE - Specialized hook for streaming TTS playback updates.
 *
 * @param {number} nodeId - Node ID to subscribe to
 * @param {Object} options
 * @param {boolean} options.enabled - Whether to connect
 * @param {Function} options.onChunkReady - Called when an audio chunk is ready
 * @param {Function} options.onAllComplete - Called when all chunks are done
 */
export function useTTSStreamSSE(nodeId, options = {}) {
  const {
    enabled = false,
    onChunkReady = null,
    onAllComplete = null,
  } = options;

  const [audioChunks, setAudioChunks] = useState([]); // Array of { index, url }
  const [isComplete, setIsComplete] = useState(false);
  const [finalUrl, setFinalUrl] = useState(null);

  // Use REACT_APP_BACKEND_URL for SSE since EventSource doesn't go through CRA proxy
  const backendUrl = process.env.REACT_APP_BACKEND_URL || '';
  const url = nodeId ? `${backendUrl}/api/sse/nodes/${nodeId}/tts-stream` : null;

  const eventHandlers = {
    chunk_ready: (data) => {
      setAudioChunks(prev => {
        const existing = prev.find(c => c.index === data.chunk_index);
        if (existing) {
          return prev;
        }
        return [...prev, { index: data.chunk_index, url: data.audio_url }].sort(
          (a, b) => a.index - b.index
        );
      });
      if (onChunkReady) {
        onChunkReady(data);
      }
    },
    all_complete: (data) => {
      setIsComplete(true);
      setFinalUrl(data.tts_url);
      if (onAllComplete) {
        onAllComplete(data);
      }
    },
    heartbeat: () => {
      // Keep-alive, no action needed
    },
  };

  const { isConnected, error, disconnect } = useSSE(url, {
    enabled,
    eventHandlers,
  });

  // Get ordered list of audio URLs for queue playback
  const getAudioQueue = useCallback(() => {
    return audioChunks.map(c => c.url);
  }, [audioChunks]);

  // Reset state
  const reset = useCallback(() => {
    setAudioChunks([]);
    setIsComplete(false);
    setFinalUrl(null);
  }, []);

  return {
    isConnected,
    error,
    audioChunks,
    isComplete,
    finalUrl,
    getAudioQueue,
    disconnect,
    reset,
  };
}

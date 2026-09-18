import { useState, useEffect, useCallback, useRef } from 'react';
import api from '../api';

/**
 * Custom hook for polling async task status
 * @param {string} endpoint - API endpoint to poll (e.g., `/nodes/${nodeId}/transcription-status`)
 * @param {Object} options - Polling options
 * @param {number} options.interval - Polling interval in ms (default: 2000)
 * @param {number} options.maxDuration - Max polling duration in ms (default: 30 minutes);
 *   0 disables the cap — for a poller whose endpoint is itself authoritative
 *   about whether the work is still running (it answers "idle" when nothing is)
 * @param {boolean} options.enabled - Whether polling is enabled
 * @param {number} options.maxConsecutiveErrors - Stop polling (with `error` set)
 *   after this many failed requests in a row; 0 (default) keeps retrying
 * @returns {Object} - { status, progress, data, error, startPolling, stopPolling }
 */
export function useAsyncTaskPolling(endpoint, options = {}) {
  const {
    interval = 2000,
    maxDuration = 30 * 60 * 1000, // 30 minutes
    enabled = false,
    maxConsecutiveErrors = 0
  } = options;

  const [status, setStatus] = useState(null); // 'pending', 'processing', 'completed', 'failed', 'cancelled'
  const [progress, setProgress] = useState(0);
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [isPolling, setIsPolling] = useState(false);

  const intervalRef = useRef(null);
  const timeoutRef = useRef(null);
  const consecutiveErrorsRef = useRef(0);
  // Track current endpoint to discard stale in-flight responses
  const currentEndpointRef = useRef(endpoint);
  currentEndpointRef.current = endpoint;

  const stopPolling = useCallback(() => {
    if (intervalRef.current) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
    if (timeoutRef.current) {
      clearTimeout(timeoutRef.current);
      timeoutRef.current = null;
    }
    setIsPolling(false);
  }, []);

  const poll = useCallback(async () => {
    if (!endpoint) {
      console.error('Cannot poll: endpoint is null or undefined');
      return;
    }
    // Capture the endpoint at call time to detect stale responses
    const requestEndpoint = endpoint;
    try {
      // Use shorter timeout for status polling (10 seconds instead of 60)
      // Add Cache-Control header to prevent Safari from caching polling responses
      const response = await api.get(endpoint, {
        timeout: 10000,
        headers: { 'Cache-Control': 'no-cache' }
      });

      // Discard response if endpoint changed while request was in flight
      if (currentEndpointRef.current !== requestEndpoint) {
        return;
      }

      const result = response.data;
      consecutiveErrorsRef.current = 0;

      setStatus(result.status);
      setProgress(result.progress || 0);
      setData(result);

      // Stop polling at a terminal state: completed, failed, or
      // cancelled (a read withdrawn before it ran).
      if (['completed', 'failed', 'cancelled'].includes(result.status)) {
        stopPolling();
        if (result.status === 'failed') {
          setError(result.error || 'Task failed');
        }
      }
    } catch (err) {
      console.error('Polling error:', err);
      // Don't stop polling on error, just log it and retry on next interval
      // The task might still be processing or there might be a temporary network issue
      consecutiveErrorsRef.current += 1;
      if (maxConsecutiveErrors && consecutiveErrorsRef.current >= maxConsecutiveErrors) {
        stopPolling();
        setError(`Polling stopped after ${consecutiveErrorsRef.current} consecutive errors`);
      }
    }
  }, [endpoint, stopPolling, maxConsecutiveErrors]);

  const startPolling = useCallback(() => {
    if (isPolling) return;
    if (!endpoint) {
      console.error('Cannot start polling: endpoint is null or undefined');
      return;
    }

    setIsPolling(true);
    setError(null);

    // Poll immediately
    poll();

    // Set up interval
    intervalRef.current = setInterval(poll, interval);

    // Set up timeout to stop polling after max duration
    if (maxDuration) {
      timeoutRef.current = setTimeout(() => {
        stopPolling();
        setError('Polling timeout - task took too long');
      }, maxDuration);
    }
  }, [isPolling, endpoint, poll, interval, maxDuration, stopPolling]);

  // Auto-start polling if enabled
  useEffect(() => {
    // Always stop any existing polling when effect runs
    if (intervalRef.current) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
    if (timeoutRef.current) {
      clearTimeout(timeoutRef.current);
      timeoutRef.current = null;
    }
    setIsPolling(false);

    // Reset stale state from previous endpoint before starting new polling
    setStatus(null);
    setData(null);
    setProgress(0);

    // Start new polling if enabled and endpoint is set
    if (enabled && endpoint) {
      setIsPolling(true);
      setError(null);
      consecutiveErrorsRef.current = 0;

      // Poll immediately
      poll();

      // Set up interval
      intervalRef.current = setInterval(poll, interval);

      // Set up timeout to stop polling after max duration
      if (maxDuration) {
        timeoutRef.current = setTimeout(() => {
          if (intervalRef.current) {
            clearInterval(intervalRef.current);
            intervalRef.current = null;
          }
          if (timeoutRef.current) {
            clearTimeout(timeoutRef.current);
            timeoutRef.current = null;
          }
          setIsPolling(false);
          setError('Polling timeout - task took too long');
        }, maxDuration);
      }
    }

    // Cleanup on unmount or when dependencies change
    return () => {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
      if (timeoutRef.current) {
        clearTimeout(timeoutRef.current);
        timeoutRef.current = null;
      }
      setIsPolling(false);
    };
  }, [enabled, endpoint, poll, interval, maxDuration]);

  // iOS throttles setInterval when backgrounded. Poll immediately on foreground.
  useEffect(() => {
    if (!isPolling) return;

    const handleVisibilityChange = () => {
      if (document.visibilityState === 'visible') {
        poll();
      }
    };

    document.addEventListener('visibilitychange', handleVisibilityChange);
    return () => document.removeEventListener('visibilitychange', handleVisibilityChange);
  }, [isPolling, poll]);

  return {
    status,
    progress,
    data,
    error,
    isPolling,
    startPolling,
    stopPolling
  };
}

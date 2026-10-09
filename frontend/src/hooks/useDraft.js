import { useState, useEffect, useCallback, useRef } from 'react';
import api from '../api';

/**
 * Custom hook for managing auto-saved drafts
 * @param {Object} options - Draft options
 * @param {number} options.nodeId - Node ID if editing an existing node
 * @param {number} options.parentId - Parent node ID if creating a new child node
 * @param {number} options.autoSaveInterval - Auto-save interval in ms (default: 3000)
 * @param {number} options.debounceDelay - Debounce delay for typing (default: 1000)
 * @returns {Object} - { draft, saveDraft, deleteDraft, lastSaved, isSaving, loadError }
 */
export function useDraft(options = {}) {
  const {
    nodeId = null,
    parentId = null,
    autoSaveInterval = 3000,
    debounceDelay = 1000
  } = options;

  const [draft, setDraft] = useState(null);
  const [lastSaved, setLastSaved] = useState(null);
  const [isSaving, setIsSaving] = useState(false);
  const [loadError, setLoadError] = useState(null);
  const [isLoaded, setIsLoaded] = useState(false);

  // Track pending content to save
  const pendingContentRef = useRef(null);
  const debounceTimerRef = useRef(null);
  const autoSaveTimerRef = useRef(null);
  // Promise of the autosave request currently on the wire (null if none)
  const inFlightSaveRef = useRef(null);
  // True from the start of deleteDraft until its DELETE has completed
  const deletingRef = useRef(false);

  // Build query params for API calls
  const buildParams = useCallback(() => {
    const params = new URLSearchParams();
    if (nodeId) params.append('node_id', nodeId);
    if (parentId) params.append('parent_id', parentId);
    return params.toString();
  }, [nodeId, parentId]);

  // Load draft on mount
  useEffect(() => {
    const loadDraft = async () => {
      try {
        const params = buildParams();
        const response = await api.get(`/drafts/?${params}`);
        setDraft(response.data);
        // Parse server timestamp as UTC (append Z if missing)
        const timestamp = response.data.updated_at;
        const utcTimestamp = timestamp.endsWith('Z') ? timestamp : timestamp + 'Z';
        setLastSaved(new Date(utcTimestamp));
        setLoadError(null);
      } catch (err) {
        if (err.response?.status === 404) {
          // No draft exists - this is fine
          setDraft(null);
        } else {
          console.error('Error loading draft:', err);
          setLoadError(err.response?.data?.error || 'Failed to load draft');
        }
      } finally {
        setIsLoaded(true);
      }
    };

    loadDraft();

    // Cleanup timers on unmount
    return () => {
      if (debounceTimerRef.current) {
        clearTimeout(debounceTimerRef.current);
      }
      if (autoSaveTimerRef.current) {
        clearInterval(autoSaveTimerRef.current);
      }
    };
  }, [buildParams]);

  // Save draft to server. The in-flight request's promise is kept in a ref
  // so deleteDraft can wait for it: a save that reaches the server after the
  // DELETE would recreate the draft with the text that was just sent.
  const saveDraftToServer = useCallback((content) => {
    // One save at a time, and none while a delete is waiting to go out.
    if (inFlightSaveRef.current || deletingRef.current) return inFlightSaveRef.current;

    setIsSaving(true);
    const request = (async () => {
      try {
        const response = await api.post('/drafts/', {
          content,
          node_id: nodeId,
          parent_id: parentId
        });
        // Only update draft state on initial load, not on every save
        // This prevents unnecessary re-renders that cause cursor jumping
        // setDraft(response.data);

        // Parse server timestamp as UTC (append Z if missing)
        const timestamp = response.data.updated_at;
        const utcTimestamp = timestamp.endsWith('Z') ? timestamp : timestamp + 'Z';
        setLastSaved(new Date(utcTimestamp));
        // Keep newer text typed while this request was in flight
        if (pendingContentRef.current === content) {
          pendingContentRef.current = null;
        }
      } catch (err) {
        console.error('Error saving draft:', err);
        // Don't clear pending content on error - will retry on next auto-save
      } finally {
        inFlightSaveRef.current = null;
        setIsSaving(false);
      }
    })();
    inFlightSaveRef.current = request;
    return request;
  }, [nodeId, parentId]);

  // Debounced save function (called when user types)
  const saveDraft = useCallback((content) => {
    // Store the pending content
    pendingContentRef.current = content;

    // Clear existing debounce timer
    if (debounceTimerRef.current) {
      clearTimeout(debounceTimerRef.current);
    }

    // Set new debounce timer
    debounceTimerRef.current = setTimeout(() => {
      if (pendingContentRef.current !== null) {
        saveDraftToServer(pendingContentRef.current);
      }
    }, debounceDelay);
  }, [debounceDelay, saveDraftToServer]);

  // Set up auto-save interval
  useEffect(() => {
    autoSaveTimerRef.current = setInterval(() => {
      if (pendingContentRef.current !== null) {
        saveDraftToServer(pendingContentRef.current);
      }
    }, autoSaveInterval);

    return () => {
      if (autoSaveTimerRef.current) {
        clearInterval(autoSaveTimerRef.current);
      }
    };
  }, [autoSaveInterval, saveDraftToServer]);

  // Delete draft from server
  const deleteDraft = useCallback(async () => {
    // Block new autosaves first, so none can start while we wait below
    deletingRef.current = true;
    // Clear any pending saves
    pendingContentRef.current = null;
    if (debounceTimerRef.current) {
      clearTimeout(debounceTimerRef.current);
    }

    try {
      // Wait for a save already on the wire; the DELETE must reach the
      // server after it. The save never rejects, so a failed save does not
      // skip the DELETE.
      if (inFlightSaveRef.current) {
        await inFlightSaveRef.current;
      }
      const params = buildParams();
      await api.delete(`/drafts/?${params}`);
      setDraft(null);
      setLastSaved(null);
    } catch (err) {
      if (err.response?.status !== 404) {
        console.error('Error deleting draft:', err);
      }
      // Even on error, clear local state
      setDraft(null);
      setLastSaved(null);
    } finally {
      deletingRef.current = false;
    }
  }, [buildParams]);

  return {
    draft,
    isLoaded,
    saveDraft,
    deleteDraft,
    lastSaved,
    isSaving,
    loadError
  };
}

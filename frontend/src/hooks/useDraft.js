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

  // Save draft to server
  const saveDraftToServer = useCallback(async (content) => {
    if (isSaving) return;

    setIsSaving(true);
    try {
      const response = await api.post('/drafts/', {
        content,
        node_id: nodeId,
        parent_id: parentId
      });
      setDraft(response.data);
      // Parse server timestamp as UTC (append Z if missing)
      const timestamp = response.data.updated_at;
      const utcTimestamp = timestamp.endsWith('Z') ? timestamp : timestamp + 'Z';
      const parsedDate = new Date(utcTimestamp);
      console.log('[DEBUG] Save response:', {
        timestamp_raw: timestamp,
        timestamp_withZ: utcTimestamp,
        parsed: parsedDate.toISOString(),
        now: new Date().toISOString()
      });
      setLastSaved(parsedDate);
      pendingContentRef.current = null;
    } catch (err) {
      console.error('Error saving draft:', err);
      // Don't clear pending content on error - will retry on next auto-save
    } finally {
      setIsSaving(false);
    }
  }, [nodeId, parentId, isSaving]);

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
      if (pendingContentRef.current !== null && !isSaving) {
        saveDraftToServer(pendingContentRef.current);
      }
    }, autoSaveInterval);

    return () => {
      if (autoSaveTimerRef.current) {
        clearInterval(autoSaveTimerRef.current);
      }
    };
  }, [autoSaveInterval, isSaving, saveDraftToServer]);

  // Delete draft from server
  const deleteDraft = useCallback(async () => {
    // Clear any pending saves
    pendingContentRef.current = null;
    if (debounceTimerRef.current) {
      clearTimeout(debounceTimerRef.current);
    }

    try {
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

import { useState, useEffect, useCallback } from 'react';
import api from '../api';

/**
 * Hook to detect interrupted streaming recordings on page load.
 *
 * Checks for drafts with streaming_status='recording' that have stored
 * audio chunks. Provides draft info, discard, and transcript recovery actions.
 *
 * @param {Object} options
 * @param {number|null} options.parentId - Parent node ID filter (null for root)
 * @param {boolean} options.skip - Skip the check (e.g. when resuming LLM processing)
 * @returns {Object}
 */
export function useInterruptedRecovery({ parentId = null, skip = false } = {}) {
  const [interruptedDraft, setInterruptedDraft] = useState(null);
  const [checked, setChecked] = useState(false);
  const [recoveryState, setRecoveryState] = useState(null); // null | 'transcribing' | 'done'

  useEffect(() => {
    if (skip) { setChecked(true); return; }
    const checkInterrupted = async () => {
      try {
        const params = parentId ? `?parent_id=${parentId}` : '';
        const res = await api.get(`/drafts/interrupted${params}`);
        if (res.data.length > 0) {
          setInterruptedDraft(res.data[0]);
        }
      } catch (_) { /* no interrupted drafts */ }
      setChecked(true);
    };
    checkInterrupted();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Trigger transcription of stored chunks and poll until done
  const handleRecover = useCallback(async () => {
    if (!interruptedDraft) return;
    setRecoveryState('transcribing');
    try {
      if (interruptedDraft.has_stored_chunks) {
        await api.post(`/drafts/streaming/${interruptedDraft.session_id}/transcribe-remaining`);
      }
      const poll = setInterval(async () => {
        try {
          const res = await api.get(`/drafts/streaming/${interruptedDraft.session_id}/status`);
          if (res.data.streaming_status === 'completed' || res.data.streaming_status === 'failed') {
            clearInterval(poll);
            setRecoveryState('done');
            setInterruptedDraft(prev => ({ ...prev, content: res.data.content }));
          }
        } catch (_) {
          clearInterval(poll);
          setRecoveryState('done');
        }
      }, 2000);
      setTimeout(() => { clearInterval(poll); setRecoveryState('done'); }, 5 * 60 * 1000);
    } catch (_) {
      setRecoveryState('done');
    }
  }, [interruptedDraft]);

  const handleDiscard = useCallback(async () => {
    if (!interruptedDraft) return;
    try {
      await api.delete(`/drafts/streaming/${interruptedDraft.session_id}/discard`);
    } catch (_) { /* best effort */ }
    setInterruptedDraft(null);
    setRecoveryState(null);
  }, [interruptedDraft]);

  const clearInterrupted = useCallback(() => {
    setInterruptedDraft(null);
  }, []);

  return {
    interruptedDraft,
    checked,
    recoveryState,
    handleRecover,
    handleDiscard,
    clearInterrupted,
  };
}

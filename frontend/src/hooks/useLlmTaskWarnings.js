import { useEffect, useRef } from 'react';
import { useToast } from '../contexts/ToastContext';

/**
 * Surface server-side LLM-task warnings (e.g. unrecognized {user_export}
 * param keys) as toasts. Designed to be called by any hook that already
 * polls /nodes/<id>/llm-status — pass the same llmData/llmStatus through.
 *
 * Deduplicates by message text using a ref-tracked set so polling
 * repeats don't flood the toast queue.
 */
export function useLlmTaskWarnings(llmData, llmStatus, { duration = 8000 } = {}) {
  const { addToast } = useToast();
  const seenRef = useRef(new Set());

  useEffect(() => {
    if (llmStatus !== 'completed') return;
    const warnings = llmData?.warnings;
    if (!Array.isArray(warnings) || warnings.length === 0) return;
    for (const w of warnings) {
      if (typeof w !== 'string' || seenRef.current.has(w)) continue;
      seenRef.current.add(w);
      addToast(w, duration);
    }
  }, [llmStatus, llmData, addToast, duration]);
}

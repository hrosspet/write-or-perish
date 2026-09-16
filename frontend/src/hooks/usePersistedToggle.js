import { useState, useCallback } from 'react';

/**
 * Boolean switch remembered in localStorage under `key`.
 *
 * - First render with nothing stored → `defaultValue`.
 * - `enabled` false → pinned to false and never written (NodeForm only
 *   offers Agentic Reply / Auto-generate on top-level entries, and a
 *   reply form must not overwrite the remembered choice).
 * - The setter takes a value or an updater, like useState's.
 */
export default function usePersistedToggle(key, enabled, defaultValue = true) {
  const [value, setValueState] = useState(() => {
    if (!enabled) return false;
    const stored = localStorage.getItem(key);
    return stored === null ? defaultValue : stored === 'true';
  });
  const setValue = useCallback((next) => {
    setValueState(prev => {
      const resolved = typeof next === 'function' ? next(prev) : next;
      if (enabled) {
        localStorage.setItem(key, String(resolved));
      }
      return resolved;
    });
  }, [key, enabled]);
  return [value, setValue];
}

import { useEffect } from 'react';

/**
 * Submit a form/input/textarea with Cmd+Return (macOS) or Ctrl+Enter
 * (Windows/Linux) — the conventional "primary submit" shortcut (#129).
 *
 * Plain Enter is left untouched, so textareas still insert a newline and
 * single-line inputs keep their native form-submit behaviour.
 *
 * Params:
 *   ref      — a React ref pointing at the element to listen on (textarea/input).
 *   onSubmit — called when the shortcut fires.
 *   enabled  — when false, the shortcut is a no-op (e.g. submit disabled / in-flight).
 */
export default function useSubmitShortcut(ref, onSubmit, enabled = true) {
  useEffect(() => {
    const el = ref && ref.current;
    if (!el || !enabled || typeof onSubmit !== 'function') return undefined;

    const handleKeyDown = (e) => {
      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        onSubmit(e);
      }
    };

    el.addEventListener('keydown', handleKeyDown);
    return () => el.removeEventListener('keydown', handleKeyDown);
  }, [ref, onSubmit, enabled]);
}

/**
 * Returns the platform-appropriate hint string for the submit shortcut,
 * e.g. "⌘↵" on macOS or "Ctrl+↵" elsewhere. Useful for inline UI hints.
 */
export function submitShortcutHint() {
  const platform =
    (typeof navigator !== 'undefined' &&
      (navigator.platform || navigator.userAgent)) || '';
  const isMac = /Mac|iPhone|iPad|iPod/i.test(platform);
  return isMac ? '⌘↵' : 'Ctrl+↵';
}

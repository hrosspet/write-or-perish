import { useEffect } from 'react';

/**
 * Call `onEscape` when the Escape key is pressed anywhere on the page — the
 * conventional "cancel the open edit form / dismiss the modal" gesture.
 *
 * Listens on `document` (not a specific element) so Esc cancels regardless of
 * which field is focused. Cooperates via `event.defaultPrevented`: it skips if
 * something earlier already handled the Escape, and marks the event handled
 * after firing. So if a modal and an edit form are both mounted, only one
 * responds to a single Esc press.
 *
 * Params:
 *   onEscape — called when Escape is pressed.
 *   enabled  — when false, the listener is not attached (e.g. not editing, or
 *              a save is in flight and the form shouldn't be cancellable).
 */
export default function useEscapeKey(onEscape, enabled = true) {
  useEffect(() => {
    if (!enabled || typeof onEscape !== 'function') return undefined;

    const handleKeyDown = (e) => {
      if (e.key === 'Escape' && !e.defaultPrevented) {
        e.preventDefault();
        onEscape(e);
      }
    };

    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [onEscape, enabled]);
}

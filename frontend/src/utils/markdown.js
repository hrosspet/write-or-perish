import { useRef, useCallback } from 'react';
import { useToast } from '../contexts/ToastContext';

/**
 * Toggle a checkbox in raw markdown content.
 * Finds the line matching `- [ ] itemText` or `- [x] itemText` and flips it.
 */
export function toggleCheckbox(content, itemText, currentChecked) {
  const lines = content.split('\n');
  const newLines = lines.map(line => {
    const itemMatch = line.match(/^(\s*)- \[([ xX])\]\s+(.+)/);
    if (itemMatch && itemMatch[3].trim() === itemText) {
      const indent = itemMatch[1];
      return currentChecked
        ? `${indent}- [ ] ${itemMatch[3]}`
        : `${indent}- [x] ${itemMatch[3]}`;
    }
    return line;
  });
  return newLines.join('\n');
}

/**
 * Hook for optimistic checkbox toggling.
 *
 * Parameters:
 *   getContent: () => string|null — returns current markdown content
 *   setContent: (newContent: string) => void — optimistically update local state
 *   save: (newContent: string) => Promise — persist the change (should reject on failure)
 *
 * Returns a toggle handler: (itemText: string, currentChecked: boolean) => void
 */
export function useCheckboxToggle(getContent, setContent, save) {
  const prevContentRef = useRef(null);
  const { addToast } = useToast();

  return useCallback((itemText, currentChecked) => {
    const content = getContent();
    if (!content) return;
    prevContentRef.current = content;
    const newContent = toggleCheckbox(content, itemText, currentChecked);
    setContent(newContent);
    save(newContent).catch((err) => {
      console.error('Failed to toggle checkbox:', err);
      if (prevContentRef.current !== null) {
        setContent(prevContentRef.current);
      }
      const reason = err.response?.data?.error
        || err.response?.statusText
        || err.message
        || 'Unknown error';
      addToast(`Couldn't save change — reverted (${reason})`);
    });
  }, [getContent, setContent, save, addToast]);
}

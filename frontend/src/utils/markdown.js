import { useRef, useCallback } from 'react';
import { useToast } from '../contexts/ToastContext';

// Strip common inline markdown so a raw source line can be compared
// against react-markdown's plain-text extraction. Without this, a line
// like `- [ ] **bold** rest` is captured with the asterisks intact and
// never matches the React-extracted itemText (`bold rest`), so the
// toggle silently no-ops.
function stripInlineMarkdown(text) {
  return text
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')   // ![alt](url) → alt
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')    // [text](url) → text
    .replace(/\*\*([^*]+?)\*\*/g, '$1')         // **bold**
    .replace(/__([^_]+?)__/g, '$1')             // __bold__
    .replace(/~~([^~]+?)~~/g, '$1')             // ~~strike~~
    .replace(/`([^`]+?)`/g, '$1')               // `code`
    .replace(/(?<![*\w])\*([^*\n]+?)\*(?!\w)/g, '$1')   // *italic*
    .replace(/(?<![_\w])_([^_\n]+?)_(?!\w)/g, '$1');    // _italic_
}

/**
 * Toggle a checkbox in raw markdown content.
 * Finds the line matching `- [ ] itemText` or `- [x] itemText` and flips it.
 */
export function toggleCheckbox(content, itemText, currentChecked) {
  const lines = content.split('\n');
  const newLines = lines.map(line => {
    const itemMatch = line.match(/^(\s*)- \[([ xX])\]\s+(.+)/);
    if (itemMatch && stripInlineMarkdown(itemMatch[3]).trim() === itemText) {
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
 * Append a new unchecked checkbox item (`- [ ] <task>`) to the END of a
 * named markdown section (a `## <sectionTitle>` heading) in `content`.
 *
 * The item is inserted right after the last non-blank line that belongs to
 * the section (its heading and any list items / text), and before the blank
 * line(s) that separate it from the next `##` heading. This keeps existing
 * spacing between sections intact.
 *
 * If the section is not found, the item (with the heading) is appended to the
 * end of the document so the task is never silently dropped.
 */
export function appendItemToSection(content, sectionTitle, task) {
  const cleanTask = (task || '').trim();
  if (!cleanTask) return content;
  const newItem = `- [ ] ${cleanTask}`;

  const base = content || '';
  const lines = base.split('\n');

  // Locate the heading line for the target section (case-insensitive match
  // on the trimmed title).
  let headingIdx = -1;
  for (let i = 0; i < lines.length; i++) {
    const m = lines[i].match(/^##\s+(.+)/);
    if (m && m[1].trim().toLowerCase() === sectionTitle.trim().toLowerCase()) {
      headingIdx = i;
      break;
    }
  }

  // Section not present — append a fresh section at the end.
  if (headingIdx === -1) {
    const sep = base.length && !base.endsWith('\n') ? '\n\n' : (base.endsWith('\n\n') ? '' : '\n');
    return `${base}${sep}## ${sectionTitle}\n\n${newItem}\n`;
  }

  // Find where this section ends: the next `##` heading, or end of document.
  let sectionEnd = lines.length;
  for (let i = headingIdx + 1; i < lines.length; i++) {
    if (/^##\s+/.test(lines[i])) {
      sectionEnd = i;
      break;
    }
  }

  // Within [headingIdx+1, sectionEnd), find the index AFTER the last
  // non-blank line, so we insert below existing items but above the trailing
  // blank lines that precede the next heading.
  let insertAt = headingIdx + 1;
  for (let i = headingIdx + 1; i < sectionEnd; i++) {
    if (lines[i].trim() !== '') {
      insertAt = i + 1;
    }
  }

  lines.splice(insertAt, 0, newItem);
  return lines.join('\n');
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

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
 * Insert a new list item immediately after the item whose text matches
 * `afterItemText` (and after that item's nested subtree), preserving the
 * matched item's bullet style ("- " plain vs "- [ ] " checkbox) and
 * indentation. Powers the per-row "+" quick-add across Todo, proposals, and
 * MarkdownBody checklists. Returns the content unchanged if no line matches.
 */
export function insertItemAfter(content, afterItemText, newText) {
  const text = (newText || '').trim();
  if (!text) return content;
  const lines = (content || '').split('\n');
  // indent | bullet | optional checkbox | label
  const lineRe = /^(\s*)([-*])\s+(\[[ xX]\]\s+)?(.*)$/;

  let idx = -1;
  let indent = '';
  let bullet = '-';
  let checkbox = '';
  for (let i = 0; i < lines.length; i++) {
    const m = lines[i].match(lineRe);
    if (m && stripInlineMarkdown(m[4]).trim() === afterItemText) {
      idx = i;
      indent = m[1];
      bullet = m[2];
      checkbox = m[3] ? '[ ] ' : '';
      break;
    }
  }
  if (idx === -1) return content;

  // Skip past the item's nested subtree (deeper-indented lines) so the new
  // sibling lands after the item's children, not in the middle of them.
  let insertAt = idx + 1;
  for (let i = idx + 1; i < lines.length; i++) {
    const lead = (lines[i].match(/^(\s*)/)[1] || '');
    if (lines[i].trim() !== '' && lead.length > indent.length) {
      insertAt = i + 1;
    } else {
      break;
    }
  }

  lines.splice(insertAt, 0, `${indent}${bullet} ${checkbox}${text}`);
  return lines.join('\n');
}

/**
 * Append a new list item to the END of a named markdown section in `content`.
 *
 * Shared by the Todo page quick-add (`## Today`, issue #108) and the
 * Voice/Text proposal quick-add (`### New Tasks` in ProposalInline). The
 * item is inserted right after the last non-blank line that belongs to the
 * section, and before the blank line(s) before the next heading — so existing
 * spacing between sections is preserved. If the section isn't found, a fresh
 * section (with `createTitle`) is appended so the task is never silently
 * dropped.
 *
 * Options:
 *   headingLevel — heading depth of the target section (2 = `##`, 3 = `###`).
 *   match        — 'exact' matches the whole heading text; 'includes' matches
 *                  a substring (e.g. 'new task' matches a `### New Tasks`).
 *   itemPrefix   — prefix for the inserted line (`- [ ] ` for checklists,
 *                  `- ` for plain bullet lists like the proposal sections).
 *   createTitle  — heading text used when the section must be created
 *                  (defaults to `sectionTitle`; useful when `match` is a
 *                  lowercase search term but the heading should be cased).
 */
export function appendItemToSection(content, sectionTitle, task, options = {}) {
  const {
    headingLevel = 2,
    match = 'exact',
    itemPrefix = '- [ ] ',
    createTitle = sectionTitle,
  } = options;

  const cleanTask = (task || '').trim();
  if (!cleanTask) return content;
  const newItem = `${itemPrefix}${cleanTask}`;

  const base = content || '';
  const lines = base.split('\n');
  const hashes = '#'.repeat(headingLevel);
  // Heading line at exactly this level; section boundary is any heading at
  // the same-or-higher level (`#{1,headingLevel}`), so a deeper subheading
  // doesn't prematurely end the section.
  const headingRe = new RegExp(`^#{${headingLevel}}\\s+(.+)`);
  const boundaryRe = new RegExp(`^#{1,${headingLevel}}\\s+`);
  const wanted = sectionTitle.trim().toLowerCase();
  const headingMatches = (h) => (match === 'includes' ? h.includes(wanted) : h === wanted);

  // Locate the heading line for the target section (case-insensitive).
  let headingIdx = -1;
  for (let i = 0; i < lines.length; i++) {
    const m = lines[i].match(headingRe);
    if (m && headingMatches(m[1].trim().toLowerCase())) {
      headingIdx = i;
      break;
    }
  }

  // Section not present — append a fresh section at the end.
  if (headingIdx === -1) {
    const sep = base.length && !base.endsWith('\n') ? '\n\n' : (base.endsWith('\n\n') ? '' : '\n');
    return `${base}${sep}${hashes} ${createTitle}\n\n${newItem}\n`;
  }

  // Find where this section ends: the next same-or-higher heading, or EOF.
  let sectionEnd = lines.length;
  for (let i = headingIdx + 1; i < lines.length; i++) {
    if (boundaryRe.test(lines[i])) {
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

/**
 * Hook for optimistic per-row task insertion (the hover "+" quick-add).
 * Same getContent/setContent/save contract as useCheckboxToggle.
 *
 * Returns: (afterItemText: string, newText: string) => void — inserts a new
 * item right after the matched row (and its subtree), optimistically, then
 * persists; reverts and toasts on failure.
 */
export function useTaskInsert(getContent, setContent, save) {
  const prevContentRef = useRef(null);
  const { addToast } = useToast();

  return useCallback((afterItemText, newText) => {
    const content = getContent();
    if (content == null) return;
    const newContent = insertItemAfter(content, afterItemText, newText);
    if (newContent === content) return;
    prevContentRef.current = content;
    setContent(newContent);
    save(newContent).catch((err) => {
      console.error('Failed to insert task:', err);
      if (prevContentRef.current !== null) {
        setContent(prevContentRef.current);
      }
      const reason = err.response?.data?.error
        || err.response?.statusText
        || err.message
        || 'Unknown error';
      addToast(`Couldn't add task — reverted (${reason})`);
    });
  }, [getContent, setContent, save, addToast]);
}

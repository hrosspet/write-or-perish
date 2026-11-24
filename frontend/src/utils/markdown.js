import { useRef, useCallback } from 'react';

// Strip common inline markdown so a raw source line can be compared
// against react-markdown's plain-text extraction. Without this, a line
// like `- [ ] **bold** rest` is captured with the asterisks intact and
// never matches the React-extracted itemText (`bold rest`), so the
// toggle silently no-ops.
export function stripInlineMarkdown(text) {
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

const CHECKBOX_LINE = /^(\s*)- \[([ xX])\]\s+(.+)/;

/**
 * Whether `content` has a checkbox line that toggleCheckbox would match for
 * `itemText` (same key: the label with inline markdown stripped).
 */
export function hasCheckboxItem(content, itemText) {
  return (content || '').split('\n').some((line) => {
    const itemMatch = line.match(CHECKBOX_LINE);
    return !!itemMatch && stripInlineMarkdown(itemMatch[3]).trim() === itemText;
  });
}

/**
 * Toggle a checkbox in raw markdown content.
 * Finds the line matching `- [ ] itemText` or `- [x] itemText` and flips it.
 */
export function toggleCheckbox(content, itemText, currentChecked) {
  const lines = content.split('\n');
  const newLines = lines.map(line => {
    const itemMatch = line.match(CHECKBOX_LINE);
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
 *   createAtStart — when the section doesn't exist, create it at the TOP of
 *                  the content instead of appending at the end (e.g. `## Today`
 *                  should be the first todo section).
 */
export function appendItemToSection(content, sectionTitle, task, options = {}) {
  const {
    headingLevel = 2,
    match = 'exact',
    itemPrefix = '- [ ] ',
    createTitle = sectionTitle,
    createAtStart = false,
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

  // Section not present — create a fresh one.
  if (headingIdx === -1) {
    const newSection = `${hashes} ${createTitle}\n\n${newItem}\n`;
    if (createAtStart) {
      const body = base.trim();
      return body ? `${newSection}\n${body}\n` : newSection;
    }
    const sep = base.length && !base.endsWith('\n') ? '\n\n' : (base.endsWith('\n\n') ? '' : '\n');
    return `${base}${sep}${newSection}`;
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

// What a page says when the server refused a tick or a row "+" because the
// text changed since the page showed it (#795). The page undoes the change
// and leaves its text as it is; the person reloads to see the newer version.
export const NEWER_ENTRY_MESSAGE = "There's a newer version of this entry, so your change wasn't saved. "
  + 'Reload the page before continuing.';
export const NEWER_ARTIFACT_MESSAGE = "There's a newer version of this artifact, so your change wasn't saved. "
  + 'Reload the page before continuing.';
export const NEWER_TODO_MESSAGE = "There's a newer version of your todo list, so your change wasn't saved. "
  + 'Reload the page before continuing.';
// Long enough to read the instruction in it.
export const NEWER_MESSAGE_MS = 8000;

/** Whether `err` is the server's "the text changed since the page showed it" (409). */
export function isContentChanged(err) {
  return err?.response?.status === 409 && err.response.data?.code === 'content_changed';
}

/** The edit a tick makes: the line's box set to the opposite of `currentChecked`. */
export const tickEdit = (itemText, currentChecked) => (content) => toggleCheckbox(content, itemText, currentChecked);

/** The edit the row "+" makes: a new item after the matched row and its subtree. */
export const insertEdit = (afterItemText, newText) => (content) => insertItemAfter(content, afterItemText, newText);

/**
 * Optimistic checklist edits (a tick, the row "+", a proposal row moved),
 * saved only over the text the page showed (#795, Peter 2026-11-24: the tick
 * shows at once; it is saved only if it skips no newer version).
 *
 * Each save sends the new text and `base`, the text it was made on. Saves
 * go one after the other: a quick second tick waits for the first and is
 * sent on the text the first one saved. When the server answers that the
 * text changed (isContentChanged), the page's pending edits are undone and
 * `notify` says there is a newer version; the page keeps its text (no
 * reload of its own). Any other failure undoes that edit and names the
 * reason.
 *
 * Options (read at call time, so they may change every render):
 *   getContent(key)            the text shown for document `key`, or null
 *   setContent(key, text)      show `text` for document `key`
 *   save(key, content, base)   Promise; rejects on failure
 *   newerMessage               the text for the refusal above
 *   notify(message, ms)        shows a toast
 *
 * Returns apply(key, edit, failure): `edit` maps a text to the edited text
 * (the same text when there is nothing to change); `failure` begins the
 * message of any other error ("Couldn't save change").
 */
export function useChecklistEdits(options) {
  const optionsRef = useRef(options);
  optionsRef.current = options;
  // Per document: base (the text the server has, as far as this page
  // knows), pending (edits shown but not saved yet, oldest first) and shown
  // (the text this hook last put on the page).
  const docsRef = useRef(new Map());
  const chainRef = useRef(Promise.resolve());

  const send = useCallback(async (key, doc, entry) => {
    // Dropped: an earlier edit of the same text was refused.
    if (!doc.pending.includes(entry)) return;
    const { getContent, setContent, save, newerMessage, notify } = optionsRef.current;
    const base = doc.base;
    const content = entry.edit(base);
    const drop = () => { doc.pending = doc.pending.filter((e) => e !== entry); };
    // Undo on the page, unless something else (a reload, a refetch) has
    // put another text there meanwhile.
    const show = (text) => {
      if (getContent(key) === doc.shown) {
        doc.shown = text;
        setContent(key, text);
      }
    };
    if (content == null || content === base) {
      drop();
      return;
    }
    try {
      await save(key, content, base);
      doc.base = content;
      drop();
    } catch (err) {
      if (isContentChanged(err)) {
        doc.pending = [];
        show(base);
        notify(newerMessage, NEWER_MESSAGE_MS);
        return;
      }
      console.error(`${entry.failure}:`, err);
      drop();
      show(doc.pending.reduce((text, e) => e.edit(text), base));
      const reason = err.response?.data?.error
        || err.response?.statusText
        || err.message
        || 'Unknown error';
      notify(`${entry.failure} — reverted (${reason})`);
    }
  }, []);

  return useCallback((key, edit, failure = "Couldn't save change") => {
    const { getContent, setContent } = optionsRef.current;
    const shown = getContent(key);
    if (shown == null) return;
    const next = edit(shown);
    if (next == null || next === shown) return;
    let doc = docsRef.current.get(key);
    if (!doc || doc.pending.length === 0) {
      // Nothing of this page is being saved: what it shows is what the
      // server sent it, and the base of this edit.
      doc = { base: shown, pending: [], shown };
      docsRef.current.set(key, doc);
    }
    const entry = { edit, failure };
    doc.pending = [...doc.pending, entry];
    doc.shown = next;
    setContent(key, next);
    chainRef.current = chainRef.current
      .then(() => send(key, doc, entry))
      .catch(() => {});
  }, [send]);
}

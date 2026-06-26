// Pure parsing helpers for the intentions artifact, kept free of any
// component / react-markdown imports so they're unit-testable in isolation.

// Parse the intentions markdown into [{ title, entries: [{ name, status,
// body, notes }] }]. Tolerant of missing status/body, a leading section-less
// run of entries, and arbitrary section titles.
export function parseIntentions(md) {
  const lines = (md || '').split('\n');
  const sections = [];
  let section = null;
  let entry = null;

  const flushEntry = () => {
    if (entry && section) section.entries.push(entry);
    entry = null;
  };
  const ensureSection = (title) => {
    section = { title, entries: [] };
    sections.push(section);
  };

  for (const raw of lines) {
    const line = raw.trim();
    const h1 = raw.match(/^#\s+(.+)/);
    const h2 = raw.match(/^##\s+(.+)/);
    const note = raw.match(/^\s*[-*]\s+(.+)/);

    if (h2) {
      flushEntry();
      if (!section) ensureSection('');
      entry = { name: h2[1].trim(), status: '', body: [], notes: [] };
      continue;
    }
    if (h1) {
      flushEntry();
      ensureSection(h1[1].trim());
      continue;
    }
    if (!entry) continue;
    if (note) {
      entry.notes.push(note[1].trim());
      continue;
    }
    if (!line) continue;
    const italic = line.match(/^\*(.+)\*$/) || line.match(/^_(.+)_$/);
    if (italic && !entry.status && entry.body.length === 0) {
      entry.status = italic[1].trim();
    } else {
      entry.body.push(line);
    }
  }
  flushEntry();
  return sections;
}

// Derive a coarse state from the status line for the dot styling.
export function statusState(status) {
  const s = (status || '').toLowerCase();
  if (s.includes('fulfilled')) return 'fulfilled';
  if (s.includes('released')) return 'released';
  if (s.includes('inferred') || s.includes('unconfirmed')) return 'inferred';
  return 'active';
}

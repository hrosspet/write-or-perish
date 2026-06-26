import { parseIntentions } from './intentions';

const SAMPLE = `# Endorsed
## Help people become more intentional through Loore
*held since early — active*
The meta-intention.

## Get Pája to see the cage
*held since early — fulfilled 2026-06-25*
Not to punish, but because without her acknowledgment the system can't move.
- 2026-06-25: User confirmed this is fulfilled.

# Inferred
## Build a life that is coherent regardless of the marriage
*noticed early — inferred, unconfirmed*
Valeč, Loore, the spiritual path — none depend on Pája.`;

test('splits into Endorsed/Inferred sections with entry counts', () => {
  const sections = parseIntentions(SAMPLE);
  expect(sections.map((s) => s.title)).toEqual(['Endorsed', 'Inferred']);
  expect(sections[0].entries).toHaveLength(2);
  expect(sections[1].entries).toHaveLength(1);
});

test('parses name, status, body and dated notes per entry', () => {
  const [endorsed] = parseIntentions(SAMPLE);
  const cage = endorsed.entries[1];
  expect(cage.name).toBe('Get Pája to see the cage');
  expect(cage.status).toBe('held since early — fulfilled 2026-06-25');
  expect(cage.body.join(' ')).toContain("can't move");
  expect(cage.notes).toEqual(['2026-06-25: User confirmed this is fulfilled.']);
});

test('an entry with only a status line has empty body and notes', () => {
  const [endorsed] = parseIntentions(SAMPLE);
  const meta = endorsed.entries[0];
  expect(meta.status).toBe('held since early — active');
  expect(meta.body).toEqual(['The meta-intention.']);
  expect(meta.notes).toEqual([]);
});

test('content with no entries returns no parsed entries (falls back to markdown)', () => {
  const sections = parseIntentions('just some freeform text, no headings');
  expect(sections.some((s) => s.entries.length > 0)).toBe(false);
});

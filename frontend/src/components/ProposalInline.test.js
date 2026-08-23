// MarkdownBody pulls in react-markdown (ESM, not transformed by CRA jest) and
// api pulls in axios — neither is needed to exercise the pure parser. Mock them
// so importing ProposalInline doesn't drag in the untransformable chain.
jest.mock('./MarkdownBody', () => () => null);
jest.mock('../api', () => ({}));

import {
  parseOrientResponse, stripProposalSections, splitProposalText,
  parseShareBlocks, hasShareBlocks, hasProposalSections,
} from './ProposalInline';

// Regression: the category badges (issue + feedback) must take only the first
// line of their heading section. The model sometimes appends a closing remark
// after the category word; without the first-line cut it leaks into the badge.
// The backend parsers (parse_github_issue / parse_feedback) already do this, so
// these guard the display side against drifting from the data.

test('feedback category takes the first line only', () => {
  const text = [
    '### Feedback',
    'The text mode feels clean and fast.',
    '### Feedback category',
    'praise',
    '',
    "Take a look and let me know — once you confirm I'll send it.",
  ].join('\n');
  const parsed = parseOrientResponse(text);
  expect(parsed.feedback).toBe('The text mode feels clean and fast.');
  expect(parsed.feedbackCategory).toBe('praise');
});

test('issue category takes the first line only', () => {
  const text = [
    '### Issue Title',
    'Add dark mode toggle',
    '### Description',
    'Users want a dark mode.',
    '### Category',
    'enhancement',
    '',
    'Want me to file this?',
  ].join('\n');
  const parsed = parseOrientResponse(text);
  expect(parsed.issueCategory).toBe('enhancement');
});

// Regression: trailing commentary the model appends below the structured block
// (after a single-line category value, with no following heading) must survive
// stripping so it stays visible in the message body.
test('strip keeps intro + trailing commentary after feedback category', () => {
  const text = [
    "That's great to hear — I'll draft that for you now.",
    '',
    '### Feedback',
    'The voice mode feels genuinely magical.',
    '',
    '### Feedback category',
    'praise',
    '',
    'Let me know if that captures it.',
  ].join('\n');
  const body = stripProposalSections(text);
  expect(body).toContain("That's great to hear");
  expect(body).toContain('Let me know if that captures it.');
  // Structured parts are rendered in the card, not the body.
  expect(body).not.toContain('### Feedback');
  expect(body).not.toContain('genuinely magical');
  expect(body).not.toMatch(/(^|\n)praise(\n|$)/);
});

test('strip keeps trailing commentary after issue category', () => {
  const text = [
    'Here is the issue I drafted.',
    '### Issue Title',
    'Add dark mode',
    '### Description',
    'Users want dark mode.',
    '### Category',
    'enhancement',
    '',
    'Sound right?',
  ].join('\n');
  const body = stripProposalSections(text);
  expect(body).toContain('Here is the issue I drafted.');
  expect(body).toContain('Sound right?');
  expect(body).not.toContain('### Category');
  expect(body).not.toContain('Add dark mode');
});

// The lead-in renders above the card; trailing commentary renders below it.
test('split separates lead-in (before) from trailing commentary (after)', () => {
  const text = [
    "Glad you're enjoying it.",
    '',
    '### Feedback',
    'Love the voice mode.',
    '',
    '### Feedback category',
    'praise',
    '',
    'Thanks for building this!',
  ].join('\n');
  const { before, after } = splitProposalText(text);
  expect(before).toBe("Glad you're enjoying it.");
  expect(after).toBe('Thanks for building this!');
});

test('split returns empty after when there is no trailing commentary', () => {
  const text = [
    'Here is the issue.',
    '### Issue Title',
    'Add dark mode',
    '### Category',
    'enhancement',
  ].join('\n');
  const { before, after } = splitProposalText(text);
  expect(before).toBe('Here is the issue.');
  expect(after).toBe('');
});

// ── Fenced :::share blocks (current share syntax) ──────────────────────────

const MULTI_SHARE = [
  'Two pieces, as you asked.',
  '',
  ':::share insight',
  '### On attention',
  '',
  'First post, with its own markdown headings.',
  ':::',
  '',
  'And the second:',
  '',
  ':::share exploration',
  'Second post body.',
  ':::',
  '',
  'Say the word.',
].join('\n');

test('parseShareBlocks handles multiple blocks and inner ### headings', () => {
  const shares = parseShareBlocks(MULTI_SHARE);
  expect(shares).toHaveLength(2);
  // ### headings inside the fence belong to the share body — the exact
  // mis-parse the fences were introduced to fix.
  expect(shares[0].content.startsWith('### On attention')).toBe(true);
  expect(shares[0].type).toBe('insight');
  expect(shares[1].content).toBe('Second post body.');
  expect(shares[1].type).toBe('exploration');
});

test('parseShareBlocks: unclosed fence runs to end, legacy headings fall back', () => {
  const unclosed = parseShareBlocks('lead-in\n\n:::share need\nno closing fence');
  expect(unclosed).toEqual([{ content: 'no closing fence', type: 'need' }]);
  const legacy = parseShareBlocks('### Share\nold style body\n### Share type\nneed');
  expect(legacy).toEqual([{ content: 'old style body', type: 'need' }]);
});

test('hasShareBlocks matches fences only, hasProposalSections includes them', () => {
  expect(hasShareBlocks(MULTI_SHARE)).toBe(true);
  expect(hasShareBlocks('prose about :::share syntax')).toBe(false);
  expect(hasShareBlocks('### Share\nlegacy')).toBe(false);
  expect(hasProposalSections(':::share\nbody\n:::')).toBe(true);
});

test('split excludes share fences; lead-in before, commentary after', () => {
  const { before, after } = splitProposalText(MULTI_SHARE);
  expect(before).toBe('Two pieces, as you asked.');
  expect(after).toContain('And the second:');
  expect(after).toContain('Say the word.');
  expect(after).not.toContain(':::');
  expect(after).not.toContain('Second post body.');
});

// Regression (caught on staging): a lead-in starting "Noted both — …" made
// includes('note') fire on the intro, rendering a phantom todo note whose
// body was the fence line — the intro part is never a section.
test('lead-in starting with "Noted" does not become a phantom todo note', () => {
  const text = 'Noted both — worth keeping.\n\n:::share insight\n### Inner\nbody\n:::\n\nDone.';
  const parsed = parseOrientResponse(text);
  expect(parsed.note).toBeUndefined();
  expect(parsed.completed).toBeUndefined();
});

test('### headings inside a share body do not trigger other proposal sections', () => {
  const text = 'Lead.\n\n:::share insight\n### Completed\n- x\n\n### Feedback\ny\n:::';
  const parsed = parseOrientResponse(text);
  expect(parsed.completed).toBeUndefined();
  expect(parsed.feedback).toBeUndefined();
  expect(hasProposalSections(text)).toBe(true); // via the share block itself
});

test('shareOnly split leaves the node\'s own ### headings in the prose', () => {
  const text = [
    '### My own heading',
    'my prose',
    ':::share insight',
    'the share',
    ':::',
    'closing thought',
  ].join('\n');
  const { before, after } = splitProposalText(text, { shareOnly: true });
  expect(before).toContain('### My own heading');
  expect(before).toContain('my prose');
  expect(after).toBe('closing thought');
  expect(before + after).not.toContain('the share');
});

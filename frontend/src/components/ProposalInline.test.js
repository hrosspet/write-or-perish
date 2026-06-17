// MarkdownBody pulls in react-markdown (ESM, not transformed by CRA jest) and
// api pulls in axios — neither is needed to exercise the pure parser. Mock them
// so importing ProposalInline doesn't drag in the untransformable chain.
jest.mock('./MarkdownBody', () => () => null);
jest.mock('../api', () => ({}));

import { parseOrientResponse } from './ProposalInline';

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

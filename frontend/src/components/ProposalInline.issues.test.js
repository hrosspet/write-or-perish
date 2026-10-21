// One reply can propose several GitHub issues (#217): each shows as its own
// card with its own create button and state.
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../api';
import ProposalInline, { parseIssueBlocks, stripProposalSections } from './ProposalInline';

jest.mock('./MarkdownBody', () => ({ children }) => <div>{children}</div>);
jest.mock('../api', () => ({
  __esModule: true,
  default: { get: jest.fn(), post: jest.fn(), put: jest.fn() },
}));

const ONE = [
  '### Issue Title', 'Record button does nothing',
  '### Description', 'Tapping record has no effect.',
  '### Category', 'bug',
].join('\n');

const THREE = [
  'Three issues.',
  ONE,
  '### Issue Title', 'Add a dark mode toggle',
  '### Description', 'Users want dark mode.',
  '### Category', 'feature',
  '### Issue Title', 'Speed up the export',
  '### Description', 'The export is slow.',
  '### Category', 'enhancement',
].join('\n');

const META = [{ name: 'propose_github_issue', status: 'success', apply_status: 'pending_approval' }];

const renderCard = (content, toolCallsMeta = META) => render(
  <MemoryRouter>
    <ProposalInline content={content} nodeId={7} toolCallsMeta={toolCallsMeta} />
  </MemoryRouter>,
);

beforeEach(() => {
  jest.clearAllMocks();
});

test('parseIssueBlocks returns every issue in content order', () => {
  const issues = parseIssueBlocks(THREE);
  expect(issues.map(i => i.title)).toEqual([
    'Record button does nothing', 'Add a dark mode toggle', 'Speed up the export']);
  expect(issues.map(i => i.category)).toEqual(['bug', 'feature', 'enhancement']);
  expect(issues[1].description).toBe('Users want dark mode.');
});

test('a single issue parses as before', () => {
  expect(parseIssueBlocks(ONE)).toEqual([{
    title: 'Record button does nothing',
    description: 'Tapping record has no effect.',
    category: 'bug',
  }]);
});

test('all issue blocks are cut out of the surrounding prose', () => {
  expect(stripProposalSections(THREE)).toBe('Three issues.');
});

test('a single issue shows one card, one button and no "Create all"', () => {
  renderCard(ONE);
  expect(screen.getAllByText('Create issue')).toHaveLength(1);
  expect(screen.queryByText('Create all issues')).not.toBeInTheDocument();
});

test('each of several issues has its own button and files on its own', async () => {
  api.post.mockResolvedValue({ data: {
    status: 'partial', issue_url: 'https://github.com/o/r/issues/12', issue_number: 12,
    issues: [{ index: 1, issue_url: 'https://github.com/o/r/issues/12', issue_number: 12 }],
  } });
  renderCard(THREE);
  expect(screen.getByText('Record button does nothing')).toBeInTheDocument();
  expect(screen.getByText('Add a dark mode toggle')).toBeInTheDocument();
  expect(screen.getByText('Speed up the export')).toBeInTheDocument();
  const buttons = screen.getAllByText('Create issue');
  expect(buttons).toHaveLength(3);

  fireEvent.click(buttons[1]);

  await waitFor(() => expect(api.post).toHaveBeenCalledWith(
    '/github/create-issue', { llm_node_id: 7, issue_index: 1 }));
  const link = await screen.findByText('#12');
  expect(link.closest('a')).toHaveAttribute('href', 'https://github.com/o/r/issues/12');
  // The other two can still be filed.
  expect(screen.getAllByText('Create issue')).toHaveLength(2);
  expect(api.post).toHaveBeenCalledTimes(1);
});

test('a failure on one issue leaves the others untouched', async () => {
  api.post.mockRejectedValue({ response: { data: { error: 'Couldn\'t create the GitHub issue. Please try again later.' } } });
  renderCard(THREE);

  fireEvent.click(screen.getAllByText('Create issue')[0]);

  expect(await screen.findByText('Couldn\'t create the GitHub issue. Please try again later.')).toBeInTheDocument();
  // The failed one can be tried again; the others show only their button.
  expect(screen.getAllByText('Create issue')).toHaveLength(3);
});

test('"Create all issues" files every issue not filed yet in one call', async () => {
  api.post.mockResolvedValue({ data: {
    status: 'completed', issue_url: 'u1', issue_number: 1,
    issues: [
      { index: 0, issue_url: 'https://github.com/o/r/issues/1', issue_number: 1 },
      { index: 1, issue_url: 'https://github.com/o/r/issues/2', issue_number: 2 },
      { index: 2, issue_url: 'https://github.com/o/r/issues/3', issue_number: 3 },
    ],
  } });
  renderCard(THREE);

  fireEvent.click(screen.getByText('Create all issues'));

  await waitFor(() => expect(api.post).toHaveBeenCalledWith(
    '/github/create-issue', { llm_node_id: 7, all: true }));
  expect(await screen.findByText('#3')).toBeInTheDocument();
  expect(screen.getAllByText(/Issue created/)).toHaveLength(3);
  expect(screen.queryByText('Create all issues')).not.toBeInTheDocument();
});

test('issues the server already filed show as created', () => {
  renderCard(THREE, [{
    name: 'propose_github_issue', status: 'success', apply_status: 'partial',
    filed: [{ index: 2, issue_url: 'https://github.com/o/r/issues/5', issue_number: 5 }],
  }]);

  expect(screen.getByText('#5')).toBeInTheDocument();
  expect(screen.getAllByText('Create issue')).toHaveLength(2);
  expect(within(screen.getByText('#5').closest('a').parentElement).getByText(/Issue created/)).toBeInTheDocument();
});

test('a single issue filed before issues were tracked one by one still shows as created', () => {
  renderCard(ONE, [{
    name: 'propose_github_issue', status: 'success', apply_status: 'completed',
    issue_url: 'https://github.com/o/r/issues/9', issue_number: 9,
  }]);

  expect(screen.getByText('#9')).toBeInTheDocument();
  expect(screen.queryByText('Create issue')).not.toBeInTheDocument();
});

test('an old completed entry with no list of filed issues marks the LAST issue', () => {
  // The old create button filed the last issue block of a reply.
  renderCard(THREE, [{
    name: 'propose_github_issue', status: 'success', apply_status: 'completed',
    issue_url: 'https://github.com/o/r/issues/9', issue_number: 9,
  }]);

  const link = screen.getByText('#9');
  expect(screen.getAllByText(/Issue created/)).toHaveLength(1);
  // The link sits under the third card, not the first.
  const cards = screen.getAllByText(/^(Record button does nothing|Add a dark mode toggle|Speed up the export)$/);
  const lastCard = cards[2].closest('div').parentElement;
  expect(lastCard).toContainElement(link);
  expect(screen.getAllByText('Create issue')).toHaveLength(2);
});

test('a ### Title inside a :::share block is not an issue (same numbering as the backend)', () => {
  const text = [
    ':::share need',
    'Looking for a thinking partner.',
    '### Title',
    'Not an issue',
    '### Description',
    'Part of the share.',
    ':::',
    ONE,
    '### Issue Title', 'Second real issue',
    '### Description', 'Second.',
  ].join('\n');
  expect(parseIssueBlocks(text).map(i => i.title)).toEqual([
    'Record button does nothing', 'Second real issue']);
});

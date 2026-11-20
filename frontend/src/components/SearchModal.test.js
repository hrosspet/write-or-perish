// Search results show stored text as text (#445). The backend escapes
// `snippet` and `preview` and adds <mark> around matches; the modal turns
// those runs into React text, so nothing in a result becomes an element
// except the highlights. The marker below is inert.
let mockUser = { is_admin: false, default_ai_usage: 'chat' };
jest.mock('../contexts/UserContext', () => ({
  useUser: () => ({ user: mockUser }),
}));
const mockGet = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: { get: (...args) => mockGet(...args) },
}));

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import SearchModal, { SEARCH_NEEDS_AI_TEXT, SEARCH_NEEDS_AI_TITLE } from './SearchModal';

const PROBE_ESCAPED = '&lt;i data-loore-probe=&quot;1&quot;&gt;probe&lt;/i&gt;';

const result = (fields) => ({
  id: 1, node_type: 'user', created_at: '2026-02-01T00:00:00Z',
  child_count: 0, score: 0.9, snippet: null, preview: '', ...fields,
});

async function search(results) {
  mockGet.mockResolvedValue({ data: { results, total: results.length } });
  const view = render(<MemoryRouter><SearchModal onClose={() => {}} /></MemoryRouter>);
  fireEvent.change(screen.getByPlaceholderText('Search your entries...'),
    { target: { value: 'equinox' } });
  await waitFor(() => expect(mockGet).toHaveBeenCalled());
  return view;
}

beforeEach(() => {
  mockGet.mockReset();
  mockUser = { is_admin: false, default_ai_usage: 'chat' };
});

test('a highlighted snippet keeps its <mark>s and shows stored markup as text', async () => {
  const { container } = await search([result({
    snippet: `Notes ${PROBE_ESCAPED} on <mark>equinox</mark> &amp; tides`,
  })]);
  const line = await screen.findByText((_, el) => el.tagName === 'DIV'
    && el.textContent === 'Notes <i data-loore-probe="1">probe</i> on equinox & tides');
  expect(line.querySelector('mark').textContent).toBe('equinox');
  expect(container.querySelector('[data-loore-probe]')).toBeNull();
  expect(container.querySelector('i')).toBeNull();
});

test('a preview (semantic results have no snippet) is shown as text', async () => {
  const { container } = await search([result({ preview: `${PROBE_ESCAPED} &amp; more` })]);
  await screen.findByText('<i data-loore-probe="1">probe</i> & more');
  expect(container.querySelector('[data-loore-probe]')).toBeNull();
  expect(container.querySelector('mark')).toBeNull();
});

test('an unescaped tag from an old or broken server still renders as text', async () => {
  const { container } = await search([result({ preview: '<i data-loore-probe="1">probe</i>' })]);
  await screen.findByText('<i data-loore-probe="1">probe</i>');
  expect(container.querySelector('[data-loore-probe]')).toBeNull();
});

// An account whose Default AI usage is None has no search by meaning
// (#684): the modal says why instead of offering the search box.
describe('an account set to None', () => {
  const pause = () => new Promise((resolve) => { setTimeout(resolve, 400); });

  test('sees why there is no search, and nothing is asked', async () => {
    mockUser = { is_admin: false, default_ai_usage: 'none' };
    const onClose = jest.fn();
    render(<MemoryRouter><SearchModal onClose={onClose} /></MemoryRouter>);
    expect(screen.getByText(SEARCH_NEEDS_AI_TITLE)).toBeTruthy();
    expect(screen.getByText(SEARCH_NEEDS_AI_TEXT)).toBeTruthy();
    expect(screen.queryByPlaceholderText('Search your entries...')).toBeNull();
    await pause();
    expect(mockGet).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText('Account settings'));
    expect(onClose).toHaveBeenCalled();
  });

  test('on the References page too', () => {
    mockUser = { is_admin: false, default_ai_usage: 'none' };
    render(<MemoryRouter><SearchModal onClose={() => {}} scope="external" /></MemoryRouter>);
    expect(screen.getByText(SEARCH_NEEDS_AI_TEXT)).toBeTruthy();
    expect(screen.queryByPlaceholderText('Search your references...')).toBeNull();
  });

  test('an admin keeps keyword search, without the Semantic switch', async () => {
    mockUser = { is_admin: true, default_ai_usage: 'none' };
    await search([]);
    expect(mockGet.mock.calls.map(([path]) => path)).toEqual(['/search']);
    expect(screen.queryByText('Semantic')).toBeNull();
    expect(screen.queryByText('Keyword')).toBeNull();
  });

  test("the server's refusal shows the same explanation", async () => {
    const error = jest.spyOn(console, 'error').mockImplementation(() => {});
    mockGet.mockRejectedValue({ response: { status: 403, data: {
      error: 'x', code: 'ai_usage_none', scope: 'account' } } });
    render(<MemoryRouter><SearchModal onClose={() => {}} /></MemoryRouter>);
    fireEvent.change(screen.getByPlaceholderText('Search your entries...'),
      { target: { value: 'equinox' } });
    await screen.findByText(SEARCH_NEEDS_AI_TEXT);
    expect(screen.queryByPlaceholderText('Search your entries...')).toBeNull();
    expect(error).not.toHaveBeenCalled();
    error.mockRestore();
  });
});

test('control: an account set to Chat searches by meaning', async () => {
  await search([]);
  expect(mockGet.mock.calls.map(([path]) => path)).toEqual(['/search/semantic']);
  expect(screen.queryByText(SEARCH_NEEDS_AI_TEXT)).toBeNull();
});

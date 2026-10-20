// Search results show stored text as text (#445). The backend escapes
// `snippet` and `preview` and adds <mark> around matches; the modal turns
// those runs into React text, so nothing in a result becomes an element
// except the highlights. The marker below is inert.
jest.mock('../contexts/UserContext', () => ({
  useUser: () => ({ user: { is_admin: false } }),
}));
const mockGet = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: { get: (...args) => mockGet(...args) },
}));

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import SearchModal from './SearchModal';

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

beforeEach(() => mockGet.mockReset());

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

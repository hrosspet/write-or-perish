// "Make ready from prepared material" (#698): hidden while bundles are
// switched off on the server; with them on, Check shows the bundle's
// metadata and the account it would go into, and Make ready starts the
// load and shows its result. Protected accounts need the force box.
import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const mockGet = jest.fn();
const mockPost = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    get: (...args) => mockGet(...args),
    post: (...args) => mockPost(...args),
  },
}));

// eslint-disable-next-line import/first
import AdminPrefillBundles, { bundleCheckLabel, bundleLoadLabel } from './AdminPrefillBundles';

const check = {
  x_id: '1001',
  bundle: {
    handle: 'alice', exported_at: '2026-11-13T10:00:00Z', newest_entry_at: '2026-11-03T07:00:00Z',
    counts: { nodes: 8000, profiles: 2, intentions: 1 }, profile_generated_by: 'gpt-6.1-sol',
  },
  account: { exists: false, username: 'alice', username_taken_by_user_id: null },
};

const routes = (overrides = {}) => (url) => {
  const table = {
    '/admin/prefill-bundles/config': { data: { enabled: true } },
    '/admin/prefill-bundles/loads': { data: { loads: [] } },
    '/admin/prefill-bundles/check': { data: check },
    ...overrides,
  };
  return url in table ? Promise.resolve(table[url]) : new Promise(() => {});
};

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
});

test('control: switched off, the section is not shown', async () => {
  mockGet.mockImplementation(routes({ '/admin/prefill-bundles/config': { data: { enabled: false } } }));
  render(<AdminPrefillBundles />);
  await waitFor(() => expect(mockGet).toHaveBeenCalled());
  expect(screen.queryByText('Make ready from prepared material')).toBeNull();
});

test('check, then make ready', async () => {
  mockGet.mockImplementation(routes());
  mockPost.mockResolvedValue({ data: { load: { id: 3, status: 'queued' } } });
  render(<AdminPrefillBundles />);
  expect(await screen.findByText('Make ready from prepared material')).toBeInTheDocument();
  const makeReady = screen.getByRole('button', { name: 'Make ready' });
  expect(makeReady).toBeDisabled();
  fireEvent.change(screen.getByLabelText('Handle or X id'), { target: { value: '@alice' } });
  fireEvent.click(screen.getByRole('button', { name: 'Check' }));
  expect(await screen.findByText(/@alice · X id 1001/)).toHaveTextContent(
    /8,000 entries · 2 profile versions \(gpt-6.1-sol\) · 1 intentions · new account @alice/);
  fireEvent.click(makeReady);
  await waitFor(() => expect(mockPost).toHaveBeenCalledWith(
    '/admin/prefill-bundles/loads', { q: '@alice', force: false }));
  expect(await screen.findByText('Queued…')).toBeInTheDocument();
});

test('an account someone signed in to needs the force box', async () => {
  mockGet.mockImplementation(routes({ '/admin/prefill-bundles/check': { data: {
    ...check,
    account: { exists: true, username: 'alice', sign_in: 'signed-in', own_writing: false },
    refusal: { code: 'signed_in', message: 'Someone has signed in.', forceable: true },
  } } }));
  render(<AdminPrefillBundles />);
  fireEvent.change(await screen.findByLabelText('Handle or X id'), { target: { value: 'alice' } });
  fireEvent.click(screen.getByRole('button', { name: 'Check' }));
  const force = await screen.findByLabelText(/Load anyway/);
  expect(screen.getByRole('button', { name: 'Make ready' })).toBeDisabled();
  fireEvent.click(force);
  expect(screen.getByRole('button', { name: 'Make ready' })).toBeEnabled();
});

test('labels say what a load did, in counts', () => {
  expect(bundleLoadLabel({ status: 'running', stage: 'nodes', nodes_done: 500, nodes_total: 8000 }))
    .toBe('Loading entries 500 / 8,000');
  expect(bundleLoadLabel({
    status: 'done', username: 'alice', created: 8000, skipped: 3, profiles: 2, artifacts: 1,
    relinked: 900, approved: true,
  })).toBe('Done: @alice — 8,000 entries (3 already there), 2 profile versions, 1 artifacts, 900 linked; let in');
  expect(bundleLoadLabel({ status: 'stopped', nodes_done: 1000, nodes_total: 8000 }))
    .toBe("Stopped by the person's No: 1,000 of 8,000 entries loaded (they stay)");
  expect(bundleLoadLabel({ status: 'failed', error: 'checksum' }))
    .toBe("Failed: the bundle's checksum does not match its manifest");
  expect(bundleCheckLabel(null)).toBeNull();
});

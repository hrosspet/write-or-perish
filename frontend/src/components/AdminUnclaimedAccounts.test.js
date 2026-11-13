// The admin's box for pre-filled accounts nobody claimed (#685): what the
// nightly run deletes (or, in the dry run, would delete), why, from which
// date the period counts, and the admin's Keep on one account or many.
const mockGet = jest.fn();
const mockPut = jest.fn();
const mockPost = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    get: (...args) => mockGet(...args),
    put: (...args) => mockPut(...args),
    post: (...args) => mockPost(...args),
  },
}));

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import AdminUnclaimedAccounts, { statusLabel } from './AdminUnclaimedAccounts';
import { purgeLabel } from './AdminPanel';

const report = {
  deletion_enabled: false,
  period_days: 90,
  max_per_run: 10,
  record_since: '2026-08-29T00:00:00Z',
  used_count: 4,
  before_record: { count: 2, ids: [3, 4] },
  accounts: [
    { user_id: 11, username: 'dueone', status: 'due', opened_at: '2026-09-10T12:00:00Z',
      delete_on: '2026-12-09T12:00:00Z', reasons: [], keep_until: null, kept_for_good: false,
      counted_from: 'pre-filled or loaded',
      why: 'no sign of a person and not excluded; 90 days counted from 2026-09-10 (pre-filled or loaded); date 2026-12-09 passed' },
    { user_id: 12, username: 'waitone', status: 'waiting', opened_at: '2026-10-10T12:00:00Z',
      delete_on: '2027-01-08T12:00:00Z', reasons: [], keep_until: null, kept_for_good: false,
      counted_from: 'pre-filled or loaded',
      why: '90 days counted from 2026-10-10 (pre-filled or loaded); date 2027-01-08' },
    { user_id: 13, username: 'adminone', status: 'kept', opened_at: '2026-10-10T12:00:00Z',
      delete_on: null, reasons: ['admin'], keep_until: null, kept_for_good: false,
      counted_from: 'pre-filled or loaded', why: 'admin' },
  ],
};

beforeEach(() => {
  mockGet.mockReset();
  mockPut.mockReset();
  mockPost.mockReset();
  mockGet.mockResolvedValue({ data: report });
  mockPut.mockResolvedValue({ data: {} });
  mockPost.mockResolvedValue({ data: { user_ids: [] } });
});

test('shows the counts and says it is a dry run', async () => {
  render(<AdminUnclaimedAccounts />);
  expect(await screen.findByText(/Dry run: nothing is deleted/)).toBeInTheDocument();
  expect(mockGet).toHaveBeenCalledWith('/admin/unclaimed_accounts');
  expect(screen.getByText(/Due now: 1 · waiting: 1 · excluded: 1/)).toBeInTheDocument();
  expect(screen.getByText(/someone used it: 4/)).toBeInTheDocument();
  expect(screen.getByText(/\(never\s+deleted automatically\): 2/)).toBeInTheDocument();
});

test('lists each account with why it is due and where its period counts from', async () => {
  render(<AdminUnclaimedAccounts />);
  fireEvent.click(await screen.findByText('Show'));
  expect(screen.getByText('@dueone (11)')).toBeInTheDocument();
  expect(screen.getByText('would be deleted tonight (dry run)')).toBeInTheDocument();
  expect(screen.getByText(/^no sign of a person and not excluded; 90 days counted from 2026-09-10/))
    .toBeInTheDocument();
  expect(screen.getAllByText(/\(pre-filled or loaded\)$/).length).toBeGreaterThan(0);
  expect(screen.getByText('never deleted')).toBeInTheDocument();
});

test('keeps one account past its date', async () => {
  const prompt = jest.spyOn(window, 'prompt').mockReturnValue('2027-03-01');
  render(<AdminUnclaimedAccounts />);
  fireEvent.click(await screen.findByText('Show'));
  // Keep is offered on due and waiting accounts, not on excluded ones.
  const keeps = screen.getAllByText('Keep…');
  expect(keeps).toHaveLength(2);
  fireEvent.click(keeps[0]);
  await waitFor(() => expect(mockPut).toHaveBeenCalledWith(
    '/admin/users/11/unclaimed_keep', { until: '2027-03-01' }));
  expect(mockGet).toHaveBeenCalledTimes(2);
  prompt.mockRestore();
});

test('an empty answer clears the Keep; cancel changes nothing', async () => {
  const prompt = jest.spyOn(window, 'prompt').mockReturnValueOnce(null).mockReturnValueOnce('');
  render(<AdminUnclaimedAccounts />);
  fireEvent.click(await screen.findByText('Show'));
  fireEvent.click(screen.getAllByText('Keep…')[1]);
  expect(mockPut).not.toHaveBeenCalled();
  fireEvent.click(screen.getAllByText('Keep…')[1]);
  await waitFor(() => expect(mockPut).toHaveBeenCalledWith(
    '/admin/users/12/unclaimed_keep', { until: null }));
  prompt.mockRestore();
});

test('keeps every upcoming account, or a selection, with one answer', async () => {
  const prompt = jest.spyOn(window, 'prompt').mockReturnValue('2027-02-01');
  render(<AdminUnclaimedAccounts />);
  fireEvent.click(await screen.findByText('Show'));
  expect(screen.getByText('Keep selected (0)…')).toBeDisabled();
  fireEvent.click(screen.getByLabelText(/Select all upcoming \(2\)/));
  fireEvent.click(screen.getByText('Keep selected (2)…'));
  await waitFor(() => expect(mockPost).toHaveBeenCalledWith(
    '/admin/unclaimed_accounts/keep', { user_ids: [11, 12], until: '2027-02-01' }));

  // One account picked by hand.
  await waitFor(() => expect(screen.getByText('Keep selected (0)…')).toBeInTheDocument());
  fireEvent.click(screen.getByLabelText('select @waitone'));
  fireEvent.click(screen.getByText('Keep selected (1)…'));
  await waitFor(() => expect(mockPost).toHaveBeenLastCalledWith(
    '/admin/unclaimed_accounts/keep', { user_ids: [12], until: '2027-02-01' }));
  // Excluded accounts cannot be selected.
  expect(screen.queryByLabelText('select @adminone')).toBeNull();
  prompt.mockRestore();
});

test('the status says what a real run does and when the nightly limit holds one back', () => {
  const due = report.accounts[0];
  expect(statusLabel(due, { dryRun: false, tonight: true })).toBe('deleted tonight');
  expect(statusLabel(due, { dryRun: true, tonight: false })).toMatch(/a later night/);
  expect(statusLabel(report.accounts[1], { dryRun: true, tonight: false })).toBe('waiting');
});

test('the Users row names an unclaimed deletion as such, not as the user\'s', () => {
  const job = { delete_account: true, source: 'unclaimed', status: 'scheduled' };
  expect(purgeLabel(job)).toMatch(/nobody signed in/);
  expect(purgeLabel(job)).not.toMatch(/the user/);
  expect(purgeLabel({ ...job, status: 'failed', error: 'X' })).toMatch(/failed: X/);
});

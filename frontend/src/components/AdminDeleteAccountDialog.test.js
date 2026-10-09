// Admin "Delete account" (#269): the dry run and the blast radius come
// first, the deletion needs the username typed, a refusal (AI or system
// account, last admin) is shown instead of a delete button.
const mockPost = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: { post: (...args) => mockPost(...args) },
}));

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import AdminDeleteAccountDialog from './AdminDeleteAccountDialog';
import { purgeLabel } from './AdminPanel';

const user = { id: 7, username: 'alice' };

beforeEach(() => mockPost.mockReset());

test('shows the blast radius and counts, then deletes only after the username is typed', async () => {
  mockPost.mockImplementation((url) => {
    if (url.includes('dry_run')) {
      return Promise.resolve({ data: {
        counts: { node: 12, api_cost_log: 4 },
        identity: { user: 1, node_reattributed: 3, api_token: 0 },
        blast_radius: { public_nodes: 2, replies_to_others: 1 },
        job: null,
      } });
    }
    return Promise.resolve({ data: { job: { id: 3, status: 'running', delete_account: true } } });
  });
  const onStarted = jest.fn();
  render(<AdminDeleteAccountDialog user={user} onClose={() => {}} onStarted={onStarted} />);

  expect(mockPost).toHaveBeenCalledWith('/admin/users/7/delete_account?dry_run=1');
  await waitFor(() => expect(screen.getByText(/2 public\s+entries and\s+1\s+reply/)).toBeTruthy());
  expect(screen.getByText('Entries and AI replies deleted')).toBeTruthy();
  expect(screen.getByText('Placeholders moved to loore-erased')).toBeTruthy();
  expect(screen.queryByText('API tokens')).toBeNull();   // zero counts are left out
  expect(screen.queryByRole('checkbox')).toBeNull();
  expect(screen.getByRole('button', { name: /keep the account/i })).toBeTruthy();

  const del = screen.getByRole('button', { name: /delete account now/i });
  expect(del.disabled).toBe(true);
  fireEvent.change(screen.getByLabelText(/type the username/i), { target: { value: 'Alice' } });
  expect(del.disabled).toBe(true);
  fireEvent.change(screen.getByLabelText(/type the username/i), { target: { value: 'alice' } });
  fireEvent.click(del);
  await waitFor(() => expect(onStarted).toHaveBeenCalled());
  expect(mockPost).toHaveBeenLastCalledWith('/admin/users/7/delete_account', { confirm_username: 'alice' });
  expect(screen.getByText(/Deletion started \(job 3/)).toBeTruthy();
});

test('a refusal shows the reason and no delete button', async () => {
  mockPost.mockRejectedValue({ response: { data: {
    error: 'This is the last admin account. Make another account an admin first.' } } });
  render(<AdminDeleteAccountDialog user={{ id: 1, username: 'admin' }} onClose={() => {}} />);
  await waitFor(() => expect(screen.getByText(/last admin account/)).toBeTruthy());
  expect(screen.queryByRole('button', { name: /delete account now/i })).toBeNull();
});

test('the Users row tells an account deletion from a data purge', () => {
  expect(purgeLabel({ status: 'scheduled', delete_account: true, scheduled_for: '2026-11-08T12:00:00Z' }))
    .toMatch(/^Account deleted by the user, hidden until/);
  expect(purgeLabel({ status: 'running', delete_account: true })).toBe('Deleting account…');
  expect(purgeLabel({ status: 'running', delete_account: false })).toBe('Purging data…');
});

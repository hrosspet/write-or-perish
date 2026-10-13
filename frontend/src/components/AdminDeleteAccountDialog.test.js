// Admin "Delete account" (#269): the dry run and the blast radius come
// first, the deletion needs the username typed, a refusal (AI or system
// account, last admin) is shown instead of a delete button. It works like
// the user's own account deletion (Peter, 2026-10-13): restorable by
// signing in, undoable in the Users table.
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
        grace_days: 30,
      } });
    }
    return Promise.resolve({ data: { job: {
      id: 3, status: 'scheduled', delete_account: true, source: 'admin',
      scheduled_for: '2026-11-12T12:00:00Z', undoable: true,
    } } });
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

  expect(screen.getByText(/Signing in within 30 days offers them the\s+restore, and you can undo it in the Users table/)).toBeTruthy();
  expect(screen.queryByText(/cannot be undone/)).toBeNull();

  const del = screen.getByRole('button', { name: /^delete account$/i });
  expect(del.disabled).toBe(true);
  fireEvent.change(screen.getByLabelText(/type the username/i), { target: { value: 'Alice' } });
  expect(del.disabled).toBe(true);
  fireEvent.change(screen.getByLabelText(/type the username/i), { target: { value: 'alice' } });
  fireEvent.click(del);
  await waitFor(() => expect(onStarted).toHaveBeenCalled());
  expect(mockPost).toHaveBeenLastCalledWith('/admin/users/7/delete_account', { confirm_username: 'alice' });
  expect(screen.getByText(/The account @alice is deleted and signed out now \(job 3\)/)).toBeTruthy();
  expect(screen.getByText(/they can restore it by signing in, and you can undo it in the Users table/)).toBeTruthy();
});

test('an account deletion already waiting is shown instead of a delete button', async () => {
  mockPost.mockResolvedValue({ data: {
    counts: { node: 2 }, identity: { user: 1 }, blast_radius: {}, grace_days: 30,
    job: { id: 9, status: 'scheduled', delete_account: true, source: 'self',
           scheduled_for: '2026-11-12T12:00:00Z' },
  } });
  render(<AdminDeleteAccountDialog user={user} onClose={() => {}} />);
  await waitFor(() => expect(screen.getByText(/already deleted by the user\. It is deleted forever on/)).toBeTruthy());
  expect(screen.queryByRole('button', { name: /^delete account$/i })).toBeNull();
  expect(screen.queryByLabelText(/type the username/i)).toBeNull();
});

test('a waiting writing deletion is taken over, and the dialog says so', async () => {
  mockPost.mockResolvedValue({ data: {
    counts: { node: 2 }, identity: { user: 1 }, blast_radius: {}, grace_days: 30,
    job: { id: 9, status: 'scheduled', delete_account: false, source: 'self',
           scope: 'hidden', scheduled_for: '2026-11-12T12:00:00Z' },
  } });
  render(<AdminDeleteAccountDialog user={user} onClose={() => {}} />);
  await waitFor(() => expect(screen.getByText(/The user already deleted\s+all their writing/)).toBeTruthy());
  expect(screen.getByText(/puts that writing deletion back as it was/)).toBeTruthy();
  expect(screen.getByRole('button', { name: /^delete account$/i })).toBeTruthy();
});

test('a refusal shows the reason and no delete button', async () => {
  mockPost.mockRejectedValue({ response: { data: {
    error: 'This is the last admin account. Make another account an admin first.' } } });
  render(<AdminDeleteAccountDialog user={{ id: 1, username: 'admin' }} onClose={() => {}} />);
  await waitFor(() => expect(screen.getByText(/last admin account/)).toBeTruthy());
  expect(screen.queryByRole('button', { name: /^delete account$/i })).toBeNull();
});

test('the Users row tells an account deletion from a data purge, and who asked', () => {
  expect(purgeLabel({ status: 'scheduled', delete_account: true, scheduled_for: '2026-11-08T12:00:00Z' }))
    .toMatch(/^Account deleted by the user, hidden until/);
  expect(purgeLabel({ status: 'scheduled', delete_account: true, source: 'admin', scheduled_for: '2026-11-08T12:00:00Z' }))
    .toMatch(/^Account deleted by an admin, hidden until/);
  expect(purgeLabel({ status: 'scheduled', scope: 'hidden', source: 'self', scheduled_for: '2026-11-08T12:00:00Z' }))
    .toMatch(/^Writing hidden by the user, deleted on/);
  expect(purgeLabel({ status: 'scheduled', scope: 'hidden', source: 'admin', scheduled_for: '2026-11-08T12:00:00Z' }))
    .toMatch(/^Writing hidden by an admin, deleted on/);
  expect(purgeLabel({ status: 'running', delete_account: true })).toBe('Deleting account…');
  expect(purgeLabel({ status: 'running', delete_account: false })).toBe('Purging data…');
});

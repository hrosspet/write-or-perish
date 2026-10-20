// Admin "Purge data" (#268): the dry run is shown before anything
// happens, the purge needs the username typed, and a refusal (AI or
// system account) is shown instead of a purge button. It works like the
// user's own "Delete all my writing" (Peter, 2026-10-13): the dialog says
// so, and a request already waiting is shown instead of a purge button.
const mockPost = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: { post: (...args) => mockPost(...args) },
}));

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import PurgeDataDialog, { purgeCountRows, waitingDeletionNote } from './PurgeDataDialog';

const user = { id: 7, username: 'alice' };

beforeEach(() => mockPost.mockReset());

const started = {
  id: 3, status: 'scheduled', scope: 'hidden', source: 'admin',
  scheduled_for: '2026-11-12T12:00:00Z', undoable: true,
};

test('shows the dry-run counts, then purges only after the username is typed', async () => {
  mockPost.mockImplementation((url) => {
    if (url.includes('dry_run')) {
      return Promise.resolve({ data: { counts: {
        node: 12, node_tombstoned: 1, api_cost_log: 4, files: 3, draft: 0,
      }, job: null, grace_days: 30 } });
    }
    return Promise.resolve({ data: { job: started } });
  });
  const onStarted = jest.fn();
  render(<PurgeDataDialog user={user} onClose={() => {}} onStarted={onStarted} />);

  expect(mockPost).toHaveBeenCalledWith('/admin/users/7/purge_data?dry_run=1');
  await waitFor(() => expect(screen.getByText('Entries and AI replies deleted')).toBeTruthy());
  expect(screen.getByText('12')).toBeTruthy();
  expect(screen.getByText(/Cost rows \(kept/)).toBeTruthy();
  expect(screen.queryByText('Drafts')).toBeNull();   // zero counts are left out

  // Like the user's own request: restorable, undoable, then forever.
  expect(screen.getByText(/can restore it themselves within 30 days, and you\s+can undo it in the Users table/)).toBeTruthy();
  expect(screen.queryByText(/cannot be undone/)).toBeNull();

  const purge = screen.getByRole('button', { name: /^purge data$/i });
  expect(purge.disabled).toBe(true);
  fireEvent.change(screen.getByLabelText(/type the username/i), { target: { value: 'alic' } });
  expect(purge.disabled).toBe(true);
  fireEvent.change(screen.getByLabelText(/type the username/i), { target: { value: 'alice' } });
  fireEvent.click(purge);

  await waitFor(() => expect(onStarted).toHaveBeenCalledWith(started));
  expect(mockPost).toHaveBeenLastCalledWith('/admin/users/7/purge_data', { confirm_username: 'alice' });
  expect(screen.getByText(/The writing of @alice is hidden now \(job 3\)\. It is\s+deleted forever on/)).toBeTruthy();
  expect(screen.getByText(/you can undo it in the Users table\.$/)).toBeTruthy();
});

test('a request already waiting is shown instead of a purge button', async () => {
  mockPost.mockResolvedValue({ data: { counts: { node: 2 }, grace_days: 30, job: {
    id: 9, status: 'scheduled', scope: 'hidden', source: 'self',
    scheduled_for: '2026-11-12T12:00:00Z',
  } } });
  render(<PurgeDataDialog user={user} onClose={() => {}} />);
  await waitFor(() => expect(screen.getByText(/The user already deleted all their writing\. It is deleted forever on/)).toBeTruthy());
  expect(screen.getByText(/only they can restore it/)).toBeTruthy();
  expect(screen.queryByRole('button', { name: /^purge data$/i })).toBeNull();
  expect(screen.queryByLabelText(/type the username/i)).toBeNull();
  expect(screen.getByRole('button', { name: /close/i })).toBeTruthy();
});

test('the note says who asked and whether the admin can undo it', () => {
  const at = '2026-11-12T12:00:00Z';
  expect(waitingDeletionNote(null)).toBeNull();
  expect(waitingDeletionNote({ status: 'done' })).toBeNull();
  expect(waitingDeletionNote({ status: 'scheduled', source: 'admin', scheduled_for: at }))
    .toMatch(/^An admin already purged this data\..*undo it in the Users table\.$/);
  expect(waitingDeletionNote({ status: 'scheduled', source: 'admin', delete_account: true, scheduled_for: at }))
    .toMatch(/^This account was already deleted by an admin\..*undo it in the Users table/);
  expect(waitingDeletionNote({ status: 'scheduled', source: 'self', delete_account: true, scheduled_for: at }))
    .not.toMatch(/undo/);
  expect(waitingDeletionNote({ status: 'running' })).toMatch(/running now/);
});

test('a refused account shows the refusal and no purge button', async () => {
  mockPost.mockRejectedValue({ response: { data: {
    error: 'Refused (AI account): AI and system accounts are never purged.',
  } } });
  render(<PurgeDataDialog user={{ id: 2, username: 'claude' }} onClose={() => {}} />);
  await waitFor(() => expect(screen.getByText(/AI and system accounts are never purged/)).toBeTruthy());
  expect(screen.queryByRole('button', { name: /^purge data$/i })).toBeNull();
  expect(screen.getByRole('button', { name: /close/i })).toBeTruthy();
});

test('unknown count keys are listed under their own name', () => {
  expect(purgeCountRows({ node: 1, new_table: 2, zero: 0 })).toEqual([
    ['node', 'Entries and AI replies deleted', 1],
    ['new_table', 'new_table', 2],
  ]);
});

// Admin "Purge data" (#268): the dry run is shown before anything
// happens, the purge needs the username typed, and a refusal (AI or
// system account) is shown instead of a purge button.
const mockPost = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: { post: (...args) => mockPost(...args) },
}));

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import PurgeDataDialog, { purgeCountRows } from './PurgeDataDialog';

const user = { id: 7, username: 'alice' };

beforeEach(() => mockPost.mockReset());

test('shows the dry-run counts, then purges only after the username is typed', async () => {
  mockPost.mockImplementation((url) => {
    if (url.includes('dry_run')) {
      return Promise.resolve({ data: { counts: {
        node: 12, node_tombstoned: 1, api_cost_log: 4, files: 3, draft: 0,
      }, job: null } });
    }
    return Promise.resolve({ data: { job: { id: 3, status: 'running' } } });
  });
  const onStarted = jest.fn();
  render(<PurgeDataDialog user={user} onClose={() => {}} onStarted={onStarted} />);

  expect(mockPost).toHaveBeenCalledWith('/admin/users/7/purge_data?dry_run=1');
  await waitFor(() => expect(screen.getByText('Entries and AI replies deleted')).toBeTruthy());
  expect(screen.getByText('12')).toBeTruthy();
  expect(screen.getByText(/Cost rows \(kept/)).toBeTruthy();
  expect(screen.queryByText('Drafts')).toBeNull();   // zero counts are left out

  const purge = screen.getByRole('button', { name: /purge now/i });
  expect(purge.disabled).toBe(true);
  fireEvent.change(screen.getByLabelText(/type the username/i), { target: { value: 'alic' } });
  expect(purge.disabled).toBe(true);
  fireEvent.change(screen.getByLabelText(/type the username/i), { target: { value: 'alice' } });
  fireEvent.click(purge);

  await waitFor(() => expect(onStarted).toHaveBeenCalledWith({ id: 3, status: 'running' }));
  expect(mockPost).toHaveBeenLastCalledWith('/admin/users/7/purge_data', { confirm_username: 'alice' });
  expect(screen.getByText(/Purge started \(job 3/)).toBeTruthy();
});

test('a refused account shows the refusal and no purge button', async () => {
  mockPost.mockRejectedValue({ response: { data: {
    error: 'Refused (AI account): AI and system accounts are never purged.',
  } } });
  render(<PurgeDataDialog user={{ id: 2, username: 'claude' }} onClose={() => {}} />);
  await waitFor(() => expect(screen.getByText(/AI and system accounts are never purged/)).toBeTruthy());
  expect(screen.queryByRole('button', { name: /purge now/i })).toBeNull();
  expect(screen.getByRole('button', { name: /close/i })).toBeTruthy();
});

test('unknown count keys are listed under their own name', () => {
  expect(purgeCountRows({ node: 1, new_table: 2, zero: 0 })).toEqual([
    ['node', 'Entries and AI replies deleted', 1],
    ['new_table', 'new_table', 2],
  ]);
});

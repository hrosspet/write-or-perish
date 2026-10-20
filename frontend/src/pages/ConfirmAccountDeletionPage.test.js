// The page behind the emailed deletion link (#269): it asks before
// sending, with both choices; signed out, it asks to sign in first.
let mockUserCtx;
jest.mock('../contexts/UserContext', () => ({
  useUser: () => mockUserCtx,
}));
const mockPost = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: { post: (...args) => mockPost(...args) },
}));

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import ConfirmAccountDeletionPage from './ConfirmAccountDeletionPage';

let assign;
beforeEach(() => {
  mockPost.mockReset();
  assign = jest.fn();
  delete window.location;
  window.location = { assign };
});

const renderAt = (user, url = '/confirm-account-deletion?token=tok') => {
  mockUserCtx = { user, loading: false };
  return render(<MemoryRouter initialEntries={[url]}><ConfirmAccountDeletionPage /></MemoryRouter>);
};

const alice = { username: 'alice', account_deletion: { grace_days: 30 } };

test('opening the link sends nothing; the delete button does', async () => {
  mockPost.mockResolvedValue({ data: { status: 'scheduled', delete_on: '2026-11-08T12:00:00Z' } });
  renderAt(alice);
  expect(screen.getByText('Delete @alice?')).toBeTruthy();
  // What will happen once confirmed, not what has happened.
  expect(screen.getByText(/When you confirm, your account is deleted/)).toBeTruthy();
  expect(screen.getByText(/after\s+that it is deleted forever/)).toBeTruthy();
  expect(screen.queryByText(/hidden/)).toBeNull();
  expect(screen.queryByText(/the writing you deleted/)).toBeNull();
  expect(mockPost).not.toHaveBeenCalled();
  expect(screen.getByText(/keep my account/i)).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: /delete my account/i }));
  await waitFor(() => expect(mockPost).toHaveBeenCalledWith('/account/delete/confirm', { token: 'tok' }));
  expect(assign).toHaveBeenCalledWith('/account-deleted?on=2026-11-08T12%3A00%3A00Z');
});

test('with a writing deletion waiting, says the writing comes back on a restore', () => {
  renderAt({ ...alice, data_deletion: {
    status: 'scheduled', purge_at: '2026-11-01T12:00:00Z', restorable: true,
  } });
  expect(screen.getByText(/When you confirm/).textContent)
    .toMatch(/If you restore your account, the writing you deleted comes\s+back too\./);
});

test('a link for another account says so', async () => {
  mockPost.mockRejectedValue({ response: { data: {
    error: 'This link was sent for a different Loore account.', reason: 'other_account' } } });
  renderAt(alice);
  fireEvent.click(screen.getByRole('button', { name: /delete my account/i }));
  await waitFor(() => expect(screen.getByText(/different Loore account/)).toBeTruthy());
  expect(screen.getByText(/Sign out and use the other account/)).toBeTruthy();
  expect(assign).not.toHaveBeenCalled();
});

test('signed out: sign in first, then come back', () => {
  renderAt(null);
  expect(screen.getByText('Sign in to confirm')).toBeTruthy();
  expect(screen.queryByRole('button', { name: /delete my account/i })).toBeNull();
});

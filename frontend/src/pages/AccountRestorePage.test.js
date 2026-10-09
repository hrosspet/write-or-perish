// The restore question after signing in to a deleted account (#269):
// both choices, a restore signs in, keeping it deleted stays signed out,
// and nothing to restore once the deletion has started.
const mockGet = jest.fn();
const mockPost = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    get: (...args) => mockGet(...args),
    post: (...args) => mockPost(...args),
  },
}));

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import AccountRestorePage from './AccountRestorePage';

let assign;
beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
  assign = jest.fn();
  delete window.location;
  window.location = { assign };
});

const renderPage = () => render(<MemoryRouter><AccountRestorePage /></MemoryRouter>);

test('shows both choices; restoring signs in and opens the app', async () => {
  mockGet.mockResolvedValue({ data: { username: 'alice', delete_on: '2026-11-08T12:00:00Z', restorable: true } });
  mockPost.mockResolvedValue({ data: { status: 'restored', next: '/' } });
  renderPage();
  await waitFor(() => expect(screen.getByText(/You deleted @alice/)).toBeTruthy());
  const body = screen.getByText(/You deleted @alice/).textContent;
  expect(body).toMatch(/You can restore it until\s+.*2026; after that it is deleted forever/);
  expect(body).not.toMatch(/hidden/);
  expect(screen.getByText(/also cancels a request to delete\s+all your writing, if one is waiting/)).toBeTruthy();
  expect(screen.getByRole('button', { name: /keep it deleted/i })).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: /restore my account/i }));
  await waitFor(() => expect(mockPost).toHaveBeenCalledWith('/account/restore'));
  expect(assign).toHaveBeenCalledWith('/');
});

test('keeping it deleted forgets the question', async () => {
  mockGet.mockResolvedValue({ data: { username: 'alice', delete_on: '2026-11-08T12:00:00Z', restorable: true } });
  mockPost.mockResolvedValue({ data: { status: 'declined' } });
  renderPage();
  await waitFor(() => screen.getByRole('button', { name: /keep it deleted/i }));
  fireEvent.click(screen.getByRole('button', { name: /keep it deleted/i }));
  await waitFor(() => expect(screen.getByText(/Your account stays deleted/)).toBeTruthy());
  expect(mockPost).toHaveBeenCalledWith('/account/restore/decline');
  expect(assign).not.toHaveBeenCalled();
});

test('no restore button once the deletion has started', async () => {
  mockGet.mockResolvedValue({ data: { username: 'alice', delete_on: null, restorable: false } });
  renderPage();
  await waitFor(() => expect(screen.getByText(/has started and cannot be undone/)).toBeTruthy());
  expect(screen.queryByRole('button', { name: /restore my account/i })).toBeNull();
});

test('without a fresh sign-in there is nothing to restore', async () => {
  mockGet.mockRejectedValue({ response: { status: 404 } });
  renderPage();
  await waitFor(() => expect(screen.getByText(/Nothing to restore here/)).toBeTruthy());
});

// The status notice shown while "Delete all my writing" (#268) waits or
// runs: the writing is hidden at once, and the notice offers the restore.
let mockUserCtx;
jest.mock('../contexts/UserContext', () => ({
  useUser: () => mockUserCtx,
}));
const mockPost = jest.fn();
const mockGet = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    post: (...args) => mockPost(...args),
    get: (...args) => mockGet(...args),
  },
}));

const mockReload = jest.fn();
jest.mock('../utils/dataDeletion', () => ({
  ...jest.requireActual('../utils/dataDeletion'),
  reloadPage: () => mockReload(),
}));

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import DataDeletionBanner from './DataDeletionBanner';

const renderAt = (path, dataDeletion) => {
  mockUserCtx = {
    user: { username: 'alice', data_deletion: dataDeletion },
    setUser: jest.fn(),
  };
  return render(
    <MemoryRouter initialEntries={[path]}><DataDeletionBanner /></MemoryRouter>
  );
};

const scheduled = {
  status: 'scheduled', purge_at: '2026-11-05T12:00:00Z', restorable: true,
};

beforeEach(() => {
  mockPost.mockReset();
  mockGet.mockReset();
  mockGet.mockResolvedValue({ data: {} });
});

test('a waiting deletion is a status with the restore action, not a dismissible toast', () => {
  renderAt('/', scheduled);
  const status = screen.getByRole('status', { name: 'Writing deleted' });
  expect(status.textContent).toMatch(/Writing deleted/);
  expect(status.textContent).toMatch(
    /You can restore your writing safely until .+\. After that it is deleted forever\./);
  expect(screen.getByRole('button', { name: 'Restore my writing' })).toBeTruthy();
  expect(screen.queryByRole('button', { name: /dismiss/i })).toBeNull();
  expect(screen.queryByText(/cancel/i)).toBeNull();
});

test('Restore my writing calls the restore route', async () => {
  mockPost.mockResolvedValue({ data: { status: null } });
  renderAt('/log', scheduled);
  fireEvent.click(screen.getByRole('button', { name: 'Restore my writing' }));
  await waitFor(() => expect(mockPost).toHaveBeenCalledWith('/account/data/restore'));
  await waitFor(() => expect(mockUserCtx.setUser).toHaveBeenCalled());
  // The page is loaded again, so what came back shows.
  await waitFor(() => expect(mockReload).toHaveBeenCalled());
});

test('a failed restore says so and keeps the button', async () => {
  mockPost.mockRejectedValue({ response: { data: { error: 'Your writing is being deleted now and can no longer be restored.' } } });
  renderAt('/', scheduled);
  fireEvent.click(screen.getByRole('button', { name: 'Restore my writing' }));
  await waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(/can no longer be restored/));
  expect(screen.getByRole('button', { name: 'Restore my writing' })).toBeTruthy();
});

test('while the purge runs there is no restore', () => {
  renderAt('/', { status: 'running', purge_at: '2026-11-05T12:00:00Z' });
  expect(screen.getByRole('status').textContent).toMatch(/being deleted forever now/);
  expect(screen.queryByRole('button')).toBeNull();
});

test('nothing is shown without a pending deletion, or on the Account page', () => {
  const { container } = renderAt('/', { status: null });
  expect(container.textContent).toBe('');
  const again = renderAt('/account', scheduled);
  expect(again.container.textContent).toBe('');
});

// The reminder shown while "Delete all my writing" (#268) waits.
let mockUserCtx;
jest.mock('../contexts/UserContext', () => ({
  useUser: () => mockUserCtx,
}));

import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import DataDeletionBanner from './DataDeletionBanner';

const renderAt = (path, dataDeletion) => {
  mockUserCtx = { user: { username: 'alice', data_deletion: dataDeletion } };
  return render(
    <MemoryRouter initialEntries={[path]}><DataDeletionBanner /></MemoryRouter>
  );
};

const scheduled = { status: 'scheduled', purge_at: '2026-11-05T12:00:00Z' };

beforeEach(() => window.sessionStorage.clear());

test('a scheduled deletion is shown with a link to cancel it', () => {
  renderAt('/', scheduled);
  expect(screen.getByText(/including anything you write before then/)).toBeTruthy();
  const link = screen.getByRole('link', { name: /cancel on the account page/i });
  expect(link.getAttribute('href')).toBe('/account#delete-data');
});

test('nothing is shown without a pending deletion, or on the Account page', () => {
  const { container } = renderAt('/', { status: null });
  expect(container.textContent).toBe('');
  const again = renderAt('/account', scheduled);
  expect(again.container.textContent).toBe('');
});

test('dismissed for the rest of the session', () => {
  renderAt('/', scheduled);
  fireEvent.click(screen.getByRole('button', { name: /dismiss/i }));
  expect(screen.queryByRole('status')).toBeNull();
  const again = renderAt('/', scheduled);
  expect(again.container.textContent).toBe('');
});

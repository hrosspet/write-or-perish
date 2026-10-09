// The page after a deletion is requested (#269): during the 30 days the
// account is scheduled for deletion, not deleted, and can be restored.
import React from 'react';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import AccountDeletedPage from './AccountDeletedPage';

const renderAt = (url) => render(
  <MemoryRouter initialEntries={[url]}><AccountDeletedPage /></MemoryRouter>);

test('says the account is scheduled for deletion on the date and can be restored until then', () => {
  renderAt('/account-deleted?on=2026-11-08T12%3A00%3A00Z');
  expect(screen.getByRole('heading').textContent).toBe('Your account is scheduled for deletion');
  expect(screen.queryByText(/Your account is deleted/)).toBeNull();
  const status = screen.getByRole('status').textContent;
  expect(status).toMatch(/On .*2026.* it is deleted with everything in it/);
  expect(status).toMatch(/Until then you\s+can restore it by signing in/);
});

test('without a date it still says when and that it can be restored', () => {
  renderAt('/account-deleted');
  const status = screen.getByRole('status').textContent;
  expect(status).toMatch(/30 days after your request/);
  expect(status).toMatch(/restore it by signing in/);
});

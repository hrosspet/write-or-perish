// The page after a deletion is requested (#269): the account is deleted,
// can be restored for 30 days, and is then deleted forever (Peter,
// 2026-10-09: not "hidden").
import React from 'react';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import AccountDeletedPage from './AccountDeletedPage';

const renderAt = (url) => render(
  <MemoryRouter initialEntries={[url]}><AccountDeletedPage /></MemoryRouter>);

test('says the account is deleted, can be restored until the date, then is deleted forever', () => {
  renderAt('/account-deleted?on=2026-11-08T12%3A00%3A00Z');
  expect(screen.getByRole('heading').textContent).toBe('Your account is deleted');
  const status = screen.getByRole('status').textContent;
  expect(status).toMatch(/If you change your mind, you can still restore it by signing in\s+until .*2026/);
  expect(status).toMatch(/deleted forever, with everything in it/);
  expect(status).not.toMatch(/hidden/);
});

test('without a date it still says for how long it can be restored', () => {
  renderAt('/account-deleted');
  const status = screen.getByRole('status').textContent;
  expect(status).toMatch(/you can still restore it by signing in\s+within 30 days/);
  expect(status).toMatch(/deleted forever/);
});

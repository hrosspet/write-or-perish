// Payments on (#697): nobody waits for approval, so the only account that
// reaches this page is one the admin blocked. It gets the plain fact and
// the address, signing out and deleting the account; no waitlist, no
// email form, nothing about subscribing.
let mockUserCtx;
jest.mock('../contexts/UserContext', () => ({
  useUser: () => mockUserCtx,
}));
jest.mock('../api', () => ({
  __esModule: true,
  default: { post: jest.fn() },
}));
jest.mock('../utils/Fade', () => ({ children }) => <div>{children}</div>);
jest.mock('../components/PrefillConsentCard', () => () => <div>consent card</div>);

import React from 'react';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import AlphaThankYouPage, { NOT_ACTIVE_TEXT } from './AlphaThankYouPage';

const renderPage = (user) => {
  mockUserCtx = {
    user: { username: 'someone', approved: false, email: 'a@example.com',
            pending_email: null, pending_email_expired: false,
            account_deletion: { grace_days: 30 }, ...user },
    setUser: jest.fn(), loading: false,
  };
  return render(<MemoryRouter><AlphaThankYouPage /></MemoryRouter>);
};

test('a blocked account: not active, the address, sign out, deletion', () => {
  renderPage({ payments_enabled: true, account_blocked: true });
  expect(screen.getByText(NOT_ACTIVE_TEXT)).toBeInTheDocument();
  expect(NOT_ACTIVE_TEXT).toBe('This account is not active. Write to info@loore.org.');
  expect(screen.getByText('Sign out').closest('a').getAttribute('href'))
    .toMatch(/\/auth\/logout$/);
  expect(screen.getByRole('button', { name: 'Delete my account…' })).toBeInTheDocument();
  expect(screen.queryByText(/letting people in/)).not.toBeInTheDocument();
  expect(screen.queryByText('consent card')).not.toBeInTheDocument();
  expect(screen.queryByText(/subscri/i)).not.toBeInTheDocument();
});

test('control: a waiting account still gets the waitlist', () => {
  renderPage({});
  expect(screen.getByText(/letting people in/)).toBeInTheDocument();
  expect(screen.getByText('consent card')).toBeInTheDocument();
  expect(screen.queryByText(/not active/)).not.toBeInTheDocument();
});

// Payments on (#697): nobody waits for approval, so the only account that
// reaches this page is one the admin blocked. It gets the plain fact and
// the address, its data as one file, Stripe's page to cancel a
// subscription that still runs, signing out and deleting the account; no
// waitlist, no email form.
let mockUserCtx;
jest.mock('../contexts/UserContext', () => ({
  useUser: () => mockUserCtx,
}));
const mockPost = jest.fn();
const mockHead = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    post: (...args) => mockPost(...args),
    head: (...args) => mockHead(...args),
    defaults: { baseURL: 'https://api.test/api' },
  },
}));
jest.mock('../utils/Fade', () => ({ children }) => <div>{children}</div>);
jest.mock('../components/PrefillConsentCard', () => () => <div>consent card</div>);

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
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

beforeEach(() => {
  mockPost.mockReset();
  mockHead.mockReset();
});

test('a blocked account: not active, the address, its data, sign out, deletion', () => {
  renderPage({ payments_enabled: true, account_blocked: true });
  expect(screen.getByText(NOT_ACTIVE_TEXT)).toBeInTheDocument();
  expect(NOT_ACTIVE_TEXT).toBe('This account is not active. Write to info@loore.org.');
  expect(screen.getByRole('button', { name: 'Download your data' })).toBeInTheDocument();
  expect(screen.getByText('Sign out').closest('a').getAttribute('href'))
    .toMatch(/\/auth\/logout$/);
  expect(screen.getByRole('button', { name: 'Delete my account…' })).toBeInTheDocument();
  expect(screen.queryByText(/letting people in/)).not.toBeInTheDocument();
  expect(screen.queryByText('consent card')).not.toBeInTheDocument();
  // No subscription running: nothing about it.
  expect(screen.queryByText(/subscri/i)).not.toBeInTheDocument();
});

test('the download goes through the server, after a check', async () => {
  mockHead.mockResolvedValue({});
  const click = jest.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
  renderPage({ payments_enabled: true, account_blocked: true });
  fireEvent.click(screen.getByRole('button', { name: 'Download your data' }));
  await waitFor(() => expect(click).toHaveBeenCalled());
  expect(mockHead).toHaveBeenCalledWith('/export/threads');
  click.mockRestore();
});

test('a subscription that still runs: Stripe\'s page to cancel it', async () => {
  mockPost.mockResolvedValue({ data: { url: 'https://billing.example/s' } });
  const assign = jest.fn();
  delete window.location;
  window.location = { assign };
  renderPage({
    payments_enabled: true, account_blocked: true,
    subscription: { status: 'active', has_customer: true, state: 'active' },
  });
  fireEvent.click(screen.getByRole('button', { name: 'Manage or cancel your subscription' }));
  await waitFor(() => expect(assign).toHaveBeenCalledWith('https://billing.example/s'));
  expect(mockPost).toHaveBeenCalledWith('/billing/portal');
});

test('control: a waiting account still gets the waitlist', () => {
  renderPage({});
  expect(screen.getByText(/letting people in/)).toBeInTheDocument();
  expect(screen.getByText('consent card')).toBeInTheDocument();
  expect(screen.queryByText(/not active/)).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Download your data' })).not.toBeInTheDocument();
});

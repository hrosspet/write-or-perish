// The page the "Confirm your email" mail links to (#260). What matters:
// it posts the token only from inside a session, exactly once, and tells a
// signed-out visitor to sign in instead of doing anything with the link.
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
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import ConfirmEmailPage from './ConfirmEmailPage';

const renderAt = (url) => render(
  <React.StrictMode>
    <MemoryRouter initialEntries={[url]}>
      <ConfirmEmailPage />
    </MemoryRouter>
  </React.StrictMode>
);

beforeEach(() => {
  mockPost.mockReset();
  mockUserCtx = {
    user: { username: 'alice', approved: true, email: 'old@example.com' },
    setUser: jest.fn(),
    loading: false,
  };
});

test('signed out: asks for sign-in with a way back, and posts nothing', () => {
  mockUserCtx = { user: null, setUser: jest.fn(), loading: false };
  renderAt('/confirm-email?token=abc.def');
  const link = screen.getByRole('link', { name: /sign in/i });
  expect(link.getAttribute('href')).toBe(
    '/login?returnUrl=' + encodeURIComponent('/confirm-email?token=abc.def'));
  expect(mockPost).not.toHaveBeenCalled();
});

test('signed in: posts the token once (StrictMode runs effects twice) and updates the user', async () => {
  mockPost.mockResolvedValue({ data: {
    message: 'Email confirmed.', email: 'new@example.com',
    pending_email: null, pending_email_expired: false,
  } });
  renderAt('/confirm-email?token=abc.def');
  await screen.findByText('Email confirmed');
  expect(screen.getByText('new@example.com')).toBeTruthy();
  expect(mockPost).toHaveBeenCalledTimes(1);
  expect(mockPost).toHaveBeenCalledWith('/dashboard/email/confirm', { token: 'abc.def' });
  const update = mockUserCtx.setUser.mock.calls[0][0];
  expect(update({ username: 'alice', email: 'old@example.com', pending_email: 'new@example.com' }))
    .toEqual({ username: 'alice', email: 'new@example.com',
               pending_email: null, pending_email_expired: false });
  expect(screen.getByRole('link', { name: /back to your account/i })
    .getAttribute('href')).toBe('/account#email');
});

test('a waitlisted account continues to the thank-you page, which it can reach', async () => {
  mockUserCtx.user = { username: 'waiting', approved: false, email: null };
  mockPost.mockResolvedValue({ data: { email: 'me@example.com', pending_email: null } });
  renderAt('/confirm-email?token=abc.def');
  const link = await screen.findByRole('link', { name: /continue/i });
  expect(link.getAttribute('href')).toBe('/alpha-thank-you');
});

test('a link requested from another account says whose session this is', async () => {
  mockPost.mockRejectedValue({ response: { status: 403, data: {
    reason: 'other_account',
    error: 'This confirmation link was requested from a different Loore account. Sign in to that account to use it.',
  } } });
  renderAt('/confirm-email?token=abc.def');
  await screen.findByText('Not confirmed');
  expect(screen.getByText(/different Loore account.*signed in as @alice/i)).toBeTruthy();
  expect(mockUserCtx.setUser).not.toHaveBeenCalled();
});

test('no answer from the server offers a retry, which posts again', async () => {
  mockPost.mockRejectedValueOnce(new Error('Network Error'));
  mockPost.mockResolvedValueOnce({ data: { email: 'new@example.com', pending_email: null } });
  renderAt('/confirm-email?token=abc.def');
  const retry = await screen.findByRole('button', { name: /try again/i });
  fireEvent.click(retry);
  await screen.findByText('Email confirmed');
  expect(mockPost).toHaveBeenCalledTimes(2);
});

test('without a token nothing is posted', async () => {
  renderAt('/confirm-email');
  expect(screen.getByText('This link is incomplete')).toBeTruthy();
  await waitFor(() => expect(mockPost).not.toHaveBeenCalled());
});

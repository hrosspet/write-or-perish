// The waitlist page is the only page an unapproved account can reach, so
// the email step (#260) must never dead-end here: a pending address keeps
// a way to get another link or to change the address, expired or not.
let mockUserCtx;
jest.mock('../contexts/UserContext', () => ({
  useUser: () => mockUserCtx,
}));
const mockPost = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: { post: (...args) => mockPost(...args) },
}));
jest.mock('../utils/Fade', () => ({ children }) => <div>{children}</div>);
jest.mock('../components/PrefillConsentCard', () => () => null);

import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import AlphaThankYouPage from './AlphaThankYouPage';

const renderPage = (user) => {
  mockUserCtx = {
    user: { username: 'waiting', approved: false, email: null,
            pending_email: null, pending_email_expired: false, ...user },
    setUser: jest.fn(), loading: false,
  };
  return render(<MemoryRouter><AlphaThankYouPage /></MemoryRouter>);
};

const pendingAnswer = (address) => ({ data: {
  email: null, pending_email: address, pending_email_expired: false,
} });

beforeEach(() => mockPost.mockReset());

test('no address yet: the form posts to the verified flow', async () => {
  mockPost.mockResolvedValue(pendingAnswer('me@example.com'));
  renderPage();
  fireEvent.change(screen.getByPlaceholderText('your@email.com'),
    { target: { value: 'me@example.com' } });
  fireEvent.click(screen.getByRole('button', { name: 'Submit' }));
  await screen.findByRole('button', { name: 'Submit' });
  expect(mockPost).toHaveBeenCalledTimes(1);
  expect(mockPost).toHaveBeenCalledWith('/dashboard/email', { email: 'me@example.com' });
  const update = mockUserCtx.setUser.mock.calls[0][0];
  expect(update({ username: 'waiting' }).pending_email).toBe('me@example.com');
});

test('pending: says where the link went and can send it again', async () => {
  mockPost.mockResolvedValue(pendingAnswer('me@example.com'));
  renderPage({ pending_email: 'me@example.com' });
  expect(screen.getByText(/We sent a confirmation link to/)).toBeTruthy();
  expect(screen.queryByPlaceholderText('your@email.com')).toBeNull();

  fireEvent.click(screen.getByRole('button', { name: 'Send it again' }));

  await screen.findByText(/New link sent/);
  expect(mockPost).toHaveBeenCalledWith('/dashboard/email', { email: 'me@example.com' });
});

test('expired: says so and offers a new link instead of "open it"', () => {
  renderPage({ pending_email: 'me@example.com', pending_email_expired: true });
  expect(screen.getByText(/has expired/)).toBeTruthy();
  expect(screen.queryByText(/Open it to finish/)).toBeNull();
  expect(screen.getByRole('button', { name: 'Send a new link' })).toBeTruthy();
});

test('a mistyped pending address can be replaced, or kept', () => {
  renderPage({ pending_email: 'mistpyed@example.com' });
  fireEvent.click(screen.getByRole('button', { name: 'Use a different address' }));
  const input = screen.getByPlaceholderText('your@email.com');
  expect(input.value).toBe('mistpyed@example.com');

  fireEvent.click(screen.getByRole('button', { name: /Keep mistpyed@example.com/ }));
  expect(screen.queryByPlaceholderText('your@email.com')).toBeNull();
  expect(screen.getByText(/We sent a confirmation link to/)).toBeTruthy();
  expect(mockPost).not.toHaveBeenCalled();
});

test('a confirmed address needs nothing more from this page', () => {
  renderPage({ email: 'me@example.com' });
  expect(screen.queryByPlaceholderText('your@email.com')).toBeNull();
  expect(screen.queryByText(/confirmation link/)).toBeNull();
});

// The email row of the Account page (#260). Each POST /dashboard/email
// mails a link that voids the one before, so one user action must be one
// request — and Cancel must go to the route that cannot touch the bound
// address.
let mockUserCtx;
jest.mock('../contexts/UserContext', () => ({
  useUser: () => mockUserCtx,
}));
const mockPost = jest.fn();
const mockDelete = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    post: (...args) => mockPost(...args),
    delete: (...args) => mockDelete(...args),
    put: jest.fn(),
    get: jest.fn(),
  },
}));
jest.mock('../components/ModelSelector', () => () => null);

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import AccountPage from './AccountPage';

const baseUser = {
  username: 'alice', email: 'alice@example.com', twitter_login: false,
  pending_email: null, pending_email_expired: false, plan: 'alpha',
};

const renderPage = (user) => {
  mockUserCtx = { user: { ...baseUser, ...user }, setUser: jest.fn() };
  return render(<MemoryRouter><AccountPage /></MemoryRouter>);
};

const never = () => new Promise(() => {});

beforeEach(() => {
  mockPost.mockReset();
  mockDelete.mockReset();
});

test('Cmd/Ctrl+Enter sends one link, not one per key handler', () => {
  mockPost.mockReturnValue(never());
  renderPage();
  const input = screen.getByPlaceholderText('alice@example.com');
  fireEvent.change(input, { target: { value: 'new@example.com' } });
  fireEvent.keyDown(input, { key: 'Enter', metaKey: true });
  expect(mockPost).toHaveBeenCalledTimes(1);
  expect(mockPost).toHaveBeenCalledWith('/dashboard/email', { email: 'new@example.com' });
});

test('plain Enter sends one link, and a request in flight blocks the next', () => {
  mockPost.mockReturnValue(never());
  renderPage();
  const input = screen.getByPlaceholderText('alice@example.com');
  fireEvent.change(input, { target: { value: 'new@example.com' } });
  fireEvent.keyDown(input, { key: 'Enter' });
  fireEvent.keyDown(input, { key: 'Enter' });
  fireEvent.click(screen.getByRole('button', { name: /sending|send confirmation link/i }));
  expect(mockPost).toHaveBeenCalledTimes(1);
});

test('a pending change offers Resend and Cancel; Cancel uses the pending-only route', async () => {
  mockDelete.mockResolvedValue({ data: {
    email: 'confirmed-elsewhere@example.com', pending_email: null, pending_email_expired: false,
  } });
  renderPage({ pending_email: 'new@example.com' });
  expect(screen.getByText(/Confirmation link sent to new@example.com/)).toBeTruthy();
  expect(screen.getByRole('button', { name: 'Resend' })).toBeTruthy();

  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

  await waitFor(() => expect(mockUserCtx.setUser).toHaveBeenCalled());
  expect(mockDelete).toHaveBeenCalledTimes(1);
  expect(mockDelete).toHaveBeenCalledWith('/dashboard/email/pending');
  // The page takes the server's word for the bound address as well: the
  // change may have been confirmed on another device meanwhile.
  const update = mockUserCtx.setUser.mock.calls[0][0];
  expect(update({ ...baseUser, pending_email: 'new@example.com' }).email)
    .toBe('confirmed-elsewhere@example.com');
});

test('an expired link says so and offers a new one for the same address', async () => {
  mockPost.mockResolvedValue({ data: {
    email: 'alice@example.com', pending_email: 'new@example.com', pending_email_expired: false,
  } });
  renderPage({ pending_email: 'new@example.com', pending_email_expired: true });
  expect(screen.getByText(/sent to new@example.com has expired/)).toBeTruthy();

  fireEvent.click(screen.getByRole('button', { name: 'Send a new link' }));

  await screen.findByText(/New link sent/);
  expect(mockPost).toHaveBeenCalledWith('/dashboard/email', { email: 'new@example.com' });
});

test('Remove email is only offered to accounts that also sign in with X', () => {
  renderPage();
  expect(screen.queryByRole('button', { name: 'Remove email' })).toBeNull();
});

test('Remove email calls the removal route', async () => {
  mockDelete.mockResolvedValue({ data: { email: null, pending_email: null } });
  renderPage({ twitter_login: true });
  fireEvent.click(screen.getByRole('button', { name: 'Remove email' }));
  await screen.findByText(/Email removed/);
  expect(mockDelete).toHaveBeenCalledWith('/dashboard/email');
});

// Connect X (#311): the row links to the backend's OAuth start and shows
// the outcome the round trip comes back with.
const renderAt = (entry, user) => {
  mockUserCtx = { user: { ...baseUser, ...user }, setUser: jest.fn() };
  return render(<MemoryRouter initialEntries={[entry]}><AccountPage /></MemoryRouter>);
};

test('an account without X offers Connect X, a link to the OAuth start', () => {
  renderPage();
  const link = screen.getByRole('link', { name: 'Connect X' });
  expect(link.getAttribute('href')).toBe('/auth/x/connect');
  expect(screen.queryByRole('button', { name: 'Disconnect X' })).toBeNull();
});

test('a connected account shows the handle and offers Disconnect only with an email', () => {
  renderPage({ twitter_login: true, twitter_handle: 'alice_on_x' });
  expect(screen.getByText('Connected as @alice_on_x')).toBeTruthy();
  expect(screen.queryByRole('link', { name: 'Connect X' })).toBeNull();
  expect(screen.getByRole('button', { name: 'Disconnect X' })).toBeTruthy();
});

test('an X-only account cannot disconnect X', () => {
  renderPage({ email: null, twitter_login: true, twitter_handle: null });
  expect(screen.getByText('Connected')).toBeTruthy();
  expect(screen.queryByRole('button', { name: 'Disconnect X' })).toBeNull();
});

test('the outcome of the round trip is shown', () => {
  Element.prototype.scrollIntoView = jest.fn(); // jsdom lacks it; #x scrolls
  renderAt('/account?x_login=taken#x');
  expect(screen.getByText(/already signs in to another Loore account/)).toBeTruthy();
});

test('an unknown outcome shows nothing', () => {
  renderAt('/account?x_login=bogus');
  expect(screen.queryByText(/X connected|not connected|another Loore account/)).toBeNull();
});

test('Disconnect X calls the removal route', async () => {
  mockDelete.mockResolvedValue({ data: { twitter_login: false, twitter_handle: null } });
  renderPage({ twitter_login: true, twitter_handle: 'alice_on_x' });
  fireEvent.click(screen.getByRole('button', { name: 'Disconnect X' }));
  await screen.findByText(/X disconnected/);
  expect(mockDelete).toHaveBeenCalledWith('/dashboard/x');
});

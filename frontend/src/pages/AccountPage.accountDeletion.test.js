// "Delete my account" on the Account page (#269): a dialog with both
// choices and no checkbox; the typed username is required; an email
// account is told to check its inbox; an account without email goes to
// the "deleted" page; a refusal (last admin) replaces the button.
let mockUserCtx;
jest.mock('../contexts/UserContext', () => ({
  useUser: () => mockUserCtx,
}));
const mockPost = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    post: (...args) => mockPost(...args),
    delete: jest.fn(),
    put: jest.fn(),
    get: jest.fn(),
  },
}));
jest.mock('../components/ModelSelector', () => () => null);

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import AccountPage from './AccountPage';

const info = {
  grace_days: 30, username_reserve_days: 365, confirm_by_email: true,
  link_expires_in: 3600, refusal: null,
};
const baseUser = {
  username: 'alice', email: 'alice@example.com', twitter_login: false,
  pending_email: null, pending_email_expired: false, plan: 'alpha',
  data_deletion: { status: null, grace_days: 30, x_connected: false },
  account_deletion: info,
};

const renderPage = (user) => {
  mockUserCtx = { user: { ...baseUser, ...user }, setUser: jest.fn() };
  return render(<MemoryRouter><AccountPage /></MemoryRouter>);
};

let assign;
beforeEach(() => {
  mockPost.mockReset();
  assign = jest.fn();
  delete window.location;
  window.location = { assign, pathname: '/account', search: '', hash: '' };
});

const openDialog = () => {
  fireEvent.click(screen.getByRole('button', { name: /delete my account…/i }));
  return screen.getByRole('dialog');
};

test('an email account confirms by link: nothing is sent until the username is typed', async () => {
  mockPost.mockResolvedValue({ data: { status: 'confirm_email', expires_in: 3600 } });
  renderPage();
  expect(screen.queryByRole('dialog')).toBeNull();
  openDialog();
  const dialog = screen.getByRole('dialog').textContent;
  expect(dialog).not.toMatch(/hidden/);
  expect(screen.getByText(/deleted at once and you are signed out everywhere/)).toBeTruthy();
  expect(dialog).toMatch(/For 30 days, until .*2026, you can restore it by signing in/);
  expect(dialog).toMatch(/After that it is deleted forever, with everything in it/);
  expect(screen.getByText(/Once it is deleted forever, nobody else can take your username\s+for 365 days/)).toBeTruthy();
  expect(screen.getByText(/imports, poll answers, settings\s+and sign-in/)).toBeTruthy();
  expect(screen.queryByRole('checkbox')).toBeNull();
  // Both choices are buttons.
  expect(screen.getByRole('button', { name: /keep my account/i })).toBeTruthy();
  const confirm = screen.getByRole('button', { name: /email me the confirmation link/i });
  expect(confirm.disabled).toBe(true);
  fireEvent.change(screen.getByLabelText(/type your username/i), { target: { value: 'bob' } });
  expect(confirm.disabled).toBe(true);
  fireEvent.change(screen.getByLabelText(/type your username/i), { target: { value: 'Alice' } });
  fireEvent.click(confirm);

  await waitFor(() => expect(mockPost).toHaveBeenCalledWith('/account/delete', { confirm: 'Alice' }));
  await waitFor(() => expect(screen.getByText(/Check your email/)).toBeTruthy());
  expect(screen.getByText(/works for\s+60 minutes/)).toBeTruthy();
  expect(assign).not.toHaveBeenCalled();
});

test('an account without email is scheduled at once and leaves the app', async () => {
  mockPost.mockResolvedValue({ data: { status: 'scheduled', delete_on: '2026-11-08T12:00:00Z' } });
  renderPage({ email: null, account_deletion: { ...info, confirm_by_email: false } });
  openDialog();
  expect(screen.queryByText(/emails a confirmation link/)).toBeNull();
  fireEvent.change(screen.getByLabelText(/type your username/i), { target: { value: 'alice' } });
  fireEvent.click(screen.getByRole('button', { name: /^delete my account signs you out now/i }));
  await waitFor(() => expect(assign).toHaveBeenCalledWith(
    '/account-deleted?on=2026-11-08T12%3A00%3A00Z'));
});

test('Keep my account and Escape close the dialog without sending', () => {
  renderPage();
  openDialog();
  fireEvent.click(screen.getByRole('button', { name: /keep my account/i }));
  expect(screen.queryByRole('dialog')).toBeNull();
  openDialog();
  fireEvent.keyDown(window, { key: 'Escape' });
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(mockPost).not.toHaveBeenCalled();
});

test('the dialog says a waiting writing deletion is replaced, and how to remove X', () => {
  renderPage({
    data_deletion: { status: 'scheduled', grace_days: 30, purge_at: '2026-10-20T12:00:00Z', x_connected: true },
  });
  openDialog();
  expect(screen.getByText(/This replaces your request to delete all your writing/)).toBeTruthy();
  expect(screen.getByText(/forgets your X connection/).textContent)
    .toMatch(/removes\s+its access on X. If X still lists Loore afterwards, remove it\s+yourself: on X/);
});

test('an account that signs in with X gets the X note, whether or not this session can revoke', () => {
  // This session holds the sign-in's token: the request revokes it.
  renderPage({ account_deletion: { ...info, x_sign_in: true, x_sign_in_revocable: true } });
  openDialog();
  expect(screen.getByText(/removes the access to your X account that signing in with X gave it/).textContent)
    .toMatch(/If X still lists Loore afterwards, remove it yourself: on X, open Settings/);
});

test('without the sign-in token in this session (e.g. an email sign-in), the note only', () => {
  renderPage({ account_deletion: { ...info, x_sign_in: true, x_sign_in_revocable: false } });
  openDialog();
  expect(screen.queryByText(/removes the access to your X account/)).toBeNull();
  expect(screen.getByText(/You sign in with X, so X may list Loore/).textContent)
    .toMatch(/To remove it: on X, open Settings and privacy/);
});

test('no X note for an account without X', () => {
  renderPage();
  const dialog = openDialog();
  expect(dialog.textContent).not.toMatch(/ X /);
});

test('a server refusal is shown in the dialog', async () => {
  mockPost.mockRejectedValue({ response: { data: { error: 'This is the last admin account.' } } });
  renderPage();
  openDialog();
  fireEvent.change(screen.getByLabelText(/type your username/i), { target: { value: 'alice' } });
  fireEvent.click(screen.getByRole('button', { name: /email me the confirmation link/i }));
  await waitFor(() => expect(screen.getByText('This is the last admin account.')).toBeTruthy());
});

test('the last admin sees why instead of the button', () => {
  renderPage({
    account_deletion: { ...info, refusal: { code: 'last_admin', message: 'This is the last admin account. Make another account an admin first.' } },
  });
  expect(screen.queryByRole('button', { name: /delete my account…/i })).toBeNull();
  expect(screen.getByText(/This is the last admin account/)).toBeTruthy();
});

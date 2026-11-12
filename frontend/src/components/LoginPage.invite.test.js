// Referrals (#439): a code kept from /invite/<code> goes with the sign-in,
// to X as a query parameter and in the email link's request.
jest.mock('../contexts/UserContext', () => ({
  useUser: () => ({ user: null, loading: false }),
}));

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import LoginPage from './LoginPage';
import { rememberInvite } from '../utils/invite';

const CODE = 'Abc_def-123456XY';

let assigned;
beforeEach(() => {
  window.localStorage.clear();
  assigned = null;
  delete window.location;
  window.location = {
    set href(v) { assigned = v; },
    get href() { return assigned || 'http://localhost/login'; },
    search: '', pathname: '/login', hash: '',
  };
  global.fetch = jest.fn().mockResolvedValue({
    ok: true, json: () => Promise.resolve({ message: 'sent' }),
  });
});

test('X sign-in carries the invite', () => {
  rememberInvite(CODE);
  render(<MemoryRouter initialEntries={['/login']}><LoginPage /></MemoryRouter>);
  fireEvent.click(screen.getByRole('button', { name: /Sign in with X/i }));
  expect(assigned).toMatch(new RegExp(`/auth/login\\?next=[^&]+&invite=${CODE}$`));
});

test('X sign-in without an invite is as before', () => {
  render(<MemoryRouter initialEntries={['/login']}><LoginPage /></MemoryRouter>);
  fireEvent.click(screen.getByRole('button', { name: /Sign in with X/i }));
  expect(assigned).not.toMatch(/invite=/);
});

test('the email link request carries the invite', async () => {
  rememberInvite(CODE);
  render(<MemoryRouter initialEntries={['/login']}><LoginPage /></MemoryRouter>);
  fireEvent.click(screen.getByRole('button', { name: /Sign in with Email/i }));
  fireEvent.change(screen.getByPlaceholderText('you@example.com'), { target: { value: 'fred@example.com' } });
  fireEvent.submit(screen.getByPlaceholderText('you@example.com').closest('form'));
  await waitFor(() => expect(global.fetch).toHaveBeenCalled());
  const body = JSON.parse(global.fetch.mock.calls[0][1].body);
  expect(body.email).toBe('fred@example.com');
  expect(body.invite).toBe(CODE);
});

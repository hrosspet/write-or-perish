// A sign-in email asked for too often from one client is answered with a
// 429 and a calm message (backend/utils/sign_in_links.py). The form shows
// the server's words and stays open, so the person can try again later.
// Control: the form already showed any error the server sent.
jest.mock('../contexts/UserContext', () => ({
  useUser: () => ({ user: null, loading: false, setUser: jest.fn() }),
}));

import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import LoginPage from './LoginPage';

const WAIT = 'If you asked a moment ago, the email is on its way. Please '
  + 'check your inbox, or try again in a few minutes.';

afterEach(() => {
  delete global.fetch;
});

test('the calm message shows and the form stays open', async () => {
  global.fetch = jest.fn(() => Promise.resolve({
    ok: false,
    status: 429,
    json: () => Promise.resolve({ error: WAIT }),
  }));
  render(<MemoryRouter initialEntries={['/login']}><LoginPage /></MemoryRouter>);

  fireEvent.click(screen.getByRole('button', { name: /sign in with email/i }));
  fireEvent.change(screen.getByLabelText(/email address/i), {
    target: { value: 'someone@example.com' },
  });
  fireEvent.click(screen.getByRole('button', { name: /send sign-in link/i }));

  expect(await screen.findByText(WAIT)).toBeInTheDocument();
  expect(screen.queryByText('Check your inbox')).toBeNull();
  expect(screen.getByRole('button', { name: /send sign-in link/i })).toBeEnabled();
  expect(global.fetch).toHaveBeenCalledWith(
    expect.stringMatching(/\/auth\/magic-link\/send$/),
    expect.objectContaining({ method: 'POST' }),
  );
});

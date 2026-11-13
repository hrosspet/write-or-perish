// The sign-in page hands where the sign-in started to X and to the emailed
// link (#704), and drops anything that is not a page inside Loore. It also
// takes the server's name for it, `next`, and says when X sent the person
// back without a sign-in.
let mockUser = null;
jest.mock('../contexts/UserContext', () => ({
  useUser: () => ({ user: mockUser, loading: false, setUser: jest.fn() }),
}));

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom';
import LoginPage from './LoginPage';

const realLocation = window.location;

beforeEach(() => {
  mockUser = null;
  delete window.location;
  window.location = { href: '' };
  global.fetch = jest.fn(() => Promise.resolve({
    ok: true, json: () => Promise.resolve({ message: 'sent' }),
  }));
});

afterEach(() => {
  window.location = realLocation;
  delete global.fetch;
});

function Where() {
  const loc = useLocation();
  return <div data-testid="where">{loc.pathname + loc.search + loc.hash}</div>;
}

const renderAt = (url) => render(
  <MemoryRouter initialEntries={[url]}>
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="*" element={<Where />} />
    </Routes>
  </MemoryRouter>,
);

const xNext = () => new URL(window.location.href, 'http://x.test').searchParams.get('next');

async function emailNext() {
  fireEvent.click(screen.getByRole('button', { name: /sign in with email/i }));
  fireEvent.change(screen.getByLabelText(/email address/i), {
    target: { value: 'ana@example.com' },
  });
  fireEvent.click(screen.getByRole('button', { name: /send sign-in link/i }));
  await screen.findByText('Check your inbox');
  return JSON.parse(global.fetch.mock.calls[0][1].body).next_url;
}

const ENTRY = '/@ana/a-first-entry?x=1#reply-7';

test('control: X and the email link get the page the sign-in started on', async () => {
  renderAt(`/login?returnUrl=${encodeURIComponent(ENTRY)}`);
  fireEvent.click(screen.getByRole('button', { name: /sign in with x/i }));
  expect(window.location.href).toMatch(/\/auth\/login\?next=/);
  expect(xNext()).toBe(ENTRY);
  expect(await emailNext()).toBe(ENTRY);
});

test("the server's `next` works as returnUrl does", async () => {
  renderAt(`/login?next=${encodeURIComponent('/node/5')}`);
  fireEvent.click(screen.getByRole('button', { name: /sign in with x/i }));
  expect(xNext()).toBe('/node/5');
  expect(await emailNext()).toBe('/node/5');
});

test.each([
  '//evil.example',
  '/\\evil.example',
  'https:evil.example',
  '/%2F%2Fevil.example',
  '/@user\nhttps://evil.example',
])('a destination outside Loore (%j) is dropped for the default page', async (value) => {
  renderAt(`/login?returnUrl=${encodeURIComponent(value)}`);
  fireEvent.click(screen.getByRole('button', { name: /sign in with x/i }));
  expect(xNext()).toBe('/dashboard');
  expect(await emailNext()).toBe('/dashboard');
});

test('signed in already: the page is opened, a hostile one is not', async () => {
  mockUser = { id: 1, approved: true };
  renderAt(`/login?returnUrl=${encodeURIComponent('//evil.example')}`);
  await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('/dashboard'));
});

test('control: signed in already, the page it was asked for opens', async () => {
  mockUser = { id: 1, approved: true };
  renderAt(`/login?returnUrl=${encodeURIComponent(ENTRY)}`);
  await waitFor(() => expect(screen.getByTestId('where').textContent).toBe(ENTRY));
});

test.each([
  ['x_cancelled', 'Sign in with X was cancelled.'],
  ['x_failed', 'Sign in with X did not finish. Please try again.'],
])('back from X without a sign-in (%s): said, and the way on is kept', (code, text) => {
  renderAt(`/login?error=${code}&returnUrl=${encodeURIComponent('/node/5')}`);
  expect(screen.getByText(text)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: /sign in with x/i }));
  expect(xNext()).toBe('/node/5');
});

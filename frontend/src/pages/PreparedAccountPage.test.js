// #714: Connect X found the account Loore prepared for this X account. The
// page asks: open it, or stay. Nothing moves until Open is pressed.
import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import PreparedAccountPage, { thisAccountText } from './PreparedAccountPage';

let assigned;
beforeEach(() => {
  assigned = null;
  delete window.location;
  window.location = {
    set href(v) { assigned = v; },
    get href() { return assigned || 'http://localhost/prepared-account'; },
  };
  global.fetch = jest.fn();
  window.sessionStorage.clear();
});

const answer = (ok, data, status = ok ? 200 : 410) => Promise.resolve({
  ok, status, json: () => Promise.resolve(data),
});

const QUESTION = {
  handle: 'anna_on_x', email: 'anna@example.com', deleted: true,
  stays_because: null, email_moves: true, trial_ends: false,
};

const renderPage = () => render(
  <MemoryRouter initialEntries={['/prepared-account']}>
    <Routes>
      <Route path="/prepared-account" element={<PreparedAccountPage />} />
      <Route path="/account" element={<div>account page</div>} />
      <Route path="/" element={<div>home</div>} />
    </Routes>
  </MemoryRouter>,
);

test('the question: the X account, and what happens to this account', async () => {
  global.fetch.mockReturnValueOnce(answer(true, QUESTION));
  renderPage();
  expect(await screen.findByText(
    'Loore set up an account for the X account @anna_on_x before you joined. Sign in with X opens it.',
  )).toBeInTheDocument();
  expect(screen.getByText(/It holds no writing, so opening the prepared account deletes it\./))
    .toBeInTheDocument();
  expect(screen.getByText(/From then on, anna@example\.com signs in to the prepared account\./))
    .toBeInTheDocument();
  expect(global.fetch.mock.calls[0][0]).toMatch(/\/auth\/prepared-account$/);
  expect(global.fetch.mock.calls[0][1].credentials).toBe('include');
});

test('open: the server moves the person, the page follows', async () => {
  global.fetch
    .mockReturnValueOnce(answer(true, QUESTION))
    .mockReturnValueOnce(answer(true, { redirect: 'http://frontend.test/dashboard' }));
  renderPage();
  fireEvent.click(await screen.findByText('Open the prepared account'));
  await waitFor(() => expect(assigned).toBe('http://frontend.test/dashboard'));
  const [url, init] = global.fetch.mock.calls[1];
  expect(url).toMatch(/\/auth\/prepared-account\/open$/);
  expect(init.method).toBe('POST');
  expect(JSON.parse(init.body)).toEqual({ delete_this_account: true });
  // No updates popup over the first page of the prepared account.
  expect(window.sessionStorage.getItem('loore.skipUpdatesOnce')).toBe('1');
});

test('stay: the offer is dropped and the account page says X was not connected', async () => {
  global.fetch
    .mockReturnValueOnce(answer(true, QUESTION))
    .mockReturnValueOnce(answer(true, { ok: true }));
  renderPage();
  fireEvent.click(await screen.findByText('Stay in this account'));
  expect(await screen.findByText('account page')).toBeInTheDocument();
  expect(global.fetch.mock.calls[1][0]).toMatch(/\/auth\/prepared-account\/stay$/);
  expect(assigned).toBeNull();
});

test('without the X proof in this browser, the question has expired', async () => {
  global.fetch.mockReturnValueOnce(answer(false, { code: 'expired' }));
  renderPage();
  expect(await screen.findByRole('status')).toHaveTextContent(
    'This question has expired. Use Sign in with X again to open the prepared account.');
  expect(screen.queryByText('Open the prepared account')).toBeNull();
});

test('a change since the question: the error, and the question anew', async () => {
  const changed = { ...QUESTION, deleted: false, stays_because: 'writing', email_moves: false };
  global.fetch
    .mockReturnValueOnce(answer(true, QUESTION))
    .mockReturnValueOnce(answer(false, { code: 'changed', error: 'Something changed.' }, 409))
    .mockReturnValueOnce(answer(true, changed));
  renderPage();
  fireEvent.click(await screen.findByText('Open the prepared account'));
  expect(await screen.findByRole('alert')).toHaveTextContent('Something changed.');
  expect(await screen.findByText(/It holds writing, so it stays as it is\./)).toBeInTheDocument();
  expect(assigned).toBeNull();
});

test('what the page says about the account the person is in', () => {
  expect(thisAccountText({ ...QUESTION, trial_ends: true })).toBe(
    'You are signed in to another account, made when you signed in with anna@example.com. '
    + 'It holds no writing, so opening the prepared account deletes it. '
    + 'After 30 days it is deleted forever. '
    + 'From then on, anna@example.com signs in to the prepared account. '
    + 'The trial you started here ends now, with nothing charged, and the prepared account '
    + 'can start its own trial with the same card.');
  expect(thisAccountText({ ...QUESTION, deleted: false, stays_because: 'writing', email_moves: false }))
    .toBe('You are signed in to another account, made when you signed in with anna@example.com. '
      + 'It holds writing, so it stays as it is. Sign in with anna@example.com to come back to it.');
  expect(thisAccountText({ ...QUESTION, deleted: false, stays_because: 'paid', email_moves: false }))
    .toMatch(/It has a paid subscription, so it stays as it is\./);
  expect(thisAccountText({ ...QUESTION, deleted: false, stays_because: 'used', email_moves: false }))
    .toMatch(/It holds invitations you sent, poll answers or feedback, so it stays as it is\./);
});

// The prepared account has an address of its own: the person's address
// stays with the deleted account, which signing in with it can restore.
test('when the address stays with the deleted account, the page says how to restore it', () => {
  const text = thisAccountText({ ...QUESTION, email_moves: false, email_stays: true });
  expect(text).toBe(
    'You are signed in to another account, made when you signed in with anna@example.com. '
    + 'It holds no writing, so opening the prepared account deletes it. '
    + 'Your address anna@example.com stays with it: if you change your mind, you can still '
    + 'restore it within 30 days by signing in with anna@example.com; after that it is deleted '
    + 'forever. The prepared account opens with Sign in with X.');
  expect(text).not.toMatch(/signs in to the prepared account/);
});

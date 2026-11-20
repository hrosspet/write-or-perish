// An invite followed, then a sign-in as someone it was not made for (#439;
// Peter, 2026-11-13): the notice, the kind of sign-in only, and the two
// ways on.
import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import InviteMismatchPage from './InviteMismatchPage';

let assigned;
beforeEach(() => {
  assigned = null;
  delete window.location;
  window.location = {
    set href(v) { assigned = v; },
    get href() { return assigned || 'http://localhost/invite-mismatch'; },
  };
  global.fetch = jest.fn();
});

const renderAt = (search) => render(
  <MemoryRouter initialEntries={[`/invite-mismatch${search}`]}>
    <Routes>
      <Route path="/invite-mismatch" element={<InviteMismatchPage />} />
      <Route path="/login" element={<div>sign-in page</div>} />
    </Routes>
  </MemoryRouter>,
);

test('the notice names the kind of sign-in, never whom', () => {
  renderAt('?for=x');
  expect(screen.getByText(
    /This invitation was made for a different email address or X account\. It was made for an X account\. If it was meant for you, sign in with that one\. You can also continue without the invitation\./,
  )).toBeInTheDocument();
  expect(document.body.textContent).not.toMatch(/@|example/);
});

test('continue without the invitation finishes the held sign-in', async () => {
  global.fetch.mockResolvedValue({
    ok: true, json: () => Promise.resolve({ redirect: 'http://frontend.test/dashboard' }),
  });
  renderAt('?for=email');
  expect(screen.getByText(/It was made for an email address\./)).toBeInTheDocument();
  fireEvent.click(screen.getByText('Continue without the invitation'));
  await waitFor(() => expect(assigned).toBe('http://frontend.test/dashboard'));
  expect(global.fetch.mock.calls[0][0]).toMatch(/\/auth\/invite-choice\/continue$/);
  expect(global.fetch.mock.calls[0][1].credentials).toBe('include');
});

test('sign in differently forgets the held sign-in and goes back', async () => {
  global.fetch.mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) });
  renderAt('?for=x');
  fireEvent.click(screen.getByText('Sign in differently'));
  expect(await screen.findByText('sign-in page')).toBeInTheDocument();
  expect(global.fetch.mock.calls[0][0]).toMatch(/\/auth\/invite-choice\/cancel$/);
});

test('an expired hold says so', async () => {
  global.fetch.mockResolvedValue({
    ok: false, json: () => Promise.resolve({ error: 'This sign-in has expired. Please sign in again.' }),
  });
  renderAt('?for=x');
  fireEvent.click(screen.getByText('Continue without the invitation'));
  expect(await screen.findByText(/This sign-in has expired/)).toBeInTheDocument();
  expect(assigned).toBeNull();
});

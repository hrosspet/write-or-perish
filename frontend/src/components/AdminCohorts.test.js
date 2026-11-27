// Admin → Cohorts (the first cohort): the next call goes to the server as
// UTC, the row offers only the actions its state allows, and Close asks
// in a dialog (never the browser's confirm()).
const mockGet = jest.fn();
const mockPost = jest.fn();
const mockPut = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    get: (...a) => mockGet(...a),
    post: (...a) => mockPost(...a),
    put: (...a) => mockPut(...a),
  },
}));
jest.mock('./MarkdownBody', () => ({ children }) => <div>{children}</div>);

import React from 'react';
import { render, screen, act, fireEvent, within } from '@testing-library/react';
import AdminCohorts, { cohortBody, formFromCohort, invitationActions } from './AdminCohorts';

const COHORT = {
  id: 1, slug: 'first-eight', name: 'First Eight', about: 'Eight people.',
  founder_name: 'Peter', founder: 'Peter', booking_url: 'https://cal.example',
  call_url: 'https://call.example', next_call_at: '2026-12-03T18:00:00Z',
  question: 'What changed?', question_set_at: '2026-11-27T10:00:00Z',
  free_until: '2027-01-08', created_at: '2026-11-27T09:00:00Z',
  state_counts: { claimed: 1, invited: 1 },
};

const row = (overrides) => ({
  id: 7, x_handle: 'mara', username: null, state: 'invited', link: '/invitation/Q2x',
  letter: 'Dear Mara', created_at: '2026-11-27T09:00:00Z', first_opened_at: null,
  claimed_at: null, interview_started_at: null, report_drafted_at: null,
  report_sent_at: null, report_skipped_at: null, call_done_at: null,
  admitted_at: null, declined_at: null, closed_at: null, ...overrides,
});

test('the next call is sent as UTC with a Z, and shown back in local time', () => {
  const before = process.env.TZ;
  process.env.TZ = 'Europe/Prague';
  try {
    const form = formFromCohort(COHORT);
    expect(form.next_call_at).toBe('2026-12-03T19:00');
    expect(cohortBody({ ...form, next_call_at: '2026-12-10T19:30' }).next_call_at)
      .toBe('2026-12-10T18:30:00.000Z');
    // Emptied fields clear.
    expect(cohortBody({ ...form, next_call_at: '', free_until: '' }))
      .toMatchObject({ next_call_at: null, free_until: null });
  } finally {
    process.env.TZ = before;
  }
});

test('each state offers its own actions', () => {
  expect(invitationActions(row())).toEqual({ callDone: false, admit: false, close: true, editLetter: true, reopen: false });
  const claimed = row({ username: 'mara', claimed_at: '2026-11-27T10:00:00Z', state: 'report_sent' });
  expect(invitationActions(claimed)).toEqual({ callDone: true, admit: true, close: true, editLetter: false, reopen: false });
  expect(invitationActions({ ...claimed, admitted_at: 'x', call_done_at: 'x' }))
    .toEqual({ callDone: false, admit: false, close: true, editLetter: false, reopen: false });
  expect(invitationActions({ ...claimed, closed_at: 'x' }))
    .toEqual({ callDone: false, admit: false, close: false, editLetter: false, reopen: false });
  // Declined before anyone accepted it: only Reopen. Declined by the
  // person who had accepted it: nothing.
  expect(invitationActions(row({ declined_at: 'x', state: 'declined' })))
    .toEqual({ callDone: false, admit: false, close: false, editLetter: false, reopen: true });
  expect(invitationActions({ ...claimed, declined_at: 'x', state: 'declined' }).reopen).toBe(false);
});

test('Close asks in a dialog, then closes', async () => {
  const confirmSpy = jest.spyOn(window, 'confirm').mockImplementation(() => true);
  const claimed = row({ username: 'mara', claimed_at: '2026-11-27T10:00:00Z', state: 'claimed' });
  mockGet.mockImplementation((url) => {
    if (url === '/admin/cohorts') return Promise.resolve({ data: { cohorts: [COHORT] } });
    if (url === '/admin/cohorts/1/invitations') return Promise.resolve({ data: { invitations: [claimed] } });
    return Promise.reject(new Error(url));
  });
  mockPost.mockResolvedValue({ data: { invitation: { ...claimed, closed_at: 'x', state: 'closed' } } });
  await act(async () => { render(<AdminCohorts />); });
  expect(screen.getByRole('heading', { name: 'First Eight' })).toBeInTheDocument();
  expect(screen.getByText('Signed in', { selector: 'td' })).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Close' }));
  expect(confirmSpy).not.toHaveBeenCalled();
  const dialog = screen.getByRole('dialog');
  expect(within(dialog).getByText(/Close the invitation for @mara/)).toBeInTheDocument();
  await act(async () => {
    fireEvent.click(within(dialog).getByRole('button', { name: 'Close invitation' }));
  });
  expect(mockPost).toHaveBeenCalledWith('/admin/cohorts/1/invitations/7/close');
  confirmSpy.mockRestore();
});

test('Reopen undoes a "Not for me" given before anyone accepted', async () => {
  const declined = row({ declined_at: '2026-11-27T11:00:00Z', state: 'declined' });
  let invitations = [declined];
  mockGet.mockImplementation((url) => {
    if (url === '/admin/cohorts') return Promise.resolve({ data: { cohorts: [COHORT] } });
    if (url === '/admin/cohorts/1/invitations') return Promise.resolve({ data: { invitations } });
    return Promise.reject(new Error(url));
  });
  mockPost.mockReset();
  mockPost.mockImplementation(() => {
    invitations = [{ ...declined, declined_at: null, state: 'opened' }];
    return Promise.resolve({ data: { invitation: invitations[0] } });
  });
  await act(async () => { render(<AdminCohorts />); });
  expect(screen.queryByRole('button', { name: 'Close' })).not.toBeInTheDocument();
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Reopen' }));
  });
  expect(mockPost).toHaveBeenCalledWith('/admin/cohorts/1/invitations/7/reopen');
  expect(screen.queryByRole('button', { name: 'Reopen' })).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Close' })).toBeInTheDocument();
});

test('Admit asks in a dialog that says the person reads the group at once, then admits', async () => {
  const confirmSpy = jest.spyOn(window, 'confirm').mockImplementation(() => true);
  const claimed = row({ username: 'ben', x_handle: 'ben', claimed_at: '2026-11-27T10:00:00Z', call_done_at: '2026-11-28T10:00:00Z', state: 'call_done' });
  mockGet.mockImplementation((url) => {
    if (url === '/admin/cohorts') return Promise.resolve({ data: { cohorts: [COHORT] } });
    if (url === '/admin/cohorts/1/invitations') return Promise.resolve({ data: { invitations: [claimed] } });
    return Promise.reject(new Error(url));
  });
  mockPost.mockReset();
  mockPost.mockResolvedValue({ data: { invitation: { ...claimed, admitted_at: 'x', state: 'admitted' } } });
  await act(async () => { render(<AdminCohorts />); });
  fireEvent.click(screen.getByRole('button', { name: 'Admit' }));
  expect(mockPost).not.toHaveBeenCalled();
  expect(confirmSpy).not.toHaveBeenCalled();
  const dialog = screen.getByRole('dialog');
  expect(within(dialog).getByRole('heading', { name: 'Admit @ben to First Eight?' })).toBeInTheDocument();
  expect(within(dialog).getByText(
    '@ben joins First Eight and can at once read everything the group has shared so far, introductions included. They get a note in Loore about the newest entries.',
  )).toBeInTheDocument();
  // Cancel does nothing.
  fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(mockPost).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Admit' }));
  await act(async () => {
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Admit' }));
  });
  expect(mockPost).toHaveBeenCalledWith('/admin/cohorts/1/invitations/7/admit');
  confirmSpy.mockRestore();
});

test('Call done acts at once, and the copy-link button holds the full link', async () => {
  const claimed = row({ username: 'ben', x_handle: 'ben', claimed_at: '2026-11-27T10:00:00Z', state: 'report_sent' });
  mockGet.mockImplementation((url) => {
    if (url === '/admin/cohorts') return Promise.resolve({ data: { cohorts: [COHORT] } });
    if (url === '/admin/cohorts/1/invitations') return Promise.resolve({ data: { invitations: [claimed] } });
    return Promise.reject(new Error(url));
  });
  mockPost.mockReset();
  mockPost.mockResolvedValue({ data: { invitation: claimed } });
  const writeText = jest.fn(() => Promise.resolve());
  Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
  await act(async () => { render(<AdminCohorts />); });
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Copy link' }));
  });
  expect(writeText).toHaveBeenCalledWith(`${window.location.origin}/invitation/Q2x`);
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Call done' }));
  });
  expect(mockPost).toHaveBeenCalledWith('/admin/cohorts/1/invitations/7/call_done');
});

test('the invitations table is read again when the founder comes back to the window', async () => {
  let invitations = [row()];
  mockGet.mockImplementation((url) => {
    if (url === '/admin/cohorts') return Promise.resolve({ data: { cohorts: [COHORT] } });
    if (url === '/admin/cohorts/1/invitations') return Promise.resolve({ data: { invitations } });
    return Promise.reject(new Error(url));
  });
  await act(async () => { render(<AdminCohorts />); });
  expect(screen.getByText('Invited', { selector: 'td' })).toBeInTheDocument();
  // Declined in another browser meanwhile.
  invitations = [row({ declined_at: '2026-11-27T12:00:00Z', state: 'declined' })];
  await act(async () => { window.dispatchEvent(new Event('focus')); });
  expect(screen.getByText('Declined', { selector: 'td' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Reopen' })).toBeInTheDocument();
});

test('a row without a handle is told apart by the start of its letter', async () => {
  mockGet.mockImplementation((url) => {
    if (url === '/admin/cohorts') return Promise.resolve({ data: { cohorts: [COHORT] } });
    if (url === '/admin/cohorts/1/invitations') {
      return Promise.resolve({ data: { invitations: [row({ x_handle: null, letter: '**Dear Mara,**\n\nI read your thread.' })] } });
    }
    return Promise.reject(new Error(url));
  });
  await act(async () => { render(<AdminCohorts />); });
  expect(screen.getByText('No handle: "Dear Mara,…"')).toBeInTheDocument();
});

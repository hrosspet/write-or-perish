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
  expect(invitationActions(row())).toEqual({ callDone: false, admit: false, close: true, editLetter: true });
  const claimed = row({ username: 'mara', claimed_at: '2026-11-27T10:00:00Z', state: 'report_sent' });
  expect(invitationActions(claimed)).toEqual({ callDone: true, admit: true, close: true, editLetter: false });
  expect(invitationActions({ ...claimed, admitted_at: 'x', call_done_at: 'x' }))
    .toEqual({ callDone: false, admit: false, close: true, editLetter: false });
  expect(invitationActions({ ...claimed, closed_at: 'x' }))
    .toEqual({ callDone: false, admit: false, close: false, editLetter: false });
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

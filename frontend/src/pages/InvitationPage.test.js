// /invitation/<code> (the first cohort): the letter while the invitation
// is open, one sentence for every other state, "Not for me" declining
// with no question, and "Start the interview" keeping the code through
// Sign in with X to /cohort (or going there directly when signed in).
const mockGet = jest.fn();
const mockPost = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    get: (...a) => mockGet(...a),
    post: (...a) => mockPost(...a),
  },
}));
let mockUser = null;
jest.mock('../components/MarkdownBody', () => ({ children }) => <div>{children}</div>);
jest.mock('../contexts/UserContext', () => ({
  useUser: () => ({ user: mockUser, loading: false }),
}));

import React from 'react';
import { render, screen, act, fireEvent } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import InvitationPage, { INVITATION_TEXT } from './InvitationPage';
import { pendingCohortCode } from '../utils/cohort';

const CODE = 'Q2xvY2tfY29kZV9mb3JfdGVzdGluZzEy';

const OPEN = {
  cohort: { name: 'First Eight', about: 'Eight people who differ.' },
  founder: 'Peter',
  state: 'open',
  letter: 'Dear Mara,\n\nYou wrote about **doubt** in public.',
};

let assigned;
const realLocation = window.location;
beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
  mockUser = null;
  window.localStorage.clear();
  assigned = null;
  delete window.location;
  window.location = {
    set href(v) { assigned = v; },
    get href() { return assigned || `http://localhost/invitation/${CODE}`; },
    search: '', pathname: `/invitation/${CODE}`, hash: '', origin: 'http://localhost',
  };
});
afterAll(() => { window.location = realLocation; });

async function renderPage() {
  await act(async () => {
    render(
      <MemoryRouter initialEntries={[`/invitation/${CODE}`]}>
        <Routes>
          <Route path="/invitation/:code" element={<InvitationPage />} />
          <Route path="/cohort" element={<div>COHORT PAGE</div>} />
        </Routes>
      </MemoryRouter>,
    );
  });
}

test('the open invitation shows the letter, what this is, and the two buttons', async () => {
  mockGet.mockResolvedValue({ data: OPEN });
  await renderPage();
  expect(mockGet).toHaveBeenCalledWith(`/invitation/${CODE}`);
  expect(screen.getByRole('heading', { name: 'A letter from Peter' })).toBeInTheDocument();
  expect(screen.getByText(/You wrote about \*\*doubt\*\* in public/)).toBeInTheDocument();
  expect(screen.getByRole('heading', { name: 'What this is' })).toBeInTheDocument();
  expect(screen.getByText('Eight people who differ.')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Start the interview' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Not for me' })).toBeInTheDocument();
  // Not for search engines.
  expect(document.head.querySelector('meta[name="robots"]').getAttribute('content'))
    .toBe('noindex, nofollow');
});

test.each([
  ['claimed', INVITATION_TEXT.claimed],
  ['declined', INVITATION_TEXT.declined],
  ['closed', INVITATION_TEXT.closed],
])('%s: one sentence instead of the letter', async (state, sentence) => {
  mockGet.mockResolvedValue({ data: { cohort: OPEN.cohort, founder: 'Peter', state } });
  await renderPage();
  expect(screen.getByText(sentence)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Start the interview' })).toBeNull();
  expect(screen.queryByText(/A letter from/)).toBeNull();
});

test('an unknown code says the link leads nowhere', async () => {
  mockGet.mockRejectedValue({ response: { status: 404, data: { error: 'Not found' } } });
  await renderPage();
  expect(screen.getByText(INVITATION_TEXT.notFound)).toBeInTheDocument();
});

test('Not for me declines and asks nothing more', async () => {
  mockGet.mockResolvedValue({ data: OPEN });
  mockPost.mockResolvedValue({ data: { state: 'declined' } });
  await renderPage();
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Not for me' }));
  });
  expect(mockPost).toHaveBeenCalledWith(`/invitation/${CODE}/decline`);
  expect(screen.getByText(INVITATION_TEXT.justDeclined)).toBeInTheDocument();
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(screen.queryByRole('button', { name: 'Start the interview' })).toBeNull();
});

test('a refused decline shows the server\'s sentence', async () => {
  mockGet.mockResolvedValue({ data: OPEN });
  mockPost.mockRejectedValue({
    response: { status: 409, data: { error: 'This invitation is no longer open.', code: 'cohort_not_open' } },
  });
  await renderPage();
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Not for me' }));
  });
  expect(screen.getByRole('alert')).toHaveTextContent('This invitation is no longer open.');
});

test('signed out, Start the interview keeps the code and goes to X sign-in, back to /cohort', async () => {
  mockGet.mockResolvedValue({ data: OPEN });
  await renderPage();
  expect(screen.getByText(INVITATION_TEXT.startNote)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Start the interview' }));
  expect(pendingCohortCode()).toBe(CODE);
  expect(assigned).toMatch(/\/auth\/login\?next=%2Fcohort$/);
});

test('signed in, Start the interview keeps the code and opens /cohort', async () => {
  mockUser = { id: 5, username: 'mara', approved: true };
  mockGet.mockResolvedValue({ data: OPEN });
  await renderPage();
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Start the interview' }));
  });
  expect(pendingCohortCode()).toBe(CODE);
  expect(assigned).toBeNull();
  expect(screen.getByText('COHORT PAGE')).toBeInTheDocument();
});

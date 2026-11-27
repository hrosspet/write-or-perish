// The first cohort's pieces around the pages: the once-per-session send to
// /cohort (CohortArrival), the link back from the interview's Text or Voice
// page (InterviewBackLink), and the waiting page's sentence for an
// invitation the account can't accept (CohortWaitingNote).
const mockGet = jest.fn();
jest.mock('../../api', () => ({
  __esModule: true,
  default: { get: (...a) => mockGet(...a) },
}));
let mockUser;
let mockLoading = false;
jest.mock('../../contexts/UserContext', () => ({
  useUser: () => ({ user: mockUser, loading: mockLoading }),
}));

import React from 'react';
import { render, screen, act, fireEvent } from '@testing-library/react';
import { MemoryRouter, Route, Routes, Link, useLocation } from 'react-router-dom';
import CohortArrival from './CohortArrival';
import InterviewBackLink from './InterviewBackLink';
import CohortWaitingNote, { waitingSentence } from './CohortWaitingNote';
import { markInterviewOpen, rememberCohortCode } from '../../utils/cohort';

const CODE = 'Q2xvY2tfY29kZV9mb3JfdGVzdGluZzEy';

function Where() {
  const loc = useLocation();
  return <div data-testid="where">{loc.pathname}</div>;
}

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
  mockGet.mockReset();
  mockLoading = false;
  mockUser = {
    id: 5, approved: true,
    cohort: { name: 'First Eight', slug: 'first-eight', role: 'invited', report_step_done: false },
  };
});

async function arriveAt(path) {
  await act(async () => {
    render(
      <MemoryRouter initialEntries={[path]}>
        <CohortArrival />
        <Routes><Route path="*" element={<Where />} /></Routes>
      </MemoryRouter>,
    );
  });
}

describe('CohortArrival', () => {
  test('an invitee with an open report step who lands on Home goes to /cohort, once', async () => {
    await arriveAt('/');
    expect(screen.getByTestId('where')).toHaveTextContent('/cohort');
    expect(window.sessionStorage.getItem('loore:cohortShown')).toBe('1');
  });

  test('not a second time in the same session', async () => {
    window.sessionStorage.setItem('loore:cohortShown', '1');
    await arriveAt('/');
    expect(screen.getByTestId('where')).toHaveTextContent(/^\/$/);
  });

  test('not from another page, and not once the report step is done', async () => {
    await arriveAt('/log');
    expect(screen.getByTestId('where')).toHaveTextContent('/log');
  });

  test('not once the report step is done', async () => {
    mockUser = { ...mockUser, cohort: { ...mockUser.cohort, report_step_done: true } };
    await arriveAt('/dashboard');
    expect(screen.getByTestId('where')).toHaveTextContent('/dashboard');
  });

  test('a kept code sends a signed-in account to /cohort to claim it', async () => {
    mockUser = { id: 9, approved: true, cohort: null };
    rememberCohortCode(CODE);
    await arriveAt('/');
    expect(screen.getByTestId('where')).toHaveTextContent('/cohort');
  });
});

describe('InterviewBackLink', () => {
  function Page() {
    return (
      <>
        <InterviewBackLink />
        <Routes><Route path="*" element={<Link to="/log">Log</Link>} /></Routes>
      </>
    );
  }

  test('on the interview\'s thread page after /cohort opened it: Back to First Eight', async () => {
    markInterviewOpen('First Eight');
    render(<MemoryRouter initialEntries={['/node/124']}><Page /></MemoryRouter>);
    expect(screen.getByRole('link', { name: 'Back to First Eight' })).toHaveAttribute('href', '/cohort');
  });

  test('on Voice too', () => {
    markInterviewOpen('First Eight');
    render(<MemoryRouter initialEntries={['/voice']}><Page /></MemoryRouter>);
    expect(screen.getByRole('link', { name: 'Back to First Eight' })).toBeInTheDocument();
  });

  test('not on a thread opened any other way', () => {
    render(<MemoryRouter initialEntries={['/node/124']}><Page /></MemoryRouter>);
    expect(screen.queryByRole('link', { name: /Back to/ })).toBeNull();
  });

  test('leaving for another page drops it', () => {
    markInterviewOpen('First Eight');
    render(<MemoryRouter initialEntries={['/node/124']}><Page /></MemoryRouter>);
    fireEvent.click(screen.getByRole('link', { name: 'Log' }));
    expect(screen.queryByRole('link', { name: /Back to/ })).toBeNull();
    expect(window.sessionStorage.getItem('loore:cohortInterview')).toBeNull();
  });
});

describe('CohortWaitingNote', () => {
  const OPEN = { cohort: { name: 'First Eight', about: null }, founder: 'Peter', state: 'open', letter: 'Dear' };

  test('an account waiting for approval with a kept code reads why it cannot accept', async () => {
    rememberCohortCode(CODE);
    mockGet.mockResolvedValue({ data: OPEN });
    await act(async () => { render(<CohortWaitingNote />); });
    expect(mockGet).toHaveBeenCalledWith(`/invitation/${CODE}`);
    expect(screen.getByRole('status')).toHaveTextContent(
      'You opened an invitation to First Eight from Peter. This account is waiting for approval');
  });

  test('nothing without a kept code, or for an invitation no longer open', async () => {
    await act(async () => { render(<CohortWaitingNote />); });
    expect(mockGet).not.toHaveBeenCalled();
    expect(screen.queryByRole('status')).toBeNull();
    expect(waitingSentence({ ...OPEN, state: 'claimed' })).toBeNull();
  });
});

// "Preparing your starting point…" (#698): after a Yes on "Start from your
// tweets" that found material Loore had prepared for the person's X
// account, the card says it is being prepared, and the profile opens once
// it is ready and the person is let in. Also after a reload while it
// runs. A failure says so. Without a load the card is as before
// (control).
import React, { useState } from 'react';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';

let mockUserCtx;
jest.mock('../contexts/UserContext', () => ({
  useUser: () => mockUserCtx,
}));
const mockGet = jest.fn();
const mockPut = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    get: (...args) => mockGet(...args),
    put: (...args) => mockPut(...args),
    post: jest.fn(),
  },
}));

// eslint-disable-next-line import/first
import PrefillConsentCard from './PrefillConsentCard';
// eslint-disable-next-line import/first
import StartingPointProgress, { STARTING_POINT_POLL_MS } from './StartingPointProgress';

const baseUser = {
  username: 'ana', twitter_login: true, twitter_handle: 'ana', approved: false,
  prefill_consent: null, prefilled_handle: null, starting_point: null,
};

function WithUser({ initial, children }) {
  const [user, setUser] = useState(initial);
  mockUserCtx = { user, setUser };
  return children;
}

const renderAt = (ui, user) => render(
  <MemoryRouter initialEntries={['/alpha-thank-you']}>
    <WithUser initial={{ ...baseUser, ...user }}>
      <Routes>
        <Route path="/alpha-thank-you" element={ui} />
        <Route path="/profile" element={<div>PROFILE PAGE</div>} />
      </Routes>
    </WithUser>
  </MemoryRouter>,
);

beforeEach(() => {
  mockGet.mockReset();
  mockPut.mockReset();
  jest.useRealTimers();
});

test('a Yes that found prepared material says the starting point is being prepared', async () => {
  mockGet.mockReturnValue(new Promise(() => {}));
  mockPut.mockResolvedValue({ data: { user: {
    ...baseUser, prefill_consent: 'yes', starting_point: { status: 'preparing', done: 0, total: 8 },
  } } });
  renderAt(<PrefillConsentCard />, {});
  fireEvent.click(screen.getByRole('button', { name: 'Yes, seed from my tweets' }));
  expect(await screen.findByText('Preparing your starting point…')).toBeInTheDocument();
  expect(screen.queryByText(/^Noted\./)).toBeNull();
});

test('control: a Yes with nothing prepared shows the usual line', async () => {
  mockPut.mockResolvedValue({ data: { user: { ...baseUser, prefill_consent: 'yes' } } });
  renderAt(<PrefillConsentCard />, {});
  fireEvent.click(screen.getByRole('button', { name: 'Yes, seed from my tweets' }));
  expect(await screen.findByText(/^Noted\. We'll seed your Loore/)).toBeInTheDocument();
  expect(screen.queryByText('Preparing your starting point…')).toBeNull();
});

test('after a reload the card still says it is being prepared', () => {
  mockGet.mockReturnValue(new Promise(() => {}));
  renderAt(<PrefillConsentCard />, {
    prefill_consent: 'yes', starting_point: { status: 'preparing', done: 2, total: 8 },
  });
  expect(screen.getByRole('status')).toHaveTextContent('Preparing your starting point…');
});

test('when it is ready and the person is let in, the profile opens', async () => {
  jest.useFakeTimers();
  mockGet.mockImplementation((url) => {
    if (url === '/dashboard/starting-point') {
      return Promise.resolve({ data: { starting_point: { status: 'ready' }, approved: true } });
    }
    return Promise.resolve({ data: { user: { ...baseUser, approved: true } } });
  });
  renderAt(<StartingPointProgress />, { starting_point: { status: 'preparing' } });
  await act(async () => { jest.advanceTimersByTime(STARTING_POINT_POLL_MS); });
  jest.useRealTimers();
  expect(await screen.findByText('PROFILE PAGE')).toBeInTheDocument();
  expect(mockGet).toHaveBeenCalledWith('/dashboard', { params: { profile: 0 } });
});

test('a load that failed says so', async () => {
  jest.useFakeTimers();
  mockGet.mockResolvedValue({ data: { starting_point: { status: 'failed' }, approved: false } });
  renderAt(<StartingPointProgress />, {});
  await act(async () => { jest.advanceTimersByTime(STARTING_POINT_POLL_MS); });
  jest.useRealTimers();
  await waitFor(() => expect(screen.getByText("Your starting point isn't ready yet.")).toBeInTheDocument());
  expect(screen.getByText(/We've been alerted and will set it up for you\./)).toBeInTheDocument();
});

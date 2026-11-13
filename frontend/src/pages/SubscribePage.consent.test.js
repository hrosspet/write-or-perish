// Payments on (#697): a new account comes from the terms straight to the
// subscribe page, not to the waitlist, so "Start from your tweets?" asks
// here (X sign-in, not answered yet).
let mockUserCtx;
jest.mock('../contexts/UserContext', () => ({
  useUser: () => mockUserCtx,
}));
const mockGet = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    get: (...args) => mockGet(...args),
    post: jest.fn(),
    put: jest.fn(),
    interceptors: { response: { handlers: [] } },
  },
}));

import React from 'react';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import SubscribePage from './SubscribePage';

const user = (over = {}) => ({
  username: 'bird', email: null, approved: true, plan: 'free',
  payments_enabled: true, twitter_login: true, prefill_consent: null,
  subscription: { state: 'none', status: 'none', trial_available: true,
                  trial_days: 14, ai_replies: false },
  ...over,
});

beforeEach(() => {
  mockGet.mockResolvedValue({ data: { subscription: user().subscription,
                                      levels: [], contact_email: 'info@loore.org' } });
});

test('an X sign-in that has not answered is asked here', () => {
  mockUserCtx = { user: user(), setUser: jest.fn() };
  render(<MemoryRouter><SubscribePage /></MemoryRouter>);
  expect(screen.getByText("Start from what you've already written?")).toBeInTheDocument();
  expect(screen.getByText('Already on X?')).toBeInTheDocument();
});

test('not asked once answered, or without X', () => {
  mockUserCtx = { user: user({ prefill_consent: 'no' }), setUser: jest.fn() };
  const { unmount } = render(<MemoryRouter><SubscribePage /></MemoryRouter>);
  expect(screen.queryByText("Start from what you've already written?")).not.toBeInTheDocument();
  unmount();
  mockUserCtx = { user: user({ twitter_login: false }), setUser: jest.fn() };
  render(<MemoryRouter><SubscribePage /></MemoryRouter>);
  expect(screen.queryByText("Start from what you've already written?")).not.toBeInTheDocument();
});

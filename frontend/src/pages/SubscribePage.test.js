// Payments on (#438; Peter, 2026-11-12): an account that never had a
// subscription sees only the subscribe page: the levels (prices plus tax),
// the code field, signing out and deleting the account. Nothing else of the
// app is shown; accounts set by hand, active and lapsed ones are let in.
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
    delete: jest.fn(),
    interceptors: { response: { handlers: [] } },
  },
}));

import React from 'react';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import SubscribePage from './SubscribePage';
import SubscriptionBanner from '../components/SubscriptionBanner';
import { notLetIn } from '../utils/subscription';

const LEVELS = [
  { level: 'standard', amount_cents: 3000, currency: 'usd' },
  { level: 'supporter', amount_cents: 9900, currency: 'usd' },
];
const sub = (over = {}) => ({
  state: 'none', level: null, status: 'none', current_period_end: null,
  trial_end: null, cancel_at_period_end: false, trial_available: true,
  trial_days: 14, plan_set_by_hand: false, ai_replies: false, ...over,
});
const user = (over = {}) => ({
  username: 'ana', email: 'ana@example.com', approved: true, plan: 'free',
  payments_enabled: true, subscription: sub(), ...over,
});

test('who is not let in', () => {
  expect(notLetIn(user())).toBe(true);
  // Payments off: no payments_enabled, nobody is stopped.
  expect(notLetIn({ approved: true, plan: 'free' })).toBe(false);
  expect(notLetIn(user({ subscription: sub({ state: 'set_by_hand' }) }))).toBe(false);
  expect(notLetIn(user({ subscription: sub({ state: 'active' }) }))).toBe(false);
  expect(notLetIn(user({ subscription: sub({ state: 'lapsed' }) }))).toBe(false);
  // The waitlist comes first.
  expect(notLetIn(user({ approved: false }))).toBe(false);
});

test('the subscribe page: levels with tax, the code, sign out, deletion', async () => {
  mockUserCtx = { user: user(), setUser: jest.fn() };
  mockGet.mockResolvedValue({ data: { subscription: sub(), levels: LEVELS, contact_email: 'info@loore.org' } });
  render(<MemoryRouter><SubscribePage /></MemoryRouter>);
  expect(screen.getByText(/no active subscription yet/)).toBeInTheDocument();
  expect(await screen.findByText(/\$30 a month, plus tax where it applies/)).toBeInTheDocument();
  expect(screen.getByLabelText('Code')).toBeInTheDocument();
  expect(screen.getByText('Sign out').closest('a').getAttribute('href')).toMatch(/\/auth\/logout$/);
  expect(screen.getByText('Delete my account')).toBeInTheDocument();
});

test('a failed payment is said on load, with the link to Settings', () => {
  mockUserCtx = {
    user: user({ subscription: sub({ state: 'lapsed', status: 'past_due', level: 'standard' }) }),
  };
  render(<MemoryRouter><SubscriptionBanner /></MemoryRouter>);
  expect(screen.getByText(/last payment didn't go through/)).toBeInTheDocument();
  expect(screen.getByText('Settings').closest('a')).toHaveAttribute('href', '/account#subscription');
});

test('no banner while payments are off', () => {
  mockUserCtx = { user: { plan: 'alpha' } };
  render(<MemoryRouter><SubscriptionBanner /></MemoryRouter>);
  expect(screen.queryByText('Settings')).toBeNull();
});

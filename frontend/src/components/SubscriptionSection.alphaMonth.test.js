// The alpha free month (#438): an alpha tester sees the month and its
// end, the discounted price offered without a code, and no card asked
// until they subscribe. After the month: what stays, and the price.
let mockUserCtx;
jest.mock('../contexts/UserContext', () => ({
  useUser: () => mockUserCtx,
}));
const mockGet = jest.fn();
const mockPost = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    get: (...args) => mockGet(...args),
    post: (...args) => mockPost(...args),
    put: jest.fn(),
    delete: jest.fn(),
  },
}));

import React from 'react';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import SubscriptionSection from './SubscriptionSection';
import SubscriptionBanner from './SubscriptionBanner';
import {
  freeMonthEndsSoon, freeMonthLine, resetSubscriptionForTests,
  subscriptionStatusLine,
} from '../utils/subscription';

const LEVELS = [
  { level: 'discounted', amount_cents: 1200, currency: 'usd' },
  { level: 'standard', amount_cents: 3000, currency: 'usd' },
  { level: 'supporter', amount_cents: 9900, currency: 'usd' },
];

const sub = (over = {}) => ({
  state: 'active', level: null, status: 'none', current_period_end: null,
  trial_end: null, cancel_at_period_end: false, trial_available: false,
  trial_days: 14, plan_set_by_hand: false, ai_replies: true,
  has_customer: false, alpha_tester: true, free_month_active: true,
  free_month_until: '2026-12-12T10:00:00Z', ...over,
});

const baseUser = {
  username: 'tess', email: 'tess@example.com', plan: 'free',
  payments_enabled: true,
};

let assign;
beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
  resetSubscriptionForTests();
  window.localStorage.clear();
  assign = jest.fn();
  delete window.location;
  window.location = { assign, search: '', pathname: '/account', hash: '' };
});

const renderSection = (subscription) => {
  mockGet.mockResolvedValue({
    data: { subscription, levels: LEVELS, contact_email: 'info@loore.org' },
  });
  render(
    <MemoryRouter initialEntries={['/account#subscription']}>
      <SubscriptionSection user={{ ...baseUser, subscription }} setUser={jest.fn()} />
    </MemoryRouter>,
  );
};

test('during the free month: its end, the price after it, no card yet', async () => {
  renderSection(sub());
  expect(await screen.findByText(
    'Your free month as an alpha tester runs until December 12, 2026. '
    + 'After it, Loore is $12 a month for you, plus tax where it applies.',
  )).toBeInTheDocument();
  expect(screen.getByText(/If you subscribe now, the first payment is on December 12, 2026/))
    .toBeInTheDocument();
  expect(screen.getByText('Reduced')).toBeInTheDocument();
  expect(screen.getByText('Standard')).toBeInTheDocument();
  // No code needed, and no 14-day trial offered on top of the month.
  expect(screen.queryByLabelText('Code')).toBeNull();
  expect(screen.queryByText(/The first 14 days are free/)).toBeNull();
});

// Control: the web sends the level the server lists; the server is what
// lets a tester have it without a code (test_alpha_free_month.py).
test('a tester subscribes at the reduced price without a code', async () => {
  mockPost.mockResolvedValue({ data: { url: 'https://checkout.stripe.test/a' } });
  renderSection(sub());
  fireEvent.click(await screen.findByText('Reduced'));
  expect(mockPost).toHaveBeenCalledWith('/billing/checkout', { level: 'discounted' });
  await waitFor(() => expect(assign).toHaveBeenCalledWith('https://checkout.stripe.test/a'));
});

test('after the free month: what stays, and the price at checkout', async () => {
  renderSection(sub({ state: 'lapsed', free_month_active: false, ai_replies: false }));
  expect(await screen.findByText(/Your free month has ended\. Your writing, reading and export stay/))
    .toBeInTheDocument();
  expect(screen.getByText('The first payment is due at checkout.')).toBeInTheDocument();
  expect(await screen.findByText('Reduced')).toBeInTheDocument();
});

test('a tester who subscribed during the month: the month, not a trial', () => {
  expect(subscriptionStatusLine(sub({
    level: 'discounted', status: 'trialing', trial_end: '2026-12-12T10:00:00Z',
  }))).toBe('Reduced. Your free month runs until December 12, 2026; the first payment is on that day.');
  // Control: #649's line for a trial of an account that is not a tester.
  expect(subscriptionStatusLine(sub({
    alpha_tester: false, level: 'standard', status: 'trialing',
    trial_end: '2026-12-12T10:00:00Z',
  }))).toBe('Standard trial until December 12, 2026. The first payment is on that day.');
});

test('the free month line is for testers only', () => {
  expect(freeMonthLine(sub({ alpha_tester: false }), '$12')).toBeNull();
});

test('the notice before the end: the last week, without a subscription', () => {
  const end = Date.parse('2026-12-12T10:00:00Z');
  const day = 86400000;
  expect(freeMonthEndsSoon(sub(), end - 6 * day)).toBe(true);
  expect(freeMonthEndsSoon(sub(), end - 8 * day)).toBe(false);
  expect(freeMonthEndsSoon(sub({ status: 'trialing' }), end - day)).toBe(false);
  expect(freeMonthEndsSoon(sub({ free_month_active: false }), end - day)).toBe(false);
  expect(freeMonthEndsSoon(sub({ alpha_tester: false }), end - day)).toBe(false);
});

const renderBanner = (subscription) => {
  mockUserCtx = { user: { ...baseUser, subscription } };
  return render(<MemoryRouter><SubscriptionBanner /></MemoryRouter>);
};

test('the banner says the month ends soon, once', () => {
  const soon = new Date(Date.now() + 3 * 86400000).toISOString();
  const { unmount } = renderBanner(sub({ free_month_until: soon }));
  expect(screen.getByText(/Your free month ends on .*To keep AI replies after it, subscribe in/))
    .toBeInTheDocument();
  act(() => { fireEvent.click(screen.getByLabelText('Dismiss')); });
  expect(screen.queryByText(/Your free month ends on/)).toBeNull();
  unmount();
  renderBanner(sub({ free_month_until: soon }));
  expect(screen.queryByText(/Your free month ends on/)).toBeNull();
});

// Control: no notice before the month's last week.
test('no banner while the month has more than a week left', () => {
  const later = new Date(Date.now() + 20 * 86400000).toISOString();
  renderBanner(sub({ free_month_until: later }));
  expect(screen.queryByText(/Your free month ends on/)).toBeNull();
});

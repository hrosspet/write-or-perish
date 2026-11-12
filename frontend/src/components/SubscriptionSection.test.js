// Payments (#438): the Subscription section of Settings. Off (no
// payments_enabled on the user), Account shows the Plan row as before; on,
// it shows the levels, the trial, the lower-price line and the code field,
// or the state and the "manage or cancel" link.
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
jest.mock('./ModelSelector', () => () => null);

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import AccountPage from '../pages/AccountPage';
import SubscriptionSection from './SubscriptionSection';
import {
  isSubscriptionRequired, resetSubscriptionForTests, subscriptionStatusLine,
} from '../utils/subscription';

const LEVELS = [
  { level: 'standard', amount_cents: 3000, currency: 'usd' },
  { level: 'supporter', amount_cents: 9900, currency: 'usd' },
];

const sub = (over = {}) => ({
  level: null, status: 'none', current_period_end: null, trial_end: null,
  cancel_at_period_end: false, trial_available: true, trial_days: 14,
  plan_set_by_hand: false, ai_replies: false, has_customer: false, ...over,
});

const baseUser = {
  username: 'ana', email: 'ana@example.com', twitter_login: false,
  pending_email: null, pending_email_expired: false, plan: 'free',
};

let assign;
beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
  resetSubscriptionForTests();
  assign = jest.fn();
  delete window.location;
  window.location = { assign, search: '', pathname: '/account', hash: '' };
});

const renderSection = (subscription, { search = '' } = {}) => {
  mockGet.mockResolvedValue({
    data: { subscription, levels: LEVELS, contact_email: 'info@loore.org' },
  });
  const setUser = jest.fn();
  const user = { ...baseUser, payments_enabled: true, subscription };
  render(
    <MemoryRouter initialEntries={[`/account${search}#subscription`]}>
      <SubscriptionSection user={user} setUser={setUser} />
    </MemoryRouter>,
  );
  return setUser;
};

test('payments off: Account keeps the Plan row and asks nothing', () => {
  mockUserCtx = { user: { ...baseUser, plan: 'alpha' }, setUser: jest.fn() };
  mockGet.mockReturnValue(new Promise(() => {}));
  render(<MemoryRouter><AccountPage /></MemoryRouter>);
  expect(screen.getByText('Plan')).toBeInTheDocument();
  expect(screen.queryByText('Subscription')).toBeNull();
  expect(mockGet).not.toHaveBeenCalledWith('/billing');
});

test('payments on: Account shows the Subscription section', async () => {
  mockUserCtx = {
    user: { ...baseUser, payments_enabled: true, subscription: sub() },
    setUser: jest.fn(),
  };
  mockGet.mockResolvedValue({ data: { subscription: sub(), levels: LEVELS } });
  render(<MemoryRouter><AccountPage /></MemoryRouter>);
  expect(screen.getByText('Subscription')).toBeInTheDocument();
  expect(screen.queryByText('Plan')).toBeNull();
  await screen.findByText('Standard');
});

test('the Subscribe view: levels, trial, the lower price line', async () => {
  renderSection(sub());
  expect(await screen.findByText('Standard')).toBeInTheDocument();
  expect(screen.getByText('Supporter')).toBeInTheDocument();
  expect(screen.getByText(/\$30 a month/)).toBeInTheDocument();
  expect(screen.getByText(/\$99 a month/)).toBeInTheDocument();
  expect(screen.getByText(/The first 14 days are free/)).toBeInTheDocument();
  expect(screen.getByText(
    'Need a lower price? Write to info@loore.org for a code. No proof needed.',
  )).toBeInTheDocument();
  expect(screen.queryByText(/12/)).toBeNull();
});

test('choosing a level goes to Checkout', async () => {
  mockPost.mockResolvedValue({ data: { url: 'https://checkout.stripe.test/x' } });
  renderSection(sub());
  fireEvent.click(await screen.findByText('Supporter'));
  expect(mockPost).toHaveBeenCalledWith('/billing/checkout', { level: 'supporter' });
  await waitFor(() => expect(assign).toHaveBeenCalledWith('https://checkout.stripe.test/x'));
});

test('a code goes to Checkout with the code', async () => {
  mockPost.mockResolvedValue({ data: { url: 'https://checkout.stripe.test/y' } });
  renderSection(sub());
  await screen.findByText('Standard');
  fireEvent.change(screen.getByLabelText('Code'), { target: { value: ' lower-1 ' } });
  fireEvent.click(screen.getByText('Use code'));
  expect(mockPost).toHaveBeenCalledWith('/billing/checkout', { code: 'lower-1' });
  await waitFor(() => expect(assign).toHaveBeenCalled());
});

test('a code that is not valid says so', async () => {
  mockPost.mockRejectedValue({
    response: { status: 400, data: { error: 'code_not_valid', message: 'That code is not valid.' } },
  });
  renderSection(sub());
  await screen.findByText('Standard');
  fireEvent.change(screen.getByLabelText('Code'), { target: { value: 'nope' } });
  fireEvent.click(screen.getByText('Use code'));
  expect(await screen.findByText('That code is not valid.')).toBeInTheDocument();
  expect(assign).not.toHaveBeenCalled();
});

test('a live subscription shows its state and the manage link', async () => {
  const live = sub({
    level: 'standard', status: 'active', current_period_end: '2026-12-12T10:00:00Z',
    ai_replies: true, has_customer: true, trial_available: false,
  });
  mockPost.mockResolvedValue({ data: { url: 'https://billing.stripe.test/p' } });
  renderSection(live);
  expect(await screen.findByText(/Standard\. Next payment on December 12, 2026\./)).toBeInTheDocument();
  expect(screen.queryByLabelText('Code')).toBeNull();
  fireEvent.click(screen.getByText('Manage or cancel'));
  expect(mockPost).toHaveBeenCalledWith('/billing/portal', {});
  await waitFor(() => expect(assign).toHaveBeenCalledWith('https://billing.stripe.test/p'));
});

test('an account set by hand has nothing to pay', async () => {
  renderSection(sub({ plan_set_by_hand: true, ai_replies: true }));
  expect(await screen.findByText(/There is nothing to pay/)).toBeInTheDocument();
  expect(screen.queryByText('Standard')).toBeNull();
});

test('back from Checkout: the subscription is read from Stripe', async () => {
  const trialing = sub({
    level: 'standard', status: 'trialing', trial_end: '2026-11-26T10:00:00Z',
    ai_replies: true, has_customer: true, trial_available: false,
  });
  mockPost.mockResolvedValue({ data: { subscription: trialing, levels: LEVELS } });
  const setUser = renderSection(sub(), { search: '?subscription=success&session_id=cs_test_1' });
  expect(mockPost).toHaveBeenCalledWith('/billing/sync', { session_id: 'cs_test_1' });
  expect(screen.getByText(/Thank you/)).toBeInTheDocument();
  expect(await screen.findByText(/Standard trial until November 26, 2026/)).toBeInTheDocument();
  // The user object (and the reply gate) follow the new state.
  expect(setUser).toHaveBeenCalled();
  const update = setUser.mock.calls[0][0];
  const next = update({ ...baseUser, payments_enabled: true, subscription: sub() });
  expect(next.subscription.status).toBe('trialing');
  expect(isSubscriptionRequired()).toBe(false);
});

test('status lines', () => {
  expect(subscriptionStatusLine(sub({
    level: 'supporter', status: 'active', cancel_at_period_end: true,
    current_period_end: '2027-01-05T00:00:00Z',
  }))).toBe('Supporter, cancelled. It ends on January 5, 2027.');
  expect(subscriptionStatusLine(sub({ status: 'ended' }))).toMatch(/has ended/);
  expect(subscriptionStatusLine(sub({ status: 'none' }))).toBeNull();
});

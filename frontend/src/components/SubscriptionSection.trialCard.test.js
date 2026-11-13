// One trial per card (#697): a trial whose card had one on another account
// was cancelled before anything was charged. The page says so plainly, and
// that subscribing without a trial is possible.
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
  },
}));

import React from 'react';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import SubscriptionSection, { TRIAL_CARD_REFUSED_TEXT } from './SubscriptionSection';

const LEVELS = [
  { level: 'standard', amount_cents: 3000, currency: 'usd' },
  { level: 'supporter', amount_cents: 9900, currency: 'usd' },
];
const sub = (over = {}) => ({
  state: 'none', level: 'standard', status: 'ended', current_period_end: null,
  trial_end: null, cancel_at_period_end: false, trial_available: false,
  trial_days: 14, plan_set_by_hand: false, ai_replies: false,
  has_customer: true, alpha_tester: false, trial_card_refused: true, ...over,
});

const renderWith = async (s) => {
  const user = { username: 'ben', plan: 'free', payments_enabled: true, subscription: s };
  mockUserCtx = { user, setUser: jest.fn() };
  mockGet.mockResolvedValue({ data: { subscription: s, levels: LEVELS, contact_email: 'info@loore.org' } });
  render(<MemoryRouter><SubscriptionSection user={user} setUser={jest.fn()} /></MemoryRouter>);
  await screen.findByText('Standard');
};

test('the card had a trial elsewhere: said plainly, the levels still offered', async () => {
  await renderWith(sub());
  expect(screen.getByText(TRIAL_CARD_REFUSED_TEXT)).toBeInTheDocument();
  expect(TRIAL_CARD_REFUSED_TEXT).toMatch(/already been used for a free trial/);
  expect(TRIAL_CARD_REFUSED_TEXT).toMatch(/subscribe without one/);
  expect(screen.getByText('Supporter')).toBeInTheDocument();
  expect(screen.queryByText(/This account's free trial was used/)).not.toBeInTheDocument();
});

test("control: the account's own trial used, the old text", async () => {
  await renderWith(sub({ trial_card_refused: false }));
  expect(screen.getByText(/This account's free trial was used/)).toBeInTheDocument();
  expect(screen.queryByText(TRIAL_CARD_REFUSED_TEXT)).not.toBeInTheDocument();
});

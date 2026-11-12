// Payments (#438) on the web: inert while the server has payments off;
// on, a refused reply raises the banner with its link to Settings, and the
// spend-cap text no longer says "free alpha".
import React from 'react';
import { act, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../api';
import SubscriptionBanner from '../components/SubscriptionBanner';
import {
  isSubscriptionRequired, isSubscriptionRequiredError, noteSkippedReply,
  paymentsEnabled, rememberSubscription, resetSubscriptionForTests,
} from './subscription';
import {
  SPEND_CAP_DEFAULT_MESSAGE, resetSpendCapForTests, spendCapBannerMessage,
} from './spendCap';

beforeEach(() => {
  resetSubscriptionForTests();
  resetSpendCapForTests();
});

test('a user without payments_enabled changes nothing', () => {
  rememberSubscription({ plan: 'free', voice_mode_enabled: false });
  expect(paymentsEnabled()).toBe(false);
  expect(isSubscriptionRequired()).toBe(false);
  expect(spendCapBannerMessage()).toBe(SPEND_CAP_DEFAULT_MESSAGE);
});

test('payments on: an account without AI replies is flagged', () => {
  rememberSubscription({ payments_enabled: true, subscription: { ai_replies: false } });
  expect(paymentsEnabled()).toBe(true);
  expect(isSubscriptionRequired()).toBe(true);
  rememberSubscription({ payments_enabled: true, subscription: { ai_replies: true } });
  expect(isSubscriptionRequired()).toBe(false);
});

test('payments on: the spend-cap text drops "free alpha"', () => {
  rememberSubscription({ payments_enabled: true, subscription: { ai_replies: true } });
  const msg = spendCapBannerMessage();
  expect(msg).not.toMatch(/free alpha/);
  expect(msg).toBe("You've reached your monthly usage limit. It resets at the start of next month.");
});

test('a saved entry whose reply was skipped raises the banner with a Settings link', () => {
  render(<MemoryRouter><SubscriptionBanner /></MemoryRouter>);
  expect(screen.queryByText('Settings')).toBeNull();
  act(() => noteSkippedReply({
    subscription_required: true,
    subscription_message: 'Your subscription has ended, so Loore doesn\'t reply. Your writing is saved.',
  }));
  expect(screen.getByText(/Your subscription has ended/)).toBeInTheDocument();
  expect(screen.getByText('Settings').closest('a')).toHaveAttribute('href', '/account#subscription');
  expect(isSubscriptionRequired()).toBe(true);
});

test('an answer without the flag raises nothing', () => {
  render(<MemoryRouter><SubscriptionBanner /></MemoryRouter>);
  act(() => noteSkippedReply({ spend_capped: true }));
  expect(screen.queryByText('Settings')).toBeNull();
});

test('the 402 raises the banner through the api interceptor', async () => {
  render(<MemoryRouter><SubscriptionBanner /></MemoryRouter>);
  const error = {
    response: {
      status: 402,
      data: { error: 'subscription_required', message: "Loore doesn't reply on an account without a subscription. Your writing is saved." },
    },
  };
  expect(isSubscriptionRequiredError(error)).toBe(true);
  const rejected = api.interceptors.response.handlers
    .filter(Boolean).map((h) => h.rejected).filter(Boolean)[0];
  await act(async () => {
    await expect(rejected(error)).rejects.toBe(error);
  });
  expect(screen.getByText(/without a subscription/)).toBeInTheDocument();
});

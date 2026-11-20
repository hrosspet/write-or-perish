// Payments (#438): the trial's usage ceiling and an alpha tester's free
// month's lift with the first payment, not when the trial ends and not with
// the month (Peter, 2026-11-13: "pls fix this in a followup"). The server
// says so (`lifts_with_payment` on a 402, `spend_cap_lifts_with_payment` on
// the user and on a saved entry's answer) and sends no date; the banner and
// the toasts raised here then say the same.
import React from 'react';
import { act, render, screen } from '@testing-library/react';
import api from '../api';
import SpendCapBanner from '../components/SpendCapBanner';
import {
  SPEND_CAP_UNTIL_PAYMENT_MESSAGE, markSpendBlocked, notifySpendBlocked,
  resetSpendCapForTests, spendCapBannerMessage, spendCapToastMessage,
} from './spendCap';

afterEach(() => resetSpendCapForTests());

const NOV_20 = new Date(Date.UTC(2026, 10, 20));
const TRIAL_402 = {
  response: {
    status: 402,
    data: {
      error: 'monthly_spend_limit_reached',
      message: "You've reached the usage limit of your trial. It lifts with your first payment.",
      lifts_on: null,
      lifts_with_payment: true,
    },
  },
};

const rejected = () => api.interceptors.response.handlers
  .filter(Boolean).map((h) => h.rejected).filter(Boolean)[0];

test('a user loaded under the trial ceiling: no reset, the first payment', () => {
  markSpendBlocked(null, true);
  expect(spendCapBannerMessage(NOV_20)).toBe(SPEND_CAP_UNTIL_PAYMENT_MESSAGE);
  expect(SPEND_CAP_UNTIL_PAYMENT_MESSAGE).toBe(
    "You've reached your usage limit. It lifts with your first payment.");
  expect(spendCapToastMessage('record', NOV_20)).toBe(
    "You've reached your usage limit, so you can't start a new recording "
    + 'until your first payment.');
  expect(spendCapToastMessage('upload', NOV_20)).toMatch(/upload audio until your first payment\.$/);
  for (const text of [spendCapBannerMessage(NOV_20), spendCapToastMessage('glean', NOV_20)]) {
    expect(text).not.toMatch(/monthly|resets|trial ends/);
  }
});

test("the 402 says which cap it is, before the caller's toast", async () => {
  render(<SpendCapBanner />);
  await act(async () => {
    await expect(rejected()(TRIAL_402)).rejects.toBe(TRIAL_402);
  });
  // The banner shows the server's own words.
  expect(screen.getByText(/usage limit of your trial\. It lifts with your first payment\./))
    .toBeInTheDocument();
  expect(spendCapToastMessage('record', NOV_20)).toMatch(/until your first payment\.$/);
});

test("a saved entry's answer passes it; a press refused here keeps it", () => {
  notifySpendBlocked(null, true);
  notifySpendBlocked();
  expect(spendCapBannerMessage(NOV_20)).toBe(SPEND_CAP_UNTIL_PAYMENT_MESSAGE);
});

test('control: a monthly cap keeps its reset date', async () => {
  // A later 402 for the month's cap (after the first payment) has no flag.
  markSpendBlocked(null, true);
  await expect(rejected()({
    response: { status: 402, data: { error: 'monthly_spend_limit_reached', message: 'x', lifts_on: null } },
  })).rejects.toBeTruthy();
  expect(spendCapToastMessage('record', NOV_20)).toMatch(/until it resets on December 1\.$/);
  notifySpendBlocked(null, undefined);
  expect(spendCapBannerMessage(NOV_20)).toMatch(/It resets at the start of next month\.$/);
});
